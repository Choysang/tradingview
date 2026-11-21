"""sync_refdata: bounded parameters, the refdata steps of each mode, the default runner (fake CLI, no network), duplicate rule and the contract."""
from __future__ import annotations

import uuid

import pytest

from pulse import contracts
from pulse.application import sync_job_service as S

pytestmark = pytest.mark.pg
URL = "/v1/x/bf/sync/jobs"
SID = contracts.backfill_id("sync")


def post(c, parameters, key=None):
    return c.post(URL, json={"kind": "sync_refdata", "parameters": parameters}, headers={"Idempotency-Key": key or uuid.uuid4().hex})


def test_modes_are_validated_and_only_backfills_take_max_codes():
    assert S.validate("sync_refdata", {"mode": "daily"}) == {"mode": "daily"}
    assert S.validate("sync_refdata", {"mode": "weekly"}) == {"mode": "weekly"}
    assert S.validate("sync_refdata", {"mode": "adjusted_backfill"}) == {"mode": "adjusted_backfill", "max_codes": 300}
    assert S.validate("sync_refdata", {"mode": "raw_backfill", "max_codes": 5000}) == {"mode": "raw_backfill", "max_codes": 5000}
    for bad in ({}, {"mode": "everything"}, {"mode": "daily", "max_codes": 10}, {"mode": "adjusted_backfill", "max_codes": 5001},
                {"mode": "raw_backfill", "max_codes": 0}, {"mode": "raw_backfill", "max_codes": True}, {"mode": "daily", "years": 3}):
        with pytest.raises(S.SyncError):
            S.validate("sync_refdata", bad)


def test_each_mode_maps_to_the_steps_of_run_refdata_ps1():
    assert S.refdata_steps({"mode": "daily"}) == [("index-bars", ["index-bars", "--years", "3"]),
                                                  ("adjusted-refresh", ["adjusted-bars", "--mode", "refresh", "--universe-listings"])]
    assert S.refdata_steps({"mode": "weekly"}) == [("listings", ["listings"]), ("boards", ["boards"])]
    assert S.refdata_steps({"mode": "adjusted_backfill", "max_codes": 700}) == [("adjusted-backfill", ["adjusted-bars", "--universe-listings", "--max-codes", "700"])]
    assert S.refdata_steps({"mode": "raw_backfill", "max_codes": 50}) == [("raw-backfill", ["adjusted-bars", "--universe-listings", "--max-codes", "50", "--raw-only"])]


def test_default_runner_runs_the_steps_in_order_and_skips_after_a_block(monkeypatch):
    from pulse.workers import refdata

    seen = []

    def fake(codes):
        def run(argv):
            seen.append(argv[0])
            print(f"step {argv[0]}")
            return codes.get(argv[0], 0)
        return run

    monkeypatch.setattr(refdata, "run", fake({}))
    out = S.ingest_runner("sync_refdata", {"mode": "weekly"})
    assert seen == ["listings", "boards"] and out.exit_status == 0 and "== listings exit 0 ==" in out.summary and "step boards" in out.summary

    seen.clear()
    monkeypatch.setattr(refdata, "run", fake({"index-bars": 2}))
    out = S.ingest_runner("sync_refdata", {"mode": "daily"})
    assert seen == ["index-bars"] and out.exit_status == 2 and "adjusted-refresh skipped: an earlier step was blocked" in out.summary

    seen.clear()
    monkeypatch.setattr(refdata, "run", fake({"index-bars": 1, "adjusted-bars": 3}))
    out = S.ingest_runner("sync_refdata", {"mode": "daily"})
    assert seen == ["index-bars", "adjusted-bars"] and out.exit_status == 1            # the first non-zero code wins, a non-block lets later steps run


def test_enqueue_list_and_duplicate_rule_for_refdata(client, conn):
    r = post(client, {"mode": "raw_backfill", "max_codes": 1000})
    assert r.status_code == 202, r.text
    contracts.check("SyncJobReceipt", r.json()["data"], SID)
    assert r.json()["data"]["parameters"] == {"mode": "raw_backfill", "max_codes": 1000}
    dup = post(client, {"mode": "daily"})
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "SYNC_ALREADY_QUEUED"
    other = client.post(URL, json={"kind": "sync_reconcile", "parameters": {"codes": ["000001"]}}, headers={"Idempotency-Key": uuid.uuid4().hex})
    assert other.status_code == 202                                                      # kinds are independent
    lst = client.get(URL).json()["data"]
    contracts.check("SyncJobList", lst, SID)
    assert "sync_refdata" in lst["kinds"] and lst["bounds"]["max_refdata_codes"] == S.MAX_REFDATA_CODES and lst["defaults"]["sync_refdata"] == {"mode": "daily"}


@pytest.mark.parametrize("parameters", [{}, {"mode": "weekly", "max_codes": 5}, {"mode": "adjusted_backfill", "max_codes": 5001}, {"mode": "daily", "x": 1}])
def test_out_of_bounds_refdata_requests_are_refused_by_the_api(client, conn, parameters):
    assert post(client, parameters).status_code == 400
    assert conn.execute("SELECT count(*) AS n FROM ops.sync_job_log WHERE kind='sync_refdata'").fetchone()["n"] == 0


def test_worker_runs_a_queued_refdata_job_and_logs_it(client, conn):
    from pulse.application import sync_job_service as svc

    assert post(client, {"mode": "weekly"}).status_code == 202
    calls = []

    def fake(kind, params):
        calls.append((kind, params))
        return svc.Outcome(0, "== listings exit 0 ==")

    done = svc.execute_pending(conn, fake)
    assert done is not None and done[1:] == ("sync_refdata", "succeeded") and calls == [("sync_refdata", {"mode": "weekly"})]
    [row] = conn.execute("SELECT exit_status, summary FROM ops.sync_job_log WHERE kind='sync_refdata' ORDER BY requested_at DESC LIMIT 1").fetchall()
    assert row["exit_status"] == 0 and row["summary"] == "== listings exit 0 =="
