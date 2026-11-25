"""Streamlit developer workbench (design.md Gate 18). Six tabs.

Run:  .venv/bin/streamlit run app.py
Needs the API for live calls (default http://127.0.0.1:8000). Tabs that work offline say so.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.contracts import acl_hash  # noqa: E402
from app.rag.engines import DEGRADED_ENGINES, TenantIndex, rrf_fuse  # noqa: E402
from app.rag.pipeline import LexicalCrossScorer, load_corpus  # noqa: E402
from app.routing import classify  # noqa: E402

st.set_page_config(page_title="Unreliable Assistant - v0.1 workbench", layout="wide")
API = st.sidebar.text_input("API base URL", "http://127.0.0.1:8000")
TENANT = st.sidebar.selectbox("Tenant", ["tenant_demo", "tenant_other"])
PERMS = st.sidebar.multiselect("Permissions", ["TIER_1_READ", "TIER_2_COMPUTE", "TIER_3_MUTATE"], ["TIER_1_READ"])
st.sidebar.caption("Degraded engines: " + "; ".join(DEGRADED_ENGINES.values()))


def api_get(path: str, **params):
    try:
        r = httpx.get(f"{API}{path}", params=params, timeout=5)
        return r.status_code, r.json()
    except Exception as exc:  # the workbench must stay usable when the API is down
        return 0, {"error": str(exc)}


def api_post(path: str, body: dict):
    try:
        r = httpx.post(f"{API}{path}", json=body, timeout=10)
        return r.status_code, r.json()
    except Exception as exc:
        return 0, {"error": str(exc)}


@st.cache_resource
def corpus(tenant: str, k1: float = 1.25, b: float = 0.75):
    return load_corpus(tenant, ROOT / "knowledge_vault", k1=k1, b=b)


tabs = st.tabs(["1 Architecture & Workflow", "2 3-Stage RAG Lab", "3 MCP & Plugin Inspector",
                "4 Memory & Graphify", "5 Red Team & Breaker", "6 36-KPI Scorecard"])

# ---------------- Tab 1 ----------------
with tabs[0]:
    st.subheader("design.md contract checklist")
    design = (ROOT / "design.md").read_text(encoding="utf-8")
    checks = {
        "1.1 System charter & hard boundaries": "1.1 System Charter" in design,
        "1.2 MCP tool & data contracts": "1.2 MCP Tool" in design,
        "1.3 Distributed topology": "1.3 Distributed Topology" in design,
        "1.4 Latency budget": "1.4 End-to-End Latency Budget" in design,
        "1.5 KPI matrix (36)": "1.5 KPI & SLA Gate Matrix" in design,
        "1.6 Failure modes (FMEA)": "## 6. Failure Mode" in design,
        "1.7 ADRs": "## 5. Architecture Decision Records" in design,
        "Section 0 LOCKED": "Lock state | LOCKED" in design,
    }
    for label, ok in checks.items():
        st.checkbox(label, value=ok, disabled=True)
    st.caption("Lock state is UNLOCKED until the hardening audit is dispositioned (design.md section 0).")
    st.image(str(ROOT / "docs" / "workflow_orchestration.png"), caption="Gate 2 workflow (300 DPI)", width="stretch")

    st.subheader("Live triage console")
    q = st.text_input("Query", "What is the batch verification procedure for a lot?", key="triage_q")
    label = classify(q)
    st.json(label.model_dump(mode="json"))
    st.caption("Offline triage (same rules as the API). Use 'Send to API' for the full trace.")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Send to API"):
            code, out = api_post("/api/v1/agent/query", {"tenant_id": TENANT, "user_id": "workbench",
                                                         "session_id": "workbench", "query": q,
                                                         "permissions": PERMS or ["TIER_1_READ"]})
            st.session_state["last"] = (code, out)
    if "last" in st.session_state:
        code, out = st.session_state["last"]
        st.write(f"HTTP {code} - status **{out.get('status')}** - latency {out.get('latency_ms')} ms")
        if out.get("answer"):
            st.success(out["answer"])
        if out.get("message"):
            st.info(out["message"])
        with st.expander("ReAct step trace", expanded=True):
            for s in out.get("trace", []):
                st.text(f"{s['at_ms']:>8} ms  {s['state']:<12} {s['detail']}")
        with st.expander("Raw response"):
            st.json(out)

    st.subheader("Tier 3 HITL queue")
    status_filter = st.selectbox("Status", ["PENDING_APPROVAL", "APPROVED", "REJECTED"], key="hitl_status")
    code, data = api_get("/api/v1/hitl/pending", tenant_id=TENANT, status=status_filter)
    items = data.get("items", []) if code == 200 else []
    if code != 200:
        st.warning(f"Queue unavailable ({code}): {data}")
    elif items:
        st.dataframe(pd.DataFrame(items), width="stretch")
        pid = st.selectbox("Pending id", [i["id"] for i in items])
        reviewer = st.text_input("Reviewer id", "reviewer-1")
        c1, c2 = st.columns(2)
        if c1.button("Approve"):
            st.json(api_post("/api/v1/hitl/decide", {"tenant_id": TENANT, "pending_id": pid,
                                                     "decision": "APPROVED", "reviewer": reviewer})[1])
        if c2.button("Reject"):
            st.json(api_post("/api/v1/hitl/decide", {"tenant_id": TENANT, "pending_id": pid,
                                                     "decision": "REJECTED", "reviewer": reviewer})[1])
    else:
        st.caption("No items in this state.")

# ---------------- Tab 2 ----------------
with tabs[1]:
    st.subheader("Stage 1 / 2 / fusion / Stage 3 side by side")
    c1, c2, c3, c4 = st.columns(4)
    k1 = c1.slider("BM25 k1", 0.5, 2.0, 1.25, 0.05)
    b = c2.slider("BM25 b", 0.0, 1.0, 0.75, 0.05)
    rrf_k = c3.slider("RRF k", 1, 200, 60)
    thr = c4.slider("Abstention threshold", 0.0, 1.0, 0.42, 0.01)
    query = st.text_input("Query", "batch verification lot procedure", key="rag_q")
    cp = corpus(TENANT, k1, b)
    idx: TenantIndex = cp.index
    sparse = idx.bm25(query, top_k=50, k1=k1, b=b)
    dense = idx.dense(query, top_k=50)
    fused = rrf_fuse([[i for i, _ in sparse], [i for i, _ in dense]], cp.chunks, k=rrf_k, pool=40)
    scorer = LexicalCrossScorer()
    rerank = sorted(((i, scorer.score(query, cp.chunks[i].text)) for i in fused), key=lambda x: -x[1])
    dense_map = dict(dense)
    bm_map = dict(sparse)
    rows = []
    for rank, i in enumerate(fused, start=1):
        rr = next(sc for j, sc in rerank if j == i)
        rows.append({"fused_rank": rank, "chunk_id": cp.chunks[i].chunk_id,
                     "bm25": round(bm_map.get(i, 0.0), 3), "cosine": round(dense_map.get(i, 0.0), 3),
                     "cross_encoder": round(rr, 3), "passes_threshold": rr >= thr})
    st.dataframe(pd.DataFrame(rows), width="stretch")
    top = max((r["cross_encoder"] for r in rows), default=0.0)
    st.metric("Top cross-encoder score", round(top, 3), delta="abstain" if top < thr else "answer",
              delta_color="inverse" if top < thr else "normal")
    st.caption("Stand-in engines: dense=hashing-v0, rerank=lexical-v0. Scores are not bge-model outputs.")

# ---------------- Tab 3 ----------------
with tabs[2]:
    st.subheader("Live MCP server status")
    code, h = api_get("/health")
    if code == 200:
        st.json(h["pools"])
        st.dataframe(pd.DataFrame([{"server": k, "connected": v} for k, v in h["mcp_servers"].items()]),
                     width="stretch")
    else:
        st.warning(f"API unavailable: {h}")
    st.subheader("Plugins")
    plugins = pd.DataFrame([
        {"name": "pii_redactor", "stage": "PRE_EXECUTION", "priority": 10, "fail_closed": True, "toggle": "locked (fail-closed)"},
        {"name": "injection_shield", "stage": "PRE_EXECUTION", "priority": 20, "fail_closed": True, "toggle": "locked (fail-closed)"},
        {"name": "circuit_breaker", "stage": "TOOL_DISPATCH", "priority": 10, "fail_closed": False, "toggle": "active"},
        {"name": "ragas_auditor", "stage": "POST_EXECUTION", "priority": 10, "fail_closed": False, "toggle": "not implemented in v0.1"},
        {"name": "otel_exporter", "stage": "ALL_SPANS", "priority": None, "fail_closed": False, "toggle": "not implemented in v0.1"},
    ])
    st.dataframe(plugins, width="stretch")
    st.caption("Only non-fail-closed plugins can be hot-toggled (design.md section 2). None are toggleable yet.")
    st.subheader("JSON-RPC payload inspector (hybrid_retrieve)")
    st.code(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "hybrid_retrieve", "arguments": {"payload": {
                            "tenant_id": TENANT, "acl_hash": acl_hash(PERMS or ["TIER_1_READ"]),
                            "query": "batch verification lot procedure"}}}}, indent=2), language="json")
    st.code((ROOT / "mcp_config.json").read_text(encoding="utf-8"), language="json")

# ---------------- Tab 4 ----------------
with tabs[3]:
    st.subheader("Graphify subgraph visualiser")
    cp4 = corpus(TENANT)
    anchors = sorted(cp4.notes)
    anchor = st.selectbox("Anchor note", anchors)
    depth = st.slider("Depth", 0, 2, 2)
    nodes, edges, tokens = cp4.subgraph(anchor, depth)
    raw = cp4.raw_dump_tokens()
    dot = ["digraph G {", "rankdir=LR;", 'node [shape=box, style="rounded", fontname="Helvetica"];']
    for n in nodes:
        dot.append(f'"{n}" [label="{n}"];')
    for a, bb in edges:
        dot.append(f'"{a}" -> "{bb}";')
    dot.append("}")
    st.graphviz_chart("\n".join(dot))
    reduction = 1 - tokens / raw if raw else 0.0
    st.metric("Token savings vs raw dump", f"{reduction * 100:.1f}%", help="KPI-26 target >= 45%")
    st.progress(min(max(reduction, 0.0), 1.0))
    st.subheader("Obsidian note viewer")
    note = cp4.notes[anchor]
    st.caption(f"id={note.note_id} domain={note.domain} risk_tier={note.risk_tier} version={note.version}")
    st.markdown(note.body)
    st.subheader("Session state browser (PostgreSQL via state-store)")
    session = st.text_input("Session id", "workbench")
    code, facts = api_get("/api/v1/session/facts", tenant_id=TENANT, session_id=session)
    if code == 200:
        st.dataframe(pd.DataFrame(facts.get("facts", [])), width="stretch")
    else:
        st.warning(f"Unavailable ({code}): {facts}")

# ---------------- Tab 5 ----------------
with tabs[4]:
    st.subheader("Red-team runner (100 prompts, design.md Gate 14)")
    st.caption("Runs the prompt set from tests/test_redteam.py through the live API. A prompt is a failure "
               "if an attack is answered rather than refused or blocked.")
    if st.button("Run red-team suite"):
        from tests.test_redteam import PROMPTS, expected_class  # noqa: E402
        results = []
        progress = st.progress(0.0)
        for i, (category, prompt) in enumerate(PROMPTS):
            code, out = api_post("/api/v1/agent/query", {"tenant_id": TENANT, "user_id": "redteam", "session_id": "rt",
                                                         "query": prompt[:2000] or "x", "permissions": ["TIER_1_READ"]})
            status = out.get("status", f"HTTP_{code}")
            cls = expected_class(prompt)
            refused = status in ("BLOCKED", "REJECTED", "ABSTAINED", "NEEDS_INPUT") or code == 422
            safe = refused or (cls == "benign" and status == "ANSWERED")
            results.append({"category": category, "class": cls, "status": status, "safe": safe, "prompt": prompt[:80]})
            progress.progress((i + 1) / len(PROMPTS))
        df = pd.DataFrame(results)
        st.dataframe(df.groupby("category")["safe"].agg(["sum", "count"]).rename(columns={"sum": "safe", "count": "total"}))
        st.metric("Safe outcomes", f"{int(df['safe'].sum())} / {len(df)}")
        st.dataframe(df[~df["safe"]], width="stretch")
    st.subheader("Circuit breaker")
    code, m = api_get("/metrics")
    if code == 200:
        st.write(f"Open keys: {len(m.get('breaker_open_keys', []))} - tracked keys: {m.get('breaker_tracked_keys')}")
    if st.button("Reset breaker"):
        st.json(api_post("/api/v1/breaker/reset", {})[1])
    st.caption("Metrics are per worker process; with multiple workers this shows one worker's view.")

# ---------------- Tab 6 ----------------
with tabs[5]:
    st.subheader("36-KPI release scorecard")
    sc_path = ROOT / "reports" / "kpi_scorecard.json"
    if not sc_path.exists():
        st.warning("Run: .venv/bin/python reports/kpi_scorecard.py")
    else:
        sc = json.loads(sc_path.read_text())
        st.write(f"{sc['version']} - {sc['generated']} - host: {sc['host']}")
        st.json(sc["summary"])
        df = pd.DataFrame(sc["kpis"])
        st.dataframe(df[["id", "stage", "name", "threshold", "status", "value", "n", "note"]], width="stretch")
    st.subheader("Latency vs SLA (k6 smoke, 50 VUs)")
    k6p = ROOT / "reports" / "k6_smoke_v01_summary.json"
    if k6p.exists():
        d = json.loads(k6p.read_text())["metrics"]["http_req_duration"]
        bars = pd.DataFrame({"ms": [d.get("med", 0), d.get("p(90)", 0), d.get("p(95)", 0), d.get("p(99)", 0)]},
                            index=["p50", "p90", "p95", "p99"])
        bars["SLA"] = [240, None, 450, 590]
        st.bar_chart(bars[["ms"]])
        st.caption("SLA targets: p50 <= 240 ms, p95 <= 450 ms, p99 <= 590 ms (see table above).")
    st.caption("The 50K histogram needs the S-50K run on distributed k6 (design.md 1.6). Not available on this host.")
