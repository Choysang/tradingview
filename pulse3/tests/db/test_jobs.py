"""Job runs with lease + fencing epoch on real PostgreSQL (M4-E03)."""
from __future__ import annotations

import threading
from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest
from w02_helpers import h, now, seed_contract

from pulse import contracts
from pulse.adapters.postgres import db
from pulse.adapters.postgres.jobs import PgJobStore
from pulse.application.ports.errors import Conflict, FencedOut, NotFound
from pulse.application.ports.jobs import JobDefinition

pytestmark = pytest.mark.pg


def definition(conn, *, timeout=60, max_concurrency=4, retry=None, enabled=True, ckey=None) -> JobDefinition:
    cid, ver = seed_contract(conn)
    conn.commit()
    d = JobDefinition(
        job_id=uuid4(), job_version=1, application_operation="ResearchService.schedule",
        schedule_document={"kind": "event"}, parameter_contract_id=cid, parameter_schema_version=ver,
        enabled=enabled, concurrency_key=ckey or f"ck-{uuid4().hex[:8]}", max_concurrency=max_concurrency,
        timeout_seconds=timeout, retry_policy=retry or {})
    PgJobStore(conn).register_definition(d)
    conn.commit()
    return d


def enqueue(store, d, key="k", params="p", **kw):
    rid = uuid4()
    run = store.enqueue(job_run_id=rid, job_id=d.job_id, job_version=d.job_version, idempotency_key=key,
                        parameter_hash=h(params), scheduled_for=now() - timedelta(seconds=1),
                        resource_type="experiment", resource_id=kw.pop("resource_id", uuid4()), **kw)
    return run


def test_register_definition_is_idempotent_and_versioned(conn):
    d = definition(conn)
    store = PgJobStore(conn)
    store.register_definition(d)                                         # identical: no-op
    changed = JobDefinition(**{**d.__dict__, "timeout_seconds": 5})
    with pytest.raises(Conflict, match="timeout_seconds"):
        store.register_definition(changed)
    conn.rollback()


def test_enqueue_is_idempotent_and_receipt_matches_contract(conn, dsn):
    d = definition(conn)
    store = PgJobStore(conn)
    a = enqueue(store, d, key="same", params="x")
    b = store.enqueue(job_run_id=a.job_run_id, job_id=d.job_id, job_version=1, idempotency_key="same",
                      parameter_hash=h("x"), scheduled_for=a.scheduled_for, resource_type="experiment",
                      resource_id=a.resource_id)
    assert b.job_run_id == a.job_run_id
    conn.commit()
    with pytest.raises(Conflict):
        store.enqueue(job_run_id=uuid4(), job_id=d.job_id, job_version=1, idempotency_key="same",
                      parameter_hash=h("different"), scheduled_for=a.scheduled_for, resource_type="experiment",
                      resource_id=uuid4())
    conn.rollback()
    receipt = store.receipt(a.job_run_id)
    contracts.check("JobReceipt", receipt)
    assert receipt == {"job_run_id": str(a.job_run_id), "resource_id": str(a.resource_id),
                       "run_state": "queued", "poll_operation_id": "getJobRun"}
    wire = store.job_run_wire(a.job_run_id)
    contracts.check("JobRun", wire)
    assert (wire["lease_epoch"], wire["attempt_count"], wire["run_state"]) == (1, 0, "queued")
    # restart: a brand-new connection answers from persisted state only
    conn.commit()
    c2 = db.connect(dsn)
    assert PgJobStore(c2).receipt(a.job_run_id) == receipt
    c2.close()
    with pytest.raises(NotFound):
        store.get(uuid4())


def test_claim_heartbeat_succeed_records_fenced_result(conn):
    d = definition(conn)
    store = PgJobStore(conn)
    run = enqueue(store, d)
    claimed = store.claim(job_id=d.job_id)
    assert (claimed.job_run_id, claimed.run_state, claimed.lease_epoch, claimed.attempt_count) == (run.job_run_id, "running", 2, 1)
    assert store.claim(job_id=d.job_id) is None                          # nothing else to claim
    hb = store.heartbeat(claimed.job_run_id, 2)
    assert hb.heartbeat_at >= claimed.heartbeat_at
    done = store.succeed(claimed.job_run_id, 2, output_ref="file:///out", output_hash=h("out"))
    assert done.run_state == "succeeded" and done.completed_at is not None
    row = conn.execute("SELECT result_lease_epoch, output_ref FROM ops.job_run_resource WHERE job_run_id=%s",
                       (run.job_run_id,)).fetchone()
    assert row == {"result_lease_epoch": 2, "output_ref": "file:///out"}
    contracts.check("JobRun", store.job_run_wire(run.job_run_id))


def test_expired_worker_cannot_overwrite_newer_result(conn):
    d = definition(conn, timeout=30)
    store = PgJobStore(conn)
    run = enqueue(store, d)
    t0 = now()
    a = store.claim(job_id=d.job_id, now=t0)
    assert a.lease_epoch == 2
    assert store.claim(job_id=d.job_id, now=t0 + timedelta(seconds=29)) is None      # lease still valid
    b = store.claim(job_id=d.job_id, now=t0 + timedelta(seconds=31))                  # lease expired -> new owner
    assert (b.job_run_id, b.lease_epoch, b.attempt_count) == (run.job_run_id, 3, 2)
    conn.commit()
    # the old worker wakes up: every write with epoch 2 is fenced out
    with pytest.raises(FencedOut):
        store.heartbeat(run.job_run_id, 2)
    with pytest.raises(FencedOut):
        store.succeed(run.job_run_id, 2, output_ref="file:///stale", output_hash=h("stale"))
    with pytest.raises(FencedOut):
        store.fail(run.job_run_id, 2, error_code="LATE")
    conn.rollback()
    store.succeed(run.job_run_id, 3, output_ref="file:///fresh", output_hash=h("fresh"))
    # even a straggler arriving after completion cannot touch the terminal run
    with pytest.raises(FencedOut):
        store.succeed(run.job_run_id, 2, output_ref="file:///stale", output_hash=h("stale"))
    conn.rollback()
    out = conn.execute("SELECT result_lease_epoch, output_ref FROM ops.job_run_resource WHERE job_run_id=%s",
                       (run.job_run_id,)).fetchone()
    assert out == {"result_lease_epoch": 3, "output_ref": "file:///fresh"}


def test_database_itself_fences_when_repository_is_bypassed(conn):
    d = definition(conn)
    store = PgJobStore(conn)
    run = enqueue(store, d)
    store.claim(job_id=d.job_id)
    store.succeed(run.job_run_id, 2, output_ref="file:///o", output_hash=h("o"))
    conn.commit()
    for sql in ("UPDATE ops.job_run SET run_state='running' WHERE job_run_id=%s",
                "UPDATE ops.job_run SET error_code='x' WHERE job_run_id=%s",
                "UPDATE ops.job_run_resource SET output_ref='file:///other' WHERE job_run_id=%s"):
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute(sql, (run.job_run_id,))
        conn.rollback()
    with pytest.raises(psycopg.errors.RaiseException, match="permanent"):
        conn.execute("DELETE FROM ops.job_run_resource WHERE job_run_id=%s", (run.job_run_id,))
    conn.rollback()
    # a stale lease cannot register a result on a run that moved to a newer epoch
    r2 = enqueue(store, d, key="second")
    store.claim(job_id=d.job_id, now=now())
    conn.commit()
    with pytest.raises(psycopg.errors.RaiseException, match="does not own"):
        conn.execute("UPDATE ops.job_run_resource SET result_lease_epoch=2, output_ref='x', output_hash=%s,"
                     " result_recorded_at=now() WHERE job_run_id=%s", (h("x"), r2.job_run_id))
    conn.rollback()
    with pytest.raises(psycopg.errors.RaiseException, match="terminal"):
        conn.execute("UPDATE ops.job_run SET run_state='queued' WHERE job_run_id=%s", (run.job_run_id,))
    conn.rollback()
    r3 = enqueue(store, d, key="third")
    conn.commit()
    with pytest.raises(psycopg.errors.RaiseException, match="Illegal job run transition"):
        conn.execute("UPDATE ops.job_run SET run_state='succeeded', completed_at=now() WHERE job_run_id=%s",
                     (r3.job_run_id,))
    conn.rollback()
    with pytest.raises(psycopg.errors.RaiseException, match="new lease epoch"):
        conn.execute("UPDATE ops.job_run SET run_state='running', attempt_count=attempt_count+1 WHERE job_run_id=%s",
                     (r3.job_run_id,))
    conn.rollback()


def test_retryable_failure_requeues_until_budget_then_fails(conn):
    d = definition(conn, retry={"max_attempts": 2})
    store = PgJobStore(conn)
    run = enqueue(store, d)
    a = store.claim(job_id=d.job_id)
    assert store.fail(run.job_run_id, a.lease_epoch, error_code="TIMEOUT", retryable=True).run_state == "queued"
    b = store.claim(job_id=d.job_id)
    assert (b.lease_epoch, b.attempt_count) == (3, 2)
    end = store.fail(run.job_run_id, b.lease_epoch, error_code="TIMEOUT", retryable=True)     # budget spent
    assert end.run_state == "failed" and end.error_code == "TIMEOUT" and end.completed_at is not None
    assert store.claim(job_id=d.job_id) is None


def test_vanished_worker_exhausts_budget_as_lease_expired(conn):
    d = definition(conn, timeout=10, retry={"max_attempts": 1})
    store = PgJobStore(conn)
    run = enqueue(store, d)
    t0 = now()
    store.claim(job_id=d.job_id, now=t0)
    assert store.claim(job_id=d.job_id, now=t0 + timedelta(seconds=11)) is None
    final = store.get(run.job_run_id)
    assert (final.run_state, final.error_code) == ("failed", "LEASE_EXPIRED")


def test_concurrency_key_limits_running_runs(conn):
    d = definition(conn, max_concurrency=1)
    store = PgJobStore(conn)
    r1, r2 = enqueue(store, d, key="a"), enqueue(store, d, key="b")
    first = store.claim(job_id=d.job_id)
    assert store.claim(job_id=d.job_id) is None                          # concurrency_key saturated
    store.succeed(first.job_run_id, first.lease_epoch, output_ref="o", output_hash=h("o"))
    second = store.claim(job_id=d.job_id)
    assert {first.job_run_id, second.job_run_id} == {r1.job_run_id, r2.job_run_id}


def test_disabled_definition_is_not_claimed(conn):
    d = definition(conn, enabled=False)
    store = PgJobStore(conn)
    enqueue(store, d)
    assert store.claim(job_id=d.job_id) is None


def test_cancel_queued_and_running(conn):
    d = definition(conn)
    store = PgJobStore(conn)
    q = enqueue(store, d, key="q")
    assert store.cancel(q.job_run_id).run_state == "cancelled"
    assert store.cancel(q.job_run_id).run_state == "cancelled"           # idempotent
    run = enqueue(store, d, key="r")
    c = store.claim(job_id=d.job_id)
    assert c.job_run_id == run.job_run_id
    store.cancel(run.job_run_id)
    with pytest.raises(FencedOut):
        store.succeed(run.job_run_id, c.lease_epoch, output_ref="o", output_hash=h("o"))
    conn.rollback()


def test_two_concurrent_claimers_get_distinct_runs(dsn):
    c0 = db.connect(dsn)
    d = definition(c0)
    store0 = PgJobStore(c0)
    runs = [enqueue(store0, d, key=f"k{i}") for i in range(2)]
    c0.commit()
    c0.close()
    got, errors = [], []
    barrier = threading.Barrier(4)

    def claimer():
        c = db.connect(dsn)
        try:
            barrier.wait()
            r = PgJobStore(c).claim(job_id=d.job_id)
            c.commit()
            got.append(r)
        except Exception as e:      # noqa: BLE001
            errors.append(e)
        finally:
            c.close()

    ts = [threading.Thread(target=claimer) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors, errors
    won = [r.job_run_id for r in got if r is not None]
    assert sorted(won) == sorted(r.job_run_id for r in runs)             # each run claimed exactly once
