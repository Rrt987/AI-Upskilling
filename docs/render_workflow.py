"""Renders docs/workflow_orchestration.png (Gate 2) at 300 DPI.

Usage:  python3 docs/render_workflow.py
Requires: Graphviz `dot` on PATH and the `graphviz` Python package.
Regenerate whenever the orchestration topology in design.md section 1.3 changes.
"""
import os

from graphviz import Digraph

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "workflow_orchestration")

g = Digraph("workflow_orchestration", format="png", engine="dot")
g.attr(
    rankdir="TB",
    dpi="300",
    bgcolor="white",
    fontname="Helvetica",
    fontsize="18",
    label="The Unreliable Assistant -> Enterprise v0: Workflow Orchestration (Gate 2)\\nDesign v0.0 - all numbers are latency budgets from design.md",
    labelloc="t",
    nodesep="0.35",
    ranksep="0.55",
    newrank="true",
    compound="true",
)
g.attr("node", shape="box", style="rounded,filled", fillcolor="white",
       color="#4A5568", fontname="Helvetica", fontsize="10", margin="0.14,0.07")
g.attr("edge", fontname="Helvetica", fontsize="9", color="#4A5568")


def cluster(name, label, fill):
    c = Digraph(name=f"cluster_{name}")
    c.attr(label=label, style="rounded,filled", fillcolor=fill, color="#718096",
           fontname="Helvetica-Bold", fontsize="12", margin="14")
    return c


# ---- 1. Ingress & Security ---------------------------------------------------
c1 = cluster("ingress", "1 - Ingress & Security Layer (fail-closed)", "#EBF8FF")
c1.node("ingress", "Ingress\nNGINX/Envoy - TLS - JWT - tenant_id\n(X-Tenant-ID must match token)")
c1.node("pii", "Pre-Hook: PII Redactor\npriority 10 - fail_closed - KPI-01 <= 15 ms")
c1.node("inj", "Pre-Hook: Injection Classifier\npriority 20 - fail_closed - KPI-03 >= 99.5%")
c1.node("cache", "Redis Semantic Cache Check\npartition = tenant_id + acl_hash - cosine >= 0.92\nKPI-27 hit ratio >= 35%")
c1.node("cached", "Cached response\n(target < 30 ms, TTL 300s +/- 45s)")
g.subgraph(c1)

# ---- 2. Triage & Subagent Routing -------------------------------------------
c2 = cluster("triage", "2 - Triage & Subagent Routing", "#F0FFF4")
c2.node("triage", "Triage Agent\ntemperature = 0.0 - <= 25 ms (KPI-05)\ntags: intent, domain, risk_tier, complexity")
c2.node("direct", "Direct Response\nDIRECT_ANSWER\n(no retrieval)")
c2.node("retr", "Retrieval Subagent\nHYBRID_RAG - read-only\nquery rewrite + citation grounding")
c2.node("exec", "Execution Subagent\nTRANSACTION_EXEC - no RAG / web\nschema validation + workflow transitions")
c2.node("audit", "Audit Subagent (Verification)\naudit / compliance intents\nclaim-to-chunk NLI + RAGAS triad")
c2.node("reject", "Deterministic reject\nREJECT_OOS")
g.subgraph(c2)

# ---- 3. MCP & 3-Stage RAG ---------------------------------------------------
c3 = cluster("mcp", "3 - MCP JSON-RPC 2.0 Calls & 3-Stage Hybrid RAG", "#FFFBEB")
c3r = cluster("rag", "mcp-hybrid-rag (stdio, timeout 150 ms)", "#FEFCBF")
c3r.node("bm25", "Stage 1: BM25Okapi (sparse)\nk1=1.25 b=0.75 top_k=50 - < 12 ms")
c3r.node("bi", "Stage 2: Bi-Encoder + FAISS HNSW (dense)\nbge-large-en-v1.5 - efSearch=64 - < 25 ms")
c3r.node("rrf", "Rank Fusion: RRF\nk=60 - dedup chunk_hash - pool=40 - < 3 ms")
c3r.node("ce", "Stage 3: Cross-Encoder\nbge-reranker-v2-m3 ONNX FP16 - batch=32\ntop 4 @ sigmoid > 0.42 - < 45 ms")
c3.subgraph(c3r)
c3.node("graph", "mcp-graphify-vault (stdio)\n1-hop / 2-hop wikilinks, depth <= 2\n~320 tokens vs ~1,400 raw")
c3.node("state", "mcp-state-store (streamable-http, pool 200)\nPostgres (audit, session_facts) + Redis\nTenant-scoped SQL and keys")
g.subgraph(c3)

# ---- 4. Control Plane & Egress ----------------------------------------------
c4 = cluster("control", "4 - Control Plane & Egress", "#FFF5F5")
c4.node("abst", "Deterministic abstention\nrerank top score < 0.42 - < 90 ms\nno LLM call")
c4.node("react", "Bounded ReAct Loop\nmax_steps = 5 - total_loop_budget 300 ms\nINIT>TRIAGE>REASONING>TOOL_CALL>OBSERVATION>VERIFY>DONE/HALTED\nGoalAnchorBlock at index 0 - last 3 turns")
c4.node("cb", "Circuit Breaker (TOOL_DISPATCH)\n(tool, canonical_args_hash) fails >= 2\n>> HALTED_BREAKER + remediation prompt")
c4.node("hitl", "Tier 3 HITL Gate\nTIER_3_MUTATE >> PENDING_APPROVAL\nreviewer Approve / Reject (UI/API)")
c4.node("verify", "Verify: claim-to-chunk NLI (per sentence)\nungrounded sentence vetoed\nKPI-24 hallucination <= 0.5%")
c4.node("egress", "Response Egress (streamed)\nTTFT p95 <= 180 ms - e2e p95 <= 450 ms")
c4.node("ragas", "Post-Hook: RAGAS auditor\nasync, sampled - off the hot path\nfaithfulness >= 0.92")
c4.node("otel", "Post-Hook: OTel Exporter\nOTLP :4317 - traceparent W3C\nCollector > Prometheus / Tempo / Grafana")
g.subgraph(c4)

# ---- Edges: ingress & security ----------------------------------------------
g.edge("ingress", "pii")
g.edge("pii", "inj", label="pass")
g.edge("inj", "cache", label="pass")
g.edge("cache", "cached", label="HIT", color="#2F855A", fontcolor="#2F855A")
g.edge("cache", "triage", label="MISS", color="#2B6CB0", fontcolor="#2B6CB0")
g.edge("cached", "egress", color="#2F855A")

# ---- Edges: triage routing ---------------------------------------------------
g.edge("triage", "direct", label="DIRECT_ANSWER")
g.edge("triage", "retr", label="HYBRID_RAG")
g.edge("triage", "exec", label="TRANSACTION_EXEC")
g.edge("triage", "audit", label="audit intent")
g.edge("triage", "reject", label="REJECT_OOS", style="dashed")
g.edge("reject", "egress", style="dashed")

# ---- Edges: MCP & RAG --------------------------------------------------------
g.edge("retr", "bm25", label="MCP JSON-RPC\nhybrid_retrieve", lhead="cluster_rag")
g.edge("retr", "bi", label="parallel")
g.edge("bm25", "rrf")
g.edge("bi", "rrf")
g.edge("rrf", "ce")
g.edge("ce", "react", label="score >= 0.42")
g.edge("ce", "abst", label="score < 0.42", color="#C53030", fontcolor="#C53030")
g.edge("abst", "egress", color="#C53030")
g.edge("retr", "graph", label="MCP stdio\nsubgraph call")
g.edge("graph", "react", label="minimal subgraph")
g.edge("exec", "state", label="Tier 1/2 MCP call")
g.edge("exec", "hitl", label="Tier 3", color="#C53030", fontcolor="#C53030")
g.edge("hitl", "state", label="approved only")
g.edge("audit", "state", label="audit log only")
g.edge("direct", "verify")

# ---- Edges: control plane ----------------------------------------------------
g.edge("react", "cb", label="tool dispatch")
g.edge("cb", "react", label="result / observation", style="dashed")
g.edge("react", "verify")
g.edge("verify", "egress", label="grounded")
g.edge("egress", "ragas", label="sampled async", style="dashed")
g.edge("ragas", "otel", style="dashed")
g.edge("react", "otel", label="spans", style="dotted")

g.render(filename=OUT, format="png", cleanup=True)
print(f"wrote {OUT}.png")
