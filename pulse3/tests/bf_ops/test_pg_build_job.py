"""The release build as a registered manual job (#15, #92): runs through the queue and keeps every guard of build_release."""
from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import datetime, timezone

import pytest

from pulse.adapters.postgres import db
from pulse.application import bf_ops_service as BF
from pulse.application import ops_service
from pulse.adapters.postgres._canon import sha256_of

pytestmark = pytest.mark.pg
UTC = timezone.utc


def artifact(params: dict) -> dict:
    import json
    raw = json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert hashlib.sha256(raw).hexdigest() == sha256_of(params)
    return {"ref": "inline-json:" + base64.urlsafe_b64encode(raw).decode().rstrip("="), "content_hash": hashlib.sha256(raw).hexdigest(),
            "contract_id": "pulse.job_parameters", "schema_version": "1"}


def test_closed_parameter_set():
    p = BF.parse_job_params({})
    assert p == {"as_of": None, "top_n": 10, "availability": "observed"}
    assert BF.parse_job_params({"as_of": "2026-11-10"})["as_of"] == datetime(2026, 11, 10, 15, 59, 59, tzinfo=UTC)          # end of day, Asia/Shanghai
    assert BF.parse_job_params({"as_of": "2026-11-10T07:00:00Z", "availability": "reconstructed"})["availability"] == "reconstructed"
    for bad in ({"slot": "x"}, {"legacy_warehouse": "C:/x"}, {"top_n": 0}, {"top_n": True}, {"top_n": "5"}, {"availability": "magic"},
                {"availability": "reconstructed"}, {"as_of": "2026-11-10T07:00:00"}, {"as_of": "yesterday"}, {"as_of": 5}):
        with pytest.raises(BF.BadJobParameters):
            BF.parse_job_params(bad)


def test_job_is_registered_manual_and_listed_for_the_run_form(world, api_on, monkeypatch):
    me = uuid.uuid4()
    monkeypatch.setenv("PULSE_OPS_PRINCIPALS", str(me))
    api = api_on(world["dsn"], me)
    defs = api.get("/v1/x/c/job-definitions").json()["data"]["definitions"]
    d = next(x for x in defs if x["application_operation"] == "ReleaseService.build_release")
    assert d["manual_trigger"] is True and d["enabled"] is True and d["parameter_contract_id"] == "pulse.job_parameters"


def test_build_runs_through_the_queue_is_idempotent_and_every_guard_still_refuses(world, api_on, monkeypatch):
    me = uuid.uuid4()
    monkeypatch.setenv("PULSE_OPS_PRINCIPALS", str(me))
    monkeypatch.setenv("PULSE_LAKE", str(world["lake_root"]))
    monkeypatch.setenv("PULSE_LEGACY_WAREHOUSE", str(world["warehouse"]))
    api = api_on(world["dsn"], me)
    conn = db.connect(world["dsn"], autocommit=True)
    try:
        d = next(x for x in api.get("/v1/x/c/job-definitions").json()["data"]["definitions"] if x["application_operation"] == "ReleaseService.build_release")

        scheduled = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")      # one value: a replay must send the same request

        def submit(params: dict, key: str) -> dict:
            body = {"job_id": d["job_id"], "job_version": d["job_version"], "expected_definition_hash": d["definition_hash"], "parameter_artifact": artifact(params),
                    "input_snapshot_refs": [], "scheduled_for": scheduled}
            r = api.post("/v1/x/run-registered-job", body, key=key)
            assert r.status_code == 202, r.text
            return r.json()["data"]["receipt"]

        def run_one() -> dict:
            out = ops_service.run_next(conn, job_id=uuid.UUID(d["job_id"]))
            assert out is not None
            return out

        n_releases = lambda: conn.execute("SELECT count(*) n FROM decision.release").fetchone()["n"]  # noqa: E731
        before = n_releases()

        # 1. a normal run builds and publishes through the queue; the same key replays the receipt and runs nothing twice
        rc = submit({"top_n": 5}, "build-1")
        assert submit({"top_n": 5}, "build-1") == rc
        first = run_one()
        assert first["state"] == "succeeded", first
        res = first["result"]
        assert res["replayed"] is False and res["pointer_generation"] >= 1 and n_releases() == before + 1
        pub = conn.execute("SELECT slot_key FROM decision.release_publication WHERE release_id=%s", (res["release_id"],)).fetchone()
        assert pub["slot_key"] == "main"
        assert ops_service.run_next(conn, job_id=uuid.UUID(d["job_id"])) is None                       # the replayed submission queued nothing new
        assert api.c.get(f"/v1/jobs/{rc['job_run_id']}", cookies=api.cookies()).json()["data"]["run_state"] == "succeeded"

        # 2. a new key with the same inputs is an idempotent replay of the published release (build_release's own behaviour)
        submit({"top_n": 5}, "build-2")
        again = run_one()
        assert again["state"] == "succeeded" and again["result"]["replayed"] is True and again["result"]["release_id"] == res["release_id"]
        assert n_releases() == before + 1

        # 3. the raw-coverage guard still refuses: the run fails with the guard's class, nothing is published
        import pulse.application.release_service as RS
        monkeypatch.setattr(RS, "MIN_RAW_COVERAGE", 1.1)
        rc3 = submit({"top_n": 5}, "build-3")
        refused = run_one()
        assert refused["state"] == "failed" and "RawCoverageMissing" in refused["error"] and n_releases() == before + 1
        run = api.c.get(f"/v1/jobs/{rc3['job_run_id']}", cookies=api.cookies()).json()["data"]
        assert run["run_state"] == "failed" and run["error_code"] == "RawCoverageMissing"
        monkeypatch.setattr(RS, "MIN_RAW_COVERAGE", 0.5)

        # 4. the research-window guard still refuses
        real_window = RS.require_research_window
        monkeypatch.setattr(RS, "require_research_window", lambda research, n: (_ for _ in ()).throw(RS.RawCoverageMissing("window too short")))
        submit({"top_n": 5}, "build-4")
        assert run_one()["state"] == "failed" and n_releases() == before + 1
        monkeypatch.setattr(RS, "require_research_window", real_window)

        # 5. a parameter outside the closed set fails the run before any build starts
        submit({"slot_key": "elsewhere"}, "build-5")
        bad = run_one()
        assert bad["state"] == "failed" and "BadJobParameters" in bad["error"] and n_releases() == before + 1
    finally:
        conn.close()


def test_future_or_today_as_of_never_points_past_now():
    from datetime import timedelta

    now = datetime(2026, 11, 16, 8, 0, tzinfo=UTC)                     # 16:00 in Asia/Shanghai
    assert BF.parse_job_params({"as_of": "2026-11-16"}, now)["as_of"] == now            # today = now, not the coming midnight
    with pytest.raises(BF.BadJobParameters, match="future"):
        BF.parse_job_params({"as_of": (now + timedelta(minutes=1)).isoformat()}, now)
