"""Real-PostgreSQL tests of the fill-research lane over HTTP: playbook board (fitness / verdict / out-of-sample average / sparkline), custom run (draft +
queued job + worker), monthly returns, factor daily IC / long-short series, analog study on demand. SYNTHETIC world (I1): proves wiring, point-in-time
discipline and consistency with the stored experiment metrics, not market facts."""
from __future__ import annotations

import uuid
from decimal import Decimal as D

import pytest

from pulse import contracts
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db
from pulse.application import bf_fillr_service as F

pytestmark = pytest.mark.pg
URL = "/v1/x/bf/fillr"
SID = contracts.backfill_id("fillr")
PLAYBOOKS = ["PB:kdj_golden_low", "PB:turtle_55"]
VERDICTS = {"effective", "regime_dependent", "weak", "overfit", "insufficient"}
H = lambda k: {"Idempotency-Key": k}  # noqa: E731


@pytest.fixture(scope="module")
def runs(world):
    from pulse.workers import research as W

    c = db.connect(world["dsn"], autocommit=True)
    lake = LakeStore(world["lake_root"])
    W.ext_b_register(c)
    out = W.ext_b_run_catalog(c, lake, [*PLAYBOOKS, "PB:value_pe_pb", "FL:momentum_20"], key="fillr")
    yield {r["catalog_id"]: r["result"] for r in out}, c, lake
    c.close()


def data(r, status=200):
    assert r.status_code == status, r.text
    return r.json()["data"]


def metric(conn, eid, name, sl="overall"):
    row = conn.execute("select value from research.metric_value where experiment_id=%s and metric_name=%s and slice_key=%s", (eid, name, sl)).fetchone()
    return None if row is None else row["value"]


def drain(conn, lake, kinds=None):
    done = []
    while True:
        r = F.execute_pending(conn, lake)
        if r is None:
            return done
        done.append(r)


# ---- board (#16 #82 #150) ----------------------------------------------------------------------------------------------------------------------
def test_board_lists_every_playbook_with_a_state_and_validates_against_the_contract(client, runs):
    d = data(client.get(f"{URL}/playbook-board"))
    contracts.check("PlaybookBoard", d, SID)
    assert d["counts"]["total"] == len(d["items"]) == 52
    assert d["counts"]["evaluated"] == 2 and d["counts"]["total"] == sum(d["counts"][k] for k in ("evaluated", "not_run", "unavailable", "failed"))
    by = {i["catalog_id"]: i for i in d["items"]}
    assert by["PB:kdj_golden_low"]["state"] == by["PB:turtle_55"]["state"] == "evaluated"
    assert by["PB:value_pe_pb"]["state"] == "unavailable" and "fundamentals_pit" in by["PB:value_pe_pb"]["reason"]          # a data gap is stated, not scored
    nr = next(i for i in d["items"] if i["state"] == "not_run")
    assert nr["evaluation"] is None and nr["sparkline"] == [] and nr["reason"] == "no_completed_experiment"          # never a made-up verdict
    assert d["regime_now"]["available"] in (True, False) and any("样本外" in n for n in d["notes"])


def test_board_figures_agree_with_an_independent_recomputation_from_the_stored_replay(client, runs):
    res, conn, _ = runs
    d = data(client.get(f"{URL}/playbook-board"))
    for cid in PLAYBOOKS:
        item = next(i for i in d["items"] if i["catalog_id"] == cid)
        rep = data(client.get("/v1/x/bf/research/playbook-replay", params={"catalog_id": cid, "trades_limit": 500}))
        eid = res[cid]["experiment_id"]
        ev = item["evaluation"]
        assert item["experiment_id"] == str(eid)
        # the split is the sealed context's train_end: the first replay session on or after it
        train_end = conn.execute("select c.train_end from research.evaluation_context c join research.experiment_run r using (evaluation_context_id) where r.experiment_id=%s",
                                 (eid,)).fetchone()["train_end"]
        cutoff = train_end.astimezone(F.B.CST).date().isoformat()
        days = [p["d"] for p in rep["equity"]["points"]]
        split = next((x for x in days if x >= cutoff), None)
        assert ev["split_day"] == split and ev["sample_label"] == ("out_of_sample" if split else "in_sample_only")
        closed = [t for t in rep["trades"]["items"] if t["status"] == "closed"]
        assert rep["trades"]["shown"] == rep["trades"]["total"]                                                     # every trade is on the page
        oos = [D(t["gross_return"]) for t in closed if split and t["entry_day"] >= split]
        ins = [D(t["gross_return"]) for t in closed if split and t["entry_day"] < split]
        assert ev["full"]["trades"] == len(closed) and ev["in_sample"]["trades"] == len(ins) and ev["out_of_sample"]["trades"] == len(oos)
        if oos:
            assert abs(D(ev["out_of_sample"]["avg_return"]) - sum(oos) / len(oos)) < D("1e-9")
        else:
            assert ev["out_of_sample"]["avg_return"] is None                                                         # no trade, no average (not 0)
        if closed:
            assert abs(D(ev["full"]["avg_return"]) - sum(D(t["gross_return"]) for t in closed) / len(closed)) < D("1e-9")
            assert abs(D(ev["full"]["win_rate"]) - D(sum(1 for t in closed if D(t["gross_return"]) > 0)) / len(closed)) < D("1e-9")
        navs = [p for p in rep["equity"]["points"] if p["nav"] is not None]
        assert item["sparkline"][0] == navs[0]["nav"] and item["sparkline"][-1] == navs[-1]["nav"] and len(item["sparkline"]) <= 60
        assert item["sparkline_start"] == navs[0]["d"] and item["sparkline_end"] == navs[-1]["d"]
        # verdict discipline that does not restate the formula: few trades can never be "effective", the fitness cap then applies
        assert ev["verdict"] in VERDICTS and 0 <= D(ev["fitness"]) <= 100 and len(ev["fitness_reasons"]) >= 6
        if ev["full"]["trades"] < 30 or ev["out_of_sample"]["trades"] < 5:
            assert ev["verdict"] == "insufficient" and D(ev["fitness"]) <= 35
        # the recent window is the last 60 sessions of the replay
        assert ev["recent"]["sessions"] <= 60


def test_the_board_is_a_pure_read_and_stable(client, runs):
    _, conn, _ = runs
    before = conn.execute("select (select count(*) from ops.ext_artifact) a, (select count(*) from research.experiment_run) r, (select count(*) from ops.job_run) j").fetchone()
    a = data(client.get(f"{URL}/playbook-board"))
    b = data(client.get(f"{URL}/playbook-board"))
    after = conn.execute("select (select count(*) from ops.ext_artifact) a, (select count(*) from research.experiment_run) r, (select count(*) from ops.job_run) j").fetchone()
    assert before == after and a == b


# ---- monthly returns (#35) ---------------------------------------------------------------------------------------------------------------------
def test_monthly_returns_compound_to_the_replay_net_return(client, runs):
    res, conn, _ = runs
    d = data(client.get(f"{URL}/monthly-returns", params={"catalog_id": "PB:kdj_golden_low"}))
    contracts.check("MonthlyReturns", d, SID)
    rep = data(client.get("/v1/x/bf/research/playbook-replay", params={"catalog_id": "PB:kdj_golden_low"}))
    assert d["subject"]["experiment_id"] == str(res["PB:kdj_golden_low"]["experiment_id"])
    assert [(m["year"], m["month"]) for m in d["months"]] == sorted((m["year"], m["month"]) for m in d["months"])
    assert d["window"]["sessions"] == len(rep["equity"]["points"]) and sum(m["sessions"] for m in d["months"]) == d["window"]["sessions"]
    growth = D(1)
    for m in d["months"]:
        assert m["value"] is not None, m                                                       # the synthetic replay has a NAV on every session
        growth *= 1 + D(m["value"])
    assert abs((growth - 1) - D(rep["equity"]["net_return"])) < D("1e-8")
    assert abs(D(metric(conn, res["PB:kdj_golden_low"]["experiment_id"], "net_return")) - (growth - 1)) < D("1e-8")
    byexp = data(client.get(f"{URL}/monthly-returns", params={"experiment_id": str(res["PB:kdj_golden_low"]["experiment_id"])}))
    assert byexp["months"] == d["months"]
    assert client.get(f"{URL}/monthly-returns").status_code == 400
    assert client.get(f"{URL}/monthly-returns", params={"catalog_id": "PB:turtle_55", "experiment_id": str(uuid.uuid4())}).status_code == 400
    assert client.get(f"{URL}/monthly-returns", params={"catalog_id": "PB:value_pe_pb"}).status_code == 404            # no replay: no invented months
    assert client.get(f"{URL}/monthly-returns", params={"experiment_id": "x"}).status_code == 400
    assert client.get(f"{URL}/monthly-returns", params={"catalog_id": "PB:turtle_55", "bogus": "1"}).status_code == 400


# ---- custom run (#21 #87) ----------------------------------------------------------------------------------------------------------------------
def post_run(client, body, key, status=202):
    r = client.post(f"{URL}/custom-run", json=body, headers=H(key))
    assert r.status_code == status, r.text
    return r.json()["data"] if status == 202 else r.json()


def test_custom_run_registers_a_draft_queues_a_job_and_shows_queued_until_a_worker_runs(client, runs):
    res, conn, lake = runs
    body = {"playbook_id": "PB:kdj_golden_low", "overrides": {}, "label": "我的 KDJ 复刻"}
    rc = post_run(client, body, "cr-1")
    contracts.check("CustomRunReceipt", rc, SID)
    assert rc["receipt"]["run_state"] == "queued" and "pulse.workers.fillr run" in rc["worker_hint"]
    assert post_run(client, body, "cr-1") == rc                                                             # same key: same receipt, nothing registered twice
    assert conn.execute("select count(*) n from ops.job_run where job_id=%s", (F.FILLR_JOB,)).fetchone()["n"] == 1
    # the draft is a real, immutable, unvalidated strategy definition with lineage to the base playbook
    sid = rc["draft"]["strategy_id"]
    dfn = conn.execute("select display_name, specification from strategy.definition where strategy_id=%s", (sid,)).fetchone()
    assert dfn["display_name"] == "我的 KDJ 复刻" and dfn["specification"]["registration"]["production_permission"] is False and dfn["specification"]["evidence_label"] == "未检验"
    parent = conn.execute("select parent_strategy_id from research.strategy_parent where strategy_id=%s", (sid,)).fetchone()
    base = conn.execute("select d.strategy_id from strategy.definition d where d.specification->>'catalog_id'='PB:kdj_golden_low'").fetchone()
    assert parent["parent_strategy_id"] == base["strategy_id"]
    # nothing ran: the API only queued it, so the page must say queued
    d = data(client.get(f"{URL}/custom-runs/{rc['receipt']['job_run_id']}"))
    contracts.check("CustomRun", d, SID)
    assert d["run"]["run_state"] == "queued" and d["result"] is None and d["result_ref"] is None and d["run"]["summary"] is None
    lst = data(client.get(f"{URL}/custom-runs"))
    assert [i["job_run_id"] for i in lst["items"]] == [rc["receipt"]["job_run_id"]] and lst["items"][0]["run_state"] == "queued"

    done = drain(conn, lake)
    assert [(k, s) for _, k, s in done] == [("custom_run", "succeeded")]
    d = data(client.get(f"{URL}/custom-runs/{rc['receipt']['job_run_id']}"))
    contracts.check("CustomRun", d, SID)
    r = d["result"]
    assert d["run"]["run_state"] == "succeeded" and r["exploratory"] is True and d["result_ref"].startswith("fillr-custom-result:")
    # no override: the run IS the experiment (same evaluator, same sealed context) -> identical stored metrics
    eid = res["PB:kdj_golden_low"]["experiment_id"]
    got = {(m["name"], m["slice"]): m for m in r["metrics"]}
    for name, sl in (("net_return", "overall"), ("net_return", "validation"), ("max_drawdown", "overall"), ("trade_count", "overall")):
        want = metric(conn, eid, name, sl)
        assert (got[(name, sl)]["value"] is None) == (want is None)
        if want is not None:
            assert abs(D(got[(name, sl)]["value"]) - D(want)) < D("1e-9"), (name, sl)
    board_item = next(i for i in data(client.get(f"{URL}/playbook-board"))["items"] if i["catalog_id"] == "PB:kdj_golden_low")
    assert r["evaluation"]["full"] == board_item["evaluation"]["full"] and r["evaluation"]["split_day"] == board_item["evaluation"]["split_day"]
    assert r["effective"] == r["base"] and any("探索性" in c for c in r["caveats"])
    assert d["run"]["summary"]["trades"] == r["trades"]["total"] and d["run"]["strategy"]["strategy_id"] == sid


def test_custom_run_overrides_change_the_rules_and_the_result(client, runs):
    res, conn, lake = runs
    ov = {"holding_days": 3, "stop_loss_pct": 4.0, "max_positions": 3}
    rc = post_run(client, {"playbook_id": "turtle_55", "overrides": ov}, "cr-2")
    drain(conn, lake)
    d = data(client.get(f"{URL}/custom-runs/{rc['receipt']['job_run_id']}"))
    r = d["result"]
    assert r["request"] == ov and r["effective"]["holding_days"] == 3 and r["effective"]["stop_loss_pct"] == 4.0 and r["effective"]["max_positions"] == 3
    assert r["base"]["holding_days"] != 3 or r["base"]["max_positions"] != 3
    closed = [t for t in r["trades"]["items"] if t["status"] == "closed"]
    assert closed and all(t["exit_reason"] in {"holding_days", "stop_loss", "trailing_stop", "unknown"} for t in closed)       # only the declared exits can fire
    base_run = next(i for i in data(client.get(f"{URL}/playbook-board"))["items"] if i["catalog_id"] == "PB:turtle_55")["evaluation"]
    assert r["evaluation"]["full"]["trades"] != base_run["full"]["trades"] or r["evaluation"]["full"]["avg_return"] != base_run["full"]["avg_return"]
    # a second principal sees neither the list entry nor the run
    other = uuid.uuid4()
    assert F.list_custom_runs(conn, other, 20, None)["items"] == []
    with pytest.raises(F.FillrError) as ei:
        F.get_custom_run(conn, other, uuid.UUID(rc["receipt"]["job_run_id"]))
    assert ei.value.status == 404


def test_custom_run_refusals(client, runs):
    res, conn, lake = runs
    ok = {"playbook_id": "PB:turtle_55", "overrides": {}}
    post_run(client, ok, "cr-3")
    r = client.post(f"{URL}/custom-run", json={**ok, "overrides": {"holding_days": 5}}, headers=H("cr-3"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"                    # same key, different request
    drafts = lambda: conn.execute("select count(*) n from strategy.definition where specification->'registration'->>'owner_principal_id' is not null").fetchone()["n"]  # noqa: E731
    jobs = lambda: conn.execute("select count(*) n from ops.job_run where job_id=%s", (F.FILLR_JOB,)).fetchone()["n"]  # noqa: E731
    d0, j0 = drafts(), jobs()
    bad = [({"playbook_id": "PB:turtle_55", "overrides": {"holding_days": 0}}, 400), ({"playbook_id": "PB:turtle_55", "overrides": {"max_positions": 51}}, 400),
           ({"playbook_id": "PB:turtle_55", "overrides": {"nonsense": 1}}, 400), ({"playbook_id": "PB:turtle_55", "overrides": {"params": {"nope": 1}}}, 400),
           ({"playbook_id": "PB:turtle_55", "overrides": {"params": {"min_margin": 99}}}, 400),
           ({"playbook_id": "PB:no_such", "overrides": {}}, 404), ({"playbook_id": "PB:value_pe_pb", "overrides": {}}, 422), ({"overrides": {}}, 400)]
    for i, (b, status) in enumerate(bad):
        rr = client.post(f"{URL}/custom-run", json=b, headers=H(f"cr-bad-{i}"))
        assert rr.status_code == status, (b, rr.status_code, rr.text)
    rr = client.post(f"{URL}/custom-run", json={**ok, "idempotency_key": "other"}, headers=H("cr-hdr"))
    assert rr.status_code == 400
    assert client.get(f"{URL}/custom-runs/{uuid.uuid4()}").status_code == 404 and client.get(f"{URL}/custom-runs/nope").status_code == 400
    assert client.get(f"{URL}/custom-runs", params={"limit": 0}).status_code == 400
    # refused requests registered no draft and queued nothing
    assert (drafts(), jobs()) == (d0, j0)


def test_a_failed_custom_run_keeps_its_error_code(client, runs, monkeypatch):
    res, conn, lake = runs
    rc = post_run(client, {"playbook_id": "PB:turtle_55", "overrides": {"holding_days": 7}}, "cr-fail")
    monkeypatch.setattr(F, "run_custom", lambda *a, **k: (_ for _ in ()).throw(F.FillrError(409, "NO_SEALED_CONTEXT", "x")))
    assert drain(conn, lake)[0][2] == "failed"
    d = data(client.get(f"{URL}/custom-runs/{rc['receipt']['job_run_id']}"))
    assert d["run"]["run_state"] == "failed" and d["run"]["error_code"] == "NO_SEALED_CONTEXT" and d["result"] is None


# ---- factor series (#37 #80) -------------------------------------------------------------------------------------------------------------------
def test_factor_series_is_queued_then_matches_the_stored_experiment_metrics(client, runs):
    res, conn, lake = runs
    eid = res["FL:momentum_20"]["experiment_id"]
    s = data(client.get(f"{URL}/factor-series", params={"factor_id": "FL:momentum_20"}))
    contracts.check("FactorSeriesStatus", s, SID)
    assert s["state"] == "not_computed" and s["experiment_id"] == str(eid) and s["series"] is None
    q = data(client.post(f"{URL}/factor-series", json={"experiment_id": str(eid)}, headers=H("fs-1")), 202)
    contracts.check("JobQueued", q, SID)
    assert q["state"] == "queued" and q["receipt"]["run_state"] == "queued" and "pulse.workers.fillr run" in q["worker_hint"]
    again = data(client.post(f"{URL}/factor-series", json={"experiment_id": str(eid)}, headers=H("fs-2")), 202)
    assert again["receipt"]["job_run_id"] == q["receipt"]["job_run_id"]                                       # one job per experiment, whoever asks
    assert data(client.get(f"{URL}/factor-series", params={"experiment_id": str(eid)}))["state"] == "queued"
    assert [(k, st) for _, k, st in drain(conn, lake)] == [("factor_series", "succeeded")]
    s = data(client.get(f"{URL}/factor-series", params={"experiment_id": str(eid)}))
    contracts.check("FactorSeriesStatus", s, SID)
    assert s["state"] == "ready" and len(s["series"]["items"]) == 4
    for it in s["series"]["items"]:
        h, sl = it["horizon"], it["slice"]
        ics = [D(x["ic"]) for x in it["ic"] if x["ic"] is not None]
        assert it["days"] == len(ics) == int(metric(conn, eid, f"ic_days_h{h}", sl))                          # one IC per usable date, same dates as the metric
        mean = metric(conn, eid, f"rank_ic_mean_h{h}", sl)
        if mean is None:
            assert it["ic_mean"] is None
        else:
            assert abs(D(it["ic_mean"]) - D(mean)) < D("1e-9") and abs(sum(ics) / len(ics) - D(mean)) < D("1e-8")
        ls = metric(conn, eid, f"long_short_return_h{h}", sl)
        if ls is None:
            assert it["long_short"] == [] and it["long_short_return"] is None
        else:
            assert abs(D(it["long_short"][-1]["cumulative"]) - D(ls)) < D("1e-9")                              # the curve ends at the stored long-short return
            g = D(1)
            for p in it["long_short"]:
                g *= 1 + D(p["period_return"])
                assert abs((g - 1) - D(p["cumulative"])) < D("1e-8")                                          # and compounds the period returns
            assert [p["d"] for p in it["long_short"]] == sorted(p["d"] for p in it["long_short"])
        assert [x["d"] for x in it["ic"]] == sorted(x["d"] for x in it["ic"])
    ready = data(client.post(f"{URL}/factor-series", json={"experiment_id": str(eid)}, headers=H("fs-3")), 202)
    assert ready["state"] == "ready" and ready["receipt"] is None
    assert drain(conn, lake) == []


def test_factor_series_refusals(client, runs):
    res, conn, lake = runs
    pb = str(res["PB:turtle_55"]["experiment_id"])
    assert client.post(f"{URL}/factor-series", json={"experiment_id": pb}, headers=H("fs-pb")).status_code == 400          # not a factor experiment
    assert client.post(f"{URL}/factor-series", json={"experiment_id": str(uuid.uuid4())}, headers=H("fs-none")).status_code == 404
    assert client.post(f"{URL}/factor-series", json={"experiment_id": "x"}, headers=H("fs-bad")).status_code == 400
    assert client.get(f"{URL}/factor-series").status_code == 400
    assert client.get(f"{URL}/factor-series", params={"experiment_id": str(uuid.uuid4())}).status_code == 404
    assert data(client.get(f"{URL}/factor-series", params={"factor_id": "FL:reversal_5"}))["state"] == "no_experiment"      # registered, never run


def test_enqueue_factor_series_command_queues_only_what_is_missing(runs, world):
    from pulse.workers import fillr as W

    _, conn, lake = runs
    assert W.run(["--dsn", world["dsn"], "enqueue-factor-series"]) == 0                                    # the series exists already: nothing to queue
    assert drain(conn, lake) == []
    assert W.run(["--dsn", world["dsn"], "run", "--max", "3"]) == 0 and W.run(["--dsn", world["dsn"], "show"]) == 0


# ---- analog study (#42 #68 #158) ----------------------------------------------------------------------------------------------------------------
def test_analog_study_on_demand(client, runs, built, monkeypatch):
    res, conn, lake = runs
    rel = conn.execute("select release_id from decision.release where release_state in ('READY','SUPERSEDED') order by published_at desc limit 1").fetchone()["release_id"]
    iid = conn.execute("select a.instrument_id from core.instrument_alias a where a.source_id='legacy_warehouse' and a.source_symbol='600011'").fetchone()["instrument_id"]
    pol = "analog-knn-matured-v1:h5"
    q = {"instrument_id": str(iid), "release_id": str(rel), "selection_policy_version": pol}
    s = data(client.get(f"{URL}/analog-study", params=q))
    contracts.check("AnalogStatus", s, SID)
    assert s["state"] == "not_built" and s["study"] is None and s["job"] is None
    cases = {"instrument_id": str(iid), "release_id": str(rel), "feature_key_hash": s["feature_key_hash"], "selection_policy_version": pol, "page": '{"limit":25}'}
    assert client.get("/v1/x/query-analog-cases", params=cases).status_code == 409                          # the read never builds
    body = {"instrument_id": str(iid), "release_id": str(rel), "selection_policy_version": pol}
    # a failing build is a failed job with its error code; asking again makes a NEW attempt (the failed one is not replayed forever)
    real = F.run_analog
    monkeypatch.setattr(F, "run_analog", lambda *a, **k: (_ for _ in ()).throw(F.FillrError(422, "UNSUPPORTED_CONTEXT_SHAPE", "x")))
    first = data(client.post(f"{URL}/analog-study", json=body, headers=H("an-0")), 202)
    assert first["state"] == "queued"
    assert drain(conn, lake)[0][2] == "failed"
    s = data(client.get(f"{URL}/analog-study", params=q))
    assert s["state"] == "failed" and s["job"]["error_code"] == "UNSUPPORTED_CONTEXT_SHAPE"
    monkeypatch.setattr(F, "run_analog", real)
    rc = data(client.post(f"{URL}/analog-study", json=body, headers=H("an-1")), 202)
    contracts.check("JobQueued", rc, SID)
    assert rc["state"] == "queued" and rc["receipt"]["job_run_id"] != first["receipt"]["job_run_id"]
    assert data(client.post(f"{URL}/analog-study", json=body, headers=H("an-2")), 202)["receipt"]["job_run_id"] == rc["receipt"]["job_run_id"]    # one job per study
    assert data(client.get(f"{URL}/analog-study", params=q))["state"] == "queued"
    assert [(k, st) for _, k, st in drain(conn, lake)] == [("analog_study", "succeeded")]
    s = data(client.get(f"{URL}/analog-study", params=q))
    assert s["state"] == "ready" and s["study"]["neighbors"] > 0 and s["study"]["horizon_sessions"] == 5
    r = client.get("/v1/x/query-analog-cases", params=cases)
    assert r.status_code == 200 and len(r.json()["data"]["neighbors"]) > 0                                   # the existing read now serves the built study
    done = data(client.post(f"{URL}/analog-study", json=body, headers=H("an-3")), 202)
    assert done["state"] == "ready" and done["receipt"] is None
    assert client.post(f"{URL}/analog-study", json={**body, "instrument_id": str(uuid.uuid4())}, headers=H("an-4")).status_code == 404
    assert client.post(f"{URL}/analog-study", json={**body, "selection_policy_version": "pick-best-outcome"}, headers=H("an-5")).status_code == 400
    assert client.get(f"{URL}/analog-study", params={"instrument_id": str(iid)}).status_code == 400


# ---- access ------------------------------------------------------------------------------------------------------------------------------------
def test_every_route_needs_a_session(world, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PULSE_DSN", world["dsn"])
    monkeypatch.setenv("PULSE_LAKE", str(world["lake_root"]))
    monkeypatch.setenv("PULSE_API_HOST", "127.0.0.1")
    monkeypatch.delenv("PULSE_DEV_PRINCIPAL", raising=False)
    monkeypatch.delenv("PULSE_ENV", raising=False)
    from pulse.api import deps
    from pulse.api.app import create_app

    deps.close_pool()
    with TestClient(create_app()) as c:
        for path in ("playbook-board", "custom-runs", "monthly-returns?catalog_id=PB:x", "factor-series?factor_id=FL:x", "analog-study"):
            assert c.get(f"{URL}/{path}").status_code == 403, path
        for path in ("custom-run", "factor-series", "analog-study"):
            assert c.post(f"{URL}/{path}", json={}, headers=H("k")).status_code == 403, path
    deps.close_pool()
