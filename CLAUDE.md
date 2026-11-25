# CLAUDE.md - The Unreliable Assistant -> Enterprise Grade

Working instructions for Claude Code in this repository.
Written for: Claude Code sessions working on this repo (and the engineers who review them).

## Source of truth

1. `design.md` is the contract. Read it before changing any code, config, or test.
2. `docs/workflow_orchestration.png` is the workflow contract (Gate 2). Regenerate it with `python3 docs/render_workflow.py` when the topology changes.
3. `docs/failure_notes.md` records every failure and its fix. Update it on every failure.

## Current status

- Stage: **Build v0.1 (candidate).** Implemented on explicit instruction ("BUILD IT") before the design lock.
- Gate 1 (`design.md`): still **UNLOCKED**. Build was done against the draft. Deviations are in `design.md` section 7.1. The hardening audit disposition (section 8) is still pending.
- Tests: 140 pass (unit, contract, Postgres integration, e2e against real MCP servers, red-team set, chaos suite, telemetry, semantic cache, usage).
- Load: validated on one 4-vCPU host only. KPI-35 (8,500 RPS) is FAIL there. The 50K stage has not been run (see `docs/failure_notes.md` FN-009).
- Open risks and gaps: `docs/failure_notes.md` FN-009 and FN-010.

## Hard rules (do not break)

- **Design lock.** v0.1 was built before lock on explicit instruction. Any new contract, budget or KPI change needs a `design.md` entry (section 7.1 or 5) first. Do not lock section 0 on your own; the user does that after the audit.
- **Tenant isolation:** every SQL query and every Redis key includes `tenant_id`. Cache partitions use `tenant_id` + `acl_hash` (ADR-3). Never write a query or key that can return another tenant's data.
- **No raw PII** in any store, log, trace, cache, or test fixture. Use the redacted or hashed form.
- **Tier 3 mutations** go through the HITL gate (`PENDING_APPROVAL`). Never auto-approve, and never add a path that skips the gate.
- **Bounded loops:** `max_iterations = 5`, `total_loop_budget = 300 ms`, `per_step_timeout = 120 ms` for tool calls. No recursive self-calls without a state mutation.
- **Fail-closed plugins** (`pii_redactor`, `injection_shield`) must not be disabled in code. Changing them requires a config change with an audit event.
- **Do not change a KPI threshold or a latency budget** in code or config without an ADR amendment in `design.md` (section 5) and a note in section 0.
- **Do not run load tests** against shared or production infrastructure without explicit confirmation from the user.
- **Do not invent commands.** Only the commands below have been run.

## Stack (pinned by design.md section 0 / Gate 0)

- Python **3.11** (the local machine has 3.14; the project must run on 3.11). Manage with `uv`.
- FastAPI + Uvicorn (async). Pydantic v2 in strict mode with `extra="forbid"`.
- MCP over JSON-RPC 2.0: stdio for `mcp-hybrid-rag` and `mcp-graphify-vault`; Streamable HTTP for `mcp-state-store`.
- Retrieval: rank-bm25 (BM25Okapi), FAISS HNSW, BGE-large ONNX bi-encoder, `bge-reranker-v2-m3` ONNX FP16.
- Storage: PostgreSQL (+ pgvector) for audit and session facts; Redis Cluster for cache. **No SQLite.**
- Telemetry: OpenTelemetry (W3C `traceparent`) -> Collector -> Prometheus, Tempo, Grafana.
- Load: k6 (distributed) and JMeter (controller + 10 workers).
- UI: Streamlit, six tabs as in the brief (Gate 18).

## Span and metric names (use exactly these)

`hook.pre`, `cache.lookup`, `triage.classify`, `mcp.rag.bm25`, `mcp.rag.biencoder`, `mcp.rag.rrf`, `mcp.rag.rerank`, `agent.react.step`, `llm.generate`, `hook.post.ragas`.

Error codes: `VALIDATION_FAILED`, `TENANT_MISMATCH`, `RBAC_DENIED`, `TIMEOUT`, `RERANKER_UNAVAILABLE`, `IDEMPOTENCY_CONFLICT`, `HITL_REQUIRED`, `BREAKER_OPEN` (design.md section 1.2.6).

## Conventions

- Behaviour comes from `design.md`; do not add a feature that is not in it. Put proposed additions in the design first.
- Each MCP tool call passes through `ToolExecutionHarness`: validate, auto-repair (regex rules in design.md 1.2.4), RBAC tier check, UUIDv4 idempotency.
- Observations passed to the model are compressed to <= 180 tokens.
- Versioning: `v0.0` design, `v0.1` Build v0, then `v0.2+` per failure cycle. Tag each version when it passes its gate. Keep the previous version runnable for rollback.
- Failures: write an entry in `docs/failure_notes.md` before fixing. Fix on a branch. Re-run the failing stage and every earlier stage.
- Prefer deleting dead code to commenting it out.

## Commands (all run on this repo)

Python 3.11 venv at `.venv` (created with `uv venv --python 3.11 .venv`).

- Install: `uv pip install --python .venv/bin/python -e ".[dev]"`
- Start Postgres + state server (leave running): `.venv/bin/python scripts/dev_stack.py`
- Start the API (validated config): `MAX_INFLIGHT=16 RAG_POOL_SIZE=4 .venv/bin/uvicorn app.main:app --port 8000 --workers 2`
- Tests: `.venv/bin/python -m pytest -q tests`
- Lint: `.venv/bin/python -m pyflakes app mcp_servers scripts app.py reports`
- Load (k6 binary at `~/.local/bin/k6`):
  - smoke: `PROFILE=smoke TARGET=http://127.0.0.1:8000 k6 run --summary-export reports/k6_smoke_v01_summary.json load_tests/k6_50k_load.js`
  - cold cache, constant arrival: add `UNIQUE=1 RPS_PROBE=350`
  - staged profiles `s5k`, `s10k`, `s50k` need distributed k6 runners (not available here)
- KPI scorecard (needs the API up): `.venv/bin/python reports/kpi_scorecard.py`
- Workbench: `.venv/bin/streamlit run app.py`
- JMeter (binary at `~/.local/share/apache-jmeter-5.6.3/bin/jmeter`): `jmeter -n -t load_tests/jmeter_50k_plan.jmx -Jthreads=10 -Jrampup=2 -Jduration=25 -Jhost=127.0.0.1 -Jport=8000 -l /tmp/jmeter.jtl`. The 50K run is `-R worker1..worker10` on distributed hosts; not done here.
- Regenerate the diagram (needs system `python3` with the `graphviz` package): `python3 docs/render_workflow.py`

Environment flags: `OTEL_EXPORTER_OTLP_ENDPOINT` (optional span export), `PRICE_IN_PER_1K` and `PRICE_OUT_PER_1K` (optional cost), `VAULT_ROOT`, `STATE_URL` (default `http://127.0.0.1:8081/mcp`), `DATABASE_URL` (state server), `REDIS_URL` (optional; memory cache otherwise), `MAX_INFLIGHT`, `RAG_POOL_SIZE`, `GRAPH_POOL_SIZE`, `STATE_POOL_SIZE`, `AUDIT_ENABLED` (diagnostic only; leave at 1).

## Hooks

- Runtime lifecycle hooks are the plugins in `design.md` section 2 (Pluggy entry points).
- No Claude Code hooks are configured yet. If any are added, declare them in `.claude/settings.json` and list them here.

## Files

| Path | Purpose | State |
|---|---|---|
| `design.md` | Gate 1 contract | Draft, unlocked (section 7.1 lists build deviations) |
| `app/` | Core library: contracts, harness, plugins, routing, RAG pipeline, store, MCP client, agent, FastAPI app | Built (v0.1) |
| `mcp_servers/` | `rag_server.py` (stdio), `graphify_server.py` (stdio), `state_server.py` (streamable HTTP) | Built (v0.1) |
| `app.py` | Streamlit workbench, six tabs | Built (v0.1) |
| `tests/` | Unit, Postgres, e2e, red-team, chaos, telemetry, semantic cache, usage suites | 140 passing |
| `app/telemetry.py` | OpenTelemetry spans (B-02); OTLP export when `OTEL_EXPORTER_OTLP_ENDPOINT` is set | Built (v0.2) |
| `reports/` | k6 summaries, `kpi_scorecard.py`, `kpi_scorecard.json` | Built (v0.1) |
| `scripts/dev_stack.py` | Embedded Postgres + state server | Built |
| `docs/workflow_orchestration.png` | Gate 2 diagram | Generated |
| `docs/render_workflow.py` | Diagram source (regenerates the PNG) | Ready |
| `docs/failure_notes.md` | Failure log and version history | Template |
| `load_tests/k6_50k_load.js` | k6 profiles (smoke, s5k, s10k, s50k), RPS probe, cold-cache mode | Built |
| `load_tests/jmeter_50k_plan.jmx` | JMeter plan: 5,000 threads per worker, 180 s ramp, 600 s loop, 600 ms duration assertion, tenant-echo assertion | Built, validated locally (10 threads, 20,638 samples, 0 failures). Distributed run not done |
| `load_tests/queries.csv` | Query data for the JMeter plan | Built |
| `knowledge_vault/` | Obsidian atomic notes, one folder per tenant | Built (demo vault, 6 + 1 notes) |
