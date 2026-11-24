"""Lifecycle plugins and harness primitives (design.md 2, 1.2.4, Gate 4, Gate 9).

PRE_EXECUTION : pii_redactor (fail-closed, prio 10), injection_shield (fail-closed, prio 20)
TOOL_DISPATCH : circuit_breaker (trips after 2 identical failures, prio 10)
Metrics are process-local counters exposed at /metrics for KPI-14/15/16/17.
"""
from __future__ import annotations

import re
import threading
import time
import unicodedata
from collections import Counter, OrderedDict
from typing import Any

from app.contracts import ErrorCode, ToolError, sha256_hex

# ---------- PII (KPI-02) ----------
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(r"(?<!\w)(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?!\w)")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_OBF_AT = re.compile(r"\s*(?:\[at\]|\(at\)|\{at\}| at )\s*", re.I)
_OBF_DOT = re.compile(r"\s*(?:\[dot\]|\(dot\)|\{dot\}| dot )\s*", re.I)


def normalize_for_pii(text: str) -> str:
    """Undo unicode homoglyphs and common obfuscation before matching (red-team category 4)."""
    t = unicodedata.normalize("NFKC", text)
    t = _OBF_AT.sub("@", t)
    t = _OBF_DOT.sub(".", t)
    return t


def redact_text(text: str) -> tuple[str, Counter]:
    counts: Counter = Counter()
    norm = normalize_for_pii(text)

    def _sub(pattern, label, s):
        def repl(_m):
            counts[label] += 1
            return f"[REDACTED_{label}]"
        return pattern.sub(repl, s)

    out = _sub(_EMAIL, "EMAIL", norm)
    out = _sub(_SSN, "SSN", out)
    out = _sub(_PHONE, "PHONE", out)
    return out, counts


def redact_payload(obj: Any) -> Any:
    """Recursively redact every string. Used by the audit store (zero raw PII persistence)."""
    if isinstance(obj, str):
        return redact_text(obj)[0]
    if isinstance(obj, dict):
        return {k: redact_payload(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_payload(v) for v in obj]
    return obj


class PIIRedactor:
    name = "pii_redactor"
    stage = "PRE_EXECUTION"
    priority = 10
    fail_closed = True

    def run(self, text: str) -> tuple[str, Counter]:
        try:
            return redact_text(text)
        except Exception as exc:  # fail closed: never pass unredacted text on
            raise ToolError(ErrorCode.PII_CHECK_UNAVAILABLE, f"pii check failed: {exc.__class__.__name__}")


# ---------- Injection (KPI-03) ----------
_INJECTION_PATTERNS = [
    re.compile(p, re.I) for p in (
        r"ignore (all |any |the )?(previous|prior|above) (instructions|prompts|rules)",
        r"disregard (all |the )?(previous|prior|above|your) (instructions|rules|guidelines)",
        r"(reveal|print|show|repeat|output) (your |the )?(system|hidden|initial) (prompt|instructions)",
        r"you are now (in )?(developer|dan|unrestricted|jailbreak)",
        r"\bjailbreak\b",
        r"\bdeveloper mode\b",
        r"<\s*/?\s*system\s*>",
        r"\bsystem prompt\b",
        r"act as (an? )?(unfiltered|unrestricted)",
    )
]


def injection_score(text: str) -> int:
    t = normalize_for_pii(text)
    return sum(1 for p in _INJECTION_PATTERNS if p.search(t))


class InjectionShield:
    name = "injection_shield"
    stage = "PRE_EXECUTION"
    priority = 20
    fail_closed = True

    def check_query(self, text: str) -> None:
        try:
            hit = injection_score(text)
        except Exception as exc:
            raise ToolError(ErrorCode.INJECTION_BLOCKED, f"shield error: {exc.__class__.__name__}")
        if hit:
            raise ToolError(ErrorCode.INJECTION_BLOCKED, "query matched prompt-injection rules")

    def sanitize_chunks(self, chunks: list[Any]) -> tuple[list[Any], int]:
        """Indirect injection (red-team category 2): drop retrieved chunks that carry injected instructions."""
        kept, dropped = [], 0
        for c in chunks:
            if injection_score(c.text):
                dropped += 1
            else:
                kept.append(c)
        return kept, dropped


# ---------- Circuit breaker (KPI-16, design 9.1) ----------
class CircuitBreaker:
    """Opens for a (tool_name, canonical_args_hash) key after `max_identical_failures` failures.

    Memory is bounded (FMEA: leak found in v0.1 load run): at most `max_keys` entries, LRU-evicted,
    and an open key expires after `open_ttl_s` (half-open retry). Explicit reset is available for ops.
    """

    name = "circuit_breaker"
    stage = "TOOL_DISPATCH"
    priority = 10

    def __init__(self, max_identical_failures: int = 2, max_keys: int = 10_000, open_ttl_s: float = 120.0,
                 clock=time.monotonic):
        self.max_failures = max_identical_failures
        self.max_keys = max_keys
        self.open_ttl_s = open_ttl_s
        self._clock = clock
        self._fails: OrderedDict[str, int] = OrderedDict()
        self._open: OrderedDict[str, float] = OrderedDict()  # key -> opened_at
        self._lock = threading.Lock()

    @staticmethod
    def key(tool: str, args: dict[str, Any]) -> str:
        return sha256_hex(tool + "|" + repr(sorted(args.items())))

    def _expire(self) -> None:
        now = self._clock()
        for k in [k for k, t0 in self._open.items() if now - t0 >= self.open_ttl_s]:
            self._open.pop(k, None)
        while len(self._open) > self.max_keys:
            self._open.popitem(last=False)
        while len(self._fails) > self.max_keys:
            self._fails.popitem(last=False)

    def check(self, tool: str, args: dict[str, Any]) -> None:
        k = self.key(tool, args)
        with self._lock:
            self._expire()
            if k in self._open:
                raise ToolError(ErrorCode.BREAKER_OPEN, "identical call failed repeatedly; breaker open")

    def record_failure(self, tool: str, args: dict[str, Any]) -> bool:
        k = self.key(tool, args)
        with self._lock:
            self._expire()
            self._fails[k] = self._fails.get(k, 0) + 1
            self._fails.move_to_end(k)
            if self._fails[k] >= self.max_failures:
                self._open[k] = self._clock()
                self._open.move_to_end(k)
                self._fails.pop(k, None)
                return True
        return False

    def record_success(self, tool: str, args: dict[str, Any]) -> None:
        with self._lock:
            self._fails.pop(self.key(tool, args), None)

    def open_keys(self) -> list[str]:
        with self._lock:
            self._expire()
            return list(self._open)

    def size(self) -> int:
        with self._lock:
            return len(self._open) + len(self._fails)

    def reset(self) -> None:
        with self._lock:
            self._fails.clear()
            self._open.clear()


# ---------- Process metrics (KPI-14/15/16/17) ----------
class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.c: Counter = Counter()

    def inc(self, name: str, n: int = 1) -> None:
        with self._lock:
            self.c[name] += n

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self.c)


METRICS = Metrics()
