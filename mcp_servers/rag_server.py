"""mcp-hybrid-rag: stdio MCP server. Tool `hybrid_retrieve`, resource `rag://{tenant_id}/chunks/{chunk_id}`.

Run by the host with: uv run mcp_servers/rag_server.py   (env VAULT_ROOT, default knowledge_vault)
"""
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from app.contracts import ERROR_JSONRPC, HybridRetrieveIn, ToolError
from app.harness import validate
from app.rag.pipeline import CorpusRegistry
from app.rag.pipeline import hybrid_retrieve as run_pipeline

mcp = FastMCP("mcp-hybrid-rag")
REGISTRY = CorpusRegistry(Path(os.environ.get("VAULT_ROOT", "knowledge_vault")))
for _tenant in REGISTRY.tenants():  # warm all corpora so the first call is not an index build
    REGISTRY.get(_tenant)


def _err(exc: ToolError) -> dict:
    return {"error": {"code": exc.code.value, "jsonrpc_code": ERROR_JSONRPC[exc.code], "message": exc.message}}


@mcp.tool()
def hybrid_retrieve(payload: dict) -> dict:
    """3-stage hybrid retrieval: BM25 + dense -> RRF -> cross-encoder. Read-only (TIER_1_READ)."""
    try:
        req = validate(HybridRetrieveIn, payload)
        corpus = REGISTRY.get(req.tenant_id)
        return run_pipeline(corpus, req).model_dump(mode="json")
    except ToolError as exc:
        return _err(exc)


@mcp.resource("rag://{tenant_id}/chunks/{chunk_id}")
def chunk_resource(tenant_id: str, chunk_id: str) -> str:
    try:
        corpus = REGISTRY.get(tenant_id)
    except ValueError:
        return ""
    for c in corpus.chunks:
        if c.chunk_id == chunk_id:
            return c.text
    return ""


if __name__ == "__main__":
    mcp.run(transport="stdio")
