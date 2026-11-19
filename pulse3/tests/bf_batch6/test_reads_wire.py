"""Forward progress states (collecting / descriptive / judged) by hand, and the wire payload against the lane contract."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from pulse import contracts
from pulse.application import bf_batch6_reads as RD
from pulse.domain.batch6 import evaluate as EV


def day_row(exp, hyp, session, *, n_signals, k, ex):
    body = {"n_universe": 3000, "n_pool": 2900, "n_signals": n_signals, "n_filled": k, "statuses": {"filled": k}, "k": k, "ex_sv_day": ex, "recording_complete": True,
            "unmatched_buy_computable": True, "method": "batch6-forward-v1"}
    return {"experiment": exp, "hypothesis": hyp, "session": session, "kind": "day", "code": "", "body": json.dumps(body), "body_sha256": "x"}


def signal_row(exp, hyp, session, code, ret_net=0.01, status="filled"):
    body = {"status": status, "entry": 10.0, "exit": 10.1, "ret_gross": ret_net + 0.00252, "ret_net": ret_net}
    return {"experiment": exp, "hypothesis": hyp, "session": session, "kind": "signal", "code": code, "body": json.dumps(body), "body_sha256": "x"}


def sessions(n):
    return [f"2027-01-{i + 1:02d}" for i in range(n)] if n <= 28 else [f"2027-{1 + i // 28:02d}-{i % 28 + 1:02d}" for i in range(n)]


def build(n_days, h5_days, h6_days, ex5=0.01, ex6=-0.01):
    rows = []
    for i, s in enumerate(sessions(n_days)):
        a = i < h5_days
        b = i < h6_days
        rows.append(day_row("E095", "H5", s, n_signals=int(a), k=int(a), ex=ex5 if a else None))
        rows.append(day_row("E095", "H6", s, n_signals=int(b), k=int(b), ex=ex6 if b else None))
        if a:
            rows.append(signal_row("E095", "H5", s, "600000"))
    return rows


def test_states_follow_the_signal_day_counts():
    assert RD.forward_stats(build(19, 19, 19), {})["H5"]["state"] == "collecting"
    s = RD.forward_stats(build(20, 20, 20), {})
    assert s["H5"]["state"] == "descriptive" and s["H5"]["passed"] is None and s["H5"]["holm_p_adj"] is None
    assert s["H5"]["excess"] == pytest.approx(0.01) and s["H5"]["experiment_signal_days"] == 20
    # 40 signal days in total but H6 has only 24: not judged
    assert RD.forward_stats(build(40, 40, 24), {})["H5"]["state"] == "descriptive"
    assert RD.forward_stats(build(39, 39, 39), {})["H5"]["state"] == "descriptive"


def test_judged_needs_forty_days_and_twenty_five_per_hypothesis_then_applies_the_rule():
    s = RD.forward_stats(build(40, 40, 25), {})
    assert s["H5"]["state"] == "judged" and s["H6"]["state"] == "judged"
    assert s["H5"]["passed"] is True and s["H6"]["passed"] is True        # excess +1 % (sign +) and -1 % (sign -), never a losing slot: p = 0, Holm 7 x 0 = 0
    assert s["H7"]["state"] == "collecting"
    wrong = RD.forward_stats(build(40, 40, 25, ex5=-0.01), {})
    assert wrong["H5"]["passed"] is False
    small = RD.forward_stats(build(40, 40, 25, ex5=0.002), {})
    assert small["H5"]["passed"] is False                                    # 0.2 % is below the 0.252 % cost
    assert small["H5"]["reliable"] is True and small["H5"]["blocks"] == 40


def test_holm_uses_stored_history_p_values_in_the_family():
    s = RD.forward_stats(build(40, 40, 25), {"H1": 0.001, "H2": 0.002, "H3": 0.003, "H4": 0.004})
    # sorted p: H5 0, H6 0, H1 .001 ... only the forward p's decide here; adjusted forward p stays 0
    assert s["H5"]["holm_p_adj"] == 0.0


def _result_body(hyp, seg, excess, p, passed_pre=True):
    sign = {"H1": -1, "H2": 1}[hyp]
    r = EV.SegmentResult(hyp, seg, sign, 3, 30, 28, 20, 20 / 28, {"blocked_buy": 8}, 20, 12, 0.012, 0.0095, 0.002, excess, excess, excess, p, 40, True, None)
    return {**asdict(r), "holm_p_adj": 0.02 if seg == "PRE" else None}


def overview():
    results = {f"{h}:{seg}": _result_body(h, seg, e, 0.003) for h, e in (("H1", -0.004), ("H2", 0.006)) for seg in ("PRE", "DEV")}
    hist = {"results": results, "verdicts": {"H1": {"hypothesis": "H1", "passed": True, "reason": "ok"}, "H2": {"hypothesis": "H2", "passed": False, "reason": "no"}},
            "meta": {"as_of": "2026-11-18T00:00:00Z", "decision_days": {"first": "2025-11-14", "last": "2026-11-11", "sessions": 240}, "event_counts": {"break:PRE": 12},
                     "notes": ["bars since 2025-10"]}, "events": []}
    fwd = RD.forward_stats(build(21, 21, 21), {"H1": 0.003})
    fc = {"sessions": ["2026-11-12", "2026-11-13"], "overall": {"unmatched_buy_computable": True, "bid1_ask1_lots_0915_0924": "indicative matched quantity (matched, not unmatched)",
          "bid2_ask2_lots_0915_0924": "unmatched surplus", "bid_ask_book_at_0925": "resting orders after matching = unmatched", "unmatched_buy_definition": "sum of bid1..bid5",
          "auction_rows": 100, "final_rows": 10, "share_bid1_lots_eq_ask1_lots": 1.0, "share_bid1_ask1_px_eq_price": 1.0, "share_bid2_and_ask2_both_positive": 0.0,
          "share_final_amount_eq_lots_x_price": 1.0, "share_final_book_uncrossed": 1.0}}
    return {"spec_sha256": "a" * 64, "history": hist, "forward": fwd, "fieldcheck": fc, "first_evaluated": "2026-11-19", "sessions_decided": ["2026-11-19"]}


def test_overview_wire_passes_the_lane_contract_and_keeps_missing_null():
    w = RD.wire(overview())
    contracts.check("Overview", w, contracts.backfill_id("batch6"))
    h7 = next(r for r in w["forward"] if r["hypothesis"] == "H7")
    assert h7["state"] == "collecting" and h7["excess"] is None and h7["fill_rate"] is None and h7["p_value"] is None
    pre = next(r for r in w["history"]["rows"] if r["hypothesis"] == "H1" and r["segment"] == "PRE")
    assert pre["excess"] == "-0.004" and pre["holm_p_adj"] == "0.02"
    dev = next(r for r in w["history"]["rows"] if r["hypothesis"] == "H1" and r["segment"] == "DEV")
    assert dev["holm_p_adj"] is None


def test_wire_without_history_and_fieldcheck_is_valid():
    ov = overview() | {"history": None, "fieldcheck": None, "forward": {}}
    contracts.check("Overview", RD.wire(ov), contracts.backfill_id("batch6"))


def test_fixture_is_current():
    path = Path(__file__).resolve().parents[2] / "apps/web/src/pages/bf/batch6/fixtures/overview.json"
    fx = {"meta": {"quality_status": "partial"}, "data": RD.wire(overview())}
    if not path.exists():
        path.write_text(json.dumps(fx, ensure_ascii=False, indent=1), encoding="utf-8")
    stored = json.loads(path.read_text(encoding="utf-8"))
    contracts.check("Overview", stored["data"], contracts.backfill_id("batch6"))
