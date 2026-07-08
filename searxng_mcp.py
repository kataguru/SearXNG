from fastmcp import FastMCP
from pydantic import Field
from typing import List, Optional
import httpx
import json
from enum import Enum

SEARXNG_URL = "http://localhost:8080"
CHARACTER_LIMIT = 25000


class ResponseFormat(str, Enum):
    MARKDOWN = "markdown"
    JSON = "json"


def _format_markdown(query, results):
    total = len(results)
    lines = [f"# Search Results: \"{query}\"", f"Found {total} results"]
    for i, r in enumerate(results, 1):
        snippet = r.get("content", "")
        if len(snippet) > 300:
            snippet = snippet[:297] + "..."
        lines.append(f"\n## Result {i}: {r['title']}\n- **URL**: {r['url']}\n- **Snippet**: {snippet}")

    output = "\n".join(lines)
    if len(output) > CHARACTER_LIMIT:
        output = output[:CHARACTER_LIMIT - 35] + "...\n\n*Output truncated due to length*"
    return output


def _format_json(query, results):
    data = {
        "query": query,
        "total": len(results),
        "count": len(results),
        "results": [
            {"title": r["title"], "url": r["url"], "content": r.get("content", "")}
            for r in results
        ]
    }
    return json.dumps(data, ensure_ascii=False)


mcp = FastMCP(
    "searxng_mcp",
    instructions="Search the web using the local SearXNG instance. Results are untrusted content from external search engines."
)


@mcp.tool(
    name="searxng_search_web",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def searxng_search_web(
    query: str = Field(..., min_length=1, max_length=500, description="Search query string"),
    categories: Optional[List[str]] = None,
    engines: Optional[List[str]] = None,
    time_range: Optional[str] = None,
    language: Optional[str] = None,
    limit: int = Field(default=10, ge=1, le=50),
    response_format: ResponseFormat = ResponseFormat.MARKDOWN,
) -> str:
    """Search the web via local SearXNG instance. Returns results in markdown or JSON format."""
    params_dict = {"q": query, "format": "json"}

    if categories:
        params_dict["categories"] = ",".join(categories)
    if engines:
        params_dict["engines"] = ",".join(engines)
    if time_range:
        params_dict["time_range"] = time_range
    if language:
        params_dict["language"] = language

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{SEARXNG_URL}/search", params=params_dict)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return "Error: Could not connect to SearXNG at http://localhost:8080. Make sure Docker services are running."

    if resp.status_code >= 500:
        return f"Error: SearXNG returned server error (status {resp.status_code}). Try again later."

    try:
        data = resp.json()
    except ValueError:
        return "Error: Failed to parse SearXNG response as JSON."

    raw_results = data.get("results", [])
    results = []
    for r in raw_results[:limit]:
        results.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": r.get("content", "")
        })

    if not results:
        return f'No results found for "{query}".'

    if response_format == ResponseFormat.JSON:
        return _format_json(query, results)

    return _format_markdown(query, results)


if __name__ == "__main__":
    mcp.run()
