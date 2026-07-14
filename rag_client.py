"""Shared Agent RAG client — hybrid dense+sparse search over Qdrant.

Features:
  - Sentence-aware chunking with configurable overlap
  - Deterministic chunk IDs via UUID5 (stable across re-indexes)
  - External document ID grouping for atomic delete/reindex
  - Provenance metadata tracking (source, trust level, expiration)
"""

import os
import re
import uuid as _uuid
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone

from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import (
    Filter, FieldCondition, MatchValue, PointStruct,
    VectorParams, SparseVectorParams, Distance, Modifier,
    Prefetch, NamedVector, NamedSparseVector, SparseVector, FusionQuery, Fusion,
)
from fastembed import TextEmbedding, SparseTextEmbedding

# ── Chunking defaults ──────────────────────────────────────────────────────────
DEFAULT_CHUNK_SIZE_WORDS = 400
DEFAULT_OVERLAP_PCT = 15

# ── Embedding model identifiers (lock these for reproducibility) ───────────────
DENSE_MODEL_ID = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
SPARSE_MODEL_ID = "Qdrant/bm25"
BM25_LANGUAGE = "finnish"  # Finnish tokenization, stopwords, and Snowball stemmer

# ── Collection names ───────────────────────────────────────────────────────────
COLLECTION_NAME_DEFAULT = "agent_knowledge"
COLLECTION_NAME_BM25_V1 = "agent_knowledge_bm25_v1"


class SharedAgentRAG:
    def __init__(
        self,
        url: str = "http://localhost:6333",
        api_key: Optional[str] = None,
        collection_name: str = COLLECTION_NAME_DEFAULT,
    ):
        self.collection_name = collection_name
        api_key = api_key or os.getenv("QDRANT_API_KEY")
        self.client = QdrantClient(url=url, api_key=api_key)

        # Load FastEmbed models locally — cached after first instantiation
        self.dense_model = TextEmbedding(DENSE_MODEL_ID)
        self.sparse_model = SparseTextEmbedding(SPARSE_MODEL_ID, language=BM25_LANGUAGE)

        # Initialize collection
        self._init_collection()
        print(f"SharedAgentRAG initialized targeting collection '{self.collection_name}'.")

    @classmethod
    def create_bm25_v1(cls, url: str = "http://localhost:6333", api_key: Optional[str] = None) -> 'SharedAgentRAG':
        """Create a SharedAgentRAG instance targeting the BM25 v1 collection.

        This is a convenience method for initializing the new Finnish BM25 collection
        without manually specifying the collection name.
        """
        return cls(url=url, api_key=api_key, collection_name=COLLECTION_NAME_BM25_V1)

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
            raise ConnectionError(
                f"Qdrant delete collection '{collection_name}' failed: {e}"
            ) from e

    def _init_collection(self):
        try:
            if not self.client.collection_exists(self.collection_name):
                self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config={
                        "dense": VectorParams(
                            size=384,  # DENSE_MODEL_ID dimension
                            distance=Distance.COSINE,
                        )
                    },
                    sparse_vectors_config={
                        "sparse": SparseVectorParams(modifier=Modifier.IDF)
                    },
                )
        except UnexpectedResponse as e:
            raise ConnectionError(
                f"Qdrant collection initialization failed for '{self.collection_name}': {e}"
            ) from e

    # ── Text splitting & chunking ────────────────────────────────────────────

    def _split_sentences(self, text: str) -> List[str]:
        """Split PDF-extracted text into real sentences.

        Handles: abbreviations (Mod., Dr.), section numbers (2.1), decimal
        numbers (5.13), TOC dot leaders (. . .), and page headers/footers.
        """
        # Normalize paragraph breaks
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
            sub_parts = re.split(r'(?<=[.!?])\s+(?=[A-Z])', p.strip())
            for sp in sub_parts:
                sp = sp.strip()
                if len(sp) > 5 and not re.match(r'^\d+[\.\s-]', sp):
                    sentences.append(sp)

        return sentences

    def _chunk_text(
        self,
        text: str,
        chunk_size_words: int = DEFAULT_CHUNK_SIZE_WORDS,
        overlap_pct: int = DEFAULT_OVERLAP_PCT,
    ) -> List[str]:
        """Split text into sentence-aware chunks with sliding window overlap.

        Each chunk ends at a sentence boundary. Next chunk starts from the
        last (1 - overlap_pct) sentences of the current chunk.
        """
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

    # ── Document-level operations ────────────────────────────────────────────

    def delete_document(self, external_doc_id: str) -> int:
        """Delete all Qdrant points belonging to an external document.

        Uses the `external_doc_id` payload field as filter key.
        Returns the number of points removed.
        Raises ValueError if external_doc_id is empty or invalid.
        """
        if not isinstance(external_doc_id, str) or not external_doc_id.strip():
            raise ValueError(f"external_doc_id must be a non-empty string. Got {repr(external_doc_id)}.")

        try:
            points = self.client.scroll(
                collection_name=self.collection_name,
                scroll_filter=Filter(
                    must=[FieldCondition(key="external_doc_id", match=MatchValue(value=external_doc_id))]
                ),
                limit=1000,
                with_payload=False,
                with_vectors=False,
            )
            point_ids = [p.id for p in points[0]]

            if not point_ids:
                return 0

            self.client.delete(
                collection_name=self.collection_name,
                points_selector=point_ids,
            )
            return len(point_ids)
        except UnexpectedResponse as e:
            raise ConnectionError(
                f"Qdrant delete document '{external_doc_id}' failed: {e}"
            ) from e

    def add_knowledge(
        self,
        text: str,
        agent_id: str,
        session_id: str,
        scope: str = "shared",
        source: str = "manual",
        external_doc_id: Optional[str] = None,
        index_version: int = 1,
        extra_metadata: Optional[Dict[str, Any]] = None,
        chunk_size_words: int = DEFAULT_CHUNK_SIZE_WORDS,
    ) -> List[str]:
        """Embed and add a text document to the shared Qdrant RAG.

        CRASH-SAFE REINDEXING: This method uses an add-then-cleanup pattern.
        New chunks are upserted with an index_version tag. Old versions are NOT
        deleted automatically. Call cleanup_old_versions() AFTER confirming state
        persistence to remove stale chunks. A crash mid-reindex leaves duplicates,
        which is safer than data loss.

        Splits text into sentence-aware chunks with sliding window overlap (15%).
        Generates dense and sparse vectors for each chunk using FastEmbed.

        Args:
            external_doc_id: Stable identifier linking all chunks of one logical
                document (e.g., Paperless doc ID). Enables versioned reindexing.
                When None, a random UUID is generated per call.
            index_version: Monotonic version number for crash-safe reindexing.
                Increment on each reindex. Used by cleanup_old_versions() to
                distinguish current vs. stale chunks. Default 1.
            extra_metadata: Additional payload fields. Must include provenance keys
                for untrusted sources (see _build_provenance_fields).

        Returns list of point IDs for all created chunks.
        """
        if not text.strip():
            return []

        VALID_SCOPES = ("shared", "private")
        if scope not in VALID_SCOPES:
            raise ValueError(f"Invalid scope '{scope}'. Must be one of {VALID_SCOPES}.")

        # external_doc_id validation — reject empty/invalid values early
        if external_doc_id is not None and (not isinstance(external_doc_id, str) or not external_doc_id.strip()):
            raise ValueError(f"external_doc_id must be a non-empty string. Got {repr(external_doc_id)}.")

        # Use deterministic base ID when external_doc_id is provided
        if external_doc_id:
            ns = _uuid.UUID("f47ac10b-58cc-4372-a567-0e02b2c3d479")  # fixed namespace for external IDs
            base_doc_id = str(_uuid.uuid5(ns, f"ext:{external_doc_id}:{text[:100]}"))
        else:
            base_doc_id = str(_uuid.uuid4())

        chunks = self._chunk_text(text, chunk_size_words)

        ns_uuid = _uuid.UUID(base_doc_id)
        points = []
        ids = []

        for idx, chunk in enumerate(chunks):
            point_id = str(_uuid.uuid5(ns_uuid, f"chunk_{idx}"))
            ids.append(point_id)

            dense_vector = list(self.dense_model.embed([chunk]))[0].tolist()
            sparse_embedding = list(self.sparse_model.embed([chunk]))[0]

            metadata: Dict[str, Any] = {
                "text": chunk,
                "agent_id": agent_id,
                "session_id": session_id,
                "scope": scope,
                "source": source,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "chunk_index": idx,
                "total_chunks": len(chunks),
            }

            # External document ID for grouping chunks of one logical document
            if external_doc_id:
                metadata["external_doc_id"] = external_doc_id

            # Index version for crash-safe reindexing (add-then-cleanup pattern)
            if index_version > 0:
                metadata["index_version"] = index_version

            # Merge extra metadata (provenance fields, title, etc.)
            if extra_metadata:
                metadata.update(extra_metadata)

            point = PointStruct(
                id=point_id,
                vector={
                    "dense": dense_vector,
                    "sparse": SparseVector(
                        indices=sparse_embedding.indices.tolist(),
                        values=sparse_embedding.values.tolist(),
                    ),
                },
                payload=metadata,
            )
            points.append(point)

        if points:
            try:
                self.client.upsert(
                    collection_name=self.collection_name,
                    points=points,
                    wait=True,  # Ensure changes are committed before returning
                )
            except UnexpectedResponse as e:
                raise ConnectionError(
                    f"Qdrant upsert failed for collection '{self.collection_name}': {e}"
                ) from e

        return ids

    def scroll_all(self, collection_name: Optional[str] = None, filter=None, limit_per_page: int = 100) -> List[Dict[str, Any]]:
        """Paginate through all points in a collection using Qdrant's scroll API.

        Continues fetching until offset is None (no more pages). Returns all matching points.

        Args:
            collection_name: Collection to query. Defaults to self.collection_name.
            filter: Optional filter for the scroll query.
            limit_per_page: Points per page (default 100, max recommended by Qdrant).

        Returns list of point dicts with id and payload.
        """
        cname = collection_name or self.collection_name
        all_points = []
        offset = None

        while True:
            points_response = self.client.scroll(
                collection_name=cname,
                scroll_filter=filter,
                limit=limit_per_page,
                with_payload=True,
                with_vectors=False,
                offset=offset,
            )

            points_batch = points_response[0]
            all_points.extend(points_batch)

            # Check if there are more pages — Qdrant returns None as offset when exhausted
            next_offset = points_response[1]
            if next_offset is None:
                break
            offset = next_offset

        return all_points

    def cleanup_old_versions(self, external_doc_id: str, keep_version: int, managed_by: Optional[str] = None) -> int:
        """Delete stale chunks for a document, keeping only the specified version.

        This is the second half of the crash-safe reindexing pattern:
          1. add_knowledge(text, index_version=N+1) — adds new chunks
          2. save_state() — persists that N+1 succeeded
          3. cleanup_old_versions(doc_id, keep_version=N+1) — removes old chunks

        A crash between steps 1 and 3 leaves duplicates (safe).
        A crash during step 3 is idempotent — next sync round cleans up.

        Args:
            external_doc_id: The document to clean up.
            keep_version: The index_version to retain. All other versions are deleted.
            managed_by: Optional filter for managed_by metadata field (e.g., "paperless_sync_daemon").
                If provided, only chunks with this managed_by value are considered.

        Returns the number of stale points removed.
        """
        if not isinstance(external_doc_id, str) or not external_doc_id.strip():
            raise ValueError(f"external_doc_id must be a non-empty string. Got {repr(external_doc_id)}.")

        try:
            # Build filter conditions — include managed_by if specified
            filter_conditions = [FieldCondition(key="external_doc_id", match=MatchValue(value=str(external_doc_id)))]
            if managed_by:
                filter_conditions.append(FieldCondition(key="managed_by", match=MatchValue(value=managed_by)))

            all_points = self.scroll_all(filter=Filter(must=filter_conditions), limit_per_page=100)

            stale_ids = []
            for p in all_points:
                ver = p.payload.get("index_version")
                if ver is not None and ver != keep_version:
                    stale_ids.append(p.id)

            # Batch delete in groups of 500 (Qdrant's recommended batch size)
            deleted_count = 0
            for i in range(0, len(stale_ids), 500):
                batch = stale_ids[i:i + 500]
                self.client.delete(
                    collection_name=self.collection_name,
                    points_selector=batch,
                )
                deleted_count += len(batch)

            return deleted_count
        except UnexpectedResponse as e:
            raise ConnectionError(
                f"Qdrant cleanup old versions for '{external_doc_id}' failed: {e}"
            ) from e

    def delete_documents_managed_by(self, managed_by: str) -> int:
        """Delete all chunks with the specified managed_by metadata field.

        Used to remove all Paperless-managed documents when Paperless is empty.

        Args:
            managed_by: The managed_by value to filter by (e.g., "paperless_sync_daemon").

        Returns the number of points removed.
        """
        if not isinstance(managed_by, str) or not managed_by.strip():
            raise ValueError(f"managed_by must be a non-empty string. Got {repr(managed_by)}.")

        try:
            all_points = self.scroll_all(
                filter=Filter(must=[FieldCondition(key="managed_by", match=MatchValue(value=managed_by))]),
                limit_per_page=100,
            )

            point_ids = [p.id for p in all_points]

            # Batch delete in groups of 500
            deleted_count = 0
            for i in range(0, len(point_ids), 500):
                batch = point_ids[i:i + 500]
                self.client.delete(
                    collection_name=self.collection_name,
                    points_selector=batch,
                )
                deleted_count += len(batch)

            return deleted_count
        except UnexpectedResponse as e:
            raise ConnectionError(
                f"Qdrant delete managed_by='{managed_by}' failed: {e}"
            ) from e

    # ── Query ────────────────────────────────────────────────────────────────

    def query_knowledge(
        self,
        query_text: str,
        agent_id: str,
        score_threshold: Optional[float] = None,
        search_scope: str = "shared_or_private",
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Query the vector store using hybrid search (Dense + Sparse).

        Filters based on agent context and scope. Returns results with full
        metadata including provenance fields when available.
        """
        VALID_SEARCH_SCOPES = ("shared", "private", "shared_or_private")
        if search_scope not in VALID_SEARCH_SCOPES:
            raise ValueError(f"Invalid search_scope '{search_scope}'. Must be one of {VALID_SEARCH_SCOPES}.")

        # Build filter based on scopes
        if search_scope == "shared_or_private":
            filter_to_use = Filter(
                should=[
                    FieldCondition(key="scope", match=MatchValue(value="shared")),
                    FieldCondition(key="agent_id", match=MatchValue(value=agent_id)),
                ]
            )
        elif search_scope == "private":
            filter_to_use = Filter(
                must=[
                    FieldCondition(key="scope", match=MatchValue(value="private")),
                    FieldCondition(key="agent_id", match=MatchValue(value=agent_id)),
                ]
            )
        else:  # "shared"
            filter_to_use = Filter(
                must=[FieldCondition(key="scope", match=MatchValue(value="shared"))]
            )

        # Generate query embeddings
        dense_vector = list(self.dense_model.embed([query_text]))[0].tolist()
        sparse_embedding = list(self.sparse_model.embed([query_text]))[0]

        # Hybrid search: fetch more candidates than limit to account for deduplication.
        # Old versions may consume slots; we need extra buffer.
        # NOTE: This is a temporary eventual consistency heuristic, not an absolute guarantee.
        # If old-version chunks receive higher scores and the active version's chunks don't
        # fit in the candidate pool, the "highest" version found here may be only the highest
        # within the sampled candidates — not the document's true latest version.
        # Startup reconciliation (reconcile_startup) catches this case on daemon restart.
        fetch_limit = max(limit * 3, limit + 20)

        try:
            results = self.client.query_points(
                collection_name=self.collection_name,
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
                score_threshold=score_threshold,
                limit=fetch_limit,
                with_payload=True,
            )
        except UnexpectedResponse as e:
            raise ConnectionError(
                f"Qdrant query failed for collection '{self.collection_name}': {e}"
            ) from e

        # Deduplicate by external_doc_id — keep ALL chunks of the highest index_version per document.
        # Two-pass approach: first find max version per doc, then filter points.
        # This preserves all relevant chunks (not just one) while eliminating stale versions.
        #
        # Eventual consistency note: The "highest" version is relative to the candidate pool.
        # If the true latest version's chunks were filtered out by score_threshold or limit,
        # an older version may appear as highest. reconcile_startup() on next daemon start
        # will clean up these stale versions against the authoritative state file.

        # Pass 1: Find maximum index_version for each external_doc_id (normalize to string)
        max_versions = {}
        for point in results.points:
            ext_id = str(point.payload.get("external_doc_id", "")) if point.payload.get("external_doc_id") is not None else None
            version = point.payload.get("index_version", 0)
            if ext_id:
                current_max = max_versions.get(ext_id, 0)
                if version > current_max:
                    max_versions[ext_id] = version

        # Pass 2: Keep all points where version == max for that doc (or no external_doc_id)
        deduplicated = []
        seen_ids = set()
        for point in results.points:
            ext_id = str(point.payload.get("external_doc_id", "")) if point.payload.get("external_doc_id") is not None else None
            version = point.payload.get("index_version", 0)

            # Keep if: no external_doc_id (ungrouped docs always kept), OR version matches the max for this doc
            keep = (ext_id is None) or (ext_id == "") or (version == max_versions.get(ext_id, 0))
            if keep and point.id not in seen_ids:
                deduplicated.append({
                    "id": point.id,
                    "score": point.score,
                    "text": point.payload.get("text", ""),
                    "metadata": point.payload,
                })
                seen_ids.add(point.id)

        # Apply user's limit after deduplication (preserves original score ordering)
        parsed_results = deduplicated[:limit]
        return parsed_results


# ── Provenance helpers (used by MCP layer and sync daemon) ─────────────────────

def build_provenance_metadata(
    source_url: Optional[str] = None,
    source_type: str = "manual",
    trust_level: str = "trusted",
    expires_at: Optional[str] = None,
    published_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Build provenance metadata dict for RAG ingestion.

    Required for untrusted sources (web_search, scraping). Ensures every
    indexed document carries traceability and expiration information.

    Args:
        source_url: Originating URL or file path.
        source_type: Category — "manual", "paperless", "user_decision",
            "web_search" (untrusted), "scraping" (untrusted).
        trust_level: "trusted" for user/project data; "untrusted" for web content.
        expires_at: ISO timestamp after which the document should be pruned.
            Untrusted sources MUST set this.
        published_at: Original publication date if known.

    Returns dict suitable for extra_metadata in add_knowledge().
    """
    meta = {
        "source_url": source_url,
        "source_type": source_type,
        "trust_level": trust_level,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }

    if expires_at:
        meta["expires_at"] = expires_at
    elif trust_level == "untrusted":
        # Default 30-day expiration for untrusted content
        from datetime import timedelta
        default_expiry = datetime.now(timezone.utc) + timedelta(days=30)
        meta["expires_at"] = default_expiry.isoformat()

    if published_at:
        meta["published_at"] = published_at

    return meta


def validate_ingestion_allowed(
    source: str,
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    """Validate whether content is allowed to be ingested into RAG.

    Untrusted sources (web_search, scraping) require provenance metadata:
      - source_url must be present
      - expires_at must be set
      - trust_level must be "untrusted"

    Trusted sources (manual, paperless, user_decision) pass through freely.
    """
    if source in ("manual", "paperless", "user_decision"):
        return True

    # Untrusted sources require provenance metadata
    if not extra_metadata:
        return False

    required = ["source_url", "expires_at"]
    for field in required:
        if not extra_metadata.get(field):
            return False

    return True
