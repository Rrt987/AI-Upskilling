"""B-03: semantic layer hits inside one (tenant, ACL) partition only."""
import asyncio

from tests.fakes import make_agent
from app.agent import QueryIn

Q = "What is the batch verification procedure for a lot?"


def _b(q, tenant="tenant_demo", perms=("TIER_1_READ",)):
    return QueryIn(tenant_id=tenant, user_id="u", session_id="s", query=q, permissions=list(perms))


def test_near_duplicate_query_hits_semantic_layer():
    agent = make_agent()
    first = asyncio.run(agent.handle(_b(Q)))
    assert first["status"] == "ANSWERED" and first["cache"] == "MISS"
    second = asyncio.run(agent.handle(_b("what is the batch verification procedure for a lot")))
    assert second["cache"] == "SEMANTIC_HIT"
    assert second["status"] == "ANSWERED"


def test_semantic_hit_never_crosses_tenant():
    agent = make_agent()
    asyncio.run(agent.handle(_b(Q)))
    other = asyncio.run(agent.handle(_b(Q, tenant="tenant_other")))
    assert other["cache"] == "MISS" and other["tenant_id"] == "tenant_other"


def test_semantic_hit_never_crosses_acl_partition():
    agent = make_agent()
    asyncio.run(agent.handle(_b(Q, perms=("TIER_1_READ",))))
    other = asyncio.run(agent.handle(_b(Q, perms=("TIER_1_READ", "TIER_2_COMPUTE"))))
    assert other["cache"] == "MISS"


def test_dissimilar_query_does_not_hit():
    agent = make_agent()
    asyncio.run(agent.handle(_b(Q)))
    other = asyncio.run(agent.handle(_b("zebra parking rules on mars")))
    assert other["cache"] == "MISS"
