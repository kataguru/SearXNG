"""Paperless-to-Qdrant sync daemon with full document lifecycle management.

Tracks modifications, deletions, and content changes via per-document state:
  - document_id (Paperless ID)
  - modified_timestamp (from Paperless API)
  - content_hash (SHA-256 of indexed text)
  - index_version (incremented on reindex)
  - chunking_version (chunking algorithm version for migration detection)

State file format: paperless_sync_state.json
{
  "version": 2,
  "documents": {
    "<doc_id>": {
      "title": "...",
      "modified_at": "2025-01-01T00:00:00Z",
      "content_hash": "sha256hex...",
      "index_version": 1,
      "chunking_version": 1
    }
  }
}

Retry state file format: paperless_retry_state.json
{
  "<doc_id>": {
    "retry_count": N,
    "last_attempt": <unix_timestamp>,
    "error": "..."
  }
}
"""

import os
import sys
import time
import hashlib
import json
from datetime import datetime
from typing import Dict, Any, Optional, Set

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

# ── Configuration ─────────────────────────────────
STATE_FILE = "paperless_sync_state.json"
RETRY_STATE_FILE = "paperless_retry_state.json"
MAX_RETRIES = 5
BASE_RETRY_DELAY = 2  # seconds
PAPERLESS_URL = os.getenv("PAPERLESS_URL", "http://localhost:8010").rstrip("/")
ADMIN_USER = os.getenv("PAPERLESS_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("PAPERLESS_ADMIN_PASSWORD", "adminpassword123")
COLLECTION_NAME = "agent_knowledge"

# Version constants — increment when chunking algorithm or metadata schema changes
CHUNKING_VERSION = 1
STATE_FORMAT_VERSION = 2


# ── State management ──────────────────────────
def _quarantine_corrupted_state(e: Exception) -> Dict[str, Any]:
    """Handle corrupted state file safely.

    Instead of silently returning empty state (fail-open), this function:
      1. Moves the corrupted file to a timestamped backup (quarantine)
      2. Logs a visible ERROR (not just Warning)
      3. Returns empty state, forcing a controlled full resync on next round

    The caller should treat recovered state as untrusted — no deletions
    should be assumed safe until Qdrant and Paperless converge.
    """
    import shutil
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{STATE_FILE}.corrupted.{timestamp}"

    try:
        if os.path.exists(STATE_FILE):
            shutil.copy2(STATE_FILE, backup_path)
            print(f"[{ts()}] ERROR: State file corrupted. Quarantined to '{backup_path}'.")
        else:
            print(f"[{ts()}] ERROR: State file vanished between check and read.")
    except Exception as copy_e:
        print(f"[{ts()}] ERROR: Failed to quarantine state file: {copy_e}")

    print(f"[{ts()}] ERROR: Corrupted state caused by: {e}")
    print(f"[{ts()}] RECOVERY: Starting with empty state (full resync required).")
    return {"version": STATE_FORMAT_VERSION, "documents": {}}


def load_state() -> Dict[str, Any]:
    """Load sync state. Migrates v1 (set of IDs) to v2 format.

    On corruption: quarantines the file and returns empty state.
    Does NOT silently swallow errors — every recovery path is logged as ERROR.
    """
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                raw = f.read()

            if not raw.strip():
                return _quarantine_corrupted_state(ValueError("State file is empty"))

            data = json.loads(raw)

            # Migration: v1 was a plain list of doc IDs
            if isinstance(data, list):
                documents = {}
                for doc_id in data:
                    documents[str(doc_id)] = {
                        "title": "",
                        "modified_at": "",
                        "content_hash": "",
                        "index_version": 0,
                        "chunking_version": 1,
                    }
                return {"version": STATE_FORMAT_VERSION, "documents": documents}

            # Already v2 format — but check if chunking_version field exists
            if data.get("version") == STATE_FORMAT_VERSION:
                docs = data.get("documents", {})
                needs_migration = False
                for doc_id, info in docs.items():
                    if "chunking_version" not in info or "content_hash" not in info:
                        needs_migration = True
                        break

                if needs_migration:
                    # Mark all existing entries as needing reindex (unknown content hash)
                    for doc_id in docs:
                        docs[doc_id].setdefault("chunking_version", 1)
                        docs[doc_id].setdefault("content_hash", "")
                        docs[doc_id].setdefault("title", "")
                        docs[doc_id].setdefault("modified_at", "")
                        docs[doc_id].setdefault("index_version", 0)

                return data

            # Unknown version — treat as needing full resync
            print(f"[{ts()}] Warning: Unknown state format version {data.get('version')}. Resyncing all.")
            return {"version": STATE_FORMAT_VERSION, "documents": {}}

        except (json.JSONDecodeError, ValueError) as e:
            return _quarantine_corrupted_state(e)
        except Exception as e:
            return _quarantine_corrupted_state(e)

    return {"version": STATE_FORMAT_VERSION, "documents": {}}


def _atomic_write_json(path: str, data):
    """Write JSON atomically using temp-file + rename to prevent corruption on crash."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        # Clean up temp file on failure
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def save_state(state: Dict[str, Any]):
    try:
        _atomic_write_json(STATE_FILE, state)
    except Exception as e:
        print(f"[{ts()}] Error saving state file: {e}")


def load_retry_state() -> Dict[str, Any]:
    if os.path.exists(RETRY_STATE_FILE):
        try:
            with open(RETRY_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[{ts()}] Warning loading retry state file: {e}")
            return {}
    return {}


def save_retry_state(state: Dict[str, Any]):
    try:
        _atomic_write_json(RETRY_STATE_FILE, state)
    except Exception as e:
        print(f"[{ts()}] Error saving retry state file: {e}")


def get_retry_delay(retry_count: int) -> float:
    return min(BASE_RETRY_DELAY * (2 ** retry_count), 300)


def ts() -> str:
    """ISO timestamp for log messages."""
    return datetime.now().isoformat()


# ── Content hashing ────────────────────
def content_hash(text: str) -> str:
    """SHA-256 hash of document text content. Used to detect modifications."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── Paperless API ───────────────────
def fetch_all_documents() -> Optional[list]:
    """Fetch ALL documents from Paperless API using pagination (page_size=200).

    Returns list of document dicts, None on auth/connection error, or empty list
    if no documents exist. Partial results returned on transient failures.
    """
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
            print(f"[{ts()}] Connection to Paperless-ngx failed: {e}")
            return all_docs if all_docs else None

        if response.status_code == 401:
            print(f"[{ts()}] Authentication failed! Check PAPERLESS_ADMIN_USER/PASSWORD.")
            return None
        elif response.status_code != 200:
            print(f"[{ts()}] Paperless-ngx returned status {response.status_code}")
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


# ── Startup reconciliation ──────────────────
def reconcile_startup(rag: SharedAgentRAG, state: Dict[str, Any]) -> int:
    """Reconcile Qdrant chunks against tracked state on startup.

    Catches crashes between save_state() and cleanup_old_versions().
    For each tracked document with index_version > 0, ensures only the declared
    version exists in Qdrant. Filters by managed_by=paperless_sync_daemon to avoid
    cleaning up manually added or web_search documents that share external_doc_id values.

    Idempotent — safe to run multiple times.

    Returns total number of stale chunks removed.
    """
    reconciled = 0
    for doc_id, info in state["documents"].items():
        expected_version = info.get("index_version", 1)
        if expected_version > 0:  # Skip empty-content docs (version=0 has no chunks to clean)
            try:
                removed = rag.cleanup_old_versions(
                    doc_id, keep_version=expected_version, managed_by="paperless_sync_daemon"
                )
                if removed > 0:
                    print(f"[{ts()}] Startup reconciliation: removed {removed} stale chunk(s) for document {doc_id}.")
                    reconciled += removed
            except Exception as e:
                # Non-fatal — next sync round will retry
                print(f"[{ts()}] Warning: Startup reconciliation failed for {doc_id}: {e}")

    if reconciled > 0:
        print(f"[{ts()}] Startup reconciliation complete. Removed {reconciled} stale chunk(s) total.")
    else:
        print(f"[{ts()}] Startup reconciliation complete. No stale chunks found.")

    return reconciled


def reconcile_full_orphans(rag: SharedAgentRAG, state: Dict[str, Any]) -> int:
    """Full orphan detection after corrupted state recovery.

    Fetches all Paperless documents and removes Qdrant chunks for docs
    no longer present in Paperless. Only runs if Paperless fetch succeeds —
    partial results mean we cannot safely delete orphans.

    Filters by managed_by=paperless_sync_daemon to avoid deleting manually
    added or web_search documents that happen to share external_doc_id values.

    Normalizes all IDs to strings before comparison to prevent type mismatch
    (e.g., Paperless ID as int vs Qdrant external_doc_id as string).

    Returns total number of orphaned chunks removed.
    """
    documents = fetch_all_documents()
    if documents is None:
        print(f"[{ts()}] Cannot perform full orphan reconciliation — Paperless unavailable.")
        return 0

    # Normalize all IDs to strings for safe set comparison
    current_ids = {str(doc["id"]) for doc in documents}

    # Use scroll_all for complete pagination through Qdrant
    try:
        all_points = rag.scroll_all(
            filter=Filter(
                must=[FieldCondition(key="managed_by", match=MatchValue(value="paperless_sync_daemon"))]
            ),
            limit_per_page=100,
        )

        # Group by external_doc_id to find unique managed documents and their chunk IDs
        managed_docs: Dict[str, List[str]] = {}
        for p in all_points:
            ext_id = str(p.payload.get("external_doc_id", ""))  # Normalize to string
            if ext_id:
                managed_docs.setdefault(ext_id, []).append(p.id)

        # Delete managed docs not present in Paperless (both sets are now strings)
        orphaned_ids = set(managed_docs.keys()) - current_ids
        deleted_count = 0

        for doc_id in sorted(orphaned_ids):
            try:
                point_ids = managed_docs[doc_id]
                if point_ids:
                    # Batch delete in groups of 500
                    for i in range(0, len(point_ids), 500):
                        batch = point_ids[i:i + 500]
                        rag.client.delete(
                            collection_name=rag.collection_name,
                            points_selector=batch,
                        )
                        deleted_count += len(batch)

                    print(f"[{ts()}] Full orphan reconciliation: removed {len(point_ids)} chunk(s) for orphaned document {doc_id}.")
            except Exception as e:
                print(f"[{ts()}] Warning: Failed to remove orphan {doc_id}: {e}")

        if deleted_count > 0:
            print(f"[{ts()}] Full orphan reconciliation complete. Removed {deleted_count} orphaned chunk(s) total.")
        else:
            print(f"[{ts()}] Full orphan reconciliation complete. No orphans found.")

        return deleted_count

    except Exception as e:
        print(f"[{ts()}] Warning: Orphan reconciliation query failed: {e}")
        return 0


# ── Sync logic ──────────────────
def sync_round(
    rag: SharedAgentRAG,
    state: Dict[str, Any],
    retry_state: Dict[str, Any],
) -> bool:
    """Run one synchronization round.

    Compares current Paperless documents against tracked state and handles:
      - NEW documents → index into Qdrant
      - MODIFIED documents (content hash changed or chunking version outdated) → reindex
      - DELETED documents (no longer in Paperless) → remove from Qdrant

    Returns True if any state changes occurred.
    """
    documents = fetch_all_documents()

    if documents is None:
        # Error already printed by fetch_all_documents
        return False

    current_docs: Dict[int, dict] = {doc["id"]: doc for doc in documents}
    tracked_ids: Set[str] = set(state.get("documents", {}).keys())
    present_ids: Set[str] = {str(d) for d in current_docs.keys()}

    new_ids = present_ids - tracked_ids
    deleted_ids = tracked_ids - present_ids
    existing_ids = present_ids & tracked_ids

    state_changed = False
    current_time = time.time()

    # ── Handle deletions first ───────────────
    for doc_id_str in sorted(deleted_ids):
        old_info = state["documents"].get(doc_id_str, {})
        title = old_info.get("title", f"Document #{doc_id_str}")
        print(f"[{ts()}] Document {doc_id_str} ('{title}') deleted from Paperless. Removing from Qdrant...")
        try:
            removed = rag.delete_document(doc_id_str)
            print(f"[{ts()}] Removed {removed} chunk(s) for document {doc_id_str}.")
        except Exception as e:
            print(f"[{ts()}] Error removing document {doc_id_str} from Qdrant: {e}")

        del state["documents"][doc_id_str]
        retry_state.pop(doc_id_str, None)
        save_retry_state(retry_state)
        state_changed = True

    # ── Handle new and modified documents ───────────
    for doc in documents:
        doc_id = str(doc["id"])
        title = doc.get("title", f"Document #{doc['id']}")
        content = doc.get("content", "").strip()
        created = doc.get("created", "")
        modified = doc.get("modified", "")

        new_content_hash = content_hash(content) if content else ""

        # Determine action needed
        old_info = state["documents"].get(doc_id, {})
        old_hash = old_info.get("content_hash", "")
        old_chunking_version = old_info.get("chunking_version", 1)
        is_new = doc_id in new_ids
        needs_reindex = False

        if is_new:
            action = "new"
            needs_reindex = True
        elif old_hash != new_content_hash or old_chunking_version != CHUNKING_VERSION:
            action = "modified" if old_hash else "unknown hash (re-sync)"
            needs_reindex = True
        else:
            # Content unchanged, chunking algorithm up to date — skip
            continue

        # Check retry backoff before attempting index/reindex
        retry_info = retry_state.get(doc_id, {})
        retry_count = retry_info.get("retry_count", 0)

        if retry_count > 0 and not is_new:
            last_attempt = retry_info.get("last_attempt", 0)
            delay = get_retry_delay(retry_count - 1)
            elapsed = current_time - last_attempt

            if elapsed < delay:
                remaining = int(delay - elapsed)
                print(f"[{ts()}] Document {doc_id} in backoff (retry {retry_count}/{MAX_RETRIES}), next attempt in {remaining}s.")
                continue

        # Handle empty content — mark as synced with no chunks needed
        if not content:
            if is_new or old_hash != "":
                print(f"[{ts()}] Document {doc_id} ('{title}') has no text content. Marking synced.")

                # Delete old Qdrant chunks BEFORE saving state (prevents orphaned chunks on crash)
                if old_hash:
                    try:
                        removed = rag.delete_document(doc_id)
                        print(f"[{ts()}] Removed {removed} chunk(s) for now-empty document {doc_id}.")
                    except Exception as e:
                        print(f"[{ts()}] Error removing old chunks for {doc_id}: {e}")

                state["documents"][doc_id] = {
                    "title": title,
                    "modified_at": modified,
                    "content_hash": "",
                    "index_version": 0,
                    "chunking_version": CHUNKING_VERSION,
                }
                save_state(state)
                retry_state.pop(doc_id, None)
                save_retry_state(retry_state)
                state_changed = True

            continue

        # CRASH-SAFE REINDEXING: add-then-cleanup pattern.
        # Phase 1: Add new version (never deletes old chunks yet)
        if needs_reindex and not is_new:
            print(f"[{ts()}] Re-indexing document {doc_id} ('{title}') — {action}...")
        else:
            print(f"[{ts()}] Indexing new document {doc_id} ('{title}') into Qdrant...")

        old_version = old_info.get("index_version", 0)
        new_version = max(old_version, 0) + 1

        try:
            chunk_ids = rag.add_knowledge(
                text=content,
                agent_id="paperless_sync_daemon",
                session_id="paperless_vault",
                scope="shared",
                source=f"paperless_id_{doc['id']}",
                external_doc_id=doc_id,
                index_version=new_version,
                extra_metadata={
                    "title": title,
                    "created_at_source": created,
                    "modified_at_source": modified,
                    "paperless_url": f"{PAPERLESS_URL}/documents/{doc['id']}",
                    "managed_by": "paperless_sync_daemon",
                }
            )

            # Phase 2: Persist state BEFORE cleanup (state confirms new version is live)
            state["documents"][doc_id] = {
                "title": title,
                "modified_at": modified,
                "content_hash": new_content_hash,
                "index_version": new_version,
                "chunking_version": CHUNKING_VERSION,
            }
            save_state(state)

            # Phase 3: Clean up stale chunks (idempotent — safe to retry on crash)
            try:
                removed = rag.cleanup_old_versions(doc_id, keep_version=new_version)
                if removed > 0:
                    print(f"[{ts()}] Cleaned up {removed} stale chunk(s) for document {doc_id}.")
            except Exception as cleanup_e:
                # Non-fatal — next sync round will retry cleanup
                print(f"[{ts()}] Warning: Cleanup of old versions for {doc_id} failed: {cleanup_e}")

            retry_state.pop(doc_id, None)
            save_retry_state(retry_state)
            state_changed = True

            label = "Re-indexed" if not is_new else "Indexed"
            print(f"[{ts()}] {label} document {doc_id}: {len(chunk_ids)} chunk(s).")

        except (UnexpectedResponse, ConnectionError) as e:
            retry_count += 1
            if retry_count >= MAX_RETRIES:
                print(f"[{ts()}] Document {doc_id} exceeded max retries ({MAX_RETRIES}). Will continue retrying with backoff.")
            retry_state[doc_id] = {
                "retry_count": retry_count,
                "last_attempt": current_time,
                "error": str(e),
            }
            save_retry_state(retry_state)
            print(f"[{ts()}] Qdrant error on {doc_id} (retry {retry_count}): {e}")
        except Exception as e:
            retry_count += 1
            if retry_count >= MAX_RETRIES:
                print(f"[{ts()}] Document {doc_id} exceeded max retries ({MAX_RETRIES}). Will continue retrying with backoff.")
            retry_state[doc_id] = {
                "retry_count": retry_count,
                "last_attempt": current_time,
                "error": str(e),
            }
            save_retry_state(retry_state)
            print(f"[{ts()}] Unexpected error indexing {doc_id} (retry {retry_count}): {e}")

    return state_changed


def main():
    print(f"============================================================")
    print(f"Starting Paperless-to-Qdrant Sync Daemon v2")
    print(f"Paperless API: {PAPERLESS_URL}")
    print(f"Qdrant target collection: {COLLECTION_NAME}")
    print(f"Polling interval: 15 seconds")
    print(f"Chunking version: {CHUNKING_VERSION}")
    print(f"============================================================")

    # Initialize RAG client
    try:
        rag = SharedAgentRAG(collection_name=COLLECTION_NAME)
    except Exception as e:
        print(f"Error initializing Qdrant RAG client: {e}")
        sys.exit(1)

    state = load_state()
    retry_state = load_retry_state()

    doc_count = len(state.get("documents", {}))
    print(f"Loaded {doc_count} tracked document(s) from state.")
    print(f"Loaded {len(retry_state)} document(s) in retry backoff.")

    # ── Startup reconciliation ───────────────
    # Phase 1: Clean up stale chunks for all tracked documents
    # (catches crashes between save_state() and cleanup_old_versions())
    reconcile_startup(rag, state)

    # Phase 2: Detect orphans in Qdrant by comparing against current Paperless document list.
    # Runs unconditionally — even with empty state after corruption quarantine,
    # this catches documents deleted from Paperless but still present in Qdrant.
    reconcile_full_orphans(rag, state)

    while True:
        try:
            sync_round(rag, state, retry_state)
        except KeyboardInterrupt:
            print("\nSync daemon stopped by user.")
            sys.exit(0)
        except Exception as e:
            print(f"[{ts()}] Unexpected daemon error: {e}")

        time.sleep(15)


if __name__ == "__main__":
    main()
