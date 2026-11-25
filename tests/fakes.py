"""Test doubles: fake MCP pools that drive the real Agent in-process (no subprocesses)."""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Callable

from app.agent import Agent
from app.cache import MemoryCache, SafeCache, SemanticIndex
from app.plugins import CircuitBreaker, InjectionShield, PIIRedactor

Handler = Callable[[str, dict[str, Any]], Any]


class FakePool:
    def __init__(self, handler: Handler):
        self.handler = handler

    async def call(self, tool: str, arguments: dict[str, Any], retry_read: bool = False) -> dict[str, Any]:
        out = self.handler(tool, arguments)
        if asyncio.iscoroutine(out):
            out = await out
        return out


def chunk(text: str, cid: str = "c1", src: str = "POL-LOT-001", tenant: str = "tenant_demo") -> dict[str, Any]:
    return {"chunk_id": cid, "chunk_hash": hashlib.sha256(text.encode()).hexdigest(), "text": text,
            "source_id": src, "rerank_score": 0.9, "source_uri": f"rag://{tenant}/chunks/{cid}"}


def retrieval(chunks: list[dict[str, Any]], top: float = 0.9, abstain: bool = False) -> dict[str, Any]:
    return {"chunks": chunks, "top_score": top, "abstain": abstain,
            "stage_ms": {"bm25": 1.0, "dense": 2.0, "rrf": 0.1, "rerank": 3.0}, "engines": {}}


ANSWER_TEXT = "The batch verification procedure for a lot requires a certificate of analysis."


def default_handler(tool: str, args: dict[str, Any]) -> Any:
    """Answerable retrieval, graph lookup with neighbours, and ok state writes."""
    if tool == "hybrid_retrieve":
        return retrieval([chunk(ANSWER_TEXT)])
    if tool == "graph_subgraph":
        return {"nodes": [args["anchor_id"], "SOP-VER-002"], "edges": [], "tokens": 10, "raw_tokens": 100,
                "labels": {args["anchor_id"]: "Lot safety protocol", "SOP-VER-002": "Batch verification procedure"}}
    return {"ok": True, "seq": 1, "hash": "h", "facts": [], "id": "p1", "status": "PENDING_APPROVAL"}


def make_agent(handler: Handler = default_handler, breaker: CircuitBreaker | None = None,
               semantic: SemanticIndex | None = None) -> Agent:
    pools = {name: FakePool(handler) for name in ("hybrid-rag", "graphify-vault", "state-store")}
    return Agent(pools, SafeCache(MemoryCache()), breaker or CircuitBreaker(), PIIRedactor(),
                 InjectionShield(), semantic=semantic)
