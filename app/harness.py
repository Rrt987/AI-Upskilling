"""ToolExecutionHarness (design.md Gate 4).

Every MCP tool call passes: (1) Pydantic v2 strict validation, (2) deterministic regex auto-repair,
(3) RBAC tier check, (4) UUIDv4 idempotency (enforced by the Tier 3 model) and the circuit breaker.
"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from app.contracts import ErrorCode, Tier, ToolError
from app.plugins import METRICS, CircuitBreaker

# ---- deterministic repair rules (design.md 1.2.4) ----
_REPAIRS: list[tuple[re.Pattern, Callable[[re.Match], str]]] = [
    (re.compile(r"^SKU-?(\d{1,6})$", re.I), lambda m: "SKU-" + m.group(1).zfill(3)),
    (re.compile(r"^LOT[\s-]*(\d{1,5})$", re.I), lambda m: "LOT-" + m.group(1).zfill(5)),
]

# Free-text entity normalisation used for GoalAnchorBlock "normalized entity IDs" (Gate 5).
_INLINE_SKU = re.compile(r"\bSKU[\s-]?(\d{1,6})\b", re.I)
_INLINE_LOT = re.compile(r"\bLOT[\s-]+(\d{1,5})\b", re.I)


def repair_value(value: str) -> str | None:
    """Return the repaired value, or None when no rule applies."""
    v = value.strip()
    for pattern, fn in _REPAIRS:
        m = pattern.match(v)
        if m:
            return fn(m)
    return None


def normalize_entities(text: str) -> tuple[str, int]:
    """Rewrite entity mentions in free text. Returns (text, number of rewrites)."""
    count = 0

    def sku(m: re.Match) -> str:
        nonlocal count
        count += 1
        return "SKU-" + m.group(1).zfill(3)

    def lot(m: re.Match) -> str:
        nonlocal count
        count += 1
        return "LOT-" + m.group(1).zfill(5)

    out = _INLINE_SKU.sub(sku, text)
    out = _INLINE_LOT.sub(lot, out)
    return out, count


def _repair_args(args: dict[str, Any]) -> tuple[dict[str, Any], int]:
    fixed = dict(args)
    changed = 0
    for k, v in args.items():
        if isinstance(v, str):
            r = repair_value(v)
            if r is not None and r != v:
                fixed[k] = r
                changed += 1
    return fixed, changed


def check_tier(required: Tier, granted: set[Tier]) -> None:
    if required not in granted:
        raise ToolError(ErrorCode.RBAC_DENIED, f"{required.value} not granted")


def validate(model: type[BaseModel], args: dict[str, Any]) -> BaseModel:
    """Strict validation with one deterministic repair pass. Updates KPI-14/15 counters."""
    METRICS.inc("kpi14_validate_total")
    try:
        obj = model.model_validate(args)
        METRICS.inc("kpi14_validate_first_pass_ok")
        return obj
    except ValidationError as first_err:
        fixed, changed = _repair_args(args)
        if changed:
            METRICS.inc("kpi15_repair_attempts")
            try:
                obj = model.model_validate(fixed)
                METRICS.inc("kpi15_repair_success")
                return obj
            except ValidationError:
                pass
        msg = "; ".join(e["msg"] for e in first_err.errors()[:3])
        raise ToolError(ErrorCode.VALIDATION_FAILED, msg)


async def call_tool(
    *,
    tool: str,
    model: type[BaseModel],
    args: dict[str, Any],
    required_tier: Tier,
    granted: set[Tier],
    breaker: CircuitBreaker,
    dispatch: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Harness pipeline for one tool call. Raises ToolError on any gate failure."""
    check_tier(required_tier, granted)
    validated = validate(model, args)
    clean = validated.model_dump(mode="json")
    breaker.check(tool, clean)
    try:
        result = await dispatch(clean)
    except ToolError as exc:
        if exc.code in (ErrorCode.VALIDATION_FAILED, ErrorCode.RBAC_DENIED):
            raise
        breaker.record_failure(tool, clean)
        raise
    except Exception:
        breaker.record_failure(tool, clean)
        raise ToolError(ErrorCode.TIMEOUT, f"{tool} failed")
    breaker.record_success(tool, clean)
    return result
