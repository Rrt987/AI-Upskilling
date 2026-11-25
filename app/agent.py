"""Bounded ReAct orchestrator (design.md Gates 4-7, 9; ADR-4).

State machine: INIT -> TRIAGE -> REASONING -> TOOL_CALL -> OBSERVATION -> VERIFY -> DONE | HALTED
Limits: max_iterations=5, total_loop_budget=300 ms (D-01), per_step_timeout=120 ms per tool call (D-02).
Zero recursive self-calls: the only loop is the iteration loop, and each iteration mutates state
(query expansion, or a graph lookup), otherwise the loop exits.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from app import telemetry
from app.cache import SafeCache, SemanticIndex, ttl_with_jitter
from app.contracts import (
    ErrorCode,
    GraphSubgraphIn,
    HybridRetrieveIn,
    Route,
    StateSubmitIn,
    Tier,
    ToolError,
    acl_hash,
    cache_key,
)
from app.harness import call_tool, normalize_entities
from app.mcp_client import MCPPool
from app.plugins import METRICS, CircuitBreaker, InjectionShield, PIIRedactor
from app.rag.engines import HashingDense
from app.routing import (
    anchors_in,
    compress_observation,
    extractive_answer,
    goal_anchor,
    route_with_fallback,
    classify,
)

MAX_ITERATIONS = 5
# Writes (audit, session facts, Tier 3 submissions) get their own timeout and are never dropped by the
# 120 ms read-step budget (v0.1 finding FN-006: audit writes were timing out under load).
WRITE_TIMEOUT_S = 2.0
# Read-only tools may be retried once on a transport failure. Writes are never retried here (no duplicates).
READ_TOOLS = {"hybrid_retrieve", "graph_subgraph", "state_list_pending", "session_get_facts"}
LOOP_BUDGET_MS = 300.0
PER_STEP_TIMEOUT_S = 0.120


_EMBED = HashingDense()
_ZERO_USAGE = {"input_tokens": 0, "output_tokens": 0, "cost_usd": None}


def _usage(input_tokens: int, output_tokens: int) -> dict[str, Any]:
    """Token meter (B-04). Word counts are the token proxy. Cost only when both prices are configured."""
    pin, pout = os.environ.get("PRICE_IN_PER_1K"), os.environ.get("PRICE_OUT_PER_1K")
    cost = None
    if pin and pout:
        cost = round(input_tokens / 1000 * float(pin) + output_tokens / 1000 * float(pout), 6)
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "cost_usd": cost}


class State(str, Enum):
    INIT = "INIT"
    TRIAGE = "TRIAGE"
    REASONING = "REASONING"
    TOOL_CALL = "TOOL_CALL"
    OBSERVATION = "OBSERVATION"
    VERIFY = "VERIFY"
    DONE = "DONE"
    HALTED = "HALTED"


class ActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow_id: str = Field(pattern=r"^[A-Z]{2,4}-[A-Z0-9-]+$")
    transition: str = Field(min_length=1, max_length=64)
    subject_id: str


class QueryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str = Field(pattern=r"^tenant_[a-z0-9_-]{1,64}$")
    user_id: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=64)
    query: str = Field(min_length=1, max_length=2000)
    permissions: list[str] = Field(default_factory=lambda: ["TIER_1_READ"], max_length=8)
    action: Optional[ActionIn] = None
    idempotency_key: Optional[uuid.UUID] = None


@dataclass
class ChunkView:
    """Minimal chunk shape used by the routing helpers (text + id)."""
    chunk_id: str
    text: str


def _user_ref(user_id: str) -> str:
    """Audit stores a salted-free reference, never the raw user identifier."""
    return hashlib.sha256(("user:" + user_id).encode()).hexdigest()[:16]


class Agent:
    def __init__(self, pools: dict[str, MCPPool], cache: SafeCache, breaker: CircuitBreaker,
                 pii: PIIRedactor, shield: InjectionShield, semantic: SemanticIndex | None = None):
        self.pools = pools
        self.cache = cache
        self.breaker = breaker
        self.pii = pii
        self.shield = shield
        self.semantic = semantic or SemanticIndex()

    # ---------- harness-wrapped tool call ----------
    async def _call(self, server: str, tool: str, model: type[BaseModel], args: dict[str, Any],
                    tier: Tier, granted: set[Tier], timeout_s: float | None = None) -> dict[str, Any]:
        pool = self.pools[server]
        step_s = timeout_s if timeout_s is not None else PER_STEP_TIMEOUT_S

        retry = tool in READ_TOOLS

        async def dispatch(clean: dict[str, Any]) -> dict[str, Any]:
            return await asyncio.wait_for(pool.call(tool, clean, retry_read=retry), timeout=step_s)

        return await call_tool(tool=tool, model=model, args=args, required_tier=tier,
                               granted=granted, breaker=self.breaker, dispatch=dispatch)

    # ---------- main entry ----------
    async def handle(self, body: QueryIn, header_tenant: str | None = None,
                     traceparent: str | None = None) -> dict[str, Any]:
        t0 = time.perf_counter()
        ctx = telemetry.request_context(traceparent)
        trace_id = telemetry.trace_id_hex(ctx)
        steps: list[dict[str, Any]] = []
        stage_ms: dict[str, float] = {}
        state = State.INIT

        def elapsed_ms() -> float:
            return (time.perf_counter() - t0) * 1000.0

        def step(st: State, detail: str) -> None:
            steps.append({"state": st.value, "detail": detail, "at_ms": round(elapsed_ms(), 3)})

        def finish(status: str, *, route: Route | None = None, answer: str | None = None,
                   citations: list[dict[str, Any]] | None = None, confidence: float = 0.0,
                   error: ErrorCode | None = None, message: str | None = None,
                   pending_id: str | None = None, react_steps: int = 0, cache: str = "MISS",
                   st: State = State.DONE, usage: dict[str, Any] | None = None) -> dict[str, Any]:
            METRICS.inc(f"status_{status.lower()}")
            METRICS.inc("kpi17_react_steps_total", react_steps)
            METRICS.inc("kpi17_tasks")
            return {
                "status": status,
                "route": route.value if route else None,
                "tenant_id": body.tenant_id,
                "answer": answer,
                "citations": citations or [],
                "confidence": round(confidence, 6),
                "abstained": status == "ABSTAINED",
                "error": error.value if error else None,
                "message": message,
                "pending_approval_id": pending_id,
                "cache": cache,
                "react_steps": react_steps,
                "final_state": st.value,
                "trace_id": trace_id,
                "latency_ms": round(elapsed_ms(), 3),
                "stage_ms": stage_ms,
                "usage": usage or dict(_ZERO_USAGE),
                "trace": steps,
            }

        # ---- INIT: tenant binding, RBAC grant set ----
        if header_tenant is not None and header_tenant != body.tenant_id:
            METRICS.inc("kpi28_tenant_mismatch")
            return finish("REJECTED", error=ErrorCode.TENANT_MISMATCH,
                          message="X-Tenant-ID does not match body tenant_id")
        try:
            granted = {Tier(p) for p in body.permissions} | {Tier.TIER_1_READ}
        except ValueError:
            return finish("REJECTED", error=ErrorCode.VALIDATION_FAILED, message="unknown permission")
        acl = acl_hash(body.permissions)

        # ---- pre-execution hooks (fail-closed) ----
        t = time.perf_counter()
        try:
            redacted, pii_counts = self.pii.run(body.query)
            if pii_counts:
                METRICS.inc("pii_redactions", sum(pii_counts.values()))
            self.shield.check_query(redacted)
        except ToolError as exc:
            stage_ms["hook.pre"] = round((time.perf_counter() - t) * 1000, 3)
            telemetry.emit("hook.pre", stage_ms["hook.pre"], ctx, {"blocked": exc.code.value})
            if exc.code is ErrorCode.INJECTION_BLOCKED:
                METRICS.inc("injection_blocked")
            step(State.INIT, f"pre-hook blocked: {exc.code.value}")
            return finish("BLOCKED", error=exc.code, message=exc.message)
        stage_ms["hook.pre"] = round((time.perf_counter() - t) * 1000, 3)
        telemetry.emit("hook.pre", stage_ms["hook.pre"], ctx)
        step(State.INIT, "pre-hooks passed (pii, injection)")

        normalized, rewrites = normalize_entities(redacted)
        if rewrites:
            METRICS.inc("kpi15_entity_rewrites", rewrites)
        norm_query = " ".join(normalized.lower().split())

        # ---- cache lookup (tenant-partitioned, ADR-3) ----
        key = cache_key(body.tenant_id, acl, norm_query)
        t = time.perf_counter()
        hit = await self.cache.get(key)
        stage_ms["cache.lookup"] = round((time.perf_counter() - t) * 1000, 3)
        telemetry.emit("cache.lookup", stage_ms["cache.lookup"], ctx)
        if hit is not None and hit.get("tenant_id") == body.tenant_id:
            METRICS.inc("cache_hit")
            step(State.INIT, "cache hit")
            return {**hit, "cache": "HIT", "trace_id": trace_id, "latency_ms": round(elapsed_ms(), 3),
                    "stage_ms": stage_ms, "trace": steps + [{"state": "DONE", "detail": "cache", "at_ms": round(elapsed_ms(), 3)}]}
        # Semantic layer (B-03): search only this tenant and ACL partition.
        emb = _EMBED.embed(norm_query)
        sem = self.semantic.best(body.tenant_id, acl, emb)
        if sem is not None:
            sem_hit = await self.cache.get(sem[1])
            if sem_hit is not None and sem_hit.get("tenant_id") == body.tenant_id:
                METRICS.inc("semantic_hit")
                step(State.INIT, f"semantic hit score={sem[0]:.3f}")
                return {**sem_hit, "cache": "SEMANTIC_HIT", "trace_id": trace_id,
                        "latency_ms": round(elapsed_ms(), 3), "stage_ms": stage_ms,
                        "trace": steps + [{"state": "DONE", "detail": "semantic cache", "at_ms": round(elapsed_ms(), 3)}]}
        METRICS.inc("cache_miss")

        # ---- TRIAGE ----
        state = State.TRIAGE
        t = time.perf_counter()
        label = classify(normalized)
        route = route_with_fallback(label)
        stage_ms["triage.classify"] = round((time.perf_counter() - t) * 1000, 3)
        telemetry.emit("triage.classify", stage_ms["triage.classify"], ctx, {"route": route.value})
        step(state, f"intent={label.intent} domain={label.domain} risk={label.risk_tier} route={route.value}")
        if route is Route.REJECT_OOS:
            return finish("REJECTED", route=route, error=None, st=State.DONE,
                          message="Outside the supported scope. Clinical, legal, and financial decisions need a human expert.")
        if route is Route.DIRECT_ANSWER:
            return finish("ANSWERED", route=route,
                          answer="Hello. Ask me about lot verification, quarantine, SKUs, or approval policies.",
                          confidence=1.0)

        # ---- TRANSACTION_EXEC: Tier 3, never executed here ----
        if route is Route.TRANSACTION_EXEC:
            state = State.REASONING
            if body.action is None:
                return finish("NEEDS_INPUT", route=route,
                              message="Provide action.workflow_id, action.transition and action.subject_id.")
            state = State.TOOL_CALL
            idem = str(body.idempotency_key or uuid.uuid4())
            args = {"tenant_id": body.tenant_id, "user_id": body.user_id,
                    "workflow_id": body.action.workflow_id, "transition": body.action.transition,
                    "subject_id": body.action.subject_id, "idempotency_key": idem}
            try:
                t = time.perf_counter()
                rec = await self._call("state-store", "state_submit_transition", StateSubmitIn, args,
                                       Tier.TIER_3_MUTATE, granted, timeout_s=WRITE_TIMEOUT_S)
                stage_ms["mcp.state.submit"] = round((time.perf_counter() - t) * 1000, 3)
            except ToolError as exc:
                step(state, f"tier3 refused: {exc.code.value}")
                return finish("REJECTED", route=route, error=exc.code, message=exc.message)
            METRICS.inc("kpi19_pending_created")
            step(state, f"PENDING_APPROVAL {rec['id']}")
            return finish("PENDING_APPROVAL", route=route, pending_id=rec["id"],
                          message="Awaiting reviewer approval. Nothing was executed.")

        # ---- HYBRID_RAG: bounded ReAct loop ----
        state = State.REASONING
        goal = goal_anchor(body.query, normalized, body.tenant_id, route, label.risk_tier)
        step(state, f"goal anchor entities={goal['entity_ids']}")
        query = normalized
        expanded = False
        react_steps = 0
        chunks: list[ChunkView] = []
        retrieval: dict[str, Any] = {}
        halted: Optional[tuple[ErrorCode, str]] = None

        for _ in range(MAX_ITERATIONS):
            if elapsed_ms() > LOOP_BUDGET_MS:
                halted = (ErrorCode.TIMEOUT, "loop budget of 300 ms exhausted")
                state = State.HALTED
                break
            react_steps += 1
            state = State.TOOL_CALL
            args = {"tenant_id": body.tenant_id, "acl_hash": acl, "query": query}
            try:
                t = time.perf_counter()
                retrieval = await self._call("hybrid-rag", "hybrid_retrieve", HybridRetrieveIn, args,
                                             Tier.TIER_1_READ, granted)
                stage_ms["mcp.rag.total"] = round((time.perf_counter() - t) * 1000, 3)
                telemetry.emit("agent.react.step", stage_ms["mcp.rag.total"], ctx, {"iteration": react_steps})
                for stage_key, span_name in telemetry.RAG_STAGE_SPANS.items():
                    if stage_key in retrieval.get("stage_ms", {}):
                        telemetry.emit(span_name, retrieval["stage_ms"][stage_key], ctx)
            except ToolError as exc:
                if exc.code is ErrorCode.BREAKER_OPEN:
                    METRICS.inc("kpi16_breaker_halts")
                    halted = (exc.code, "retrieval breaker open for this call; rephrase or contact support")
                else:
                    halted = (exc.code, exc.message)
                state = State.HALTED
                step(state, f"tool failed: {exc.code.value}")
                break
            for k, v in retrieval.get("stage_ms", {}).items():
                stage_ms[f"mcp.rag.{k}"] = v

            state = State.OBSERVATION
            views = [ChunkView(c["chunk_id"], c["text"]) for c in retrieval.get("chunks", [])]
            kept_views, dropped = self.shield.sanitize_chunks(views)
            if dropped:
                METRICS.inc("indirect_injection_chunks_dropped", dropped)
            chunks = kept_views
            observation = compress_observation(chunks)
            step(state, f"observation: {len(chunks)} chunks, dropped={dropped}, "
                        f"top={retrieval.get('top_score')}, obs_tokens={len(observation.split())}")
            if chunks and not retrieval.get("abstain"):
                break
            # Bounded expansion: one graph lookup per request, only when the query names a vault anchor.
            anchors = anchors_in(normalized)
            if anchors and not expanded and react_steps < MAX_ITERATIONS:
                expanded = True
                state = State.TOOL_CALL
                try:
                    g = await self._call("graphify-vault", "graph_subgraph", GraphSubgraphIn,
                                         {"tenant_id": body.tenant_id, "anchor_id": anchors[0], "depth": 2},
                                         Tier.TIER_1_READ, granted)
                except ToolError as exc:
                    step(state, f"graph lookup failed: {exc.code.value}")
                    break
                neighbours = [v for n, v in g.get("labels", {}).items() if n != anchors[0]]
                if not neighbours:
                    break
                query = normalized + " " + " ".join(neighbours)
                react_steps += 1
                step(State.REASONING, f"expanded query with {len(neighbours)} linked notes")
                continue
            break

        if halted:
            code, message = halted
            if state is not State.HALTED:
                state = State.HALTED
            step(State.HALTED, message)
            return finish("HALTED", route=route, error=code, message=message,
                          react_steps=react_steps, st=State.HALTED)

        # ---- VERIFY / answer ----
        state = State.VERIFY
        if not chunks or retrieval.get("abstain"):
            step(state, "abstain: no chunk above threshold")
            return finish("ABSTAINED", route=route, confidence=float(retrieval.get("top_score", 0.0)),
                          message="I could not find support for this in the approved knowledge base.",
                          react_steps=react_steps)
        t_gen = time.perf_counter()
        kept, total, verified = extractive_answer(normalized, chunks, max_sentences=2)
        telemetry.emit("llm.generate", (time.perf_counter() - t_gen) * 1000, ctx, {"generator": "extractive"})
        if not kept:
            step(state, f"verify veto: {total} candidates, 0 verified")
            METRICS.inc("kpi24_verify_veto")
            return finish("ABSTAINED", route=route, message="No sentence could be verified against the sources.",
                          react_steps=react_steps)
        by_id = {c["chunk_id"]: c for c in retrieval["chunks"]}
        citations = []
        seen: set[str] = set()
        for s in kept:
            if s.chunk_id in seen:
                continue
            seen.add(s.chunk_id)
            c = by_id[s.chunk_id]
            citations.append({"chunk_id": c["chunk_id"], "source_id": c["source_id"],
                              "rerank_score": c["rerank_score"], "source_uri": c["source_uri"]})
        answer = " ".join(s.text for s in kept)
        confidence = float(retrieval.get("top_score", 0.0))
        step(state, f"verified {verified}/{total} sentences")
        in_tokens = len(compress_observation(chunks).split()) + len(normalized.split())
        response = finish("ANSWERED", route=route, answer=answer, citations=citations,
                          confidence=confidence, react_steps=react_steps,
                          usage=_usage(in_tokens, len(answer.split())))

        # ---- persistence: audit (every request), session fact, cache (ANSWERED only) ----
        await self._audit(body, trace_id, response)
        if response["status"] == "ANSWERED":
            try:
                await self._call("state-store", "session_put_fact", _FactIn, {
                    "tenant_id": body.tenant_id, "session_id": body.session_id,
                    "fact_key": "verified_answer", "fact_value": answer[:500],
                    "source_chunk_hash": by_id[kept[0].chunk_id]["chunk_hash"]}, Tier.TIER_1_READ, granted,
                    timeout_s=WRITE_TIMEOUT_S)
            except ToolError:
                METRICS.inc("session_fact_write_failed")
            cacheable = {k: v for k, v in response.items() if k not in ("trace", "trace_id", "latency_ms", "stage_ms", "cache")}
            await self.cache.set(key, cacheable, ttl_with_jitter())
            self.semantic.add(body.tenant_id, acl, key, emb)
        return response

    async def _audit(self, body: QueryIn, trace_id: str, response: dict[str, Any]) -> None:
        if os.environ.get("AUDIT_ENABLED", "1") == "0":  # diagnostic flag, see failure_notes FN-003
            return
        payload = {"trace_id": trace_id, "tenant_id": body.tenant_id, "user_ref": _user_ref(body.user_id),
                   "status": response["status"], "route": response["route"],
                   "citations": [c["chunk_id"] for c in response["citations"]],
                   "react_steps": response["react_steps"]}
        try:
            await self._call("state-store", "audit_append", _AuditIn, {
                "tenant_id": body.tenant_id, "event_type": "QUERY", "payload": payload},
                Tier.TIER_1_READ, {Tier.TIER_1_READ}, timeout_s=WRITE_TIMEOUT_S)
        except ToolError:
            METRICS.inc("audit_write_failed")


class _FactIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    session_id: str
    fact_key: str
    fact_value: str
    source_chunk_hash: Optional[str] = None


class _AuditIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    event_type: str
    payload: dict
