import os
import sys
import time
import json
from datetime import datetime
from typing import Dict, Any
import requests
from qdrant_client.http.exceptions import UnexpectedResponse

# Custom helper to load .env file
def load_env(path=".env"):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip()

# Load env file first
load_env()

# Now import RAG client
try:
    from rag_client import SharedAgentRAG
except ImportError:
    print("Error: Could not import rag_client.py. Make sure it is in the same directory.")
    sys.exit(1)

# Configurations
STATE_FILE = "paperless_sync_state.json"
RETRY_STATE_FILE = "paperless_retry_state.json"
MAX_RETRIES = 5
BASE_RETRY_DELAY = 2  # seconds
PAPERLESS_URL = os.getenv("PAPERLESS_URL", "http://localhost:8010").rstrip("/")
ADMIN_USER = os.getenv("PAPERLESS_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("PAPERLESS_ADMIN_PASSWORD", "adminpassword123")
COLLECTION_NAME = "agent_knowledge"

def load_state() -> set:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception as e:
            print(f"[{datetime.now().isoformat()}] Warning loading state file: {e}")
            return set()
    return set()

def save_state(state: set):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(list(state)), f, indent=2)
    except Exception as e:
        print(f"[{datetime.now().isoformat()}] Error saving state file: {e}")

def load_retry_state() -> Dict[str, Any]:
    if os.path.exists(RETRY_STATE_FILE):
        try:
            with open(RETRY_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[{datetime.now().isoformat()}] Warning loading retry state file: {e}")
            return {}
    return {}

def save_retry_state(state: Dict[str, Any]):
    try:
        with open(RETRY_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"[{datetime.now().isoformat()}] Error saving retry state file: {e}")

def get_retry_delay(retry_count: int) -> float:
    return min(BASE_RETRY_DELAY * (2 ** retry_count), 300)

def fetch_all_documents() -> list:
    """Fetch ALL documents from Paperless API using pagination (page_size=200)."""
    all_docs = []
    base_url = f"{PAPERLESS_URL}/api/documents/?ordering=-id&page_size=200&count=true"

    page_num = 1
    while True:
        try:
            url = base_url + (f"&page={page_num}" if page_num > 1 else "")
            response = requests.get(
                url,
                auth=(ADMIN_USER, ADMIN_PASSWORD),
                timeout=30
            )
        except requests.exceptions.RequestException as e:
            print(f"[{datetime.now().isoformat()}] Connection to Paperless-ngx failed: {e}")
            return all_docs if all_docs else None

        if response.status_code == 401:
            print(f"[{datetime.now().isoformat()}] Authentication failed! Check PAPERLESS_ADMIN_USER/PASSWORD.")
            return None
        elif response.status_code != 200:
            print(f"[{datetime.now().isoformat()}] Paperless-ngx returned status {response.status_code}")
            return None

        data = response.json()
        results = data.get("results", [])

        if not results:
            break

        all_docs.extend(results)

        # Check if there's a next page (Django REST Framework pagination)
        if data.get("next") is None or len(results) < 200:
            break

        page_num += 1

    return all_docs

def sync_round(rag: SharedAgentRAG, synced_ids: set, retry_state: Dict[str, Any]) -> bool:
    documents = fetch_all_documents()

    if documents is None:
        # Error already printed by fetch_all_documents
        return False
    elif not documents:
        return False

    print(f"[{datetime.now().isoformat()}] Fetched {len(documents)} document(s) from Paperless.")

    state_changed = False
    current_time = time.time()

    for doc in documents:
        doc_id = doc.get("id")
        if doc_id in synced_ids:
            continue

        # Check retry state with exponential backoff
        retry_info = retry_state.get(doc_id, {})
        retry_count = retry_info.get("retry_count", 0)

        if retry_count > 0:
            last_attempt = retry_info.get("last_attempt", 0)
            delay = get_retry_delay(retry_count - 1)
            elapsed = current_time - last_attempt

            if elapsed < delay:
                remaining = int(delay - elapsed)
                print(f"[{datetime.now().isoformat()}] Document {doc_id} in backoff (retry {retry_count}/{MAX_RETRIES}), next attempt in {remaining}s.")
                continue

        title = doc.get("title", f"Document #{doc_id}")
        content = doc.get("content", "").strip()
        created = doc.get("created", "")
        modified = doc.get("modified", "")

        if not content:
            print(f"[{datetime.now().isoformat()}] Document {doc_id} ('{title}') has no text content. Skipping.")
            synced_ids.add(doc_id)
            save_state(synced_ids)
            retry_state.pop(doc_id, None)
            save_retry_state(retry_state)
            state_changed = True
            continue

        print(f"[{datetime.now().isoformat()}] Indexing new document {doc_id} ('{title}') into Qdrant...")

        try:
            rag.add_knowledge(
                text=content,
                agent_id="paperless_sync_daemon",
                session_id="paperless_vault",
                scope="shared",
                source=f"paperless_id_{doc_id}",
                extra_metadata={
                    "title": title,
                    "created_at_source": created,
                    "modified_at_source": modified,
                    "paperless_url": f"{PAPERLESS_URL}/documents/{doc_id}"
                }
            )
            print(f"[{datetime.now().isoformat()}] Document {doc_id} indexed successfully.")
            synced_ids.add(doc_id)
            save_state(synced_ids)
            retry_state.pop(doc_id, None)
            save_retry_state(retry_state)
            state_changed = True
        except UnexpectedResponse as e:
            retry_count += 1
            if retry_count >= MAX_RETRIES:
                print(f"[{datetime.now().isoformat()}] Document {doc_id} exceeded max retries ({MAX_RETRIES}). Will continue retrying with backoff.")
            retry_state[doc_id] = {
                "retry_count": retry_count,
                "last_attempt": current_time
            }
            save_retry_state(retry_state)
            print(f"[{datetime.now().isoformat()}] Qdrant connection/indexing error on {doc_id} (retry {retry_count}): {e}")
        except Exception as e:
            retry_count += 1
            if retry_count >= MAX_RETRIES:
                print(f"[{datetime.now().isoformat()}] Document {doc_id} exceeded max retries ({MAX_RETRIES}). Will continue retrying with backoff.")
            retry_state[doc_id] = {
                "retry_count": retry_count,
                "last_attempt": current_time
            }
            save_retry_state(retry_state)
            print(f"[{datetime.now().isoformat()}] Unexpected error indexing {doc_id} (retry {retry_count}): {e}")

    return state_changed

def main():
    print(f"============================================================")
    print(f"Starting Paperless-to-Qdrant Sync Daemon")
    print(f"Paperless API: {PAPERLESS_URL}")
    print(f"Qdrant target collection: {COLLECTION_NAME}")
    print(f"Polling interval: 15 seconds")
    print(f"============================================================")

    # Initialize RAG client
    try:
        rag = SharedAgentRAG(collection_name=COLLECTION_NAME)
    except Exception as e:
        print(f"Error initializing Qdrant RAG client: {e}")
        sys.exit(1)

    synced_ids = load_state()
    retry_state = load_retry_state()
    print(f"Loaded {len(synced_ids)} already synced document ID(s) from state.")
    print(f"Loaded {len(retry_state)} document(s) in retry backoff.")

    while True:
        try:
            sync_round(rag, synced_ids, retry_state)
        except KeyboardInterrupt:
            print("\nSync daemon stopped by user.")
            sys.exit(0)
        except Exception as e:
            print(f"[{datetime.now().isoformat()}] Unexpected daemon error: {e}")

        time.sleep(15)

if __name__ == "__main__":
    main()
