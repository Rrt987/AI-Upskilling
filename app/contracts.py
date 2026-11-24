"""Shared contracts: error codes, tool models, tenant/ACL helpers (design.md 1.2, ADR-3)."""
from __future__ import annotations

import hashlib
import re
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

TENANT_RE = re.compile(r"^tenant_[a-z0-9_-]{1,64}$")
SKU_RE = re.compile(r"^SKU-\d{3,6}$")
LOT_RE = re.compile(r"^LOT-\d{5}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")

Tenant = Annotated[str, Field(pattern=r"^tenant_[a-z0-9_-]{1,64}$")]


class ErrorCode(str, Enum):
    """design.md 1.2.6. JSON-RPC codes are attached in ERROR_JSONRPC."""

    VALIDATION_FAILED = "VALIDATION_FAILED"
    TENANT_MISMATCH = "TENANT_MISMATCH"
    RBAC_DENIED = "RBAC_DENIED"
    TIMEOUT = "TIMEOUT"
    RERANKER_UNAVAILABLE = "RERANKER_UNAVAILABLE"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    HITL_REQUIRED = "HITL_REQUIRED"
    BREAKER_OPEN = "BREAKER_OPEN"
    PII_CHECK_UNAVAILABLE = "PII_CHECK_UNAVAILABLE"
    INJECTION_BLOCKED = "INJECTION_BLOCKED"


ERROR_JSONRPC: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_FAILED: -32602,
    ErrorCode.TENANT_MISMATCH: -32001,
    ErrorCode.RBAC_DENIED: -32002,
    ErrorCode.TIMEOUT: -32003,
    ErrorCode.RERANKER_UNAVAILABLE: -32004,
    ErrorCode.IDEMPOTENCY_CONFLICT: -32005,
    ErrorCode.HITL_REQUIRED: -32006,
    ErrorCode.BREAKER_OPEN: -32007,
    ErrorCode.PII_CHECK_UNAVAILABLE: -32008,
    ErrorCode.INJECTION_BLOCKED: -32009,
}


class Tier(str, Enum):
    TIER_1_READ = "TIER_1_READ"
    TIER_2_COMPUTE = "TIER_2_COMPUTE"
    TIER_3_MUTATE = "TIER_3_MUTATE"


class Route(str, Enum):
    DIRECT_ANSWER = "DIRECT_ANSWER"
    HYBRID_RAG = "HYBRID_RAG"
    TRANSACTION_EXEC = "TRANSACTION_EXEC"
    REJECT_OOS = "REJECT_OOS"


class ToolError(Exception):
    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class HybridRetrieveIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    tenant_id: Tenant
    acl_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    query: Annotated[str, Field(min_length=1, max_length=2000)]
    k1: float = Field(1.25, ge=0.5, le=2.0)
    b: float = Field(0.75, ge=0.0, le=1.0)
    rrf_k: int = Field(60, ge=1, le=200)
    threshold: float = Field(0.42, ge=0.0, le=1.0)
    top_k: int = Field(50, ge=1, le=50)


class Chunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    chunk_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    text: str
    source_id: str
    rerank_score: float
    source_uri: str


class HybridRetrieveOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunks: list[Chunk] = Field(max_length=4)
    top_score: float
    abstain: bool
    stage_ms: dict[str, float]
    engines: dict[str, str]


class GraphSubgraphIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    tenant_id: Tenant
    anchor_id: Annotated[str, Field(pattern=r"^[A-Z]{2,4}-[A-Z0-9]+-\d{3}$")]
    depth: int = Field(2, ge=0, le=2)


class GraphSubgraphOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[str]
    edges: list[tuple[str, str]]
    tokens: int


class StateSubmitIn(BaseModel):
    """TIER_3_MUTATE. Always creates PENDING_APPROVAL, never executes (design 1.2.5)."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: Tenant
    user_id: Annotated[str, Field(min_length=1, max_length=64)]
    workflow_id: Annotated[str, Field(pattern=r"^[A-Z]{2,4}-[A-Z0-9-]+$")]
    transition: Annotated[str, Field(min_length=1, max_length=64)]
    subject_id: Annotated[str, Field(pattern=r"^(SKU-\d{3,6}|LOT-\d{5})$")]
    idempotency_key: UUID

    @field_validator("idempotency_key")
    @classmethod
    def _uuid4_only(cls, v: UUID) -> UUID:
        if v.version != 4:
            raise ValueError("idempotency_key must be UUIDv4")
        return v


class TriageLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: str
    domain: str
    risk_tier: Literal["LOW", "MEDIUM", "HIGH"]
    complexity: Literal["simple", "multi_step"]
    route: Route
    confidence: float


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def acl_hash(permissions: list[str]) -> str:
    """ADR-3: hash of the sorted permission set. Users with identical ACLs share cache partitions."""
    return sha256_hex("|".join(sorted(set(permissions))))


def cache_key(tenant_id: str, acl: str, norm_query: str) -> str:
    """ADR-3: cache:v1:{tenant_id}:{acl_hash}:{sha256(norm_query)}. Tenant is always first."""
    if not TENANT_RE.match(tenant_id):
        raise ValueError("invalid tenant_id")
    return f"cache:v1:{tenant_id}:{acl}:{sha256_hex(norm_query)}"
