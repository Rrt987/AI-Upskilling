"""Persistence for mcp-state-store (design.md 3, 1.4 FMEA-05/11, D-08).

Two backends with one interface:
  - PostgresStore: production path (audit chain, pending approvals, session facts, idempotency).
  - MemoryStore:   unit tests only. Not used by any deployed process (D-08: no SQLite).

Audit hash chain (design.md 3): hash = SHA256(prev_hash || canonical_json(payload_redacted)).
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from app.contracts import ErrorCode, ToolError, sha256_hex
from app.plugins import redact_payload

GENESIS = "0" * 64


def canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def chain_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    return sha256_hex(prev_hash + canonical(payload))


def args_hash(args: dict[str, Any]) -> str:
    return sha256_hex(canonical(args))


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS audit_events (
  id BIGSERIAL PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  seq BIGINT NOT NULL,
  event_type TEXT NOT NULL,
  payload JSONB NOT NULL,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, seq)
);
CREATE TABLE IF NOT EXISTS pending_approvals (
  id UUID PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  workflow_id TEXT NOT NULL,
  transition TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  idempotency_key UUID NOT NULL,
  args_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  decided_by TEXT,
  decided_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS session_facts (
  id BIGSERIAL PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  fact_key TEXT NOT NULL,
  fact_value TEXT NOT NULL,
  source_chunk_hash TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS session_facts_tenant_session ON session_facts (tenant_id, session_id);
"""


class MemoryStore:
    """Test backend. Same semantics as PostgresStore."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._audit: dict[str, list[dict[str, Any]]] = {}
        self._pending: dict[tuple[str, str], dict[str, Any]] = {}
        self._facts: list[dict[str, Any]] = []

    def append_audit(self, tenant_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        safe = redact_payload(payload)
        with self._lock:
            chain = self._audit.setdefault(tenant_id, [])
            prev = chain[-1]["hash"] if chain else GENESIS
            ev = {"seq": len(chain) + 1, "event_type": event_type, "payload": safe,
                  "prev_hash": prev, "hash": chain_hash(prev, safe)}
            chain.append(ev)
            return dict(ev)

    def audit_chain(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(e) for e in self._audit.get(tenant_id, [])]

    def verify_chain(self, tenant_id: str) -> dict[str, Any]:
        return verify_events(self.audit_chain(tenant_id))

    def submit_pending(self, tenant_id: str, idem: str, a_hash: str, row: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            key = (tenant_id, idem)
            if key in self._pending:
                existing = self._pending[key]
                if existing["args_hash"] != a_hash:
                    raise ToolError(ErrorCode.IDEMPOTENCY_CONFLICT, "same idempotency_key, different arguments")
                return dict(existing)
            rec = {**row, "id": str(uuid.uuid4()), "status": "PENDING_APPROVAL", "args_hash": a_hash,
                   "decided_by": None, "decided_at": None}
            self._pending[key] = rec
            return dict(rec)

    def list_pending(self, tenant_id: str, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = [dict(r) for (t, _), r in self._pending.items() if t == tenant_id]
        return [r for r in rows if status is None or r["status"] == status]

    def decide(self, tenant_id: str, pending_id: str, decision: str, reviewer: str) -> dict[str, Any]:
        if decision not in ("APPROVED", "REJECTED"):
            raise ToolError(ErrorCode.VALIDATION_FAILED, "decision must be APPROVED or REJECTED")
        with self._lock:
            for (t, _), r in self._pending.items():
                if t == tenant_id and r["id"] == pending_id:
                    if r["status"] != "PENDING_APPROVAL":
                        raise ToolError(ErrorCode.VALIDATION_FAILED, "already decided")
                    if reviewer == r["user_id"]:
                        raise ToolError(ErrorCode.RBAC_DENIED, "requester cannot approve own mutation")
                    r["status"] = decision
                    r["decided_by"] = reviewer
                    r["decided_at"] = datetime.now(timezone.utc).isoformat()
                    return dict(r)
        raise ToolError(ErrorCode.VALIDATION_FAILED, "unknown pending id for tenant")

    def put_fact(self, tenant_id: str, session_id: str, key: str, value: str, source_chunk_hash: str | None) -> None:
        with self._lock:
            self._facts.append({"tenant_id": tenant_id, "session_id": session_id, "fact_key": key,
                                "fact_value": value, "source_chunk_hash": source_chunk_hash})

    def get_facts(self, tenant_id: str, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(f) for f in self._facts if f["tenant_id"] == tenant_id and f["session_id"] == session_id]


def verify_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Walk a tenant chain. Returns ok flag and the first broken seq (design.md 3)."""
    prev = GENESIS
    for ev in events:
        if ev["prev_hash"] != prev or chain_hash(prev, ev["payload"]) != ev["hash"]:
            return {"ok": False, "broken_at_seq": ev["seq"], "length": len(events)}
        prev = ev["hash"]
    return {"ok": True, "broken_at_seq": None, "length": len(events)}


class PostgresStore:
    """Production backend. Requires psycopg and a reachable Postgres (DATABASE_URL)."""

    def __init__(self, dsn: str, pool_min: int = 2, pool_max: int = 10):
        import psycopg  # local import: unit tests with MemoryStore do not need a DB driver
        from psycopg_pool import ConnectionPool
        self._psycopg = psycopg
        self.dsn = dsn
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(SCHEMA_SQL)
        # Pooled connections (v0.1 finding FN-003: connect-per-operation capped the store at ~88 writes/s).
        self._pool = ConnectionPool(dsn, min_size=pool_min, max_size=pool_max, open=True)

    def _conn(self):
        return self._pool.connection()

    def close(self) -> None:
        self._pool.close()

    def append_audit(self, tenant_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        from psycopg.types.json import Jsonb
        safe = redact_payload(payload)
        with self._conn() as conn:
            with conn.transaction():
                # Serialise appends per tenant so the chain stays linear.
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (tenant_id,))
                row = conn.execute(
                    "SELECT seq, hash FROM audit_events WHERE tenant_id=%s ORDER BY seq DESC LIMIT 1",
                    (tenant_id,)).fetchone()
                seq = (row[0] if row else 0) + 1
                prev = row[1] if row else GENESIS
                h = chain_hash(prev, safe)
                conn.execute(
                    "INSERT INTO audit_events (tenant_id, seq, event_type, payload, prev_hash, hash)"
                    " VALUES (%s,%s,%s,%s,%s,%s)",
                    (tenant_id, seq, event_type, Jsonb(safe), prev, h))
                return {"seq": seq, "event_type": event_type, "payload": safe, "prev_hash": prev, "hash": h}

    def audit_chain(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT seq, event_type, payload, prev_hash, hash FROM audit_events"
                " WHERE tenant_id=%s ORDER BY seq", (tenant_id,)).fetchall()
        return [{"seq": r[0], "event_type": r[1], "payload": r[2], "prev_hash": r[3], "hash": r[4]} for r in rows]

    def verify_chain(self, tenant_id: str) -> dict[str, Any]:
        return verify_events(self.audit_chain(tenant_id))

    def submit_pending(self, tenant_id: str, idem: str, a_hash: str, row: dict[str, Any]) -> dict[str, Any]:
        new_id = str(uuid.uuid4())
        with self._conn() as conn:
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (tenant_id + ":" + idem,))
                existing = conn.execute(
                    "SELECT id, args_hash, status FROM pending_approvals WHERE tenant_id=%s AND idempotency_key=%s",
                    (tenant_id, idem)).fetchone()
                if existing:
                    if existing[1] != a_hash:
                        raise ToolError(ErrorCode.IDEMPOTENCY_CONFLICT, "same idempotency_key, different arguments")
                    return {"id": str(existing[0]), "status": existing[2], "args_hash": existing[1],
                            "idempotent_replay": True, **row}
                conn.execute(
                    "INSERT INTO pending_approvals (id, tenant_id, user_id, workflow_id, transition, subject_id,"
                    " idempotency_key, args_hash, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'PENDING_APPROVAL')",
                    (new_id, tenant_id, row["user_id"], row["workflow_id"], row["transition"], row["subject_id"],
                     idem, a_hash))
        return {"id": new_id, "status": "PENDING_APPROVAL", "args_hash": a_hash, "idempotent_replay": False, **row}

    def list_pending(self, tenant_id: str, status: str | None = None) -> list[dict[str, Any]]:
        q = ("SELECT id, user_id, workflow_id, transition, subject_id, status, created_at FROM pending_approvals"
             " WHERE tenant_id=%s")
        params: list[Any] = [tenant_id]
        if status:
            q += " AND status=%s"
            params.append(status)
        q += " ORDER BY created_at"
        with self._conn() as conn:
            rows = conn.execute(q, params).fetchall()
        return [{"id": str(r[0]), "user_id": r[1], "workflow_id": r[2], "transition": r[3],
                 "subject_id": r[4], "status": r[5], "created_at": r[6].isoformat()} for r in rows]

    def decide(self, tenant_id: str, pending_id: str, decision: str, reviewer: str) -> dict[str, Any]:
        if decision not in ("APPROVED", "REJECTED"):
            raise ToolError(ErrorCode.VALIDATION_FAILED, "decision must be APPROVED or REJECTED")
        with self._conn() as conn:
            with conn.transaction():
                row = conn.execute(
                    "SELECT user_id, status FROM pending_approvals WHERE tenant_id=%s AND id=%s FOR UPDATE",
                    (tenant_id, pending_id)).fetchone()
                if row is None:
                    raise ToolError(ErrorCode.VALIDATION_FAILED, "unknown pending id for tenant")
                if row[1] != "PENDING_APPROVAL":
                    raise ToolError(ErrorCode.VALIDATION_FAILED, "already decided")
                if reviewer == row[0]:
                    raise ToolError(ErrorCode.RBAC_DENIED, "requester cannot approve own mutation")
                conn.execute(
                    "UPDATE pending_approvals SET status=%s, decided_by=%s, decided_at=now()"
                    " WHERE tenant_id=%s AND id=%s", (decision, reviewer, tenant_id, pending_id))
        return {"id": pending_id, "status": decision, "decided_by": reviewer}

    def put_fact(self, tenant_id: str, session_id: str, key: str, value: str, source_chunk_hash: str | None) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO session_facts (tenant_id, session_id, fact_key, fact_value, source_chunk_hash)"
                " VALUES (%s,%s,%s,%s,%s)", (tenant_id, session_id, key, value, source_chunk_hash))

    def get_facts(self, tenant_id: str, session_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT fact_key, fact_value, source_chunk_hash FROM session_facts"
                " WHERE tenant_id=%s AND session_id=%s ORDER BY id", (tenant_id, session_id)).fetchall()
        return [{"fact_key": r[0], "fact_value": r[1], "source_chunk_hash": r[2]} for r in rows]
