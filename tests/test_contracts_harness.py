import uuid

import pytest
from pydantic import ValidationError

from app.contracts import (ErrorCode, HybridRetrieveIn, StateSubmitIn, Tier, ToolError, acl_hash, cache_key)
from app.harness import check_tier, normalize_entities, repair_value, validate
from app.plugins import METRICS, CircuitBreaker

H = "a" * 64


def test_cache_key_is_tenant_first_and_partitioned_by_acl():
    k1 = cache_key("tenant_a", H, "q")
    k2 = cache_key("tenant_b", H, "q")
    assert k1.startswith("cache:v1:tenant_a:")
    assert k1 != k2


def test_cache_key_rejects_bad_tenant():
    with pytest.raises(ValueError):
        cache_key("Tenant A; DROP", H, "q")


def test_acl_hash_is_order_insensitive_and_sensitive_to_permissions():
    assert acl_hash(["TIER_1_READ", "TIER_2_COMPUTE"]) == acl_hash(["TIER_2_COMPUTE", "TIER_1_READ"])
    assert acl_hash(["TIER_1_READ"]) != acl_hash(["TIER_1_READ", "TIER_3_MUTATE"])


def test_repair_rules_match_design_examples():
    assert repair_value("SKU99") == "SKU-099"
    assert repair_value("sku-7") == "SKU-007"
    assert repair_value("lot 44") == "LOT-00044"
    assert repair_value("LOT-88392") == "LOT-88392"
    assert repair_value("banana") is None


def test_inline_entity_normalisation_counts_rewrites():
    text, n = normalize_entities("check SKU99 and lot 44 today")
    assert text == "check SKU-099 and LOT-00044 today"
    assert n == 2


def test_strict_validation_rejects_extra_fields_and_repairs_subject():
    with pytest.raises(ValidationError):
        HybridRetrieveIn.model_validate({"tenant_id": "tenant_a", "acl_hash": H, "query": "x", "rogue": 1})
    args = {"tenant_id": "tenant_a", "user_id": "u", "workflow_id": "QA-RELEASE-1", "transition": "RELEASE",
            "subject_id": "lot 44", "idempotency_key": str(uuid.uuid4())}
    before = METRICS.snapshot().get("kpi15_repair_success", 0)
    obj = validate(StateSubmitIn, args)
    assert obj.subject_id == "LOT-00044"
    assert METRICS.snapshot().get("kpi15_repair_success", 0) == before + 1


def test_idempotency_key_must_be_uuid4():
    args = {"tenant_id": "tenant_a", "user_id": "u", "workflow_id": "QA-RELEASE-1", "transition": "RELEASE",
            "subject_id": "LOT-00044", "idempotency_key": str(uuid.uuid1())}
    with pytest.raises(ToolError) as e:
        validate(StateSubmitIn, args)
    assert e.value.code is ErrorCode.VALIDATION_FAILED


def test_rbac_tiers():
    check_tier(Tier.TIER_1_READ, {Tier.TIER_1_READ})
    with pytest.raises(ToolError) as e:
        check_tier(Tier.TIER_3_MUTATE, {Tier.TIER_1_READ})
    assert e.value.code is ErrorCode.RBAC_DENIED


def test_circuit_breaker_trips_on_second_identical_failure():
    b = CircuitBreaker(max_identical_failures=2)
    args = {"query": "x"}
    assert b.record_failure("hybrid_retrieve", args) is False
    assert b.record_failure("hybrid_retrieve", args) is True
    with pytest.raises(ToolError) as e:
        b.check("hybrid_retrieve", args)
    assert e.value.code is ErrorCode.BREAKER_OPEN
    b.check("hybrid_retrieve", {"query": "different"})  # other keys unaffected
    b.reset()
    b.check("hybrid_retrieve", args)


def test_circuit_breaker_open_state_expires_and_memory_is_capped():
    now = [0.0]
    b = CircuitBreaker(max_identical_failures=2, max_keys=50, open_ttl_s=120.0, clock=lambda: now[0])
    args = {"query": "x"}
    b.record_failure("hybrid_retrieve", args)
    b.record_failure("hybrid_retrieve", args)
    with pytest.raises(ToolError):
        b.check("hybrid_retrieve", args)
    now[0] = 121.0  # half-open after the TTL
    b.check("hybrid_retrieve", args)
    for i in range(500):  # unique failing keys must not grow memory past the cap (FN-007)
        b.record_failure("hybrid_retrieve", {"query": f"q{i}"})
        b.record_failure("hybrid_retrieve", {"query": f"q{i}"})
    assert b.size() <= 2 * 50
