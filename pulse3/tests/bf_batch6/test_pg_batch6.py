"""The batch 6 forward step on real PostgreSQL: imported recordings -> field check -> E095 / E096 rows, append only, registry, reads, HTTP."""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from bf_live import support as T
from pulse import contracts
from pulse.adapters.sources.legacy_recordings import LegacyRecordings
from pulse.application import bf_batch6_fieldcheck as FC
from pulse.application import bf_batch6_forward as FW
from pulse.application import bf_batch6_reads as RD
from pulse.application.bf_batch6_service import Batch6Service
from pulse.application.bf_live_service import LiveService

pytestmark = pytest.mark.pg
UTC = timezone.utc
PRE_DAY, DAY = date(2026, 11, 17), date(2026, 11, 19)
CODES = [f"6001{i:02d}" for i in range(1, 11)]
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)                     # 20:00 CST on the evaluated session


def quote(i, code, day, hhmm, kind, price, *, open_=0.0, b1=0.0, bl=0.0, a1=0.0, al=0.0, **extra):
    r = T.quote_rows(code, f"股{i}", 10.0, 11.0, [(hhmm, price, b1, bl, a1, al)], kind=kind, day=day)[0]
    r.update({"open": open_, "float_cap": 20.0 * i, **extra})
    return r


def session_rows(day, codes=CODES, full=True):
    rows = []
    for i, code in enumerate(codes, 1):
        p925 = {9: 10.40, 10: 10.80}.get(i, 10.20)
        p919 = 10.60 if i in (3, 4) else p925
        p925 = 10.40 if i == 4 else p925                              # i=4: fade of only 2 points (10.60 -> 10.40)
        matched = 5.0 * i
        rows.append(quote(i, code, day, "09:15:02", "auction", 10.00, b1=10.00, bl=matched, a1=10.00, al=matched))
        rows.append(quote(i, code, day, "09:19:02", "auction", p919, b1=p919, bl=matched, a1=p919, al=matched, bid2_lots=3.0))
        rows.append(quote(i, code, day, "09:24:02", "auction", p925, b1=p925, bl=matched, a1=p925, al=matched, ask2_lots=2.0))
        vol = round(i * 1e6 / (100 * p925))
        rows.append(quote(i, code, day, "09:25:10", "auction_final", p925, open_=p925, b1=round(p925 - 0.01, 2), bl=40.0, a1=round(p925 + 0.01, 2), al=30.0,
                          bid2_lots=10.0, bid3_lots=5.0, volume_lots=float(vol), amount_yuan=vol * 100.0 * p925))
        if not full:
            continue
        rows.append(quote(i, code, day, "09:30:02", "full", p925, open_=p925, b1=p925 - 0.01, bl=10.0, a1=p925 + 0.01, al=10.0, outer_lots=1000.0, inner_lots=1000.0, volume_lots=2000.0))
        p10 = 10.30 if i == 10 else p925
        rows.append(quote(i, code, day, "10:00:02", "full", p10, open_=p925, b1=p10 - 0.01, bl=10.0, a1=round(p10 + 0.01, 2), al=50.0,
                          outer_lots=1000.0 + 10 * i, inner_lots=1000.0 + (100 - 10 * i), volume_lots=3000.0))
        close = {9: 10.80, 10: 10.50, 3: 10.00}.get(i, 10.30)
        rows.append(quote(i, code, day, "15:01:30", "close", close, open_=p925, b1=close - 0.01, bl=10.0, a1=close + 0.01, al=10.0, volume_lots=5000.0))
    return rows


def hist_stub(conn, day, as_of):
    return FW.History({c: 1e7 for c in CODES}, {c: 0.01 + 0.001 * i for i, c in enumerate(CODES, 1)}, [], {c: date.min for c in CODES}, frozenset())


@pytest.fixture
def setup(conn, lake, tmp_path):
    root = tmp_path / "legacy"
    root.mkdir()
    for day in (PRE_DAY, DAY):
        T.write_quotes(root, "quotes", "part-0001.parquet", session_rows(day), day=day)
        (root / "intraday" / day.isoformat() / "done.json").write_text('{"end": "done"}', encoding="utf-8")
    live = LiveService(conn, lake, LegacyRecordings(root), clock=lambda: NOW)
    live.bootstrap()
    for day in (PRE_DAY, DAY):
        live.import_intraday(day)
    return Batch6Service(conn, lake, history_loader=hist_stub, clock=lambda: NOW, live=live)


def signals(svc, hyp):
    rows = [r for r in FW.stored_rows(svc.conn, svc.lake) if r["kind"] == "signal" and r["hypothesis"] == hyp]
    return {r["code"]: json.loads(r["body"]) for r in rows}


def test_field_check_is_decided_from_the_recorded_session_before_the_start_and_stored_once(setup):
    out, new = setup.fieldcheck(as_of=NOW)
    assert new and out["sessions"] == ["2026-11-17"]
    o = out["overall"]
    assert o["bid1_ask1_lots_0915_0924"].startswith("indicative matched") and o["bid_ask_book_at_0925"].startswith("resting orders") and o["unmatched_buy_computable"]
    assert o["share_bid2_and_ask2_both_positive"] == 0.0 and o["median_final_over_last_indicative_matched_volume_same_price"] is not None
    again, new2 = setup.fieldcheck(as_of=NOW)
    assert not new2 and again["sessions"] == out["sessions"]


def test_forward_day_signals_fills_and_append_only(setup, conn):
    rep = setup.forward(DAY, as_of=NOW)
    assert rep["decided"] and rep["unmatched_buy_computable"]
    h5, h6, h7 = signals(setup, "H5"), signals(setup, "H6"), signals(setup, "H7")
    assert list(h5) == ["600109"] and list(h6) == ["600103"] and list(h7) == ["600110"]
    # H5: gap +4 %, auction amount ratio 0.9 (top 20 %); buy the open 10.40, sell the close 10.80
    assert h5["600109"]["status"] == "filled" and h5["600109"]["ret_gross"] == pytest.approx(10.80 / 10.40 - 1)
    assert h5["600109"]["ret_net"] == pytest.approx(10.80 / 10.40 - 1 - 0.00252) and h5["600109"]["features"]["gap_925"] == pytest.approx(0.04)
    assert h5["600109"]["features"]["unmatched_buy"] == 55.0          # bid1 40 + bid2 10 + bid3 5
    # H6: 09:19 gap +6 %, 09:25 gap +2 %: fade 4 points; buy the open 10.20, sell the close 10.00
    assert h6["600103"]["ret_gross"] == pytest.approx(10.00 / 10.20 - 1) and h6["600103"]["features"]["fade"] == pytest.approx(0.04)
    # H7: active buy ratio 1.0 (100 of 100), +3 % at 10:00; buy the ask 10.31, sell the close 10.50
    assert h7["600110"]["ret_gross"] == pytest.approx(10.50 / 10.31 - 1) and h7["600110"]["abr_30"] == pytest.approx(1.0)
    assert "ex_sv" in h5["600109"] and h5["600109"]["base_sv"] is not None
    days = {(r["hypothesis"]): json.loads(r["body"]) for r in FW.stored_rows(conn, setup.lake) if r["kind"] == "day"}
    assert days["H5"]["n_signals"] == 1 and days["H5"]["k"] == 1 and days["H5"]["n_universe"] == 10 and days["H5"]["recording_complete"]
    features = [r for r in FW.stored_rows(conn, setup.lake) if r["kind"] == "feature"]
    assert len(features) == 10
    assert json.loads(next(r for r in features if r["code"] == "600101")["body"])["matched_915"] == 5.0        # the overnight-order approximation, recorded only
    # append only: a second run adds nothing; an earlier session is never evaluated
    n = len(FW.stored_rows(conn, setup.lake))
    again = setup.forward(DAY, as_of=NOW)
    assert not again["decided"] and "already" in again["reason"] and len(FW.stored_rows(conn, setup.lake)) == n
    early = setup.forward(PRE_DAY, as_of=NOW)
    assert not early["decided"] and "before the first evaluated" in early["reason"]
    assert FW.decided_sessions(conn) == {"2026-11-19"}


def test_registered_as_shadow_with_the_document_hash(setup, conn):
    setup.forward(DAY, as_of=NOW)
    rows = conn.execute("SELECT run_purpose, run_state, run_manifest FROM research.experiment_run WHERE run_manifest->>'lane'='batch6' ORDER BY run_manifest->>'hypotheses'").fetchall()
    assert len(rows) == 2 and {r["run_purpose"] for r in rows} == {"shadow"} and {r["run_state"] for r in rows} == {"running"}
    assert all("93a4b8136a219bff2f36b633abac1a7845a9695a1991097257f81a0289b7320d" in json.dumps(r["run_manifest"]) for r in rows)
    setup.forward(date(2026, 11, 20), as_of=NOW)                         # nothing recorded: registration stays idempotent
    assert conn.execute("SELECT count(*) n FROM research.experiment_run WHERE run_manifest->>'lane'='batch6'").fetchone()["n"] == 2


def test_overview_progress_and_http(setup, fresh_dsn, lake, monkeypatch):
    setup.forward(DAY, as_of=NOW)
    ov = RD.overview(setup.conn, lake)
    st = {h: s for h, s in ov["forward"].items()}
    assert st["H5"]["state"] == "collecting" and st["H5"]["signal_days"] == 1 and st["H5"]["signals"] == 1 and st["H5"]["filled"] == 1
    assert ov["fieldcheck"]["overall"]["unmatched_buy_computable"] and ov["sessions_decided"] == ["2026-11-19"]
    monkeypatch.setenv("PULSE_DSN", fresh_dsn)
    monkeypatch.setenv("PULSE_LAKE", str(lake.root))
    monkeypatch.setenv("PULSE_ENV", "dev")
    monkeypatch.setenv("PULSE_API_HOST", "127.0.0.1")
    monkeypatch.setenv("PULSE_DEV_PRINCIPAL", "00000000-0000-0000-0000-000000000001")
    from pulse.api import deps
    from pulse.api.app import create_app

    deps.close_pool()
    with TestClient(create_app()) as c:
        r = c.get("/v1/x/bf/batch6/overview")
        assert r.status_code == 200
        body = r.json()
        contracts.check("Overview", body["data"], contracts.backfill_id("batch6"))
        assert body["meta"]["warnings"][0]["code"] == "SHADOW_RESEARCH"
        h5 = next(x for x in body["data"]["forward"] if x["hypothesis"] == "H5")
        assert h5["state"] == "collecting" and h5["excess"] is not None and h5["p_value"] is None
    deps.close_pool()


def test_history_publish_and_read_round_trip(conn, lake):
    from dataclasses import asdict

    from pulse.application import bf_batch6_history as HI
    from pulse.application.data_service import DataService
    from pulse.domain.batch6 import evaluate as EV

    svc = DataService(conn, lake, clock=lambda: NOW)
    svc.bootstrap()

    def seg(h, sg, ex, sign):
        return EV.SegmentResult(h, sg, sign, 3, 30, 28, 20, 20 / 28, {"blocked_buy": 8}, 20, 12, 0.012, 0.0095, 0.002, ex, ex, ex, 0.01, 40, True, None)

    results = {h: {"hypothesis": h, "experiment": "E093", "sign": s, "hold": 3, "days": [], "segments": {"PRE": seg(h, "PRE", e, s), "DEV": seg(h, "DEV", e / 2, s)}}
               for h, s, e in (("H1", -1, -0.004), ("H2", 1, 0.006))}
    adj = EV.holm({"H1": 0.001, "H2": 0.02})
    verdicts = {h: EV.judge(h, r["segments"]["PRE"], r["segments"]["DEV"], adj[h]) for h, r in results.items()}
    out = {"results": results, "holm": adj, "verdicts": verdicts, "events": [{"day": "2026-03-02", "industry": "x", "kind": "break", "leader": "600000", "leader_streak_prev": 3, "targets": 2}],
           "event_counts": {"break:PRE": 1}, "decision_days": {"first": "2025-11-14", "last": "2026-11-11", "sessions": 240}, "notes": ["n"], "cap": {"present": True}, "lhb_stock_days": 5, "bars_source": "adjusted"}
    man = HI.publish(svc, out, as_of=NOW)
    hist = RD.history_latest(conn, lake)
    assert hist["manifest_version"] == man["manifest_version"] and set(hist["results"]) == {"H1:PRE", "H1:DEV", "H2:PRE", "H2:DEV"}
    assert hist["results"]["H1:PRE"]["holm_p_adj"] == pytest.approx(adj["H1"]) and hist["results"]["H1:DEV"]["holm_p_adj"] is None
    assert hist["verdicts"]["H1"]["passed"] is True and hist["meta"]["spec_sha256"].startswith("93a4b8136a21") and len(hist["events"]) == 1
    wire = RD.wire({"spec_sha256": "x" * 64, "history": hist, "forward": {}, "fieldcheck": None, "first_evaluated": "2026-11-19", "sessions_decided": []})
    contracts.check("Overview", wire, contracts.backfill_id("batch6"))
    assert [r["segment"] for r in wire["history"]["rows"]] == ["DEV", "PRE", "DEV", "PRE"]
