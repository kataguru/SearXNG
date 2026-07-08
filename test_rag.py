import time
from rag_client import SharedAgentRAG

def test_rag():
    print("Ensuring fresh collection for tests...")
    from qdrant_client import QdrantClient
    client = QdrantClient(url="http://localhost:6333")
    if client.collection_exists("test_agent_knowledge"):
        client.delete_collection("test_agent_knowledge")
        print("Deleted existing test collection.")

    print("Initializing SharedAgentRAG client...")
    rag = SharedAgentRAG(collection_name="test_agent_knowledge")
    
    # Wait a second for models to load/initialize
    time.sleep(1)
    
    print("\nAdding sample knowledge documents...")
    doc1 = rag.add_knowledge(
        text="SearXNG is a privacy-respecting, hackable metasearch engine running on http://localhost:8080.",
        agent_id="test_agent_1",
        session_id="session_001",
        scope="shared",
        source="manual"
    )
    doc2 = rag.add_knowledge(
        text="Valkey is an open-source, high-performance key-value database fork of Redis used for caching query metadata.",
        agent_id="test_agent_1",
        session_id="session_001",
        scope="private",
        source="manual"
    )
    doc3 = rag.add_knowledge(
        text="Configuration properties for rate limiting are located inside a configuration file named limiter.toml.",
        agent_id="test_agent_2",
        session_id="session_002",
        scope="shared",
        source="manual"
    )
    
    print(f"Added documents successfully. IDs: {doc1}, {doc2}, {doc3}")
    
    # Wait for Qdrant index write synchronization
    print("Waiting 2 seconds for indexing...")
    time.sleep(2)
    
    print("\n--- Test 1: Conceptual/Semantic Search ('What is SearXNG?') ---")
    results_conceptual = rag.query_knowledge(
        query_text="What is SearXNG?",
        agent_id="test_agent_1",
        search_scope="shared_or_private",
        limit=2
    )
    for i, res in enumerate(results_conceptual):
        print(f"Match {i+1} (Score: {res['score']:.4f}):")
        print(f"  Text: {res['text']}")
        print(f"  Meta: {res['metadata']}")

    print("\n--- Test 2: Exact Keyword Search ('limiter.toml') ---")
    results_keyword = rag.query_knowledge(
        query_text="limiter.toml",
        agent_id="test_agent_1",
        search_scope="shared_or_private",
        limit=2
    )
    for i, res in enumerate(results_keyword):
        print(f"Match {i+1} (Score: {res['score']:.4f}):")
        print(f"  Text: {res['text']}")
        print(f"  Meta: {res['metadata']}")

    print("\n--- Test 3: Scope Filtering (Private access check) ---")
    # test_agent_2 queries for private info of test_agent_1
    # It should NOT see the Valkey private document (doc2) because it belongs to test_agent_1
    results_scope = rag.query_knowledge(
        query_text="caching database Valkey",
        agent_id="test_agent_2", # test_agent_2 is searching
        search_scope="shared_or_private",
        limit=2
    )
    for i, res in enumerate(results_scope):
        print(f"Match {i+1} (Score: {res['score']:.4f}):")
        print(f"  Text: {res['text']}")
        print(f"  Meta: {res['metadata']}")

if __name__ == "__main__":
    test_rag()
