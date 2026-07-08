import os
import uuid
from typing import List, Dict, Any, Optional
from datetime import datetime

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Filter, FieldCondition, MatchValue, PointStruct, 
    VectorParams, SparseVectorParams, Distance,
    Prefetch, NamedVector, NamedSparseVector, SparseVector, FusionQuery, Fusion
)
from fastembed import TextEmbedding, SparseTextEmbedding

class SharedAgentRAG:
    def __init__(
        self,
        url: str = "http://localhost:6333",
        api_key: Optional[str] = None,
        collection_name: str = "agent_knowledge"
    ):
        self.collection_name = collection_name
        api_key = api_key or os.getenv("QDRANT_API_KEY")
        self.client = QdrantClient(url=url, api_key=api_key)
        
        # Load FastEmbed models locally
        self.dense_model = TextEmbedding("BAAI/bge-small-en-v1.5")
        self.sparse_model = SparseTextEmbedding("prithivida/Splade_PP_en_v1")
        
        # Initialize collection
        self._init_collection()
        print(f"SharedAgentRAG initialized targeting collection '{self.collection_name}'.")

    def _init_collection(self):
        if not self.client.collection_exists(self.collection_name):
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config={
                    "dense": VectorParams(
                        size=384, # bge-small-en-v1.5 dimension
                        distance=Distance.COSINE
                    )
                },
                sparse_vectors_config={
                    "sparse": SparseVectorParams()
                }
            )

    def add_knowledge(
        self,
        text: str,
        agent_id: str,
        session_id: str,
        scope: str = "shared",
        source: str = "manual",
        extra_metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Embeds and adds a text document to the shared Qdrant RAG.
        Generates dense and sparse vectors using FastEmbed.
        """
        doc_id = str(uuid.uuid4())
        
        # Compute embeddings
        dense_vector = list(self.dense_model.embed([text]))[0].tolist()
        sparse_embedding = list(self.sparse_model.embed([text]))[0]
        
        metadata = {
            "text": text, # Store text in payload for retrieval
            "agent_id": agent_id,
            "session_id": session_id,
            "scope": scope,
            "source": source,
            "created_at": datetime.utcnow().isoformat()
        }
        if extra_metadata:
            metadata.update(extra_metadata)
            
        point = PointStruct(
            id=doc_id,
            vector={
                "dense": dense_vector,
                "sparse": SparseVector(
                    indices=sparse_embedding.indices.tolist(),
                    values=sparse_embedding.values.tolist()
                )
            },
            payload=metadata
        )
        
        self.client.upsert(
            collection_name=self.collection_name,
            points=[point]
        )
        return doc_id

    def query_knowledge(
        self,
        query_text: str,
        agent_id: str,
        search_scope: str = "shared_or_private",
        limit: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Queries the vector store using hybrid search (Dense + Sparse).
        Configured for Option 3: Conceptual Search.
        Filters based on agent context and scope.
        """
        # Filter based on scopes
        if search_scope == "shared_or_private":
            filter_to_use = Filter(
                should=[
                    FieldCondition(key="scope", match=MatchValue(value="shared")),
                    FieldCondition(key="agent_id", match=MatchValue(value=agent_id))
                ]
            )
        elif search_scope == "private":
            filter_to_use = Filter(
                must=[
                    FieldCondition(key="scope", match=MatchValue(value="private")),
                    FieldCondition(key="agent_id", match=MatchValue(value=agent_id))
                ]
            )
        else: # "shared"
            filter_to_use = Filter(
                must=[FieldCondition(key="scope", match=MatchValue(value="shared"))]
            )

        # Generate query embeddings
        dense_vector = list(self.dense_model.embed([query_text]))[0].tolist()
        sparse_embedding = list(self.sparse_model.embed([query_text]))[0]

        # Option 3 (Conceptual Search): 70% Dense (limit=50) / 30% Sparse (limit=20)
        results = self.client.query_points(
            collection_name=self.collection_name,
            prefetch=[
                Prefetch(
                    query=dense_vector,
                    using="dense",
                    limit=50
                ),
                Prefetch(
                    query=SparseVector(
                        indices=sparse_embedding.indices.tolist(),
                        values=sparse_embedding.values.tolist()
                    ),
                    using="sparse",
                    limit=20
                )
            ],
            query=FusionQuery(fusion=Fusion.RRF),
            query_filter=filter_to_use,
            limit=limit,
            with_payload=True
        )

        parsed_results = []
        for point in results.points:
            parsed_results.append({
                "id": point.id,
                "score": point.score,
                "text": point.payload.get("text", ""),
                "metadata": point.payload
            })
        return parsed_results
