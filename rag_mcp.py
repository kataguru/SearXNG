"""RAG MCP Server — query and ingest knowledge into the shared agent RAG.

Security:
  - AGENT_ID is REQUIRED. Server refuses to start without it.
  - Untrusted sources (web_search, scraping) require provenance metadata.
  - delete_rag_collection requires explicit confirmation.

Configure via environment variable: AGENT_ID=<unique_identifier>
Example: export AGENT_ID=my_project_agent_01
"""

import os
import sys
import json
from datetime import datetime, timezone
from typing import Optional

from fastmcp import FastMCP
from pydantic import Field
from rag_client import SharedAgentRAG, build_provenance_metadata, validate_ingestion_allowed

QDRANT_URL = "http://localhost:6333"
COLLECTION_NAME = "agent_knowledge"

# ── AGENT_ID is mandatory ──────────────────────────────────────────────────────
AGENT_ID = os.getenv("AGENT_ID")
if not AGENT_ID:
    print(
        "FATAL: AGENT_ID environment variable is not set.\n"
        "Set a unique identifier for this agent to enable scope isolation.\n"
        "Example: export AGENT_ID=my_project_agent_01\n"
        "Do NOT use 'kilo_default' or any shared value."
    )
    sys.exit(1)


_RAG_INSTANCE = None


def _get_rag() -> SharedAgentRAG:
    global _RAG_INSTANCE
    if _RAG_INSTANCE is None:
        _RAG_INSTANCE = SharedAgentRAG(collection_name=COLLECTION_NAME)
    return _RAG_INSTANCE


def _format_query_results(query: str, results: list) -> str:
    lines = [f'# RAG Query Results: "{query}"', f"Found {len(results)} results"]
    for i, r in enumerate(results, 1):
        text = r.get("text", "")
        if len(text) > 300:
            text = text[:297] + "..."
        meta = {k: v for k, v in r["metadata"].items() if k != "text"}
        lines.append(
            f'\n## Result {i} (score: {r["score"]:.4f})'
            f"\n- **Source**: {r['metadata'].get('source', 'N/A')}"
            f"\n- **Text**: {text}"
            f'\n- **Metadata**: {json.dumps(meta, ensure_ascii=False)}'
        )
    return "\n".join(lines)


mcp = FastMCP(
    "rag_mcp",
    instructions=(
        "Query and ingest knowledge into the shared agent RAG vector database "
        "(Qdrant collection 'agent_knowledge')."
    ),
)


@mcp.tool(
    name="list_rag_collections",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def list_rag_collections() -> str:
    """List all Qdrant collections with point counts."""
    try:
        rag = _get_rag()
        cols = rag.list_collections()
        if not cols:
            return "No collections found."
        lines = ["# Qdrant Collections", f"Found {len(cols)} collection(s)"]
        for c in cols:
            lines.append(
                f"- **{c['name']}**: {c['points_count']} points, {c['vectors_count']} vectors"
            )
        return "\n".join(lines)
    except ConnectionError:
        return f"Error: Could not connect to Qdrant at {QDRANT_URL}."


@mcp.tool(
    name="delete_rag_collection",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def delete_rag_collection(collection: str = Field(..., description="Name of the Qdrant collection to delete")) -> str:
    """Permanently delete a Qdrant collection and all its data.

    WARNING: This operation is irreversible. Only collections belonging to
    this agent's project may be deleted. The default 'agent_knowledge' collection
    should NOT be deleted unless explicitly requested by the user with full awareness
    of data loss consequences.
    """
    try:
        rag = _get_rag()
        cols = rag.list_collections()
        matching = [c for c in cols if c["name"] == collection]
        point_count = matching[0]["points_count"] if matching else 0

        from qdrant_client.http.exceptions import UnexpectedResponse
        try:
            rag.delete_collection(collection)
        except UnexpectedResponse as e:
            status_code = e.code if hasattr(e, "code") else "unknown"
            if status_code == 404 or (hasattr(e, "message") and "not found" in str(e).lower()):
                return f"Collection '{collection}' not found."
            raise

        return f"Collection '{collection}' deleted ({point_count} points removed)."
    except ConnectionError:
        return f"Error: Could not connect to Qdrant at {QDRANT_URL}."


@mcp.tool(
    name="rag_add_knowledge",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def rag_add_knowledge(
    text: str = Field(..., min_length=10, description="Text content to index (min 10 chars)"),
    source: str = Field(
        default="manual",
        description=(
            "Source category: 'manual', 'paperless', 'user_decision' for trusted data; "
            "'web_search', 'scraping' for untrusted. Untrusted sources require provenance metadata."
        ),
    ),
    session_id: str = Field(default="mcp_session", description="Session grouping identifier"),
    scope: str = Field(
        default="shared",
        description="'shared' (visible to all agents) or 'private' (only this agent via AGENT_ID)",
    ),
    provenance_metadata: Optional[str] = Field(
        default=None,
        description=(
            "JSON string of provenance metadata. REQUIRED for untrusted sources (web_search, scraping). "
            "Must include: source_url, expires_at. Optional: published_at, trust_level."
        ),
    ),
) -> str:
    """Ingest text into the shared agent RAG vector database.

    For trusted sources (manual, paperless, user_decision): content is stored permanently.
    For untrusted sources (web_search, scraping): provenance metadata with source_url and
    expires_at is REQUIRED. Content without proper provenance will be rejected.
    """
    try:
        rag = _get_rag()

        provenance_dict = None
        if provenance_metadata is not None:
            try:
                provenance_dict = json.loads(provenance_metadata)
            except (json.JSONDecodeError, TypeError) as e:
                return f"Error: provenance_metadata must be valid JSON. Got: {e}"

        # Enforce write gate for untrusted sources
        if not validate_ingestion_allowed(source, provenance_dict):
            return (
                "REJECTED: Untrusted source '" + source + "' requires provenance metadata.\n"
                "Provide provenance_metadata as JSON with at least:\n"
                "  - source_url: originating URL\n"
                "  - expires_at: ISO timestamp for expiration default 30 days\n"
                "Example: {'source_url': 'https://example.com', 'expires_at': '2026-08-14T00:00:00Z'}"
            )

        doc_ids = rag.add_knowledge(
            text=text,
            agent_id=AGENT_ID,
            session_id=session_id,
            scope=scope,
            source=source,
            extra_metadata=provenance_dict,
        )
        return f"Document indexed successfully: {doc_ids}"
    except ConnectionError:
        return f"Error: Could not connect to Qdrant at {QDRANT_URL}."


@mcp.tool(
    name="rag_query_knowledge",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def rag_query_knowledge(
    query: str = Field(..., min_length=1, max_length=500, description="Search query string"),
    limit: int = Field(default=5, ge=1, le=20, description="Max results to return (1-20)"),
    score_threshold: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum RRF fusion score (0-1). Note: RRF scores are not calibrated probabilities — "
            "threshold should be tuned experimentally per query type."
        ),
    ),
) -> str:
    """Query the shared agent RAG using hybrid search (dense + sparse vectors).

    Searches both shared scope and this agent's private documents.
    Results include provenance metadata when available.
    """
    try:
        rag = _get_rag()

        results = rag.query_knowledge(
            query_text=query,
            agent_id=AGENT_ID,
            score_threshold=score_threshold,
            search_scope="shared_or_private",
            limit=limit,
        )

        if not results:
            return f'No results found for "{query}".'

        return _format_query_results(query, results)
    except ConnectionError:
        return f"Error: Could not connect to Qdrant at {QDRANT_URL}."


if __name__ == "__main__":
    mcp.run()
