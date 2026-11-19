"""Lab lane over HTTP on real PostgreSQL (the I1 synthetic world): ledger / retired-tool reads, tear sheet of a stored replay and of a paper book,
batch comparison as a queued job. Payloads are checked against ``backfill_lab.schema.json`` by the route (a violation would be a 500)."""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from statistics import mean, stdev

import pytest

from pulse import contracts
from pulse.adapters.postgres import db
from pulse.application import bf_lab_ledger as LED
from pulse.application import bf_lab_service as LAB
from pulse.application import portfolio_service as PORT
from pulse.application import screen_service as S
from pulse.application.bindings import RESEARCH_DATASET
from pulse.adapters.postgres import market_repo

pytestmark = pytest.mark.pg
URL = "/v1/x/bf/lab"
PRINCIPAL = uuid.UUID(int=7)
ASOF = "2026-11-12T00:00:00Z"
H = lambda k: {"Idempotency-Key": k}  # noqa: E731
SID = contracts.backfill_id("lab")


@pytest.fixture(scope="module")
def runs(world):
    from pulse.adapters.lake import LakeStore
    from pulse.workers import research as W

    c = db.connect(world["dsn"], autocommit=True)
    W.ext_b_register(c)
    out = W.ext_b_run_catalog(c, LakeStore(world["lake_root"]), ["PB:kdj_golden_low", "PB:turtle_55", "PB:value_pe_pb"], key="lab")
    yield {r["catalog_id"]: r["result"] for r in out}, c
    c.close()


@pytest.fixture(scope="module")
def ledger_imported(world):
    c = db.connect(world["dsn"], autocommit=True)
    LED.import_ledger(c)
    c.close()


def get(client, path, **q):
    return client.get(f"{URL}/{path}", params={k: v for k, v in q.items() if v is not None})


def data(r, status=200):
    assert r.status_code == status, r.text
    return r.json()["data"]


# ---- ledger over HTTP ------------------------------------------------------------------------------------------------------------------
def test_ledger_list_filters_and_entry_detail_over_http(client, ledger_imported):
    d = data(get(client, "ledger", limit=200))
    assert d["matched"] == 92 and len(d["entries"]) == 92 and d["ledger"]["ledger_sha256"] == LED.load_manifest()["versions"][0]["sha256"]
    assert d["counts"]["by_verdict"]["fails"] == 28
    f = data(get(client, "ledger", verdict="works", track="factor"))
    assert f["entries"] and all(e["verdict"] == "works" and e["track"] == "factor" for e in f["entries"])
    e = f["entries"][0]
    assert e["why"] and e["reactivate_when"] and isinstance(e["artifacts"], list)
    one = data(get(client, f"ledger/{e['id']}"))
    assert one["entry"]["id"] == e["id"] and one["ledger"]["entry_count"] == 92
    assert data(get(client, "ledger/E003"))["retired_families"][0]["family_id"] == "traditional_batteries"
    assert get(client, "ledger/E999").status_code == 404
    assert get(client, "ledger", verdict="great").status_code == 400
    assert get(client, "ledger", bogus="1").status_code == 400
    assert get(client, "ledger", ledger_sha256="XYZ").status_code == 400
    assert get(client, "ledger", ledger_sha256="0" * 64).status_code == 404
    imports = data(get(client, "ledger/imports"))
    assert len(imports["imports"]) == 1 and imports["not_imported"] == []


def test_retired_tool_lookup_over_http(client, ledger_imported):
    d = data(get(client, "retired-tools", tool="rl_fqi"))
    assert [f["family_id"] for f in d["families"]] == ["stock_level_rl"]
    fam = d["families"][0]
    assert fam["entries"][0]["id"] == "E015" and fam["entries"][0]["verdict"] == "fails" and fam["reason"]
    assert len(data(get(client, "retired-tools"))["families"]) == 10
    assert [f["family_id"] for f in data(get(client, "retired-tools", ledger_id="E084"))["families"]] == ["us_overnight"]


def test_every_route_needs_a_session(world, built, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PULSE_DSN", world["dsn"])
    monkeypatch.setenv("PULSE_API_HOST", "127.0.0.1")
    monkeypatch.delenv("PULSE_DEV_PRINCIPAL", raising=False)
    monkeypatch.delenv("PULSE_ENV", raising=False)
    from pulse.api import deps
    from pulse.api.app import create_app

    deps.close_pool()
    with TestClient(create_app()) as c:
        for path in ("ledger", "ledger/imports", "ledger/E003", "retired-tools", "tearsheet?catalog_id=PB:x", "batch-compare"):
            assert c.get(f"{URL}/{path}").status_code == 403, path
        assert c.post(f"{URL}/batch-compare", json={}, headers=H("k")).status_code == 403
    deps.close_pool()


# ---- tear sheet of a stored replay ---------------------------------------------------------------------------------------------------------
def test_replay_tear_sheet_agrees_with_the_research_lane_and_an_independent_recomputation(client, runs):
    res, _ = runs
    assert res["PB:kdj_golden_low"]["state"] == "completed", res
    ts_resp = data(get(client, "tearsheet", catalog_id="PB:kdj_golden_low"))
    rep = client.get("/v1/x/bf/research/playbook-replay", params={"catalog_id": "PB:kdj_golden_low", "trades_limit": 500}).json()["data"]
    sheet, m = ts_resp["tearsheet"], ts_resp["tearsheet"]["metrics"]
    assert ts_resp["subject"]["experiment_id"] == str(res["PB:kdj_golden_low"]["experiment_id"]) and sheet["version"] == "tearsheet-v1"
    # the figures the research lane already serves are the same numbers
    assert D(m["total_return"]["value"]) == D(rep["equity"]["net_return"]) and D(m["max_drawdown"]["value"]) == D(rep["equity"]["max_drawdown"])
    assert ts_resp["window"] == {"start": rep["window"]["start"], "end": rep["window"]["end"], "sessions": rep["window"]["sessions"]}
    # independent recomputation of the ratios from the served NAV points, in floats
    navs = [float(p["nav"]) for p in rep["equity"]["points"] if p["nav"] is not None]
    rets = [b / a - 1 for a, b in zip([float(rep["initial_cash"])] + navs[:-1], navs)]
    assert sheet["returns"] == len(rets) == rep["window"]["sessions"]
    if stdev(rets) > 0:
        assert float(m["sharpe"]["value"]) == pytest.approx(mean(rets) / stdev(rets) * 252 ** 0.5, rel=1e-9)
        assert float(m["annualised_volatility"]["value"]) == pytest.approx(stdev(rets) * 252 ** 0.5, rel=1e-9)
    down = (sum(min(r, 0) ** 2 for r in rets) / len(rets)) ** 0.5
    if down > 0:
        assert float(m["sortino"]["value"]) == pytest.approx(mean(rets) / down * 252 ** 0.5, rel=1e-9)
    # trade statistics from the served trade list
    closed = [t for t in rep["trades"]["items"] if t["status"] == "closed"]
    pnl = [t["quantity"] * (float(t["exit_price"]) - float(t["entry_price"])) for t in closed]
    assert sheet["trades"] == {"closed": len(closed), "open": rep["trades"]["open"], "breakeven": sum(1 for x in pnl if x == 0)}
    if closed:
        assert float(m["win_rate"]["value"]) == pytest.approx(sum(1 for x in pnl if x > 0) / len(closed), abs=1e-9)
    losses = [x for x in pnl if x < 0]
    if losses:
        assert float(m["profit_factor"]["value"]) == pytest.approx(sum(x for x in pnl if x > 0) / -sum(losses), rel=1e-9)
    else:
        assert m["profit_factor"] == {"value": None, "reason": "no_losses" if closed else "no_closed_trades"}
    exposure = float(m["exposure"]["value"])
    assert 0 <= exposure <= 1
    held = sum(1 for p in rep["equity"]["points"] if any(t["entry_day"] <= p["d"] and (t["exit_day"] is None or p["d"] < t["exit_day"]) for t in rep["trades"]["items"]))
    assert exposure == pytest.approx(held / len(rep["equity"]["points"]), abs=1e-9)
    assert any("毛收益" in c for c in ts_resp["caveats"])


def test_replay_tear_sheet_by_experiment_id_and_missing_cases(client, runs):
    res, _ = runs
    eid = str(res["PB:turtle_55"]["experiment_id"])
    assert data(get(client, "tearsheet", experiment_id=eid))["subject"]["catalog_id"] == "PB:turtle_55"
    assert get(client, "tearsheet", catalog_id="PB:value_pe_pb").status_code == 404                  # failed experiment: no replay, no invented sheet
    assert get(client, "tearsheet").status_code == 400
    assert get(client, "tearsheet", catalog_id="PB:turtle_55", book_key="x").status_code == 400
    assert get(client, "tearsheet", experiment_id="nope").status_code == 400
    assert get(client, "tearsheet", catalog_id="PB:turtle_55", extra="1").status_code == 400


# ---- tear sheet of a paper book --------------------------------------------------------------------------------------------------------------
def test_book_tear_sheet_has_navs_but_says_trade_figures_are_unavailable(client, world, built):
    conn = db.connect(world["dsn"], autocommit=True)
    try:
        PORT.register_forward_books(conn, [PRINCIPAL])
        key = "main_h5"
        acct = conn.execute("SELECT account_id FROM execution.tracked_account WHERE book_key=%s", (key,)).fetchone()["account_id"]
        rel = conn.execute("SELECT release_id FROM decision.release LIMIT 1").fetchone()["release_id"]
        navs = ["1000000", "1010000", None, "1005000", "1030000"]
        for k, v in enumerate(navs):
            conn.execute("INSERT INTO execution.tracked_book_day(account_id, trade_date, release_id, nav_cny, cash_cny, missing_marks, account_version)"
                         " VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", (acct, date(2026, 3, 2) + timedelta(days=k), rel, v, v, 0 if v else 1, k + 1))
        d = data(get(client, "tearsheet", book_key=key))
    finally:
        conn.close()
    assert d["subject"]["kind"] == "book" and d["subject"]["id"] == key and d["basis"]["initial_nav"] is None
    sheet = d["tearsheet"]
    assert sheet["sessions"] == 5 and sheet["missing_nav_sessions"] == 1 and sheet["trades"] is None
    assert sheet["returns"] == 2                                            # 1000000 -> 1010000 and 1005000 -> 1030000 are the only adjacent pairs; the gap bridges none
    assert D(sheet["metrics"]["total_return"]["value"]) == D("0.03")
    for k in ("win_rate", "profit_factor", "payoff_ratio", "exposure", "avg_win"):
        assert sheet["metrics"][k] == {"value": None, "reason": "trade_list_unavailable"}
    other = client.get(f"{URL}/tearsheet", params={"book_key": "no_such_book"})
    assert other.status_code == 404


# ---- batch comparison ----------------------------------------------------------------------------------------------------------------------
def parse(client, text):
    r = client.get("/v1/x/parse-screen-text", params={"text": text, "field_catalog_version": "screen-fields-v1", "locale": "zh-CN"})
    assert r.status_code == 200, r.text
    return r.json()["data"]["ast"]


def screen(client, conn, text, key, asof=ASOF):
    m = market_repo.latest_ready(conn, RESEARCH_DATASET)
    body = {"idempotency_key": key, "ast": parse(client, text), "universe_policy_version": "screen-universe-main-v1",
            "input_snapshot_refs": [market_repo.snapshot_ref(m)], "decision_as_of": asof, "strategy_instances": []}
    r = client.post("/v1/x/run-screen", json=body, headers=H(key))
    assert r.status_code == 200, r.text
    return r.json()["data"]["receipt"]["resource_id"]


def submit(client, body, key, status=202):
    r = client.post(f"{URL}/batch-compare", json=body, headers=H(key))
    assert r.status_code == status, r.text
    return r.json()["data"]["receipt"] if status == 202 else r.json()


def test_batch_compare_runs_through_the_queue_and_matches_an_independent_single_instrument_replay(client, conn, lake, world):
    sid = screen(client, conn, "涨幅大于0 近5日涨幅大于-100% 剔除ST", "lab-s1")
    rows = conn.execute("SELECT sort_position, instrument_id FROM research.screen_run_row WHERE screen_run_id=%s AND condition_value='true' ORDER BY sort_position",
                        (sid,)).fetchall()
    assert len(rows) >= 10
    body = {"screen_run_id": sid, "playbook_id": "PB:turtle_55", "window_sessions": 60}
    rc = submit(client, body, "lab-b1")
    assert submit(client, body, "lab-b1") == rc                                                          # same key, same receipt, nothing queued twice
    assert conn.execute("SELECT count(*) n FROM ops.job_run WHERE job_id=%s", (LAB.LAB_JOB,)).fetchone()["n"] == 1
    d = data(client.get(f"{URL}/batch-compare/{rc['job_run_id']}"))                                      # the background task ran the job after the response
    assert d["run"]["run_state"] == "succeeded" and d["run"]["error_code"] is None and d["result_ref"].startswith("lab-batch-result:")
    res = d["result"]
    assert d["run"]["request"] == {"screen_run_id": sid, "playbook_id": "turtle_55", "top_n": 10, "window_sessions": 60}
    assert [r["sort_position"] for r in res["rows"]] == [r["sort_position"] for r in rows[:10]]
    assert [r["instrument_id"] for r in res["rows"]] == [str(r["instrument_id"]) for r in rows[:10]]
    assert res["screen"]["selected"] == 10 and res["screen"]["panel_matches_screen"] is True and res["screen"]["decision_as_of"] == ASOF
    assert res["playbook"]["id"] == "turtle_55" and res["window"]["sessions"] == 60 and any("样本外" in c for c in res["caveats"])
    ok = [r for r in res["rows"] if r["status"] in ("ok", "no_trades")]
    assert any(r["status"] == "ok" for r in res["rows"]), [r["status"] for r in res["rows"]]
    assert ok and all(r["tearsheet"]["sessions"] == 60 and r["tearsheet"]["first_session"] == res["window"]["start"] for r in ok)
    assert all(r["tearsheet"]["last_session"] == res["window"]["end"] and r["code"] for r in ok)
    assert d["run"]["summary"] == {"instruments": 10, "with_trades": sum(1 for r in res["rows"] if r["status"] == "ok"), "skipped": 0}

    # independent replay of the first instrument with the same machinery, by hand
    from pulse.application import playbook_service as PS
    from pulse.domain.lab import tearsheet as T
    from pulse.domain.market_rules.costs import user_account_2026
    from pulse.domain.playbooks import exits as EX
    from pulse.domain.playbooks import library as L
    from pulse.domain.playbooks.builder import prepare, run_signal
    from pulse.domain.playbooks.replay import ReplayConfig, replay

    asof = datetime.fromisoformat(ASOF.replace("Z", "+00:00"))
    panel = S.load_panel(conn, lake, asof, board="main", with_caps=False)
    pb = L.REGISTRY["turtle_55"]
    prepare(panel.views, panel.sessions, [pb])
    first = res["rows"][0]
    view = panel.by_id()[first["instrument_id"]]
    specs = PS._specs(panel)
    end = len(panel.sessions)
    run = replay(signals={view.instrument_id: run_signal(pb, view, pb.default_params())}, views={view.instrument_id: view}, specs={view.instrument_id: specs[view.instrument_id]},
                 sessions=panel.sessions, cfg=ReplayConfig(PS.DEFAULT_CASH, user_account_2026(), end - 60, end, 1, 1, EX.ExitRules.from_spec(pb.exits)), run_id="check")
    want = T.compute(run.nav, [T.TradeIn(t.entry_day, t.exit_day if t.exit_price is not None else None,
                                         None if t.exit_price is None else D(t.quantity) * (t.exit_price - t.entry_price), t.gross_return) for t in run.trades],
                     initial=PS.DEFAULT_CASH)
    assert first["tearsheet"] == contracts.to_wire(LAB.tearsheet_wire(want))
    assert (first["status"] == "ok") == bool(run.trades)

    # the archive lists it for its owner
    lst = data(client.get(f"{URL}/batch-compare"))
    assert [i["job_run_id"] for i in lst["items"]] == [rc["job_run_id"]] and lst["items"][0]["run_state"] == "succeeded" and lst["items"][0]["summary"]["instruments"] == 10
    # another principal sees neither the list entry nor the run
    other = uuid.uuid4()
    assert LAB.list_runs(conn, other, 20, None)["items"] == []
    with pytest.raises(LAB.LabError) as ei:
        LAB.get_run(conn, other, uuid.UUID(rc["job_run_id"]))
    assert ei.value.status == 404


def test_idempotency_key_with_another_request_is_a_conflict_and_bad_requests_are_refused(client, conn):
    sid = screen(client, conn, "涨幅大于0 近5日涨幅大于-100% 剔除ST", "lab-s2")
    submit(client, {"screen_run_id": sid, "playbook_id": "turtle_55", "top_n": 3, "window_sessions": 60}, "lab-conf")
    r = client.post(f"{URL}/batch-compare", json={"screen_run_id": sid, "playbook_id": "turtle_55", "top_n": 4, "window_sessions": 60}, headers=H("lab-conf"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    bad = [({"screen_run_id": sid}, 400), ({"screen_run_id": "x", "playbook_id": "turtle_55"}, 400), ({"screen_run_id": sid, "playbook_id": "turtle_55", "top_n": 11}, 400),
           ({"screen_run_id": sid, "playbook_id": "turtle_55", "window_sessions": 10}, 400), ({"screen_run_id": sid, "playbook_id": "turtle_55", "x": 1}, 400),
           ({"screen_run_id": sid, "playbook_id": "no_such_playbook"}, 404), ({"screen_run_id": str(uuid.uuid4()), "playbook_id": "turtle_55"}, 404),
           ({"screen_run_id": sid, "playbook_id": "value_pe_pb"}, 422)]
    for i, (b, status) in enumerate(bad):
        r = client.post(f"{URL}/batch-compare", json=b, headers=H(f"lab-bad-{i}"))
        assert r.status_code == status, (b, r.text)
    r = client.post(f"{URL}/batch-compare", json={"screen_run_id": sid, "playbook_id": "turtle_55", "idempotency_key": "other"}, headers=H("lab-hdr"))
    assert r.status_code == 400
    # a screen with no TRUE row has nothing to compare
    empty = screen(client, conn, "涨幅大于100%", "lab-s-empty")
    r = client.post(f"{URL}/batch-compare", json={"screen_run_id": empty, "playbook_id": "turtle_55"}, headers=H("lab-none"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "NO_MATCHING_ROWS"
    assert client.get(f"{URL}/batch-compare/{uuid.uuid4()}").status_code == 404
    assert client.get(f"{URL}/batch-compare/not-a-uuid").status_code == 400
    assert client.get(f"{URL}/batch-compare", params={"limit": 0}).status_code == 400


def test_batch_replay_uses_the_screen_runs_cutoff_not_now(client, conn, lake, monkeypatch):
    early = "2026-11-11T08:30:00Z"                                       # before the late revision of one bar (ingested 09:00): a different panel than ASOF
    sid = screen(client, conn, "涨幅大于0 近5日涨幅大于-100% 剔除ST", "lab-s-early", asof=early)
    seen = []
    real = S.load_panel

    def spy(conn_, lake_, decision_as_of, **kw):
        seen.append((decision_as_of, kw.get("board")))
        return real(conn_, lake_, decision_as_of, **kw)

    monkeypatch.setattr(S, "load_panel", spy)
    rc = submit(client, {"screen_run_id": sid, "playbook_id": "turtle_55", "top_n": 2, "window_sessions": 60}, "lab-pit")
    d = data(client.get(f"{URL}/batch-compare/{rc['job_run_id']}"))
    assert d["run"]["run_state"] == "succeeded"
    want = datetime.fromisoformat(early.replace("Z", "+00:00"))
    assert seen and all(a == want and b == "main" for a, b in seen), seen          # never "now", never a later cutoff
    assert d["result"]["screen"]["decision_as_of"] == early and d["result"]["screen"]["panel_matches_screen"] is True


def test_a_failing_replay_fails_the_job_visibly_and_stores_no_result(client, conn, monkeypatch):
    sid = screen(client, conn, "涨幅大于0 近5日涨幅大于-100% 剔除ST", "lab-s3")

    def boom(*a, **k):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(LAB, "run_batch", boom)
    rc = submit(client, {"screen_run_id": sid, "playbook_id": "turtle_55", "top_n": 2, "window_sessions": 60}, "lab-fail")
    d = data(client.get(f"{URL}/batch-compare/{rc['job_run_id']}"))
    assert d["run"]["run_state"] == "failed" and d["run"]["error_code"] == "RuntimeError" and d["result"] is None and d["result_ref"] is None


def test_the_lab_job_is_registered_as_a_manual_job(conn):
    row = conn.execute("SELECT * FROM ops.job_definition WHERE job_id=%s", (LAB.LAB_JOB,)).fetchone()
    assert row["application_operation"] == LAB.JOB_OPERATION and row["enabled"] and row["schedule_document"]["manual_trigger"] is True
    assert row["parameter_contract_id"] == SID and row["max_concurrency"] == 1
    assert json.loads(json.dumps(row["retry_policy"])) == {"max_attempts": 1}
