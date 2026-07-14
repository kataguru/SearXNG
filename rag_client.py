import os
import re
import uuid as _uuid
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone

from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
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
        self.dense_model = TextEmbedding("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
        self.sparse_model = SparseTextEmbedding("prithivida/Splade_PP_en_v1")
        
        # Initialize collection
        self._init_collection()
        print(f"SharedAgentRAG initialized targeting collection '{self.collection_name}'.")

    def list_collections(self) -> List[Dict[str, Any]]:
        try:
            cols = self.client.get_collections().collections
            result = []
            for c in cols:
                info = self.client.get_collection(c.name)
                result.append({
                    "name": c.name,
                    "vectors_count": info.vectors_count if hasattr(info, 'vectors_count') else info.points_count,
                    "points_count": info.points_count,
                })
            return result
        except UnexpectedResponse as e:
            raise ConnectionError(f"Qdrant list collections failed: {e}") from e

    def delete_collection(self, collection_name: str) -> bool:
        try:
            self.client.delete_collection(collection_name=collection_name)
            return True
        except UnexpectedResponse as e:
            raise ConnectionError(f"Qdrant delete collection '{collection_name}' failed: {e}") from e

    def _init_collection(self):
        try:
            if not self.client.collection_exists(self.collection_name):
                self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config={
                        "dense": VectorParams(
                            size=384, # paraphrase-multilingual-MiniLM-L12-v2 dimension
                            distance=Distance.COSINE
                        )
                    },
                    sparse_vectors_config={
                        "sparse": SparseVectorParams()
                    }
                )
        except UnexpectedResponse as e:
            raise ConnectionError(f"Qdrant collection initialization failed for '{self.collection_name}': {e}") from e

    def _split_sentences(self, text: str):
        """Split PDF-extracted text into real sentences.
        Handles: abbreviations (Mod., Dr.), section numbers (2.1), decimal numbers (5.13),
        TOC dot leaders (. . .), and page headers/footers."""
        # Strip whitespace-only lines, normalize repeated newlines to paragraph breaks
        text = re.sub(r'\n{3,}', '\n\n', text.strip())

        parts = []
        for para in text.split('\n\n'):
            cleaned = ' '.join(para.split()).strip()
            if not cleaned:
                continue
            # Skip TOC entries with dot leaders (5+ dots including spaces)
            dot_count = sum(1 for c in cleaned if c == '.')
            if dot_count > 10 and len(cleaned.replace('.', '')) < dot_count * 3:
                continue
            parts.append(cleaned)

        sentences = []
        for p in parts:
            # Split on sentence-ending punctuation followed by capital letter
            sub_parts = re.split(r'(?<=[.!?])\s+(?=[A-Z])', p.strip())
            for sp in sub_parts:
                sp = sp.strip()
                if len(sp) > 5 and not re.match(r'^\d+[\.\s-]', sp):
                    sentences.append(sp)

        return sentences

    def _chunk_text(self, text: str, chunk_size_words: int = DEFAULT_CHUNK_SIZE_WORDS, overlap_pct: int = DEFAULT_OVERLAP_PCT) -> List[str]:
        """Split text into sentence-aware chunks with sliding window overlap.
        Each chunk ends at a sentence boundary. Next chunk starts from the
        last (1 - overlap_pct) sentences of the current chunk."""
        sentences = self._split_sentences(text.strip())

        words = text.strip().split()
        if len(words) <= chunk_size_words or not sentences:
            return [text.strip()] if words else []

        chunks = []
        i = 0

        while i < len(sentences):
            current_chunk_sentences = []
            current_words = 0

            j = i
            while j < len(sentences):
                sw = len(sentences[j].split())
                if current_words + sw > chunk_size_words and current_chunk_sentences:
                    break
                current_chunk_sentences.append(sentences[j])
                current_words += sw
                j += 1

            chunks.append(" ".join(current_chunk_sentences))

            n = len(current_chunk_sentences)
            if n == 0:
                i += 1
                continue

            # Next chunk starts from the overlap portion (last floor(n*overlap_pct/100) sentences)
            overlap_count = max(1, int(n * overlap_pct / 100))
            move_back = min(overlap_count, n - 1) if n > 1 else 0
            i += max(n - move_back, 1)

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
        base_doc_id = str(_uuid.uuid4())

        if not text.strip():
            return []

        chunks = self._chunk_text(text, chunk_size_words)

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
                "created_at": datetime.now(timezone.utc).isoformat(),
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
            try:
                self.client.upsert(
                    collection_name=self.collection_name,
                    points=points
                )
            except UnexpectedResponse as e:
                raise ConnectionError(f"Qdrant upsert failed for collection '{self.collection_name}': {e}") from e

        return ids

    def query_knowledge(
        self,
        query_text: str,
        agent_id: str,
        score_threshold: Optional[float] = None,
        search_scope: str = "shared_or_private",
        limit: int = 10
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
        try:
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
                score_threshold=score_threshold,
                limit=limit,
                with_payload=True
            )
        except UnexpectedResponse as e:
            raise ConnectionError(f"Qdrant query failed for collection '{self.collection_name}': {e}") from e

        parsed_results = []
        for point in results.points:
            parsed_results.append({
                "id": point.id,
                "score": point.score,
                "text": point.payload.get("text", ""),
                "metadata": point.payload
            })
        return parsed_results
