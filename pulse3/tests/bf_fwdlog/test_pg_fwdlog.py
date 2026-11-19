"""The fwdlog daily step on real PostgreSQL: recordings -> imported datasets -> E077 / E078 / E089 / paper rows, append-only, registry, reads, reconcile, HTTP."""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from decimal import Decimal as D

import polars as pl
import pytest
from fastapi.testclient import TestClient

from bf_live import support as T
from pulse import contracts
from pulse.adapters.sources.legacy_recordings import LegacyRecordings
from pulse.application import bf_fwdlog_paper as PA
from pulse.application import bf_fwdlog_reads as R
from pulse.application import bf_fwdlog_reconcile as RC
from pulse.application import bf_fwdlog_registry as REG
from pulse.application import bf_fwdlog_service as S
from pulse.application.bf_live_service import LiveService

pytestmark = pytest.mark.pg
WEB_FIXTURES = Path(__file__).resolve().parents[2] / "apps" / "web" / "src" / "pages" / "bf" / "fwdlog" / "fixtures"
UTC = timezone.utc
DAY1, DAY2 = date(2026, 11, 12), date(2026, 11, 13)
CODES = [f"6001{i:02d}" for i in range(1, 11)]


def stock_rows(i, code, day, prev, price, close_price, times=(("10:00:00", "full"),)):
    pts = []
    rows = []
    for hhmm, kind in times:
        rows += T.quote_rows(code, f"股{i}", prev, 10.67, [(hhmm, price, round(price - 0.01, 2), 100.0, round(price + 0.01, 2), 100.0)], kind=kind, day=day)
    rows += T.quote_rows(code, f"股{i}", prev, 10.67, [("15:01:00", close_price, round(close_price - 0.01, 2), 100.0, round(close_price + 0.01, 2), 100.0)], kind="close", day=day)
    for r in rows:
        r.update({"turnover": 6.0, "float_cap": 20.0 * i, "volume_ratio": 1 + i / 10, "volume_lots": 1000.0, "amount_yuan": 10_000_000.0})
    return rows


def day1_rows():
    out = []
    for i, c in enumerate(CODES, 1):
        out += stock_rows(i, c, DAY1, 9.70, 10.00, 10.10, times=(("10:00:00", "full"), ("14:30:00", "full"), ("14:50:00", "full")))
    return out


def day2_rows():
    out = []
    for i, c in enumerate(CODES, 1):
        out += stock_rows(i, c, DAY2, 10.00, round(10 + 0.1 * i, 2), 10.50)
    return out


def ext_rows(day):
    rows = []
    for s in ("sh510300", "sh510310", "sh510050", "sz159919", "sh510500", "sh512100", "sz159845"):
        for t, kind, amount in (("14:30:02", "grid", 2.4e9), ("15:01:30", "close", 3.0e9)):
            r = T.ext_row(s, "etf", 4.0, t, day)
            r.update({"kind": kind, "prev_close": 4.0, "volume": 1000.0, "amount": amount, "quote_ts": T._naive(day, "15:00:00")})
            rows.append(r)
    for t, kind, price, open_ in (("10:00:00", "grid", 3000.0, 2980.0), ("14:30:00", "grid", 3030.0, 2980.0), ("15:01:00", "close", 3036.0, 2980.0)):
        r = T.ext_row("sh000001", "index", price, t, day)
        r.update({"kind": kind, "prev_close": 2970.0, "open": open_})
        rows.append(r)
    return rows


def bars_stub(conn, as_of, until, since):
    days = [date(2026, 10, 12) + timedelta(days=i) for i in range(32)]
    days = [d for d in days if d.weekday() < 5 and d <= date(2026, 11, 12)]
    closes = {d: D("10") for d in days}
    closes[date(2026, 11, 10)], closes[date(2026, 11, 11)] = D("9.70"), D("10.67")
    by = {c: [(d, closes[d], closes[d], D("1000000")) for d in days if since <= d <= until] for c in CODES}
    return PA.Bars(by, tuple(d for d in days if since <= d <= until), tuple(days))


@pytest.fixture
def setup(conn, lake, legacy_root, clock, tmp_path):
    for day, rows in ((DAY1, day1_rows()), (DAY2, day2_rows())):
        T.write_quotes(legacy_root, "quotes", "part-0001.parquet", rows, day=day)
        (legacy_root / "intraday" / day.isoformat() / "done.json").write_text('{"end": "done"}', encoding="utf-8")
    T.write_ext(legacy_root, ext_rows(DAY1), DAY1)
    (legacy_root / "intraday_ext" / DAY1.isoformat() / "summary.json").write_text(json.dumps({"end": "done", "missed": 0, "failed_requests": 0, "never_returned": [], "blocked": {}}))
    rec = LegacyRecordings(legacy_root)
    live = LiveService(conn, lake, rec, clock=clock)
    live.bootstrap()
    for day in (DAY1, DAY2):
        live.import_intraday(day)
    live.import_ext(DAY1)
    svc = S.FwdlogService(conn, lake, rec, clock=clock, events_path=tmp_path / "no-events.jsonl", bars_loader=bars_stub,
                          hist_loader=lambda c, a, u, s: PA.hist_from_bars(bars_stub(c, a, u, s)))
    return svc


def count(conn, dataset):
    return conn.execute("SELECT count(*) n FROM market.dataset_manifest WHERE dataset_id=%s AND manifest_state='READY'", (dataset,)).fetchone()["n"]


def test_daily_step_rows_registry_and_append_only(setup, conn):
    svc = setup
    rep = svc.run(DAY2)
    assert rep.errors == [] and rep.registered == ["E077", "E078", "E089", "PAPER"]
    assert rep.added["E078"] == ["2026-11-12", "2026-11-13"] and rep.added["E077"] == ["2026-11-12"] and rep.added["PAPER"] == ["2026-11-12", "2026-11-13"]
    assert rep.added["E089"] == ["2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"]
    assert rep.pending["E077"][0]["session_date"] == "2026-11-13" and "next trading day 2026-11-16" in rep.pending["E077"][0]["reason"]

    # E077 = the hand-computed domain case (ret 8, cell 7.9, ex 0.1, net 7.748) on recordings that went through the real import
    row = svc.stored(S.DS["E077"])[0]["body"]
    assert row["signals"] == ["600110", "600109", "600108", "600107", "600106"] and row["pool"] == 10 and row["n"] == 5
    assert tuple(D(row[k]) for k in ("ret", "cell", "ex", "net")) == (D("8"), D("7.9"), D("0.1"), D("7.748"))
    assert row["provenance"]["quote_snapshot_id"] and row["provenance"]["exit_quote_snapshot_id"]

    # E078: x = 10:00 / previous close - 1 on 9.70 -> 3.0928 percent; y = close 10.10 / 14:30 price 10.00 -> 1.0; the index series from the extended recording
    e078 = {r["session_date"]: r["body"] for r in svc.stored(S.DS["E078"])}
    assert {k: D(v) for k, v in e078["2026-11-12"]["ew"].items()} == {"n": 10, "x": D("3.0928"), "x2": D("3.0928"), "y": D("1")}
    assert {k: D(v) for k, v in e078["2026-11-12"]["index"].items()} == {"n": 1, "x": D("1.0101"), "x2": D("0.6711"), "y": D("0.198")}
    assert "index" not in e078["2026-11-13"]                                            # no extended recording that day: the key is absent, not an empty series

    # E089: two sessions without any recording are gap rows; the recorded one carries the ETF sums, ratio against the frozen seed, and health
    e089 = {r["session_date"]: r["body"] for r in svc.stored(S.DS["E089"])}
    assert e089["2026-11-10"]["ext_missing"] is True and e089["2026-11-10"]["surge"] is None and e089["2026-11-11"]["etf_amount"] is None
    d12 = e089["2026-11-12"]
    seed = svc.frozen.seed_sums
    base = sorted(seed[k] for k in sorted(seed)[-20:])
    assert D(d12["etf_amount"]) == D("21000000000") and d12["n_etf"] == 7 and D(d12["late_share"]) == D("0.2") and D(d12["ew_pct"]) == D("4.1237")
    assert D(d12["ratio"]) == (D("21000000000") / ((base[9] + base[10]) / 2)).quantize(D("0.0001")) and d12["health"]["end"] == "done" and d12["health"]["merged"] is True
    assert D(e089["2026-11-13"]["ew_pct"]) == D("5") and e089["2026-11-13"]["ext_missing"] is True      # day 2 has main-board quotes, no extended file

    # paper replay: ten previous-session limit-ups, every rule side by side, nothing booked; the second session has an empty book
    days = {r["session_date"]: r["body"] for r in svc.stored(S.DS["PAPER_DAYS"])}
    assert days["2026-11-12"]["book_size"] == 10 and days["2026-11-12"]["booked"] == {"B1": False, "B3": False} and days["2026-11-13"]["book_size"] == 0
    sells = {r["row_key"]: r["body"] for r in svc.stored(S.DS["PAPER_SELLS"], session_dates=["2026-11-12"])}
    hold = sells["600101|HOLD_close"]
    assert D(hold["price"]) == D("10.10") and D(hold["cost"]) == D("10.67") and hold["qty"] == 900 and hold["status"] == "FILLED" and hold["t1_status"] == "SELLABLE_TODAY"
    assert {"S0_open", "HOLD_close", "POLICY_v2", "POLICY_v3_shadow"} <= {k.split("|")[1] for k in sells}

    # registry: four shadow runs, running, with the pre-registration hashes
    runs = conn.execute("SELECT run_purpose, run_state, run_manifest FROM research.experiment_run ORDER BY run_manifest->>'ledger_ids'").fetchall()
    assert len(runs) == 4 and {r["run_purpose"] for r in runs} == {"shadow"} and {r["run_state"] for r in runs} == {"running"}
    e077_run = next(r for r in runs if "E077" in r["run_manifest"]["ledger_ids"])
    assert e077_run["run_manifest"]["preregistration"] == {"prereg/2026-11-08-batch4-preregistration.md": svc.frozen.files["prereg/2026-11-08-batch4-preregistration.md"]}
    inst = conn.execute("SELECT lifecycle, implementation_status FROM strategy.instance").fetchall()
    assert len(inst) == 4 and {i["lifecycle"] for i in inst} == {"shadow"}

    # append-only: a second run decides nothing and publishes nothing; a decided session cannot be written again
    before = {k: count(conn, v) for k, v in S.DS.items()}
    again = svc.run(DAY2)
    assert again.added == {} and again.registered == [] and {k: count(conn, v) for k, v in S.DS.items()} == before
    with pytest.raises(RuntimeError, match="already decided"):
        svc.publish(S.DS["E078"], [{"session_date": "2026-11-12", "row_key": "2026-11-12", "body": {"date": "2026-11-12"}}], deps=[])


def test_incomplete_recording_is_pending_not_decided(setup, conn, legacy_root, lake, clock):
    live = LiveService(conn, lake, LegacyRecordings(legacy_root), clock=clock)
    d3 = date(2026, 11, 16)
    T.write_quotes(legacy_root, "quotes", "part-0001.parquet", stock_rows(1, "600101", d3, 10.0, 10.2, 10.3), day=d3)
    live.import_intraday(d3)
    svc = setup
    rep = svc.run(d3)
    assert "2026-11-16" in [p["session_date"] for p in rep.pending["E078"]] and "done marker" in rep.pending["E078"][-1]["reason"]
    assert all(r["session_date"] != "2026-11-16" for r in svc.stored(S.DS["E078"]))
    assert "2026-11-16" not in svc.decided(S.DS["PAPER_DAYS"])


def test_reads_cards_progress_and_contract(setup):
    svc = setup
    svc.run(DAY2)
    data = R.cards(svc)
    contracts.check("ExperimentsData", contracts.to_wire(data), contracts.backfill_id("fwdlog"))
    cards = {c["experiment_key"]: c for c in data["experiments"]}
    e77 = cards["E077"]
    assert e77["status"] == "recording" and e77["progress"]["done"] == 1 and e77["progress"]["target"] == 40 and e77["progress"]["remaining"] == 39
    m = {x["name"]: x for x in e77["metrics"]}
    assert m["mean_excess_pct"]["provisional"] is True and m["mean_excess_pct"]["pending_reason"].startswith("pending until 40") and m["h1_excess_positive_and_p_below_0_05"]["value"] is None
    assert e77["decision"]["decided"] is False and e77["decision"]["verdict"] is None and e77["registry"]["run_state"] == "running"
    assert cards["E078"]["progress"] == {"unit": "sessions", "done": 2, "target": 60, "remaining": 58, "share": D("0.0333")}
    assert cards["E089"]["decision"]["pending_reason"].startswith("pending until 10 independent") and cards["E089"]["progress"]["done"] == 0
    paper = cards["PAPER"]
    assert paper["progress"]["target"] == 20 and {r["rule"] for r in paper["rule_table"]} >= {"S0_open", "HOLD_close", "POLICY_v2"} and paper["purpose"] == "shadow"
    assert all(len(c["preregistration"][0]["sha256"]) == 64 for c in cards.values())
    summ = R.summaries(svc)
    assert summ["e077"]["decision_days"] == 1 and summ["e078"]["days"] == 2 and summ["e089"]["days"] == 4 and summ["paper"]["sessions"] == 2


def old_dir(tmp_path, svc, *, change_paper=False, extra_day=True):
    old = tmp_path / "old"
    (old / "paper" / "2026-11-12").mkdir(parents=True)
    e077 = svc.stored(S.DS["E077"])[0]["body"]
    rows = [{k: (float(v) if k in ("ret", "cell", "ex", "net") else v) for k, v in e077.items() if k != "provenance"}]
    if extra_day:
        rows.append({"date": "2026-11-03", "exit_day": "2026-11-04", "pool": 1, "signals": [], "n": 0, "missing_exit": 0})
    (old / "e077_tail.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    (old / "e078_halfhour.jsonl").write_text(json.dumps({"date": "2026-11-12", "ew": {"n": 10, "x": 3.0928, "x2": 3.0928, "y": 1.0}, "index": {"n": 1, "x": 1.0101, "x2": 0.6711, "y": 0.198}}) + "\n", encoding="utf-8")
    sells = []
    for r in svc.stored(S.DS["PAPER_SELLS"], session_dates=["2026-11-12"]):
        b = r["body"]
        price = float(b["price"]) + (0.5 if change_paper and r["row_key"] == "600101|S0_open" else 0)
        sells.append({"code": b["code"], "rule": b["rule"], "status": b["status"], "qty": b["qty"], "cost": float(b["cost"]), "price": price, "return_pct": float(b["return_pct"]),
                      "vs_close_pct": float(b["vs_close_pct"]), "time": datetime.fromisoformat(b["time"].replace("Z", "+00:00")).astimezone(RC.CST).replace(tzinfo=None),
                      "held_back_tape": b["held_back_tape"], "held_back_strict": b["held_back_strict"]})
    pl.DataFrame(sells).write_parquet(old / "paper" / "2026-11-12" / "sells.parquet")
    pl.DataFrame({"opportunity_id": ["x"]}).clear().write_parquet(old / "paper" / "2026-11-12" / "opportunities.parquet")
    (old / "paper" / "2026-11-12" / "summary.md").write_text("- 2026-11-11 收盘市场状态：主板涨停 76 家，炸板率 25%，热度 hot，炸板 lo_fail。", encoding="utf-8")
    return old


def test_reconcile_agrees_names_reasons_and_catches_a_real_difference(setup, tmp_path):
    svc = setup
    svc.run(DAY2)
    out = RC.reconcile(svc, old_dir(tmp_path, svc), tolerance="0.0015")
    assert out["compared"]["E077"]["common"] == ["2026-11-12"] and out["compared"]["PAPER"]["common"] == ["2026-11-12"]
    reasons = {(d["experiment"], d["session_date"]): d for d in out["differences"]}
    gone = reasons[("E077", "2026-11-03")]
    assert gone["explained"] and "not imported" in gone["reason"]                        # an old day pulse3 never recorded: named, explained
    assert out["unexplained"] == 0, RC.render(out)
    text = RC.render(out)
    assert "UNEXPLAINED: 0" in text and "[explained] E077 2026-11-03" in text

    bad = RC.reconcile(svc, old_dir(tmp_path / "bad", svc, change_paper=True))
    fields = {d["field"] for d in bad["differences"] if not d["explained"]}
    assert bad["unexplained"] >= 1 and "sells[600101|S0_open].price" in fields             # a real price difference is never explained away


def test_reconcile_tolerance_and_shifted_values(setup, tmp_path):
    svc = setup
    svc.run(DAY2)
    old = old_dir(tmp_path, svc, extra_day=False)
    row = json.loads((old / "e077_tail.jsonl").read_text(encoding="utf-8").splitlines()[0])
    row["ret"] = 8.0009                                                                   # within the 0.0015 rounding tolerance
    row["ex"] = 0.2                                                                       # a real difference
    (old / "e077_tail.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    out = RC.reconcile(svc, old)
    assert [d["field"] for d in out["differences"] if d["experiment"] == "E077"] == ["ex"] and out["unexplained"] == 1


@pytest.fixture
def client(setup, fresh_dsn, lake, monkeypatch):
    setup.run(DAY2)
    monkeypatch.setenv("PULSE_DSN", fresh_dsn)
    monkeypatch.setenv("PULSE_LAKE", str(lake.root))
    monkeypatch.setenv("PULSE_ENV", "dev")
    monkeypatch.setenv("PULSE_API_HOST", "127.0.0.1")
    monkeypatch.setenv("PULSE_DEV_PRINCIPAL", "00000000-0000-0000-0000-000000000001")
    from pulse.api import deps
    from pulse.api.app import create_app

    deps.close_pool()
    with TestClient(create_app()) as c:
        yield c
    deps.close_pool()


def test_http_reads(client):
    sid = contracts.backfill_id("fwdlog")
    r = client.get("/v1/x/bf/fwdlog/experiments")
    assert r.status_code == 200
    body = r.json()
    if os.environ.get("PULSE_WRITE_WEB_FIXTURES"):                                   # regenerate the web fixtures from the real API
        WEB_FIXTURES.mkdir(parents=True, exist_ok=True)
        (WEB_FIXTURES / "experiments.json").write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
        for key in ("E077", "E078", "E089", "PAPER"):
            (WEB_FIXTURES / f"rows-{key.lower()}.json").write_text(json.dumps(client.get(f"/v1/x/bf/fwdlog/experiments/{key}/rows").json(), ensure_ascii=False, indent=1), encoding="utf-8")
    contracts.check("ExperimentsData", body["data"], sid)
    contracts.check("ResponseMeta", body["meta"])
    cards = {c["experiment_key"]: c for c in body["data"]["experiments"]}
    assert body["meta"]["warnings"][0]["code"] == "SHADOW_FORWARD_LOG" and cards["E077"]["pending"][0]["session_date"] == "2026-11-13"
    rows = client.get("/v1/x/bf/fwdlog/experiments/E078/rows", params={"from": "2026-11-13"})
    assert rows.status_code == 200 and rows.json()["data"]["total"] == 1
    contracts.check("RowsData", rows.json()["data"], sid)
    assert client.get("/v1/x/bf/fwdlog/experiments/E077/rows").json()["data"]["rows"][0]["values"]["signals"][0] == "600110"
    assert client.get("/v1/x/bf/fwdlog/experiments/E999/rows").status_code == 404
    fills = client.get("/v1/x/bf/fwdlog/paper/2026-11-12/fills", params={"kind": "sells", "rule": "HOLD_close"})
    assert fills.status_code == 200 and fills.json()["data"]["total"] == 10
    contracts.check("PaperFillsData", fills.json()["data"], sid)
    assert client.get("/v1/x/bf/fwdlog/paper/2026-11-12/fills", params={"kind": "buys"}).json()["data"]["total"] == 0
    assert client.get("/v1/x/bf/fwdlog/paper/2026-11-03/fills").status_code == 404
    assert client.get("/v1/x/bf/fwdlog/paper/2026-11-12/fills", params={"kind": "x"}).status_code in (400, 422)


def test_web_fixtures_pass_the_contract():
    """The fixtures the page tests render are real API responses: they must satisfy the same contract (no mock drift)."""
    sid = contracts.backfill_id("fwdlog")
    exp = json.loads((WEB_FIXTURES / "experiments.json").read_text(encoding="utf-8"))
    contracts.check("ExperimentsData", exp["data"], sid)
    contracts.check("ResponseMeta", exp["meta"])
    for key in ("e077", "e078", "e089", "paper"):
        rows = json.loads((WEB_FIXTURES / f"rows-{key}.json").read_text(encoding="utf-8"))
        contracts.check("RowsData", rows["data"], sid)
