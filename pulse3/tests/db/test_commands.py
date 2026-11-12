"""ops.command_receipt idempotency on real PostgreSQL (INTEGRATION_GUIDE section 6)."""
from __future__ import annotations

import threading
import time
from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest
from w02_helpers import now, seed_api_binding, seed_contract

from pulse.adapters.postgres import db
from pulse.adapters.postgres.commands import PgCommandStore
from pulse.adapters.postgres.outbox import PgOutbox
from pulse.application.ports.delivery import OutboxEvent
from pulse.application.ports.errors import Conflict

pytestmark = pytest.mark.pg
TTL = timedelta(hours=24)


@pytest.fixture
def op(pg_dsn):
    c = db.connect(pg_dsn)
    name = f"op{uuid4().hex[:8]}"
    seed_api_binding(c, name)
    c.commit()
    c.close()
    return name


def run(store, principal, op, key, request, handler, ttl=TTL, now_=None):
    cid, ver = seed_contract(store.conn)
    store.conn.commit()                       # the command itself must start from an idle connection
    return store.execute(principal_id=principal, operation_id=op, idempotency_key=key, request=request,
                         response_contract_id=cid, response_schema_version=ver, handler=handler, ttl=ttl, now=now_)


def test_same_key_same_payload_returns_original_and_runs_once(conn, op):
    store, p, calls = PgCommandStore(conn), uuid4(), []

    def handler(c):
        calls.append(1)
        return {"resource_id": str(uuid4()), "n": len(calls)}

    first = run(store, p, op, "k1", {"a": 1, "b": [1, 2]}, handler)
    second = run(store, p, op, "k1", {"b": [1, 2], "a": 1}, handler)       # key order is irrelevant
    assert not first.replayed and second.replayed
    assert second.response == first.response and calls == [1]
    assert second.committed_at == first.committed_at


def test_same_key_different_payload_conflicts(conn, op):
    store, p = PgCommandStore(conn), uuid4()
    run(store, p, op, "k", {"a": 1}, lambda c: {"ok": True})
    with pytest.raises(Conflict):
        run(store, p, op, "k", {"a": 2}, lambda c: {"ok": False})
    conn.rollback()


def test_scope_is_principal_and_operation(conn, op):
    store = PgCommandStore(conn)
    seed_api_binding(conn, op + "b")
    p1, p2 = uuid4(), uuid4()
    r1 = run(store, p1, op, "k", {"a": 1}, lambda c: {"who": 1})
    r2 = run(store, p2, op, "k", {"a": 1}, lambda c: {"who": 2})               # other principal, same key
    r3 = run(store, p1, op + "b", "k", {"a": 1}, lambda c: {"who": 3})         # other operation, same key
    assert (r1.response, r2.response, r3.response) == ({"who": 1}, {"who": 2}, {"who": 3})
    assert not (r1.replayed or r2.replayed or r3.replayed)


def test_failed_handler_leaves_no_receipt_and_no_business_effect(conn, op):
    store, p = PgCommandStore(conn), uuid4()
    agg = f"cmd-{uuid4().hex[:8]}"

    def failing(c):
        PgOutbox(c).append(OutboxEvent(event_id=uuid4(), event_type="account.updated", schema_version="3.0.0",
                                       aggregate_type="cmd", aggregate_id=agg, aggregate_version=1,
                                       occurred_at=now(), producer_version="t", payload={}))
        raise RuntimeError("business rule rejected")

    with pytest.raises(RuntimeError):
        run(store, p, op, "k", {"a": 1}, failing)
    conn.rollback()
    assert conn.execute("SELECT count(*) AS n FROM ops.outbox WHERE aggregate_id=%s", (agg,)).fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM ops.command_receipt WHERE principal_id=%s", (p,)).fetchone()["n"] == 0
    ok = run(store, p, op, "k", {"a": 1}, lambda c: {"ok": True})              # the retry is a fresh execution
    assert not ok.replayed


def test_receipt_event_and_business_write_commit_together(conn, op):
    store, p = PgCommandStore(conn), uuid4()
    agg = f"cmd-{uuid4().hex[:8]}"

    def handler(c):
        PgOutbox(c).append(OutboxEvent(event_id=uuid4(), event_type="account.updated", schema_version="3.0.0",
                                       aggregate_type="cmd", aggregate_id=agg, aggregate_version=1,
                                       occurred_at=now(), producer_version="t", payload={"x": 1}))
        return {"done": True}

    run(store, p, op, "k", {"a": 1}, handler)
    run(store, p, op, "k", {"a": 1}, handler)                                   # replay: no second event
    assert conn.execute("SELECT count(*) AS n FROM ops.outbox WHERE aggregate_id=%s", (agg,)).fetchone()["n"] == 1


def test_concurrent_identical_requests_run_handler_once(dsn, op):
    p, calls, results, errors = uuid4(), [], [], []
    barrier = threading.Barrier(3)

    def worker():
        c = db.connect(dsn)
        try:
            store = PgCommandStore(c)
            barrier.wait()

            def handler(cn):
                calls.append(1)
                time.sleep(0.3)
                return {"v": 1}

            results.append(run(store, p, op, "same", {"a": 1}, handler))
        except Exception as e:          # noqa: BLE001
            errors.append(e)
        finally:
            c.close()

    ts = [threading.Thread(target=worker) for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors, errors
    assert len(calls) == 1 and sorted(r.replayed for r in results) == [False, True, True]


def test_expired_receipt_is_treated_as_absent(conn, op):
    store, p, calls = PgCommandStore(conn), uuid4(), []

    def handler(c):
        calls.append(1)
        return {"run": len(calls)}

    old = now() - timedelta(hours=2)
    run(store, p, op, "k", {"a": 1}, handler, ttl=timedelta(hours=1), now_=old)     # expired an hour ago
    again = run(store, p, op, "k", {"a": 2}, handler)                                # even a different payload is new
    assert not again.replayed and calls == [1, 1] and again.response == {"run": 2}


def test_receipts_are_immutable_until_expiry(conn, op):
    store, p = PgCommandStore(conn), uuid4()
    run(store, p, op, "k", {"a": 1}, lambda c: {"ok": 1})
    conn.commit()
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        conn.execute("UPDATE ops.command_receipt SET response_document='{}' WHERE principal_id=%s", (p,))
    conn.rollback()
    with pytest.raises(psycopg.errors.RaiseException, match="unexpired"):
        conn.execute("DELETE FROM ops.command_receipt WHERE principal_id=%s", (p,))
    conn.rollback()
