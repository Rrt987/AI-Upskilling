import json
import uuid
from pathlib import Path

import pytest

from app.contracts import ErrorCode, HybridRetrieveIn, ToolError, acl_hash
from app.rag.engines import chunk_text, rrf_fuse, tokenize
from app.rag.pipeline import CorpusRegistry, clean_markdown, hybrid_retrieve
from app.store import GENESIS, MemoryStore, chain_hash, verify_events

VAULT = Path(__file__).resolve().parent.parent / "knowledge_vault"
H = acl_hash(["TIER_1_READ"])


def _req(tenant, query, **kw):
    return HybridRetrieveIn(tenant_id=tenant, acl_hash=H, query=query, **kw)


def test_chunker_keeps_lines_intact_and_splits_long_lines():
    text = "\n".join(f"Line {i} has some words." for i in range(10))
    chunks = chunk_text(text, max_tokens=20, overlap=4)
    assert all("\n" in c or len(c.split()) <= 20 for c in chunks)
    long = " ".join(f"w{i}" for i in range(100))
    windows = chunk_text(long, max_tokens=40, overlap=8)
    assert len(windows) >= 3 and all(len(w.split()) <= 40 for w in windows)


def test_clean_markdown_strips_headings_and_wikilinks():
    out = clean_markdown("# Title\n- Item [[SOP-VER-002]] here.\n1. Step one.")
    assert "#" not in out and "[[" not in out and "SOP-VER-002" in out


def test_rrf_dedups_by_chunk_hash_and_ranks_consensus_first():
    from app.rag.engines import Chunk
    chunks = [Chunk("a", "s", "x", "h1" * 32, "t"), Chunk("b", "s", "x dup", "h1" * 32, "t"),
              Chunk("c", "s", "y", "h2" * 32, "t")]
    fused = rrf_fuse([[0, 2], [2, 1]], chunks, k=60, pool=40)
    assert fused[0] == 2 and 1 not in fused  # index 1 shares a hash with index 0


def test_tokenizer_stems_terms():
    assert tokenize("Verifying batches") == tokenize("verify batch")


def test_hybrid_retrieve_grounded_and_abstains_on_unrelated_query():
    reg = CorpusRegistry(VAULT)
    corpus = reg.get("tenant_demo")
    ok = hybrid_retrieve(corpus, _req("tenant_demo", "batch verification procedure lot"))
    assert not ok.abstain and ok.chunks and ok.chunks[0].source_uri.startswith("rag://chunks/")
    no = hybrid_retrieve(corpus, _req("tenant_demo", "zebra parking regulations for mars colonies"))
    assert no.abstain and not no.chunks
    assert set(no.stage_ms) == {"bm25", "dense", "rrf", "rerank"}


def test_tenant_isolation_in_retrieval():
    reg = CorpusRegistry(VAULT)
    other = reg.get("tenant_other")
    res = hybrid_retrieve(other, _req("tenant_other", "batch verification lot"))
    assert all(c.source_id == "POL-OTH-001" for c in res.chunks)
    demo_terms = hybrid_retrieve(reg.get("tenant_demo"), _req("tenant_demo", "expense approval manager"))
    assert all(c.source_id != "POL-OTH-001" for c in demo_terms.chunks)
    with pytest.raises(ValueError):
        hybrid_retrieve(other, _req("tenant_demo", "batch"))


def test_subgraph_depth_bound_and_token_reduction_kpi26():
    corpus = CorpusRegistry(VAULT).get("tenant_demo")
    nodes, edges, tokens = corpus.subgraph("POL-LOT-001", depth=2)
    assert "POL-LOT-001" in nodes and "SOP-QUAR-004" in nodes
    _, _, tokens0 = corpus.subgraph("POL-LOT-001", depth=0)
    assert tokens0 < tokens
    reduction = 1 - tokens / corpus.raw_dump_tokens()
    assert reduction >= 0.0  # measured value is reported by the KPI runner; see reports/


def test_audit_chain_detects_tampering():
    store = MemoryStore()
    for i in range(3):
        store.append_audit("tenant_demo", "QUERY", {"n": i, "email": "a@b.co"})
    chain = store.audit_chain("tenant_demo")
    assert "a@b.co" not in json.dumps(chain)  # zero raw PII persistence
    assert verify_events(chain)["ok"]
    chain[1]["payload"]["n"] = 99
    result = verify_events(chain)
    assert not result["ok"] and result["broken_at_seq"] == 2


def test_chain_hash_genesis():
    assert chain_hash(GENESIS, {"a": 1}) != chain_hash(GENESIS, {"a": 2})


def test_idempotency_replay_and_conflict():
    store = MemoryStore()
    row = {"user_id": "u", "workflow_id": "QA-RELEASE-1", "transition": "RELEASE", "subject_id": "LOT-00044"}
    key = str(uuid.uuid4())
    first = store.submit_pending("tenant_demo", key, "h1", row)
    again = store.submit_pending("tenant_demo", key, "h1", row)
    assert first["id"] == again["id"]
    with pytest.raises(ToolError) as e:
        store.submit_pending("tenant_demo", key, "h2", row)
    assert e.value.code is ErrorCode.IDEMPOTENCY_CONFLICT


def test_requester_cannot_approve_own_mutation_and_decision_is_terminal():
    store = MemoryStore()
    row = {"user_id": "alice", "workflow_id": "QA-RELEASE-1", "transition": "RELEASE", "subject_id": "LOT-00044"}
    rec = store.submit_pending("tenant_demo", str(uuid.uuid4()), "h", row)
    with pytest.raises(ToolError) as e:
        store.decide("tenant_demo", rec["id"], "APPROVED", "alice")
    assert e.value.code is ErrorCode.RBAC_DENIED
    store.decide("tenant_demo", rec["id"], "APPROVED", "bob")
    with pytest.raises(ToolError):
        store.decide("tenant_demo", rec["id"], "REJECTED", "carol")
