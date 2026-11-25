# Failure notes & version history

Written for: engineers running the staged load tests and the failure-rectification loop.

Every failure gets an entry before the fix. Every stage gets an entry even when it passes.
Re-test the failing stage and every earlier stage after each fix.

## Version ledger

| Version | Date | Content | Stage result | Rolled back? |
|---|---|---|---|---|
| v0.0 | 2026-11-23 | Design, CLAUDE.md, workflow diagram (design UNLOCKED) | n/a (design) | n/a |
| v0.1 (candidate) | 2026-11-23 | Build v0.1 per design.md, FN-001 to FN-008 fixed | Unit + e2e 126/126 pass; smoke 50 VU pass; cold 350 RPS **fails KPI-35** (see FN-009) | No |

Validated v0.1 configuration (single 4-vCPU host, see FN-005):
`uvicorn app.main:app --workers 2` with `MAX_INFLIGHT=16`, `RAG_POOL_SIZE=4`, `STATE_POOL_SIZE=8`.

## Entry template

### FN-<NNN> - <short title>
- **Version:** vX.Y
- **Stage:** unit / contract / S-5K / S-10K / S-50K / S-SOAK / red-team / chaos
- **Detected:** date, trace id or run id, KPI(s) breached
- **Symptom:** what was observed (numbers, not adjectives)
- **Root cause:** evidence-backed; say "unknown" if unknown
- **Fix:** commit or branch; design section changed (if any; ADR amendment reference)
- **Re-test:** stages re-run and result
- **Rollback used:** yes/no; which path (blue/green, config, index)
- **Status:** OPEN / FIXED / ACCEPTED-WITH-RISK

## Entries

### FN-001 - Integration defects found by the first smoke test
- **Version:** v0.1 (pre-candidate)
- **Stage:** contract / functional smoke
- **Detected:** first end-to-end query. Every retrieval returned `HALTED TIMEOUT`.
- **Symptom:** MCP calls failed with an MCP error. The FastMCP tools take a single `payload` argument, but the client sent the fields flat. Separately: a question about quarantine routed to Tier 3 (triage treated the verb as an action); "sku-7" was not repaired (rule needed 2+ digits); the first extractive answers quoted markdown headings and wikilink markup.
- **Root cause:** argument shape mismatch between client and server; triage had no question-form guard; the repair rule was narrower than the design's examples; chunking dropped line boundaries.
- **Fix:** `PAYLOAD_TOOLS` wrapping in the client; error text from the server now surfaces in the exception; question-form guard (questions never route to TRANSACTION_EXEC); repair rule `\d{1,6}` (design.md 1.2.4 updated); line-aware chunking and markdown cleaning; stopword filtering for extraction.
- **Re-test:** 126 unit and e2e tests pass. Smoke answers are cited and grounded.
- **Rollback used:** no
- **Status:** FIXED

### FN-002 - MCP sessions died under concurrent load
- **Version:** v0.1 candidate
- **Stage:** S-cold probe (600 RPS, cold cache)
- **Detected:** `/health` showed `hybrid-rag: false`; 65,000+ requests returned `HALTED`. Health listed `live_sessions` of 2 of 4 before the fix.
- **Symptom:** rag_server processes disappeared with no Python traceback. The client logged `ClosedResourceError`, and the pool kept a dead session in its list.
- **Root cause:** cancelling in-flight MCP requests (`asyncio.wait_for` around `session.call_tool`) corrupts the SDK's response stream. A later `BrokenResourceError` kills the session. The concurrent probe reproduced it. A sequential cancel probe did not, so the trigger is load-dependent. Compounding it, nothing supervised the pool, so dead sessions stayed listed.
- **Fix:** (a) in-flight requests are never cancelled. The caller gets TIMEOUT and the request finishes in the background with its result discarded. (b) Each session is owned by its own task. A supervisor respawns dead slots. (c) Transport errors retire the slot. (d) Read-only tools retry once. Writes are never retried. (e) `/health` now reports per-pool status.
- **Re-test:** 600 RPS cold run, restarts 0 (was 13). Pool stress test: 6,000 calls, 0 restarts.
- **Rollback used:** no
- **Status:** FIXED (SDK behaviour reported upstream as a candidate; not verified upstream)

### FN-003 - State store ceiling of ~88 writes/s
- **Version:** v0.1 candidate
- **Stage:** S-cold probe (350 RPS)
- **Detected:** direct probe of `audit_append`: 20 ms per write serial, 88 writes/s with 200 concurrent calls. Answered requests need two writes (audit and session fact), so the ceiling was about 44 answered requests/s.
- **Symptom:** accepted throughput flat at ~90 RPS regardless of pool size.
- **Root cause:** `PostgresStore` opened a new connection for every operation, and the synchronous calls ran on the event loop of the state server, serialising everything.
- **Fix:** `psycopg_pool` connection pool (2 to 10). Tool handlers are async and run DB work in worker threads (`anyio.to_thread`).
- **Re-test:** 151 writes/s concurrent across tenants. Serial write is 14.8 ms, bound by the per-tenant advisory lock and durable commit. That cost is kept on purpose: the hash chain must stay ordered and durable.
- **Rollback used:** no
- **Status:** FIXED (per-tenant ceiling remains; see OI-5)

### FN-004 - Overload produced a HALTED storm instead of clean shedding
- **Version:** v0.1 candidate
- **Stage:** S-cold probe (600 RPS, then 350 RPS)
- **Detected:** 600 RPS: 66,550 of 74,554 requests `HALTED`, p95 1.2 s.
- **Symptom:** requests queued in the app and in the 4 retrieval slots until the 120 ms per-step budget fired. Each timeout became a HALTED response.
- **Root cause:** the single uvicorn event loop ran at about 91% CPU at 350 RPS (rag_server processes were 21 to 36% each). Queue wait pushed calls past the step budget. Pool size made no difference (4 and 8 slots both halted), which ruled out the retrieval pool as the bottleneck.
- **Fix:** admission control. Beyond `MAX_INFLIGHT` (default 16 per worker) the worker returns 503 `OVERLOADED` with `Retry-After: 1` at once. Two workers give roughly double the capacity. Design deviation D-18.
- **Re-test (350 RPS cold, 2 workers, limit 16):** halted 190 of 45,443 (0.4%). Shed 56% as 503. Accepted about 170 RPS. p95 of accepted requests stays inside budget.
- **Rollback used:** no
- **Status:** FIXED for overload behaviour; capacity itself is OPEN (FN-009)

### FN-005 - Load-test checks hid the failures
- **Version:** v0.1 candidate
- **Stage:** k6 harness
- **Detected:** the first 600 RPS runs reported `checks 100%` while 89% of requests were HALTED.
- **Symptom:** the check `status is a defined outcome` accepted HALTED.
- **Root cause:** test-harness defect. HALTED was treated as a valid outcome. The threshold on checks was therefore meaningless for the failure mode that mattered.
- **Fix:** new check `not halted`. 503 OVERLOADED counts as a failure in `status is 200`. Thresholds are unchanged (design D-12).
- **Re-test:** the runs above now report the failures honestly.
- **Rollback used:** no
- **Status:** FIXED. Process lesson: a load check must fail on every non-success outcome it does not explicitly allow.

### FN-006 - Audit writes dropped under load
- **Version:** v0.1 candidate
- **Stage:** S-cold probe (350 RPS, before the write-timeout change)
- **Detected:** `audit_write_failed` counter = 25 in one run.
- **Symptom:** audit events were missing from the hash chain for those requests.
- **Root cause:** audit and session writes used the same 120 ms per-step budget as read calls. Under load they timed out, and the failure was swallowed.
- **Fix:** writes use `WRITE_TIMEOUT_S = 2.0`. Writes are not retried, because audit appends are not idempotent. A write failure is still counted. Design deviation D-19.
- **Re-test:** the validated run shows no `audit_write_failed`. `verify` returns ok over 22,752 entries.
- **Rollback used:** no
- **Status:** FIXED. Note that a failed write is still lost; a durable local spool is OPEN (see known gaps).

### FN-007 - Circuit breaker memory grew without bound
- **Version:** v0.1 candidate
- **Stage:** S-cold probe (600 RPS)
- **Detected:** `kpi16_breaker_halts` = 3,604 and tracked keys grew past 10,000 within one run.
- **Symptom:** a key per distinct argument set, never released.
- **Root cause:** the first implementation had no expiry or size cap. This is a leak under unique queries (KPI-36 risk).
- **Fix:** open entries expire after 120 s (half-open). Tracked keys are capped at 10,000 with LRU eviction. Design deviation D-17.
- **Re-test:** unit test coverage for TTL and cap is OPEN (listed below). The live counter stays at the cap.
- **Rollback used:** no
- **Status:** FIXED (unit test OPEN)

### FN-008 - PyStemmer is not thread-safe
- **Version:** v0.1 candidate
- **Stage:** code review (defensive)
- **Detected:** the PyStemmer documentation states that an instance "must not be called concurrently". The module held one global instance.
- **Symptom:** not observed in any run. Preventive.
- **Root cause:** shared global stemmer.
- **Fix:** one stemmer per thread (`threading.local`).
- **Re-test:** unit suite passes.
- **Rollback used:** no
- **Status:** FIXED (preventive)

### FN-009 - Single-host capacity is far below KPI-35 (OPEN)
- **Version:** v0.1 candidate
- **Stage:** S-cold probe (350 RPS offered, 2 workers, limit 16)
- **Detected:** KPI-35 = FAIL. Target 8,500 RPS. Measured: about 375 RPS offered, about 170 RPS accepted, the rest shed with 503.
- **Symptom:** sustained overload on the 4-vCPU host. Load average near 3.3 with k6 co-located.
- **Root cause:** the Python app process is CPU-bound (about 2.5 ms CPU per request). The host is shared by k6, retrieval processes, state server, and Postgres. No multi-host topology exists yet.
- **Fix:** not attempted in v0.1. Next steps: reduce per-request CPU (fewer MCP round trips per request, batch audit and session-fact writes into one call); run app workers on separate hosts behind a load balancer; move Postgres off the app host. These are the S-5K and S-10K stage work in design.md 1.6.
- **Re-test:** S-5K and above require distributed k6 runners (not available here). Not run.
- **Rollback used:** n/a
- **Status:** OPEN. This is the main blocker to the 50K target.

### FN-010 - Known gaps recorded for the audit (OPEN)
- Dense (hashing-v0) and rerank (lexical-v0) are stand-ins, not bge models. Degraded engines are reported in `/health` and Tab 1 and Tab 2. (OI-1)
- No LLM is deployed. Generation is extractive. Per-sentence NLI is a verbatim check, not a model. (OI-2)
- RAGAS, TTFT (no streaming), cost model (KPI-29), and MCP serialization timer are not measured. The scorecard marks these NOT_MEASURED.
- Semantic cache layer not built (exact-key only). KPI-27 is not representative (5-query set).
- Metrics are per worker process. Multi-worker aggregation is not implemented.
- OTel exporter not wired (spans are recorded in traces and the scorecard only). Signed MCP manifests (D-15) not done.
- JMeter plan written and validated locally only (10 threads). The 50K distributed run is not done. Soak (KPI-36) not run. Chaos suite (KPI-16) not run beyond the e2e bounded-loop test.
- Audit writes have no durable local spool on failure.
- Labeled datasets for triage, PII, injection, unanswerable, and IR are too small for PASS verdicts. Scorecard marks them PARTIAL.

### FN-011 - Audit report claims without supporting evidence (2026-11-25)
- **Version:** v0.1 candidate
- **Stage:** audit disposition
- **Detected:** the submitted "Post-Hardening Audit Report" reports 50K-VU passes, 9,240 RPS, RAGAS 0.94/0.91/0.89, $0.38 per 1K queries, and "all 36 KPIs PASSED".
- **Symptom:** none of these have an artifact in the repo. The scorecard shows 12 PASS, 7 PARTIAL, 12 NOT_MEASURED, 1 FAIL. KPI-35 is FAIL on this host. Several described components (Redis semantic cache, OTel export, ONNX batching) are not implemented.
- **Root cause:** the report describes a system and a test campaign that are not present in this repository. The cache key it describes is the superseded user-scoped key.
- **Fix:** every claim was checked and dispositioned in `design.md` section 8 (A-01 to A-18). Unsupported claims are REJECTED, not adopted. No code changed.
- **Re-test:** not applicable. Re-check after raw artifacts arrive for DEFERRED rows (A-02, A-03).
- **Rollback used:** no
- **Status:** FIXED (disposition recorded). Open: the user must supply raw 50K and RAGAS artifacts, or the affected KPIs stay NOT_MEASURED.

### FN-012 - Informational queries routed to Tier 3 (found by chaos suite)
- **Version:** v0.2 build
- **Stage:** chaos suite (B-01)
- **Detected:** 60 of 300 chaos trials returned `NEEDS_INPUT`. The query was "quarantine procedure temperature excursion".
- **Symptom:** a statement that names "quarantine" (a noun) was classified as TRANSACTION_EXEC. No mutation happened, because a Tier 3 request without an action is refused, but the route is wrong and the user gets an action prompt for a lookup.
- **Root cause:** the Tier 3 pattern matched "release" and "quarantine" anywhere in the query, not only as a leading imperative.
- **Fix:** the mid-sentence pattern now holds only explicit action phrases (submit, approve, reject, transition, change status, set status). Imperative verbs still count at the start of the query.
- **Re-test:** full suite; chaos suite status counts re-checked (see reports/chaos_result.json).
- **Rollback used:** no
- **Status:** FIXED

### FN-013 - v0.2 build: items delivered and what is still missing
- **Version:** v0.2 build
- **Stage:** audit follow-up (design.md section 7.2)
- **Delivered and tested:** chaos suite (B-01, KPI-16 PASS, 300 trials, 0 escapes); OpenTelemetry spans with the CLAUDE.md names and the caller's trace id (B-02); partitioned semantic cache (B-03); per-request token meter (B-04); red-team expected-class labels (B-05). Tests 126 to 140, all passing.
- **Not delivered:** RAGAS triad and golden set (B-06, needs an LLM judge and labeled data, OI-2 and OI-3); Redis cache (B-07, no server on this host); 50K and soak runs (B-08, distributed runners); OTLP export verified against a real collector (not tested here).
- **Live evidence:** `traceparent` from the request reaches the response; the near-duplicate query returns `SEMANTIC_HIT`; token counts come back with each answer.
- **Status:** FIXED for B-01 to B-05; OPEN for B-06 to B-08 and collector verification
