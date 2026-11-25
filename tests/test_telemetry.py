"""B-02: spans carry the CLAUDE.md names and the caller's W3C trace id."""
import asyncio

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import telemetry
from tests.fakes import make_agent
from tests.test_chaos import body

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
TRACEPARENT = f"00-{TRACE_ID}-00f067aa0ba902b7-01"


@pytest.fixture(scope="module")
def exporter():
    exp = InMemorySpanExporter()
    telemetry.configure(exp)
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    provider.add_span_processor(SimpleSpanProcessor(exp))
    return exp


def test_rag_request_emits_named_spans_under_caller_trace(exporter):
    exporter.clear()
    d = asyncio.run(make_agent().handle(body("What is the batch verification procedure for a lot?"),
                                        traceparent=TRACEPARENT))
    trace.get_tracer_provider().force_flush()
    spans = exporter.get_finished_spans()
    names = {s.name for s in spans}
    for expected in ("hook.pre", "cache.lookup", "triage.classify", "mcp.rag.bm25", "mcp.rag.biencoder",
                     "mcp.rag.rrf", "mcp.rag.rerank", "agent.react.step", "llm.generate"):
        assert expected in names, expected
    assert "hook.post.ragas" not in names  # RAGAS is not built (B-06)
    assert {format(s.context.trace_id, "032x") for s in spans} == {TRACE_ID}
    assert d["trace_id"] == TRACE_ID


def test_unknown_span_name_is_rejected():
    with pytest.raises(ValueError):
        telemetry.emit("made.up.span", 1.0, telemetry.request_context(None))


def test_request_without_traceparent_gets_a_fresh_trace_id():
    a = telemetry.trace_id_hex(telemetry.request_context(None))
    b = telemetry.trace_id_hex(telemetry.request_context(None))
    assert a != b and len(a) == 32
