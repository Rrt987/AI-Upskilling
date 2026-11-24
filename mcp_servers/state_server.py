"""mcp-state-store: Streamable HTTP MCP server (default http://127.0.0.1:8081/mcp).

Owns PostgreSQL access (DATABASE_URL). Every query is tenant-scoped (design.md 3).
Tier 3 mutations are never executed here: state_submit_transition only writes PENDING_APPROVAL.
"""
import os

import anyio
from mcp.server.fastmcp import FastMCP

from app.contracts import ERROR_JSONRPC, TENANT_RE, ErrorCode, StateSubmitIn, ToolError
from app.harness import validate
from app.plugins import redact_text
from app.store import PostgresStore, args_hash

PORT = int(os.environ.get("STATE_PORT", "8081"))
mcp = FastMCP("mcp-state-store", host="127.0.0.1", port=PORT)
DSN = os.environ.get("DATABASE_URL")
if not DSN:
    raise SystemExit("DATABASE_URL is required for mcp-state-store")
STORE = PostgresStore(DSN)


async def _run(fn, *args, **kwargs):
    """Run blocking DB work in a worker thread so the event loop keeps serving other tenants' calls."""
    return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))


def _err(exc: ToolError) -> dict:
    return {"error": {"code": exc.code.value, "jsonrpc_code": ERROR_JSONRPC[exc.code], "message": exc.message}}


def _tenant(tenant_id: str) -> str:
    if not TENANT_RE.match(tenant_id or ""):
        raise ToolError(ErrorCode.VALIDATION_FAILED, "bad tenant")
    return tenant_id


@mcp.tool()
async def state_submit_transition(payload: dict) -> dict:
    """TIER_3_MUTATE. Writes PENDING_APPROVAL; a reviewer must approve before any effect."""
    try:
        req = validate(StateSubmitIn, payload)
        raw = req.model_dump(mode="json")
        row = {"user_id": req.user_id, "workflow_id": req.workflow_id,
               "transition": req.transition, "subject_id": req.subject_id}
        rec = await _run(STORE.submit_pending, req.tenant_id, str(req.idempotency_key), args_hash(raw), row)
        if not rec.get("idempotent_replay"):
            await _run(STORE.append_audit, req.tenant_id, "PENDING_APPROVAL_CREATED",
                       {"pending_id": rec["id"], "workflow_id": req.workflow_id,
                        "transition": req.transition, "subject_id": req.subject_id,
                        "requested_by": req.user_id})
        return {"id": rec["id"], "status": rec["status"], "idempotent_replay": bool(rec.get("idempotent_replay"))}
    except ToolError as exc:
        return _err(exc)


@mcp.tool()
async def state_list_pending(tenant_id: str, status: str | None = None) -> dict:
    """TIER_1_READ."""
    try:
        return {"items": await _run(STORE.list_pending, _tenant(tenant_id), status)}
    except ToolError as exc:
        return _err(exc)


@mcp.tool()
async def state_decide(tenant_id: str, pending_id: str, decision: str, reviewer: str) -> dict:
    """Reviewer action (UI/API). Requester cannot approve own mutation."""
    try:
        t = _tenant(tenant_id)
        out = await _run(STORE.decide, t, pending_id, decision, reviewer)
        await _run(STORE.append_audit, t, "HITL_DECISION",
                   {"pending_id": pending_id, "decision": decision, "reviewer": reviewer})
        return out
    except ToolError as exc:
        return _err(exc)


@mcp.tool()
async def audit_append(tenant_id: str, event_type: str, payload: dict) -> dict:
    try:
        ev = await _run(STORE.append_audit, _tenant(tenant_id), event_type[:64], payload)
        return {"seq": ev["seq"], "hash": ev["hash"]}
    except ToolError as exc:
        return _err(exc)


@mcp.tool()
async def audit_verify(tenant_id: str) -> dict:
    try:
        return await _run(STORE.verify_chain, _tenant(tenant_id))
    except ToolError as exc:
        return _err(exc)


@mcp.tool()
async def session_put_fact(tenant_id: str, session_id: str, fact_key: str, fact_value: str,
                           source_chunk_hash: str | None = None) -> dict:
    try:
        t = _tenant(tenant_id)
        await _run(STORE.put_fact, t, session_id[:64], fact_key[:64], redact_text(fact_value)[0], source_chunk_hash)
        return {"ok": True}
    except ToolError as exc:
        return _err(exc)


@mcp.tool()
async def session_get_facts(tenant_id: str, session_id: str) -> dict:
    try:
        return {"facts": await _run(STORE.get_facts, _tenant(tenant_id), session_id)}
    except ToolError as exc:
        return _err(exc)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
