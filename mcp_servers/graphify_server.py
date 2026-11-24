"""mcp-graphify-vault: stdio MCP server. Tool `graph_subgraph` (TIER_1_READ), depth <= 2 wikilink traversal."""
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from app.contracts import ERROR_JSONRPC, GraphSubgraphIn, ToolError
from app.harness import validate
from app.rag.pipeline import CorpusRegistry

mcp = FastMCP("mcp-graphify-vault")
REGISTRY = CorpusRegistry(Path(os.environ.get("VAULT_ROOT", "knowledge_vault")))
for _tenant in REGISTRY.tenants():
    REGISTRY.get(_tenant)


def _heading(body: str, fallback: str) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


@mcp.tool()
def graph_subgraph(payload: dict) -> dict:
    """Minimal connected subgraph around an anchor note. Returns raw_tokens as the KPI-26 baseline."""
    try:
        req = validate(GraphSubgraphIn, payload)
        corpus = REGISTRY.get(req.tenant_id)
        nodes, edges, tokens = corpus.subgraph(req.anchor_id, req.depth)
        labels = {n: _heading(corpus.notes[n].body, n) for n in nodes}
        return {"nodes": nodes, "edges": [list(e) for e in edges], "tokens": tokens,
                "labels": labels, "raw_tokens": corpus.raw_dump_tokens()}
    except ToolError as exc:
        return {"error": {"code": exc.code.value, "jsonrpc_code": ERROR_JSONRPC[exc.code], "message": exc.message}}


if __name__ == "__main__":
    mcp.run(transport="stdio")
