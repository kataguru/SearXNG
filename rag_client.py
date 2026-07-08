import os
import re
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

DEFAULT_CHUNK_SIZE_WORDS = 400
DEFAULT_OVERLAP_PCT = 15

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

    def _chunk_text(self, text: str, chunk_size_words: int = DEFAULT_CHUNK_SIZE_WORDS, overlap_pct: int = DEFAULT_OVERLAP_PCT) -> List[str]:
        """Split text into sentence-aware chunks with sliding window overlap."""
        sentences = re.split(r'(?<=[.!?])\s+', text.strip())
        if not sentences or len(sentences) == 1 and ' ' in sentences[0] if sentences else True:
            words = text.strip().split()
            if len(words) <= chunk_size_words:
                return [text.strip()]
        
        chunks = []
        overlap_words = max(1, int(chunk_size_words * overlap_pct / 100))
        step = chunk_size_words - overlap_words

        pos = 0
        sentence_word_counts = [len(s.split()) for s in sentences]

        while pos < len(sentences):
            current_chunk_sentences = []
            current_words = 0

            end = min(pos + len(sentences), len(sentences))
            i = pos
            while i < end:
                if current_words + sentence_word_counts[i] > chunk_size_words and current_chunk_sentences:
                    break
                current_chunk_sentences.append(sentences[i])
                current_words += sentence_word_counts[i]
                i += 1

            chunks.append(" ".join(current_chunk_sentences))
            pos += max(step, 1)

        if not chunks:
            return [text.strip()]

        return chunks

    def add_knowledge(
        self,
        text: str,
        agent_id: str,
        session_id: str,
        scope: str = "shared",
        source: str = "manual",
        extra_metadata: Optional[Dict[str, Any]] = None,
        chunk_size_words: int = DEFAULT_CHUNK_SIZE_WORDS
    ) -> List[str]:
        """
        Embeds and adds a text document to the shared Qdrant RAG.
        Splits text into sentence-aware chunks with sliding window overlap (15%).
        Generates dense and sparse vectors for each chunk using FastEmbed.
        Returns list of doc IDs for all created points.
        """
        base_doc_id = str(uuid.uuid4())

        if not text.strip():
            return []

        chunks = self._chunk_text(text, chunk_size_words)

        import uuid as _uuid
        ns = _uuid.UUID(base_doc_id)

        points = []
        ids = []

        for idx, chunk in enumerate(chunks):
            point_id = str(_uuid.uuid5(ns, f"chunk_{idx}"))
            ids.append(point_id)

            dense_vector = list(self.dense_model.embed([chunk]))[0].tolist()
            sparse_embedding = list(self.sparse_model.embed([chunk]))[0]

            metadata = {
                "text": chunk,
                "agent_id": agent_id,
                "session_id": session_id,
                "scope": scope,
                "source": source,
                "created_at": datetime.utcnow().isoformat(),
                "chunk_index": idx,
                "total_chunks": len(chunks)
            }
            if extra_metadata:
                metadata.update(extra_metadata)

            point = PointStruct(
                id=point_id,
                vector={
                    "dense": dense_vector,
                    "sparse": SparseVector(
                        indices=sparse_embedding.indices.tolist(),
                        values=sparse_embedding.values.tolist()
                    )
                },
                payload=metadata
            )
            points.append(point)

        if points:
            self.client.upsert(
                collection_name=self.collection_name,
                points=points
            )

        return ids

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
