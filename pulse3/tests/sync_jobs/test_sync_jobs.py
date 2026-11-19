"""Real-PostgreSQL tests of the sync-job lane: enqueue, duplicate refusal, bounds, worker with a FAKE runner (no network), status transitions,
API authorisation. Offline: the default ingest runner is never called here; its argv construction is checked as a pure function."""
from __future__ import annotations

import uuid
from datetime import date

import pytest

from pulse import contracts
from pulse.application import sync_job_service as S
from pulse.workers import sync_jobs as W

pytestmark = pytest.mark.pg
URL = "/v1/x/bf/sync/jobs"
SID = contracts.backfill_id("sync")
DAILY = {"kind": "sync_daily_bars", "parameters": {"codes": ["000001", "600000"]}}
LEGACY = {"kind": "sync_legacy_import", "parameters": {"since_days": 30}}
RECON = {"kind": "sync_reconcile", "parameters": {"codes": ["000001"], "last": 3}}


def post(c, body, key=None):
    return c.post(URL, json=body, headers={"Idempotency-Key": key or uuid.uuid4().hex})


def rows(conn):
    return conn.execute("SELECT l.*, r.run_state, r.error_code FROM ops.sync_job_log l JOIN ops.job_run r USING (job_run_id)"
                        " ORDER BY l.requested_at").fetchall()


class Fake:
    """Stands in for the ingest runner: records its calls, never touches the network."""

    def __init__(self, code=0, text="ok", boom=None):
        self.calls, self.code, self.text, self.boom = [], code, text, boom

    def __call__(self, kind, params):
        self.calls.append((kind, params))
        if self.boom:
            raise self.boom
        return S.Outcome(self.code, self.text)


# ---- enqueue + list ------------------------------------------------------------------------------------------------------
def test_enqueue_records_parameters_and_lists_them_as_queued(client):
    r = post(client, DAILY)
    assert r.status_code == 202, r.text
    d = r.json()["data"]
    contracts.check("SyncJobReceipt", d, SID)
    assert d["receipt"]["run_state"] == "queued" and d["kind"] == "sync_daily_bars"
    assert d["parameters"] == {"source": "sina", "count": 320, "codes": ["000001", "600000"]}
    lst = client.get(URL).json()["data"]
    contracts.check("SyncJobList", lst, SID)
    [item] = lst["items"]
    assert item["run_state"] == "queued" and item["started_at"] is None and item["exit_status"] is None and item["summary"] is None
    assert lst["bounds"]["max_codes"] == S.MAX_CODES and "run --max 1" in lst["worker_hint"] and lst["executor_enabled"] is True


def test_same_idempotency_key_returns_the_same_run_and_is_not_a_duplicate(client, conn):
    a = post(client, DAILY, "k1").json()["data"]["receipt"]["job_run_id"]
    b = post(client, DAILY, "k1")
    assert b.status_code == 202 and b.json()["data"]["receipt"]["job_run_id"] == a
    assert len(rows(conn)) == 1


def test_a_second_run_of_the_same_kind_is_refused_while_one_is_queued_or_running(client, conn):
    assert post(client, DAILY).status_code == 202
    r = post(client, {**DAILY, "parameters": {"codes": ["000002"]}})
    assert r.status_code == 409 and r.json()["error"]["code"] == "SYNC_ALREADY_QUEUED"
    assert len(rows(conn)) == 1
    assert post(client, LEGACY).status_code == 202            # other kinds are independent
    conn.execute("UPDATE ops.job_run SET run_state='running', heartbeat_at=now(), lease_epoch=2, attempt_count=1, started_at=now()"
                 " WHERE job_id=%s AND run_state='queued'", (S.JOB_IDS["sync_daily_bars"],))
    assert post(client, DAILY).status_code == 409             # a running run blocks as well


def test_a_new_run_is_allowed_after_the_previous_one_finished(client, conn):
    assert post(client, RECON).status_code == 202
    assert S.execute_pending(conn, Fake()) is not None
    assert post(client, RECON).status_code == 202
    assert len(rows(conn)) == 2


# ---- bounds ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("body", [
    {"kind": "sync_daily_bars", "parameters": {"codes": [f"{i:06d}" for i in range(S.MAX_CODES + 1)]}},
    {"kind": "sync_daily_bars", "parameters": {"codes": ["000001"], "universe": True}},
    {"kind": "sync_daily_bars", "parameters": {}},
    {"kind": "sync_daily_bars", "parameters": {"codes": ["000001"], "count": S.MAX_COUNT + 1}},
    {"kind": "sync_daily_bars", "parameters": {"codes": ["000001"], "source": "eastmoney"}},
    {"kind": "sync_daily_bars", "parameters": {"codes": ["12345"]}},
    {"kind": "sync_legacy_import", "parameters": {"since_days": S.MAX_SINCE_DAYS + 1}},
    {"kind": "sync_legacy_import", "parameters": {"since_days": 0}},
    {"kind": "sync_legacy_import", "parameters": {"since_days": 5, "warehouse": "C:/x"}},
    {"kind": "sync_reconcile", "parameters": {"last": 3}},
    {"kind": "sync_reconcile", "parameters": {"codes": ["000001"], "last": S.MAX_LAST + 1}},
    {"kind": "sync_everything", "parameters": {}},
])
def test_out_of_bounds_parameters_are_refused_and_nothing_is_queued(client, conn, body):
    r = post(client, body)
    assert r.status_code == 400, r.text
    assert rows(conn) == []


def test_universe_is_a_valid_daily_request(client):
    r = post(client, {"kind": "sync_daily_bars", "parameters": {"universe": True, "source": "tencent", "count": 100}})
    assert r.status_code == 202 and r.json()["data"]["parameters"] == {"source": "tencent", "count": 100, "universe": True}


def test_service_validation_is_independent_of_the_json_contract():
    with pytest.raises(S.SyncError):
        S.validate("sync_daily_bars", {"codes": ["000001"], "count": True})       # a bool is not an int
    with pytest.raises(S.SyncError):
        S.validate("sync_legacy_import", {})
    assert S.validate("sync_legacy_import", {"since_days": 7, "codes": ["600000", "600000"]})["codes"] == ["600000"]


def test_ingest_argv_maps_every_kind_onto_the_ingest_command():
    today = date(2026, 11, 19)
    assert S.ingest_argv("sync_daily_bars", {"codes": ["000001"], "source": "both", "count": 50}, today) == ["live-daily", "--source", "both", "--count", "50", "--codes", "000001"]
    assert S.ingest_argv("sync_daily_bars", {"universe": True, "source": "sina", "count": 320}, today)[-1] == "--universe"
    assert S.ingest_argv("sync_legacy_import", {"since_days": 30, "boards": "main"}, today) == ["legacy-import", "--since", "2026-10-20", "--boards", "main"]
    assert S.ingest_argv("sync_reconcile", {"codes": ["000001", "600000"], "last": 5}, today) == ["reconcile", "--last", "5", "--codes", "000001", "600000"]


# ---- worker ---------------------------------------------------------------------------------------------------------------
def test_worker_runs_a_queued_job_and_records_start_end_exit_and_summary(client, conn):
    post(client, LEGACY)
    fake = Fake(0, "== legacy-import ==\n  bars: 12\nhttp_requests: 0")
    done = S.execute_pending(conn, fake)
    assert done is not None and done[1:] == ("sync_legacy_import", "succeeded")
    assert fake.calls == [("sync_legacy_import", {"since_days": 30, "boards": "main"})]
    [row] = rows(conn)
    assert row["run_state"] == "succeeded" and row["exit_status"] == 0 and "bars: 12" in row["summary"]
    assert row["started_at"] is not None and row["finished_at"] >= row["started_at"] and row["error_code"] is None
    item = client.get(URL).json()["data"]["items"][0]
    contracts.check("SyncJob", item, SID)
    assert item["run_state"] == "succeeded" and item["summary"].startswith("== legacy-import")
    assert S.execute_pending(conn, fake) is None


@pytest.mark.parametrize("code,error", [(2, "SOURCE_BLOCKED"), (3, "SOURCE_UNREACHABLE"), (1, "EXIT_1")])
def test_a_nonzero_exit_fails_the_run_with_its_summary_and_is_not_retried(client, conn, code, error):
    post(client, DAILY)
    fake = Fake(code, "BLOCKED: circuit open")
    assert S.execute_pending(conn, fake)[2] == "failed"
    [row] = rows(conn)
    assert row["run_state"] == "failed" and row["error_code"] == error and row["exit_status"] == code and row["summary"] == "BLOCKED: circuit open"
    assert S.execute_pending(conn, fake) is None and len(fake.calls) == 1


def test_a_runner_exception_is_recorded_not_swallowed(client, conn):
    post(client, RECON)
    assert S.execute_pending(conn, Fake(boom=RuntimeError("disk full")))[2] == "failed"
    [row] = rows(conn)
    assert row["error_code"] == "RuntimeError" and "disk full" in row["summary"] and row["exit_status"] == 1


def test_long_summaries_are_clipped_to_the_bound():
    out = S._clip("x" * (S.MAX_SUMMARY_CHARS * 3))
    assert len(out) < S.MAX_SUMMARY_CHARS + 100 and "chars cut" in out


def test_worker_command_honours_max_and_only_claims_sync_jobs(client, conn, dsn):
    for body in (DAILY, LEGACY, RECON):
        post(client, body)
    fake = Fake()
    assert W.run(["--dsn", dsn, "run", "--max", "2"], fake) == 0
    assert sorted(r["run_state"] for r in rows(conn)) == ["queued", "succeeded", "succeeded"] and len(fake.calls) == 2
    # a queued run of a foreign job is invisible to this worker
    from pulse.application import ops_service

    ops_service.ensure_jobs(conn)
    foreign = next(iter(ops_service.DEFINITIONS.values()))
    conn.execute("INSERT INTO ops.job_run(job_run_id, job_id, job_version, idempotency_key, parameter_hash, scheduled_for, run_state, lease_epoch, attempt_count)"
                 " VALUES (%s,%s,1,'foreign',%s,now(),'queued',1,0)", (uuid.uuid4(), foreign.job_id, "0" * 64))
    assert W.run(["--dsn", dsn, "run", "--max", "5"], fake) == 0
    assert len(fake.calls) == 3
    assert conn.execute("SELECT count(*) AS n FROM ops.job_run WHERE job_id=%s AND run_state='queued'", (foreign.job_id,)).fetchone()["n"] == 1


# ---- authorisation --------------------------------------------------------------------------------------------------------
def test_anonymous_callers_are_refused_on_both_routes(anonymous, conn):
    assert post(anonymous, DAILY).status_code == 403
    assert anonymous.get(URL).status_code == 403
    assert rows(conn) == []


def test_a_session_without_ops_permission_is_forbidden(outsider, conn):
    r = post(outsider, DAILY)
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"
    assert outsider.get(URL).status_code == 403
    assert rows(conn) == []


def test_unknown_query_parameters_and_missing_idempotency_key_are_refused(client):
    assert client.get(URL, params={"bogus": 1}).status_code == 400
    assert client.post(URL, json=DAILY).status_code in (400, 422)


def test_default_runner_calls_the_ingest_command_and_captures_its_output(monkeypatch):
    from pulse.workers import ingest

    seen = []

    def fake_run(argv):
        seen.append(argv)
        print("== live-daily sina ==")
        return 2

    monkeypatch.setattr(ingest, "run", fake_run)
    out = S.ingest_runner("sync_reconcile", {"codes": ["000001"], "last": 2})
    assert seen == [["reconcile", "--last", "2", "--codes", "000001"]] and out == S.Outcome(2, "== live-daily sina ==")

    def exits(argv):
        raise SystemExit("no codes: pass --codes or import the research snapshot first")

    monkeypatch.setattr(ingest, "run", exits)
    out = S.ingest_runner("sync_daily_bars", {"universe": True, "source": "sina", "count": 5})
    assert out.exit_status == 1 and "no codes" in out.summary


# ---- executor switch (before the cutover nothing executes) ----------------------------------------------------------------
def test_submissions_are_refused_and_flagged_while_no_executor_is_enabled(client_no_executor, conn):
    lst = client_no_executor.get(URL).json()["data"]
    contracts.check("SyncJobList", lst, SID)
    assert lst["executor_enabled"] is False
    r = post(client_no_executor, DAILY)
    assert r.status_code == 409 and r.json()["error"]["code"] == "SYNC_EXECUTOR_DISABLED"
    assert rows(conn) == []


# ---- lease: a killed worker must not block its kind ---------------------------------------------------------------------
def test_worker_default_takes_one_job(client, conn, dsn):
    post(client, DAILY)
    post(client, LEGACY)
    fake = Fake()
    assert W.run(["--dsn", dsn, "run"], fake) == 0
    assert len(fake.calls) == 1


def test_heartbeat_renews_the_lease_while_the_runner_works(client, conn, dsn):
    import time

    from pulse.adapters.postgres import db

    post(client, RECON)
    beats = []

    def slow(kind, params):
        beats.append(conn.execute("SELECT heartbeat_at FROM ops.job_run WHERE job_id=%s AND run_state='running'", (S.JOB_IDS[kind],)).fetchone()["heartbeat_at"])
        time.sleep(0.6)
        beats.append(conn.execute("SELECT heartbeat_at FROM ops.job_run WHERE job_id=%s AND run_state='running'", (S.JOB_IDS[kind],)).fetchone()["heartbeat_at"])
        return S.Outcome(0, "done")

    done = S.execute_pending(conn, slow, connect=lambda: db.connect(dsn, autocommit=True), heartbeat_seconds=0.05)
    assert done is not None and done[2] == "succeeded"
    assert beats[1] > beats[0]


def test_a_dead_workers_run_stops_blocking_and_is_closed_as_lease_expired(client, conn):
    from pulse.adapters.postgres.jobs import PgJobStore

    assert post(client, DAILY).status_code == 202
    dead = PgJobStore(conn).claim(job_id=S.JOB_IDS["sync_daily_bars"])            # a worker claimed it ... and was killed
    assert dead is not None and post(client, DAILY).status_code == 409           # alive lease: still blocked
    conn.execute("UPDATE ops.job_run SET heartbeat_at = now() - make_interval(secs => %s) WHERE job_run_id=%s", (S.LEASE_SECONDS + 60, dead.job_run_id))
    assert post(client, {**DAILY, "parameters": {"codes": ["000002"]}}).status_code == 202   # lease expired: accepted
    fake = Fake()
    done = S.execute_pending(conn, fake)
    assert done is not None and done[0] != dead.job_run_id and done[2] == "succeeded"
    assert fake.calls == [("sync_daily_bars", {"source": "sina", "count": 320, "codes": ["000002"]})]     # the dead run is never re-run
    old = conn.execute("SELECT run_state, error_code FROM ops.job_run WHERE job_run_id=%s", (dead.job_run_id,)).fetchone()
    assert (old["run_state"], old["error_code"]) == ("failed", "LEASE_EXPIRED")
