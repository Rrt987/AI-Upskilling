"""OpenTelemetry spans for the request path (design.md B-02, CLAUDE.md span names).

Spans are emitted with the durations the agent already measures, so a span's length equals the stage time.
Export: OTLP gRPC when OTEL_EXPORTER_OTLP_ENDPOINT is set; otherwise spans are created but not exported.
W3C traceparent from the request header is the parent. Without it, a fresh trace id is used per request.
"""
from __future__ import annotations

import os
import secrets
import time

from opentelemetry import trace
from opentelemetry.propagate import extract
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

TRACER_NAME = "unreliable-assistant"

# Span names. Must match CLAUDE.md. hook.post.ragas is deliberately absent (no RAGAS in v0.2).
SPAN_NAMES = {
    "hook.pre", "cache.lookup", "triage.classify", "mcp.rag.bm25", "mcp.rag.biencoder",
    "mcp.rag.rrf", "mcp.rag.rerank", "agent.react.step", "llm.generate",
}
# Map from the retrieval server's stage keys to span names.
RAG_STAGE_SPANS = {"bm25": "mcp.rag.bm25", "dense": "mcp.rag.biencoder", "rrf": "mcp.rag.rrf", "rerank": "mcp.rag.rerank"}


def configure(exporter: SpanExporter | None = None) -> TracerProvider:
    """Install a tracer provider once. Adds an exporter to the existing provider if one is already set."""
    if exporter is None and os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        exporter = OTLPSpanExporter(endpoint=os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"])
    current = trace.get_tracer_provider()
    if isinstance(current, TracerProvider):
        provider = current
    else:
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    if exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


def request_context(traceparent: str | None):
    """Context for one request. Uses the caller's traceparent when valid, otherwise a new trace id."""
    if traceparent:
        ctx = extract({"traceparent": traceparent})
        if trace.get_current_span(ctx).get_span_context().is_valid:
            return ctx
    span_ctx = trace.SpanContext(
        trace_id=secrets.randbits(128) or 1, span_id=secrets.randbits(64) or 1,
        is_remote=True, trace_flags=trace.TraceFlags(trace.TraceFlags.SAMPLED))
    return trace.set_span_in_context(trace.NonRecordingSpan(span_ctx))


def emit(name: str, duration_ms: float, ctx, attributes: dict | None = None) -> None:
    """Record a finished span whose length is duration_ms, ending now."""
    if name not in SPAN_NAMES:
        raise ValueError(f"unknown span name: {name}")
    end = time.time_ns()
    start = end - int(max(duration_ms, 0.0) * 1_000_000)
    span = trace.get_tracer(TRACER_NAME).start_span(name, context=ctx, start_time=start,
                                                    attributes=attributes or {})
    span.end(end_time=end)


def trace_id_hex(ctx) -> str:
    return format(trace.get_current_span(ctx).get_span_context().trace_id, "032x")
