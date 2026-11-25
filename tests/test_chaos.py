"""Chaos suite for KPI-16 (design.md B-01). In-process fault injection against the real Agent.

Pass: every trial terminates with <= MAX_ITERATIONS tool steps, no unhandled exception, and bounded latency.
Writes reports/chaos_result.json for the KPI scorecard.
"""
import asyncio
import json
import random
from pathlib import Path


from app.agent import MAX_ITERATIONS, QueryIn
from app.contracts import ErrorCode, ToolError
from tests.fakes import default_handler, make_agent, retrieval, chunk
from app.plugins import CircuitBreaker

ROOT = Path(__file__).resolve().parent.parent
QUERIES = [
    "What is the batch verification procedure for a lot?",
    "POL-LOT-001 zebra mars parking rules",
    "SOP-QUAR-004 unrelated moon colony rules",
    "CAT-SKU-001 martian submarine catalogue",
    "quarantine procedure temperature excursion",
]
TRIALS = 300


def body(q: str) -> QueryIn:
    return QueryIn(tenant_id="tenant_demo", user_id="chaos", session_id="chaos", query=q,
                   permissions=["TIER_1_READ"])


def chaos_handler(rng: random.Random):
    def handler(tool, args):
        if tool in ("hybrid_retrieve", "graph_subgraph"):
            mode = rng.choice(["ok", "abstain", "timeout", "raise", "slow", "garbage", "graph_fail"])
            if tool == "graph_subgraph" and mode == "graph_fail":
                raise ToolError(ErrorCode.TIMEOUT, "graph down")
            if mode == "timeout":
                raise ToolError(ErrorCode.TIMEOUT, "injected")
            if mode == "raise":
                raise RuntimeError("injected crash")
            if mode == "slow":
                async def slow():
                    await asyncio.sleep(0.5)
                    return retrieval([chunk("x")])
                return slow()
            if mode == "garbage":
                return {"unexpected": True}
            if tool == "hybrid_retrieve" and mode == "abstain":
                return retrieval([], top=0.1, abstain=True)
            if tool == "graph_subgraph":
                return default_handler(tool, args)
        return default_handler(tool, args)
    return handler


def test_breaker_opens_on_second_identical_failure_and_stays_bounded():
    def always_timeout(tool, args):
        if tool == "hybrid_retrieve":
            raise ToolError(ErrorCode.TIMEOUT, "down")
        return default_handler(tool, args)
    agent = make_agent(always_timeout)
    q = "batch verification lot"
    first = asyncio.run(agent.handle(body(q)))
    second = asyncio.run(agent.handle(body(q)))
    third = asyncio.run(agent.handle(body(q)))
    assert first["status"] == "HALTED" and second["status"] == "HALTED"
    assert third["status"] == "HALTED" and third["error"] == "BREAKER_OPEN"


def test_anchored_abstaining_query_never_exceeds_five_steps():
    def abstain(tool, args):
        if tool == "hybrid_retrieve":
            return retrieval([], top=0.05, abstain=True)
        return default_handler(tool, args)
    d = asyncio.run(make_agent(abstain).handle(body("POL-LOT-001 zebra mars parking")))
    assert d["react_steps"] <= MAX_ITERATIONS
    assert d["status"] in ("ABSTAINED", "HALTED")


def test_slow_tool_is_cut_by_step_budget():
    async def slow(tool, args):
        await asyncio.sleep(0.5)
        return retrieval([chunk("x")])
    def handler(tool, args):
        return slow(tool, args) if tool == "hybrid_retrieve" else default_handler(tool, args)
    d = asyncio.run(make_agent(handler).handle(body("batch verification lot")))
    assert d["status"] == "HALTED" and d["error"] == "TIMEOUT"
    assert d["latency_ms"] < 400


def test_randomised_fault_injection_escape_rate_is_zero():
    rng = random.Random(20260125)
    escapes = []
    statuses = {}
    for i in range(TRIALS):
        agent = make_agent(chaos_handler(rng), breaker=CircuitBreaker(clock=lambda: 0.0))
        q = rng.choice(QUERIES)
        try:
            d = asyncio.run(agent.handle(body(q)))
        except Exception as exc:  # an unhandled exception is an escape
            escapes.append({"trial": i, "error": repr(exc)})
            continue
        statuses[d["status"]] = statuses.get(d["status"], 0) + 1
        if d["react_steps"] > MAX_ITERATIONS or d["latency_ms"] > 1500:
            escapes.append({"trial": i, "steps": d["react_steps"], "latency_ms": d["latency_ms"]})
    result = {"trials": TRIALS, "escapes": len(escapes), "escape_rate": len(escapes) / TRIALS,
              "statuses": statuses, "max_steps_allowed": MAX_ITERATIONS}
    (ROOT / "reports" / "chaos_result.json").write_text(json.dumps(result, indent=2))
    assert not escapes, escapes[:5]
