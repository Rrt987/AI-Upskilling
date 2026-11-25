"""Triage agent, extractive generation + verification, observation compression (design.md Gates 5-7).

v0 notes (recorded in failure_notes.md / design OI-1, OI-2):
  - Triage is a rule-based classifier with the same output schema as the distilled model.
  - Generation is extractive: sentences are quoted from retrieved chunks, never composed.
    That makes unsupported claims impossible by construction in v0. The per-sentence NLI
    verifier is a substring/coverage check standing in for the NLI model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.contracts import Route, TriageLabel
from app.rag.engines import tokenize

_GREETING = re.compile(r"^\s*(hi|hello|hey|thanks|thank you)\b", re.I)
_OOS = re.compile(r"\b(diagnos\w*|medical advice|legal advice|lawsuit|stock|invest\w*|tax advice|prescri\w*)\b", re.I)
_TXN = re.compile(r"\b(submit|approve|reject|transition|change status|set status)\b", re.I)
_TXN_LEAD = re.compile(r"^\s*(release|quarantine|approve|reject|submit|transition|mark|set|change)\b", re.I)
_QUESTION_LEAD = re.compile(r"^\s*(what|how|why|who|when|where|which|does|do|is|are|can|should|could|would)\b", re.I)
_AUDIT = re.compile(r"\b(audit|compliance|hash chain)\b", re.I)
_HIGH_RISK = re.compile(r"\b(quarantine|release|expense|payment|clinical|legal)\b", re.I)
_DOMAIN = [
    ("quality", re.compile(r"\b(lot|batch|sku|temperature|certificate|verification|safety)\b", re.I)),
    ("finance", re.compile(r"\b(expense|invoice|budget|reimburs\w*)\b", re.I)),
    ("governance", re.compile(r"\b(approval|reviewer|hitl|policy)\b", re.I)),
]
_ANCHOR = re.compile(r"\b([A-Z]{2,4}-[A-Z0-9]+-\d{3})\b")


def classify(query: str) -> TriageLabel:
    """Return (intent, domain, risk_tier, complexity, route). Deterministic, temperature-0 semantics."""
    q = query.strip()
    domain = next((d for d, pat in _DOMAIN if pat.search(q)), "general")
    complexity = "multi_step" if (" and " in q.lower() or q.count("?") > 1 or len(q.split()) > 30) else "simple"
    if _OOS.search(q):
        return TriageLabel(intent="out_of_scope", domain="professional", risk_tier="HIGH",
                           complexity=complexity, route=Route.REJECT_OOS, confidence=0.9)
    if _GREETING.match(q) and len(q.split()) <= 6:
        return TriageLabel(intent="greeting", domain="general", risk_tier="LOW",
                           complexity="simple", route=Route.DIRECT_ANSWER, confidence=0.95)
    is_question = bool(_QUESTION_LEAD.match(q)) or q.endswith("?")
    if not is_question and (_TXN.search(q) or _TXN_LEAD.match(q)):
        risk = "HIGH" if _HIGH_RISK.search(q) or domain == "finance" else "MEDIUM"
        return TriageLabel(intent="transaction", domain=domain, risk_tier=risk,
                           complexity=complexity, route=Route.TRANSACTION_EXEC, confidence=0.85)
    intent = "audit_question" if _AUDIT.search(q) else "question"
    risk = "HIGH" if _HIGH_RISK.search(q) else "LOW"
    return TriageLabel(intent=intent, domain=domain, risk_tier=risk, complexity=complexity,
                       route=Route.HYBRID_RAG, confidence=0.8 if domain != "general" else 0.6)


def route_with_fallback(label: TriageLabel) -> Route:
    """FMEA-08: low-confidence classifications take the safe read path, never TRANSACTION_EXEC."""
    if label.confidence < 0.6 and label.route is Route.TRANSACTION_EXEC:
        return Route.HYBRID_RAG
    return label.route


def anchors_in(text: str) -> list[str]:
    return sorted(set(_ANCHOR.findall(text)))


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _norm_ws(s: str) -> str:
    return " ".join(s.split())


@dataclass
class Sentence:
    text: str
    chunk_id: str
    chunk_text: str
    score: float


_STOP_RAW = ("a an and are as at be by do does for from how in is it of on or the to what which who why "
             "with can should i my we our you your this that")
_STOP = set(tokenize(_STOP_RAW))


def extract_candidates(query: str, chunks) -> list[Sentence]:
    q = set(tokenize(query)) - _STOP
    out: list[Sentence] = []
    for c in chunks:
        for s in (piece for line in c.text.split("\n") for piece in _SENT_SPLIT.split(line)):
            s = s.strip()
            if not s:
                continue
            if not s.endswith((".", "!", "?")) and len(s.split()) < 8:
                continue  # headings and fragments are not citable claims
            toks = set(tokenize(s))
            if not toks or not q:
                continue
            score = len(q & toks) / len(q)  # coverage of content terms; stopwords excluded
            if score > 0:
                out.append(Sentence(s, c.chunk_id, c.text, score))
    out.sort(key=lambda x: (-x.score, x.chunk_id))
    return out


def extractive_answer(query: str, chunks, max_sentences: int = 2) -> tuple[list[Sentence], int, int]:
    """Pick top sentences quoted from chunks. Returns (kept, candidates_total, verified_total)."""
    cands = extract_candidates(query, chunks)[:max_sentences]
    verified = [s for s in cands if verify_sentence(s)]
    return verified, len(cands), len(verified)


def verify_sentence(s: Sentence) -> bool:
    """Stand-in for the per-sentence NLI check. Strict extractive quote: sentence must appear verbatim."""
    return _norm_ws(s.text) in _norm_ws(s.chunk_text)


def compress_observation(chunks, token_limit: int = 180) -> str:
    """Gate 5.2: strip boilerplate, keep chunk ids and text, truncate to <= token_limit tokens."""
    parts: list[str] = []
    budget = token_limit
    for c in chunks:
        head = f"[{c.chunk_id}] "
        words = (head + c.text).split()
        take = words[:max(0, budget)]
        if not take:
            break
        parts.append(" ".join(take))
        budget -= len(take)
        if budget <= 0:
            break
    return "\n".join(parts)


def goal_anchor(raw_query: str, normalized_query: str, tenant_id: str, route: Route, risk: str) -> dict:
    """Gate 5.1 GoalAnchorBlock: placed at index 0 of every reasoning prompt, never dropped."""
    return {
        "original_intent": raw_query[:500],
        "normalized_query": normalized_query[:500],
        "entity_ids": anchors_in(normalized_query),
        "policy": {"tenant_id": tenant_id, "route": route.value, "risk_tier": risk,
                   "rules": ["cite sources", "abstain when unsupported", "no autonomous Tier 3"]},
    }
