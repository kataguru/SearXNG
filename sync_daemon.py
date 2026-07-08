import os
import sys
import time
import json
from datetime import datetime
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

def sync_round(rag: SharedAgentRAG, synced_ids: set) -> bool:
    api_url = f"{PAPERLESS_URL}/api/documents/?ordering=-id"
    
    try:
        response = requests.get(
            api_url,
            auth=(ADMIN_USER, ADMIN_PASSWORD),
            timeout=10
        )
    except requests.exceptions.RequestException as e:
        # Silently log network issues (e.g. Paperless container starting up)
        print(f"[{datetime.now().isoformat()}] Connection to Paperless-ngx failed: {e}")
        return False

    if response.status_code == 401:
        print(f"[{datetime.now().isoformat()}] Authentication failed! Check PAPERLESS_ADMIN_USER/PASSWORD.")
        return False
    elif response.status_code != 200:
        print(f"[{datetime.now().isoformat()}] Paperless-ngx returned status {response.status_code}")
        return False

    documents = response.json().get("results", [])
    if not documents:
        return False

    state_changed = False
    
    for doc in documents:
        doc_id = doc.get("id")
        if doc_id in synced_ids:
            continue

        title = doc.get("title", f"Document #{doc_id}")
        content = doc.get("content", "").strip()
        created = doc.get("created", "")
        modified = doc.get("modified", "")
        
        if not content:
            print(f"[{datetime.now().isoformat()}] Document {doc_id} ('{title}') has no text content. Skipping.")
            synced_ids.add(doc_id)
            state_changed = True
            continue

        print(f"[{datetime.now().isoformat()}] Indexing new document {doc_id} ('{title}') into Qdrant...")
        
        try:
            # Add to Qdrant vector database via RAG client
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
            state_changed = True
        except UnexpectedResponse as e:
            print(f"[{datetime.now().isoformat()}] Qdrant connection/indexing error: {e}")
            break
        except Exception as e:
            print(f"[{datetime.now().isoformat()}] Unexpected error indexing {doc_id}: {e}")
            break

    if state_changed:
        save_state(synced_ids)
        return True
        
    return False

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
    print(f"Loaded {len(synced_ids)} already synced document ID(s) from state.")
    
    while True:
        try:
            sync_round(rag, synced_ids)
        except KeyboardInterrupt:
            print("\nSync daemon stopped by user.")
            sys.exit(0)
        except Exception as e:
            print(f"[{datetime.now().isoformat()}] Unexpected daemon error: {e}")
            
        time.sleep(15)

if __name__ == "__main__":
    main()
