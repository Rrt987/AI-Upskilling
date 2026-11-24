"""Integration: PostgresStore against an embedded Postgres (pgserver). Skipped if pgserver is absent."""
import uuid

import pytest

pgserver = pytest.importorskip("pgserver")


@pytest.fixture(scope="module")
def dsn(tmp_path_factory):
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("pg")))
    yield srv.get_uri()
    srv.cleanup()


def test_postgres_chain_and_pending_flow(dsn):
    from app.store import PostgresStore
    store = PostgresStore(dsn)
    tenant = "tenant_pgtest"
    for i in range(3):
        store.append_audit(tenant, "QUERY", {"i": i, "contact": "x@y.org"})
    assert store.verify_chain(tenant)["ok"]
    row = {"user_id": "alice", "workflow_id": "QA-RELEASE-1", "transition": "RELEASE", "subject_id": "LOT-00044"}
    key = str(uuid.uuid4())
    rec = store.submit_pending(tenant, key, "h1", row)
    assert rec["status"] == "PENDING_APPROVAL"
    replay = store.submit_pending(tenant, key, "h1", row)
    assert replay["id"] == rec["id"] and replay["idempotent_replay"]
    store.decide(tenant, rec["id"], "APPROVED", "bob")
    assert store.list_pending(tenant, "APPROVED")[0]["id"] == rec["id"]
    store.put_fact(tenant, "s1", "k", "v", None)
    assert store.get_facts(tenant, "s1")[0]["fact_value"] == "v"
    assert store.list_pending("tenant_other_pg") == []  # tenant scoped
