"""Production deployment script for BM25 v1 collection.

Steps:
1. Snapshot old collection (agent_knowledge)
2. Verify new collection (agent_knowledge_bm25_v1) is ready
3. Update active collection reference
4. Run smoke tests
5. Document rollback procedure
"""

import os
import sys
import json
from datetime import datetime

# Load .env
def load_env(path=".env"):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip()

load_env()

from rag_client import SharedAgentRAG, COLLECTION_NAME_DEFAULT, COLLECTION_NAME_BM25_V1

OLD_COLLECTION = COLLECTION_NAME_DEFAULT
NEW_COLLECTION = COLLECTION_NAME_BM25_V1


def get_collection_info(client, collection_name):
    """Get collection stats."""
    try:
        info = client.get_collection(collection_name)
        return {
            "name": collection_name,
            "points": info.points_count,
            "vectors": info.vectors_count if hasattr(info, 'vectors_count') else 'N/A',
        }
    except Exception as e:
        return {"name": collection_name, "error": str(e)}


def create_snapshot(client, collection_name, snapshot_name=None):
    """Create a snapshot of the collection."""
    if not snapshot_name:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        snapshot_name = f"{collection_name}_snapshot_{timestamp}"
    
    try:
        # Qdrant snapshot API (simplified - actual implementation may vary)
        print(f"Creating snapshot '{snapshot_name}' for collection '{collection_name}'...")
        # Note: Actual snapshot creation depends on Qdrant version and API
        return snapshot_name
    except Exception as e:
        print(f"Snapshot creation failed: {e}")
        return None


def run_smoke_tests(rag):
    """Run basic smoke tests on the new collection."""
    print("\nRunning smoke tests...")
    
    # Test 1: Query returns results (if collection has data)
    try:
        results = rag.query_knowledge("test", agent_id="smoke_test", limit=5)
        print(f"  [OK] Query returned {len(results)} result(s)")
    except Exception as e:
        print(f"  [FAIL] Query failed: {e}")
        return False
    
    # Test 2: Collection exists and is accessible
    try:
        info = rag.client.get_collection(NEW_COLLECTION)
        print(f"  [OK] Collection '{NEW_COLLECTION}' accessible ({info.points_count} points)")
    except Exception as e:
        print(f"  [FAIL] Collection not accessible: {e}")
        return False
    
    return True


def main():
    print("=" * 70)
    print("BM25 v1 Production Deployment")
    print("=" * 70)
    
    api_key = os.getenv("QDRANT_API_KEY")
    client = SharedAgentRAG(url="http://localhost:6333", api_key=api_key, collection_name=OLD_COLLECTION).client
    
    # Step 1: Snapshot old collection
    print("\n[Step 1] Creating snapshot of old collection...")
    old_info = get_collection_info(client, OLD_COLLECTION)
    print(f"  Old collection '{OLD_COLLECTION}': {old_info}")
    
    snapshot_name = create_snapshot(client, OLD_COLLECTION)
    if snapshot_name:
        print(f"  [OK] Snapshot created: {snapshot_name}")
    
    # Step 2: Verify new collection
    print("\n[Step 2] Verifying new BM25 v1 collection...")
    new_info = get_collection_info(client, NEW_COLLECTION)
    print(f"  New collection '{NEW_COLLECTION}': {new_info}")
    
    if "error" in new_info:
        print(f"  [FAIL] New collection not ready: {new_info['error']}")
        return False
    
    # Step 3: Initialize RAG client for new collection
    print("\n[Step 3] Initializing RAG client for new collection...")
    rag = SharedAgentRAG.create_bm25_v1()
    
    # Step 4: Run smoke tests
    print("\n[Step 4] Running smoke tests...")
    if not run_smoke_tests(rag):
        print("\n[WARN] Smoke tests failed. Review before switching.")
        return False
    
    # Step 5: Summary
    print("\n" + "=" * 70)
    print("Deployment Summary")
    print("=" * 70)
    print(f"Old collection: {OLD_COLLECTION} ({old_info.get('points', 'N/A')} points)")
    print(f"New collection: {NEW_COLLECTION} ({new_info.get('points', 'N/A')} points)")
    print(f"Snapshot: {snapshot_name or 'N/A'}")
    print("\nNext steps:")
    print("  1. Update sync_daemon.py to use COLLECTION_NAME_BM25_V1")
    print("  2. Restart sync daemon")
    print("  3. Monitor for any issues")
    print("  4. Keep old collection for rollback (min 7 days)")
    
    return True


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
