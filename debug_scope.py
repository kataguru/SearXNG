import os, sys, time
sys.path.insert(0, '.')
from qdrant_client import QdrantClient
from rag_client import SharedAgentRAG
from qdrant_client.models import Filter, FieldCondition, MatchValue, Prefetch, NamedSparseVector, SparseVector, FusionQuery, Fusion

COLLECTION = "test_scope_debug"

api_key = os.getenv("QDRANT_API_KEY")
qc = QdrantClient(url="http://localhost:6333", api_key=api_key)

if qc.collection_exists(COLLECTION):
    qc.delete_collection(COLLECTION)

rag = SharedAgentRAG(collection_name=COLLECTION)
time.sleep(0.5)

# Test T13: Private scope
print("=== T13: Private scope ===")
rag.add_knowledge(text='Private data belonging to agent alpha only.', agent_id='alpha', session_id='s1', scope='private', source='manual')
time.sleep(0.5)

# Check what's in the collection
pts, _ = qc.scroll(COLLECTION, limit=10, with_payload=True, with_vectors=False)
print(f"Total points: {len(pts)}")
for p in pts:
    print(f"  id={p.id} scope={p.payload.get('scope')} agent={p.payload.get('agent_id')} external_doc_id={p.payload.get('external_doc_id')} index_version={p.payload.get('index_version')} text='{p.payload.get('text', '')[:40]}...'")

# Test with explicit filter using same approach as query_knowledge
print("\nTesting with explicit should filter (hybrid search):")
filter_to_use = Filter(
    should=[
        FieldCondition(key="scope", match=MatchValue(value="shared")),
        FieldCondition(key="agent_id", match=MatchValue(value="alpha")),
    ]
)

# Generate embeddings like query_knowledge does
dense_vector = list(rag.dense_model.embed(["private data agent alpha"]))[0].tolist()
sparse_embedding = list(rag.sparse_model.embed(["private data agent alpha"]))[0]

results_raw = qc.query_points(
    COLLECTION,
    prefetch=[
        Prefetch(query=dense_vector, using="dense", limit=50),
        Prefetch(
            query=SparseVector(
                indices=sparse_embedding.indices.tolist(),
                values=sparse_embedding.values.tolist(),
            ),
            using="sparse",
            limit=20,
        ),
    ],
    query=FusionQuery(fusion=Fusion.RRF),
    query_filter=filter_to_use,
    limit=5,
    with_payload=True,
)
print(f"Raw results: {len(results_raw.points)}")
for p in results_raw.points:
    print(f"  id={p.id} score={p.score:.4f} scope={p.payload.get('scope')} agent={p.payload.get('agent_id')} external_doc_id={p.payload.get('external_doc_id')} index_version={p.payload.get('index_version')}")

# Manually trace through deduplication logic
print("\n=== Deduplication trace ===")
max_versions = {}
for point in results_raw.points:
    ext_id = str(point.payload.get("external_doc_id", "")) if point.payload.get("external_doc_id") is not None else None
    version = point.payload.get("index_version", 0)
    print(f"Point {point.id}: ext_id={repr(ext_id)} (is None: {point.payload.get('external_doc_id') is None}), version={version}")
    if ext_id:
        current_max = max_versions.get(ext_id, 0)
        if version > current_max:
            max_versions[ext_id] = version

print(f"max_versions: {max_versions}")

deduplicated = []
seen_ids = set()
for point in results_raw.points:
    ext_id = str(point.payload.get("external_doc_id", "")) if point.payload.get("external_doc_id") is not None else None
    version = point.payload.get("index_version", 0)
    keep = (ext_id == "") or (version == max_versions.get(ext_id, 0))
    print(f"Point {point.id}: ext_id={repr(ext_id)}, version={version}, max_for_doc={max_versions.get(ext_id, 0)}, keep={keep}")
    if keep and point.id not in seen_ids:
        deduplicated.append(point)
        seen_ids.add(point.id)

print(f"Deduplicated count: {len(deduplicated)}")

results = rag.query_knowledge('private data agent alpha', agent_id='alpha', limit=5)
print(f"\nQuery results: {len(results)}")
for r in results:
    print(f"  text='{r['text'][:60]}...' scope={r['metadata'].get('scope')} agent={r['metadata'].get('agent_id')} external_doc_id={r['metadata'].get('external_doc_id')} index_version={r['metadata'].get('index_version')}")

# Test T15: Shared scope  
print("\n=== T15: Shared scope ===")
rag.add_knowledge(text='This shared knowledge base entry for all agents.', agent_id='alpha', session_id='s1', scope='shared', source='manual')
time.sleep(0.5)

pts, _ = qc.scroll(COLLECTION, limit=10, with_payload=True, with_vectors=False)
print(f"Total points: {len(pts)}")
for p in pts:
    print(f"  id={p.id} scope={p.payload.get('scope')} agent={p.payload.get('agent_id')} external_doc_id={p.payload.get('external_doc_id')} index_version={p.payload.get('index_version')} text='{p.payload.get('text', '')[:40]}...'")

results = rag.query_knowledge('shared knowledge base entry', agent_id='beta', search_scope='shared_or_private', limit=5)
print(f"\nQuery results: {len(results)}")
for r in results:
    print(f"  text='{r['text'][:60]}...' scope={r['metadata'].get('scope')} agent={r['metadata'].get('agent_id')} external_doc_id={r['metadata'].get('external_doc_id')} index_version={r['metadata'].get('index_version')}")

qc.delete_collection(COLLECTION)
