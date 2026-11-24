"""Corpus loading (Obsidian vault -> chunks + wikilink graph) and the 3-stage pipeline."""
from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from app.contracts import Chunk as ChunkModel
from app.contracts import HybridRetrieveIn, HybridRetrieveOut, TENANT_RE, sha256_hex
from app.rag.engines import (
    DEGRADED_ENGINES,
    Chunk,
    LexicalCrossScorer,
    TenantIndex,
    chunk_text,
    rrf_fuse,
)

_WIKILINK_RE = re.compile(r"\[\[([A-Za-z0-9\-]+)\]\]")
_FM_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)


@dataclass
class Note:
    note_id: str
    domain: str
    risk_tier: str
    version: str
    body: str
    links: list[str]
    path: str


@dataclass
class Corpus:
    tenant_id: str
    notes: dict[str, Note]
    chunks: list[Chunk]
    index: TenantIndex

    def raw_dump_tokens(self) -> int:
        """Baseline for KPI-26: every note body concatenated (the 'raw chunk dump')."""
        return sum(len(n.body.split()) for n in self.notes.values())

    def subgraph(self, anchor: str, depth: int) -> tuple[list[str], list[tuple[str, str]], int]:
        """BFS over wikilinks, at most `depth` hops (Gate 17). Returns nodes, edges, token count."""
        if anchor not in self.notes:
            return [], [], 0
        seen = {anchor: 0}
        edges: list[tuple[str, str]] = []
        q = deque([anchor])
        while q:
            cur = q.popleft()
            if seen[cur] >= depth:
                continue
            for nxt in self.notes[cur].links:
                if nxt not in self.notes:
                    continue
                edges.append((cur, nxt))
                if nxt not in seen:
                    seen[nxt] = seen[cur] + 1
                    q.append(nxt)
        tokens = sum(len(self.notes[n].body.split()) for n in seen)
        return sorted(seen), sorted(set(edges)), tokens


def clean_markdown(body: str) -> str:
    """Plain, sentence-per-line text for retrieval: headings and list markers removed, wikilinks -> ids."""
    out = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"^#+\s*", "", line)
        line = re.sub(r"^(?:[-*]|\d+\.)\s+", "", line)
        line = _WIKILINK_RE.sub(r"\1", line)
        out.append(line)
    return "\n".join(out)


def parse_note(path: Path) -> Note:
    raw = path.read_text(encoding="utf-8")
    m = _FM_RE.match(raw)
    meta: dict[str, str] = {}
    body = raw
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        body = raw[m.end():]
    links = sorted(set(_WIKILINK_RE.findall(body)))
    return Note(
        note_id=meta.get("id", path.stem),
        domain=meta.get("domain", "unknown"),
        risk_tier=meta.get("risk_tier", "HIGH"),  # unknown tier defaults to the safest value
        version=meta.get("version", "0"),
        body=body.strip(),
        links=links,
        path=str(path),
    )


def load_corpus(tenant_id: str, vault_root: Path, k1: float = 1.25, b: float = 0.75) -> Corpus:
    if not TENANT_RE.match(tenant_id):
        raise ValueError("invalid tenant_id")
    tdir = vault_root / tenant_id
    notes: dict[str, Note] = {}
    chunks: list[Chunk] = []
    if tdir.is_dir():
        for p in sorted(tdir.rglob("*.md")):
            n = parse_note(p)
            notes[n.note_id] = n
            for text in chunk_text(clean_markdown(n.body)):
                h = sha256_hex(text)
                chunks.append(Chunk(
                    chunk_id=f"{n.note_id}#{h[:8]}",
                    source_id=n.note_id,
                    text=text,
                    chunk_hash=h,
                    tenant_id=tenant_id,
                ))
    return Corpus(tenant_id, notes, chunks, TenantIndex(chunks, k1=k1, b=b))


class CorpusRegistry:
    """One corpus per tenant. Lookups are keyed by tenant_id only (NG-5)."""

    def __init__(self, vault_root: Path):
        self.vault_root = vault_root
        self._cache: dict[str, Corpus] = {}
        self.scorer = LexicalCrossScorer()

    def get(self, tenant_id: str) -> Corpus:
        if tenant_id not in self._cache:
            self._cache[tenant_id] = load_corpus(tenant_id, self.vault_root)
        return self._cache[tenant_id]

    def tenants(self) -> list[str]:
        return sorted(p.name for p in self.vault_root.iterdir() if p.is_dir() and TENANT_RE.match(p.name))


def hybrid_retrieve(corpus: Corpus, req: HybridRetrieveIn, scorer: LexicalCrossScorer | None = None) -> HybridRetrieveOut:
    """3-stage pipeline with per-stage timing (design.md 1.4)."""
    if req.tenant_id != corpus.tenant_id:
        raise ValueError("tenant mismatch")  # the MCP layer maps this to TENANT_MISMATCH
    scorer = scorer or LexicalCrossScorer()
    timings: dict[str, float] = {}

    t = time.perf_counter()
    sparse = corpus.index.bm25(req.query, top_k=req.top_k, k1=req.k1, b=req.b)
    timings["bm25"] = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    dense = corpus.index.dense(req.query, top_k=req.top_k)
    timings["dense"] = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    pool = rrf_fuse([[i for i, _ in sparse], [i for i, _ in dense]], corpus.chunks, k=req.rrf_k, pool=40)
    timings["rrf"] = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    scored = sorted(((i, scorer.score(req.query, corpus.chunks[i].text)) for i in pool),
                    key=lambda x: -x[1])
    top_score = scored[0][1] if scored else 0.0
    picked = [(i, sc) for i, sc in scored[:4] if sc >= req.threshold]
    timings["rerank"] = (time.perf_counter() - t) * 1000

    abstain = top_score < req.threshold or not picked
    chunks_out = []
    if not abstain:
        for idx, score in picked:
            c = corpus.chunks[idx]
            chunks_out.append(ChunkModel(
                chunk_id=c.chunk_id,
                chunk_hash=c.chunk_hash,
                text=c.text,
                source_id=c.source_id,
                rerank_score=round(score, 6),
                source_uri=f"rag://chunks/{c.chunk_id}",
            ))
    return HybridRetrieveOut(
        chunks=chunks_out,
        top_score=round(top_score, 6),
        abstain=abstain,
        stage_ms={k: round(v, 3) for k, v in timings.items()},
        engines=dict(DEGRADED_ENGINES),
    )
