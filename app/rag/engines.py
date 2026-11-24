"""3-stage hybrid retrieval engines (design.md Gate 3).

Stage 1  BM25Okapi + Porter stemming           k1=1.25 b=0.75 top_k=50
Stage 2  Dense + FAISS HNSW                     efSearch=64 top_k=50
         Engine: HashingDense (v0 stand-in, see DEGRADED_ENGINES). Interface is the
         contract for bge-large-en-v1.5 ONNX.
Fusion   Reciprocal Rank Fusion k=60, dedup by chunk_hash, pool=40
Stage 3  Cross-encoder rerank -> top 4, sigmoid > threshold
         Engine: LexicalCrossScorer (v0 stand-in). Interface is the contract for
         bge-reranker-v2-m3 ONNX FP16.
"""
from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass

import faiss
import numpy as np
import Stemmer
from rank_bm25 import BM25Okapi

from app.contracts import sha256_hex

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-]*")
_LOCAL = threading.local()


def _stemmer() -> Stemmer.Stemmer:
    """PyStemmer instances are not thread-safe; one per thread (the docstring forbids concurrent calls)."""
    st = getattr(_LOCAL, "stemmer", None)
    if st is None:
        st = _LOCAL.stemmer = Stemmer.Stemmer("english")
    return st

# Engines that are stand-ins for the production models. Reported in /health and Tab 3.
DEGRADED_ENGINES = {
    "dense": "hashing-v0 (stand-in for bge-large-en-v1.5 ONNX)",
    "rerank": "lexical-v0 (stand-in for bge-reranker-v2-m3 ONNX FP16)",
}


def tokenize(text: str) -> list[str]:
    stem = _stemmer().stemWord
    return [stem(t) for t in _TOKEN_RE.findall(text.lower())]


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    source_id: str
    text: str
    chunk_hash: str
    tenant_id: str


def chunk_text(text: str, max_tokens: int = 512, overlap: int = 64) -> list[str]:
    """Line-aware chunking (chunk=512 tokens, overlap=64 for over-long lines, Stage 2 hyperparameters).

    Lines are packed into chunks without splitting a line. A single line longer than max_tokens is
    split into word windows with `overlap` words shared between windows.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    chunks: list[str] = []
    current: list[str] = []
    count = 0
    for line in lines:
        words = line.split()
        if len(words) > max_tokens:
            if current:
                chunks.append("\n".join(current))
                current, count = [], 0
            step = max_tokens - overlap
            for start in range(0, len(words), step):
                chunks.append(" ".join(words[start:start + max_tokens]))
                if start + max_tokens >= len(words):
                    break
            continue
        if count + len(words) > max_tokens and current:
            chunks.append("\n".join(current))
            current, count = [], 0
        current.append(line)
        count += len(words)
    if current:
        chunks.append("\n".join(current))
    return chunks


class HashingDense:
    """Deterministic hashed unigram+bigram embedding, L2-normalised. Cosine via inner product."""

    def __init__(self, dim: int = 512):
        self.dim = dim

    def embed(self, text: str) -> np.ndarray:
        toks = tokenize(text)
        grams = toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:])]
        vec = np.zeros(self.dim, dtype=np.float32)
        for g in grams:
            h = int(sha256_hex(g)[:12], 16)
            sign = 1.0 if (h >> 1) & 1 else -1.0
            vec[h % self.dim] += sign
        norm = float(np.linalg.norm(vec))
        return vec / norm if norm > 0 else vec


class TenantIndex:
    """Per-tenant index. Every query is scoped to one tenant; there is no cross-tenant index (ADR-3)."""

    def __init__(self, chunks: list[Chunk], k1: float = 1.25, b: float = 0.75, dense_dim: int = 512):
        self.chunks = chunks
        self.k1 = k1
        self.b = b
        self._bm25 = BM25Okapi([tokenize(c.text) for c in chunks], k1=k1, b=b) if chunks else None
        self._dense = HashingDense(dense_dim)
        if chunks:
            mat = np.stack([self._dense.embed(c.text) for c in chunks]).astype(np.float32)
            self._hnsw = faiss.IndexHNSWFlat(dense_dim, 32, faiss.METRIC_INNER_PRODUCT)
            self._hnsw.hnsw.efConstruction = 200
            self._hnsw.add(mat)
            self._hnsw.hnsw.efSearch = 64
        else:
            self._hnsw = None

    def bm25(self, query: str, top_k: int = 50, k1: float | None = None, b: float | None = None) -> list[tuple[int, float]]:
        if self._bm25 is None:
            return []
        bm = self._bm25
        if (k1 is not None and k1 != self.k1) or (b is not None and b != self.b):
            bm = BM25Okapi([tokenize(c.text) for c in self.chunks],
                           k1=self.k1 if k1 is None else k1, b=self.b if b is None else b)
        scores = bm.get_scores(tokenize(query))
        order = np.argsort(-scores)[:top_k]
        return [(int(i), float(scores[i])) for i in order if scores[i] > 0]

    def dense(self, query: str, top_k: int = 50) -> list[tuple[int, float]]:
        if self._hnsw is None:
            return []
        q = self._dense.embed(query).reshape(1, -1)
        sims, ids = self._hnsw.search(q, min(top_k, len(self.chunks)))
        return [(int(i), float(s)) for i, s in zip(ids[0], sims[0]) if i >= 0]


def rrf_fuse(ranked_lists: list[list[int]], chunks: list[Chunk], k: int = 60, pool: int = 40) -> list[int]:
    """Reciprocal Rank Fusion. score(d) = sum 1/(k + rank). Dedup by chunk_hash keeps first occurrence."""
    scores: dict[int, float] = {}
    for lst in ranked_lists:
        for rank, idx in enumerate(lst, start=1):
            scores[idx] = scores.get(idx, 0.0) + 1.0 / (k + rank)
    seen_hash: set[str] = set()
    out: list[int] = []
    for idx, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0])):
        h = chunks[idx].chunk_hash
        if h in seen_hash:
            continue
        seen_hash.add(h)
        out.append(idx)
        if len(out) == pool:
            break
    return out


class LexicalCrossScorer:
    """Stand-in cross-encoder. Query-term coverage mapped through a logistic, so output is in (0, 1).

    Deterministic and monotone in coverage, which is enough to exercise the threshold/abstention path.
    Replace with bge-reranker-v2-m3 ONNX FP16 without changing the interface.
    """

    def score(self, query: str, text: str) -> float:
        q = set(tokenize(query))
        if not q:
            return 0.0
        t = set(tokenize(text))
        coverage = len(q & t) / len(q)
        logit = 6.0 * coverage - 3.0
        return 1.0 / (1.0 + math.exp(-logit))
