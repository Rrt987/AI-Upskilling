"""Tenant-partitioned response cache (ADR-3, FMEA-01).

Keys come from contracts.cache_key: cache:v1:{tenant_id}:{acl_hash}:{sha256(norm_query)}.
TTL is 300 s +/- 45 s jitter. Redis errors are treated as a miss (bypass), never as a failure
and never as a cross-tenant fallback. v0 implements exact-key caching only. The semantic
(cosine >= 0.92) layer is deferred (failure_notes v0.1 known gaps).
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Any, Protocol

TTL_BASE_S = 300
TTL_JITTER_S = 45


def ttl_with_jitter(rng: random.Random | None = None) -> int:
    r = rng or random
    return int(TTL_BASE_S + r.uniform(-TTL_JITTER_S, TTL_JITTER_S))


class CacheBackend(Protocol):
    name: str

    async def get(self, key: str) -> dict[str, Any] | None: ...
    async def set(self, key: str, value: dict[str, Any], ttl_s: int) -> None: ...
    async def ping(self) -> bool: ...


class MemoryCache:
    name = "memory"

    def __init__(self) -> None:
        self._d: dict[str, tuple[float, str]] = {}

    async def get(self, key: str) -> dict[str, Any] | None:
        item = self._d.get(key)
        if item is None:
            return None
        if item[0] < time.monotonic():
            self._d.pop(key, None)
            return None
        return json.loads(item[1])

    async def set(self, key: str, value: dict[str, Any], ttl_s: int) -> None:
        self._d[key] = (time.monotonic() + ttl_s, json.dumps(value))

    async def ping(self) -> bool:
        return True


class RedisCache:
    name = "redis"

    def __init__(self, url: str):
        import redis.asyncio as redis_asyncio
        self._r = redis_asyncio.from_url(url, socket_timeout=0.05, socket_connect_timeout=0.2)

    async def get(self, key: str) -> dict[str, Any] | None:
        raw = await self._r.get(key)
        return json.loads(raw) if raw else None

    async def set(self, key: str, value: dict[str, Any], ttl_s: int) -> None:
        await self._r.set(key, json.dumps(value), ex=ttl_s)

    async def ping(self) -> bool:
        return bool(await self._r.ping())


class SafeCache:
    """Wraps a backend. Any backend error becomes a miss or a skipped write (FMEA-01)."""

    def __init__(self, backend: CacheBackend, timeout_s: float = 0.05):
        self.backend = backend
        self.timeout_s = timeout_s
        self.bypass_count = 0

    @property
    def name(self) -> str:
        return self.backend.name

    async def get(self, key: str) -> dict[str, Any] | None:
        try:
            return await asyncio.wait_for(self.backend.get(key), self.timeout_s)
        except Exception:
            self.bypass_count += 1
            return None

    async def set(self, key: str, value: dict[str, Any], ttl_s: int) -> None:
        try:
            await asyncio.wait_for(self.backend.set(key, value, ttl_s), self.timeout_s)
        except Exception:
            self.bypass_count += 1


class SemanticIndex:
    """Process-local semantic layer (design.md B-03). Partitioned by (tenant_id, acl_hash): a match is only
    ever searched inside the caller's own partition, so it cannot cross tenants or permission sets.
    Vectors are the hashing embedding. The cache itself stores the response under the exact key."""

    def __init__(self, threshold: float = 0.92, max_per_partition: int = 1000):
        self.threshold = threshold
        self.max_per_partition = max_per_partition
        self._parts: dict[tuple[str, str], list[tuple[object, str]]] = {}

    def add(self, tenant_id: str, acl: str, key: str, vec) -> None:
        rows = self._parts.setdefault((tenant_id, acl), [])
        rows.append((vec, key))
        if len(rows) > self.max_per_partition:
            del rows[0]

    def best(self, tenant_id: str, acl: str, vec) -> tuple[float, str] | None:
        import numpy as np
        rows = self._parts.get((tenant_id, acl))
        if not rows:
            return None
        mat = np.stack([r[0] for r in rows])
        sims = mat @ vec
        i = int(np.argmax(sims))
        score = float(sims[i])
        return (score, rows[i][1]) if score >= self.threshold else None
