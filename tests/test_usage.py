"""B-04: token meter. Counts are word proxies; cost is reported only when both prices are set."""
import asyncio

from app.agent import QueryIn
from tests.fakes import make_agent


def _b(q):
    return QueryIn(tenant_id="tenant_demo", user_id="u", session_id="s", query=q, permissions=["TIER_1_READ"])


def test_answered_usage_counts_observation_and_answer(monkeypatch):
    monkeypatch.delenv("PRICE_IN_PER_1K", raising=False)
    monkeypatch.delenv("PRICE_OUT_PER_1K", raising=False)
    d = asyncio.run(make_agent().handle(_b("What is the batch verification procedure for a lot?")))
    assert d["status"] == "ANSWERED"
    assert d["usage"]["output_tokens"] == len(d["answer"].split())
    assert 0 < d["usage"]["input_tokens"] <= 180 + 20
    assert d["usage"]["cost_usd"] is None


def test_cost_is_computed_when_prices_are_configured(monkeypatch):
    monkeypatch.setenv("PRICE_IN_PER_1K", "0.5")
    monkeypatch.setenv("PRICE_OUT_PER_1K", "1.5")
    d = asyncio.run(make_agent().handle(_b("What is the batch verification procedure for a lot?")))
    u = d["usage"]
    expected = round(u["input_tokens"] / 1000 * 0.5 + u["output_tokens"] / 1000 * 1.5, 6)
    assert u["cost_usd"] == expected


def test_non_answer_has_zero_usage(monkeypatch):
    monkeypatch.delenv("PRICE_IN_PER_1K", raising=False)
    d = asyncio.run(make_agent().handle(_b("zebra parking rules on mars")))
    assert d["status"] == "ABSTAINED"
    assert d["usage"] == {"input_tokens": 0, "output_tokens": 0, "cost_usd": None}
