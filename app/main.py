"""FastAPI entrypoint: /api/v1/agent/query and ops endpoints (design.md 1.3, Gate 15).

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000   (needs the state server, see scripts/dev_stack.py)
"""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app import telemetry
from app.agent import Agent, QueryIn
from app.cache import MemoryCache, RedisCache, SafeCache
from app.contracts import ErrorCode, ToolError
from app.mcp_client import ROOT, build_pools
from app.plugins import METRICS, CircuitBreaker, InjectionShield, PIIRedactor
from app.rag.engines import DEGRADED_ENGINES

VAULT_ROOT = os.environ.get("VAULT_ROOT", str(ROOT / "knowledge_vault"))
STATE_URL = os.environ.get("STATE_URL", "http://127.0.0.1:8081/mcp")
REDIS_URL = os.environ.get("REDIS_URL")


@asynccontextmanager
async def lifespan(app: FastAPI):
    telemetry.configure()
    cfg = json.loads((ROOT / "mcp_config.json").read_text(encoding="utf-8"))
    sizes = {"hybrid-rag": int(os.environ.get("RAG_POOL_SIZE", "4")),
             "graphify-vault": int(os.environ.get("GRAPH_POOL_SIZE", "2")),
             "state-store": int(os.environ.get("STATE_POOL_SIZE", "8"))}
    pools = build_pools(cfg, VAULT_ROOT, STATE_URL, pool_sizes=sizes)
    for pool in pools.values():
        await pool.start()
    backend = RedisCache(REDIS_URL) if REDIS_URL else MemoryCache()
    cache = SafeCache(backend)
    breaker = CircuitBreaker(max_identical_failures=cfg["plugins"][2].get("max_identical_calls", 2))
    app.state.pools = pools
    app.state.cache = cache
    app.state.breaker = breaker
    app.state.agent = Agent(pools, cache, breaker, PIIRedactor(), InjectionShield())
    try:
        yield
    finally:
        for pool in pools.values():
            await pool.stop()


app = FastAPI(title="Unreliable Assistant - Enterprise v0.1", version="0.1.0", lifespan=lifespan)


class DecideIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    pending_id: str
    decision: str = Field(pattern=r"^(APPROVED|REJECTED)$")
    reviewer: str = Field(min_length=1, max_length=64)


# Admission control (Gate 9 bulkhead). Beyond MAX_INFLIGHT the worker sheds load with 503 at once,
# instead of queueing requests until per-step timeouts turn them into HALTED (failure_notes FN-004).
# Default 16 per worker: validated on the 4-vCPU test host (failure_notes FN-005). Raise only after a
# staged run shows the bigger number holds the latency budget.
MAX_INFLIGHT = int(os.environ.get("MAX_INFLIGHT", "16"))
_INFLIGHT = {"n": 0}


@app.post("/api/v1/agent/query")
async def query(body: QueryIn, x_tenant_id: str | None = Header(default=None),
                traceparent: str | None = Header(default=None)):
    if _INFLIGHT["n"] >= MAX_INFLIGHT:
        METRICS.inc("admission_shed_503")
        return JSONResponse(status_code=503, headers={"Retry-After": "1"},
                            content={"status": "OVERLOADED", "error": "OVERLOADED",
                                     "message": "worker at capacity; retry after 1 s", "tenant_id": body.tenant_id})
    _INFLIGHT["n"] += 1
    try:
        result = await app.state.agent.handle(body, header_tenant=x_tenant_id, traceparent=traceparent)
    finally:
        _INFLIGHT["n"] -= 1
    if result["status"] == "REJECTED" and result["error"] == "TENANT_MISMATCH":
        raise HTTPException(status_code=403, detail=result["message"])
    return result


_HTTP_STATUS = {
    ErrorCode.RBAC_DENIED: 403,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.VALIDATION_FAILED: 422,
    ErrorCode.TIMEOUT: 503,
}


async def _state_call(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Call mcp-state-store and map tool errors to HTTP status codes."""
    try:
        return await app.state.pools["state-store"].call(tool, args)
    except ToolError as exc:
        raise HTTPException(status_code=_HTTP_STATUS.get(exc.code, 400),
                            detail={"error": exc.code.value, "message": exc.message})


@app.get("/api/v1/hitl/pending")
async def pending(tenant_id: str = Query(pattern=r"^tenant_[a-z0-9_-]{1,64}$"),
                  status: str | None = None) -> dict[str, Any]:
    return await _state_call("state_list_pending", {"tenant_id": tenant_id, "status": status})


@app.post("/api/v1/hitl/decide")
async def decide(body: DecideIn) -> dict[str, Any]:
    return await _state_call("state_decide", body.model_dump())


@app.get("/api/v1/session/facts")
async def session_facts(tenant_id: str = Query(pattern=r"^tenant_[a-z0-9_-]{1,64}$"),
                        session_id: str = Query(min_length=1, max_length=64)) -> dict[str, Any]:
    return await _state_call("session_get_facts", {"tenant_id": tenant_id, "session_id": session_id})


@app.get("/api/v1/audit/verify")
async def audit_verify(tenant_id: str = Query(pattern=r"^tenant_[a-z0-9_-]{1,64}$")) -> dict[str, Any]:
    return await _state_call("audit_verify", {"tenant_id": tenant_id})


@app.post("/api/v1/breaker/reset")
async def breaker_reset() -> dict[str, Any]:
    app.state.breaker.reset()
    return {"ok": True}


@app.get("/health")
async def health() -> dict[str, Any]:
    pools = app.state.pools
    servers = {name: await p.ping() for name, p in pools.items()}
    overall = "ok" if all(servers.values()) else "degraded"
    return {"status": overall, "mcp_servers": servers,
            "pools": {name: p.status() for name, p in pools.items()},
            "cache_backend": app.state.cache.name,
            "degraded_engines": DEGRADED_ENGINES, "design_version": "v0.1"}


@app.get("/metrics")
async def metrics() -> dict[str, Any]:
    snap = METRICS.snapshot()
    return {"counters": snap,
            "breaker_open_keys": app.state.breaker.open_keys(),
            "breaker_tracked_keys": app.state.breaker.size(),
            "cache_bypass_count": app.state.cache.bypass_count}
