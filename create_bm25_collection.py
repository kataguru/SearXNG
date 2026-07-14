"""Create and verify the new BM25 v1 collection in Qdrant."""

import os
import sys
from qdrant_client import QdrantClient
from qdrant_client.models import (
    VectorParams, SparseVectorParams, Distance, Modifier
)

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

COLLECTION_NAME = "agent_knowledge_bm25_v1"
DENSE_SIZE = 384

def main():
    api_key = os.getenv("QDRANT_API_KEY")
    client = QdrantClient(url="http://localhost:6333", api_key=api_key)
    
    # Check if collection already exists
    if client.collection_exists(COLLECTION_NAME):
        print(f"Collection '{COLLECTION_NAME}' already exists. Skipping creation.")
        info = client.get_collection(COLLECTION_NAME)
        print(f"  Points: {info.points_count}")
        print(f"  Vectors: {info.vectors_count if hasattr(info, 'vectors_count') else 'N/A'}")
        return
    
    # Create the new BM25 v1 collection
    print(f"Creating collection '{COLLECTION_NAME}' with BM25 (Finnish) configuration...")
    
    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config={
            "dense": VectorParams(
                size=DENSE_SIZE,
                distance=Distance.COSINE,
            )
        },
        sparse_vectors_config={
            "sparse": SparseVectorParams(modifier=Modifier.IDF)
        },
    )
    
    print(f"[OK] Collection '{COLLECTION_NAME}' created successfully.")
    
    # Verify configuration
    info = client.get_collection(COLLECTION_NAME)
    print(f"\nCollection details:")
    print(f"  Points: {info.points_count}")
    print(f"  Vectors: {info.vectors_count if hasattr(info, 'vectors_count') else 'N/A'}")
    
    # Check sparse config
    try:
        config = client.get_collection(COLLECTION_NAME).config
        sparse_config = config.params.sparse_vectors
        if "sparse" in sparse_config:
            print(f"  Sparse modifier: {sparse_config['sparse'].modifier}")
    except Exception as e:
        print(f"  Could not verify sparse config: {e}")

if __name__ == "__main__":
    main()
