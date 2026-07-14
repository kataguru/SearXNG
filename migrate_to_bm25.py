"""Migrate all content from Paperless to the new BM25 v1 collection."""

import os
import sys
import time
from datetime import datetime

# Load .env (same pattern as sync_daemon.py)
def load_env(path=".env"):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip()

# Load env file first (before importing rag_client which may use env vars)
load_env()

from rag_client import SharedAgentRAG, COLLECTION_NAME_BM25_V1

PAPERLESS_URL = os.getenv("PAPERLESS_URL", "http://localhost:8010").rstrip("/")
ADMIN_USER = os.getenv("PAPERLESS_ADMIN_USER", "admin")
# Note: Docker Compose interprets $$Secure as empty, so actual Paperless password is without trailing part
ADMIN_PASSWORD = os.getenv("PAPERLESS_ADMIN_PASSWORD", "Paperl3ss!Ngx#2024").split("$$")[0]


def fetch_all_documents():
    """Fetch all documents from Paperless API."""
    import requests
    
    all_docs = []
    base_url = f"{PAPERLESS_URL}/api/documents/?ordering=-id&page_size=200&count=true"
    
    page_num = 1
    while True:
        try:
            url = base_url + (f"&page={page_num}" if page_num > 1 else "")
            
            # Use session for persistent connection
            with requests.Session() as session:
                session.auth = (ADMIN_USER, ADMIN_PASSWORD)
                response = session.get(url, timeout=30)
            
            if response.status_code == 401:
                print(f"Authentication failed! Check PAPERLESS_ADMIN_USER/PASSWORD.")
                return all_docs
            elif response.status_code != 200:
                print(f"Paperless-ngx returned status {response.status_code}")
                return all_docs
            
            data = response.json()
            results = data.get("results", [])
            
            if not results:
                break
            
            all_docs.extend(results)
            
            # Check if there's a next page (Django REST Framework pagination)
            if data.get("next") is None or len(results) < 200:
                break
            
            page_num += 1
        
        except requests.exceptions.RequestException as e:
            print(f"Connection to Paperless-ngx failed: {e}")
            return all_docs
    
    return all_docs


def main():
    print("=" * 60)
    print("BM25 v1 Migration Script")
    print("=" * 60)
    
    # Initialize RAG client for BM25 collection
    print(f"\nInitializing RAG client for collection: {COLLECTION_NAME_BM25_V1}")
    rag = SharedAgentRAG.create_bm25_v1()
    
    # Fetch all documents from Paperless
    print("\nFetching documents from Paperless...")
    documents = fetch_all_documents()
    
    if not documents:
        print("No documents found in Paperless.")
        return
    
    print(f"Found {len(documents)} document(s) to index.\n")
    
    # Index each document
    success_count = 0
    error_count = 0
    
    for i, doc in enumerate(documents, 1):
        doc_id = str(doc["id"])
        title = doc.get("title", f"Document #{doc['id']}")
        content = doc.get("content", "").strip()
        created = doc.get("created", "")
        modified = doc.get("modified", "")
        
        if not content:
            print(f"[{i}/{len(documents)}] {doc_id} ('{title}') - empty content, skipping")
            continue
        
        try:
            # Index into BM25 collection with managed_by metadata
            chunk_ids = rag.add_knowledge(
                text=content,
                agent_id="paperless_sync_daemon",
                session_id="paperless_vault",
                scope="shared",
                source=f"paperless_id_{doc['id']}",
                external_doc_id=doc_id,
                index_version=1,  # Initial version for migration
                extra_metadata={
                    "title": title,
                    "created_at_source": created,
                    "modified_at_source": modified,
                    "paperless_url": f"{PAPERLESS_URL}/documents/{doc['id']}",
                    "managed_by": "paperless_sync_daemon",
                }
            )
            
            success_count += 1
            if i % 10 == 0 or i == len(documents):
                print(f"[{i}/{len(documents)}] {doc_id} ('{title}') - indexed {len(chunk_ids)} chunk(s)")
        
        except Exception as e:
            error_count += 1
            print(f"[{i}/{len(documents)}] {doc_id} ('{title}') - ERROR: {e}")
    
    # Summary
    print("\n" + "=" * 60)
    print("Migration Summary")
    print("=" * 60)
    print(f"Total documents: {len(documents)}")
    print(f"Successfully indexed: {success_count}")
    print(f"Errors: {error_count}")
    
    # Verify collection stats
    try:
        info = rag.client.get_collection(COLLECTION_NAME_BM25_V1)
        print(f"\nCollection '{COLLECTION_NAME_BM25_V1}':")
        print(f"  Points: {info.points_count}")
        print(f"  Vectors: {info.vectors_count if hasattr(info, 'vectors_count') else 'N/A'}")
    except Exception as e:
        print(f"\nCould not get collection stats: {e}")


if __name__ == "__main__":
    main()
