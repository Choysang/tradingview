"""Hand-computed cases: daily panel (limits, streak), forward legs with fill rules and deferral, Holm, pass rule, selections."""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from pulse.domain.batch6 import evaluate as EV
from pulse.domain.batch6 import panel as PN
from pulse.domain.batch6 import rules as R

DAYS = [date(2026, 3, d) for d in (2, 3, 4, 5, 6, 9)]


def bars(code: str, rows: list[tuple[int, int]]) -> list[dict]:
    """rows: (open, close) in cents per session; high / low are max / min of the two."""
    return [{"code": code, "date": d, "o": o, "h": max(o, c), "l": min(o, c), "c": c, "v": 100} for d, (o, c) in zip(DAYS, rows)]


def frame(*groups: list[dict]) -> pl.DataFrame:
    return pl.DataFrame([r for g in groups for r in g], schema={"code": pl.String, "date": pl.Date, "o": pl.Int64, "h": pl.Int64, "l": pl.Int64, "c": pl.Int64, "v": pl.Int64})


A = bars("A", [(1000, 1000), (1050, 1100), (1100, 1210), (1331, 1331), (1300, 1290), (1290, 1200)])
B = bars("B", [(1000, 1000), (1000, 1000), (1000, 900), (810, 830), (830, 830), (830, 830)])


def test_panel_limit_streak_hand_computed():
    p = PN.build_panel(frame(A), DAYS)
    a = p.filter(pl.col("code") == "A").sort("date")
    assert a["up_c"].to_list()[1:4] == [1100, 1210, 1331]              # 10.00 -> 11.00 ; 11.00 -> 12.10 ; 12.10 -> 13.31
    assert a["lu"].to_list() == [False, True, True, True, False, False]
    assert a["streak"].to_list() == [0, 1, 2, 3, 0, 0]
    assert a["amt"].to_list()[1] == pytest.approx(11.0 * 100)          # close x shares


def test_forward_filled_blocked_pending_and_returns():
    p = PN.build_panel(frame(A), DAYS)
    f = PN.forward(p, 2, len(DAYS)).filter(pl.col("code") == "A").sort("date")
    st = dict(zip(f["date"].to_list(), f["status"].to_list()))
    assert st[DAYS[0]] == "filled" and st[DAYS[1]] == "filled"
    assert st[DAYS[2]] == "blocked_buy"                                  # D+1 open 13.31 == limit-up of 12.10
    assert st[DAYS[3]] == "filled"
    assert st[DAYS[4]] == "pending_exit" and st[DAYS[5]] == "pending_entry"
    ret = dict(zip(f["date"].to_list(), f["ret"].to_list()))
    assert ret[DAYS[0]] == pytest.approx(1210 / 1050 - 1)                # buy d2 open 10.50, sell d3 close 12.10
    assert ret[DAYS[1]] == pytest.approx(1331 / 1100 - 1)
    assert ret[DAYS[3]] == pytest.approx(1200 / 1300 - 1)


def test_forward_sell_deferred_past_limit_down():
    p = PN.build_panel(frame(B), DAYS)
    f = PN.forward(p, 2, len(DAYS)).filter(pl.col("code") == "B").sort("date")
    row = f.row(0, named=True)                                           # D = d1: buy d2 open 10.00, planned exit d3 close 9.00 = limit-down
    assert row["status"] == "filled"
    assert row["exit_si"] == 3 and row["exit_c"] == 830                  # d4 open 8.10 is limit-down too; d4 close 8.30 sells
    assert row["ret"] == pytest.approx(830 / 1000 - 1)


def test_no_bar_on_entry_session_is_not_filled():
    rows = [r for r in A if r["date"] != DAYS[1]]
    p = PN.build_panel(frame(rows), DAYS)
    f = PN.forward(p, 1, len(DAYS)).sort("date")
    assert f.filter(pl.col("date") == DAYS[0])["status"][0] == "no_bar"


def test_holm_family_of_seven_with_untested_last():
    adj = EV.holm({"H1": 0.001, "H2": 0.02, "H3": 0.5, "H4": None})
    assert adj["H1"] == pytest.approx(0.007)                             # 7 x 0.001
    assert adj["H2"] == pytest.approx(0.12)                              # 6 x 0.02
    assert adj["H3"] == pytest.approx(1.0)                               # 5 x 0.5 -> capped at 1
    assert adj["H4"] is None
    mono = EV.holm({"a": 0.01, "b": 0.012})                              # 7 x 0.01 = 0.07 ; 6 x 0.012 = 0.072
    assert mono["a"] == pytest.approx(0.07) and mono["b"] == pytest.approx(0.072)
    assert EV.holm({"a": 0.02, "b": 0.001})["a"] == pytest.approx(0.12)


def _seg(excess, reliable=True, p=0.001):
    return EV.SegmentResult("H2", "PRE", 1, 3, 10, 10, 10, 1.0, {}, 10, 5, 0.0, 0.0, 0.0, excess, excess, excess, p, 20, reliable, None)


def test_pass_rule_needs_sign_size_significance_reliability_and_dev_sign():
    pre, dev = _seg(0.004), _seg(0.001)
    assert EV.judge("H2", pre, dev, 0.01).passed
    assert not EV.judge("H2", _seg(0.002), dev, 0.01).passed             # below 0.252 %
    assert not EV.judge("H2", pre, dev, 0.2).passed                      # not significant after Holm
    assert not EV.judge("H2", pre, _seg(-0.001), 0.01).passed            # DEV opposite
    assert not EV.judge("H2", _seg(0.004, reliable=False), dev, 0.01).passed
    assert not EV.judge("H2", pre, None, 0.01).passed                    # DEV missing is not a pass
    assert EV.judge("H1", _seg(-0.004), _seg(-0.0001), 0.01).passed      # H1 expects a negative excess
    assert not EV.judge("H1", _seg(0.004), _seg(0.001), 0.01).passed


def test_percent_rank_top_includes_ties_and_rounds_up():
    vals = {f"s{i}": float(i) for i in range(10)}
    assert EV.percent_rank_top(vals, 0.2) == {"s9", "s8"}
    assert EV.percent_rank_top(vals, 0.1) == {"s9"}
    assert EV.percent_rank_top({"a": 1.0, "b": 1.0, "c": 0.0}, 0.34) == {"a", "b"}      # ceil(0.34 x 3) = 2 ; tie at the cut
    assert EV.percent_rank_top({}, 0.2) == set()


def test_evaluate_segment_matched_excess_by_hand():
    # one day, pool of 10 names in one size x vol cell (>= 5 names): returns 0.00 .. 0.09, signal = the two names with 0.08 and 0.09
    rows = [(f"n{i}", i / 100.0, 1.0, 1.0) for i in range(10)]
    pool = EV.build_pool(rows)
    res = EV.evaluate_segment(hypothesis="H2", segment="PRE", sign=1, hold=3, all_days=["2026-03-02", "2026-03-03"], signals_by_day={"2026-03-02": ["n8", "n9"]},
                              pool_by_day={"2026-03-02": pool}, status_counts={"filled": 2, "blocked_buy": 1}, n_signals=3)
    # baseline mean of the cell = 0.045 ; signal mean 0.085 ; excess 0.04 ; net return after cost 0.085 - 0.00252
    assert res.excess == pytest.approx(0.04)
    assert res.mean_ret_net == pytest.approx(0.085 - 0.00252)
    assert res.fill_rate == pytest.approx(2 / 3)
    assert res.n_matched == 2 and res.signal_days == 1
    assert res.blocks == 1 and not res.reliable                          # 2 days, block 3 -> 1 block: report only (E082)


def test_excess_from_means_equals_the_shared_matched_excess():
    from pulse.domain.research import baseline as BL
    rows = [(f"n{i}", (i % 7) / 100.0, float(i % 3), float(i % 4)) for i in range(60)]
    pool = EV.build_pool(rows)
    sig = {"2026-03-02": ["n3", "n10", "n59", "missing"]}
    a = BL.matched_excess(sig, {"2026-03-02": pool})
    b = EV.excess_from_means(sig, {"2026-03-02": pool}, {"2026-03-02": BL.matched_means(pool)})
    assert (a.n, a.unmatched, a.days, a.ex_all, a.ex_sv, a.ex_svi, a.by_day) == (b.n, b.unmatched, b.days, b.ex_all, b.ex_sv, b.ex_svi, b.by_day)


def fbars(code, rows):
    """Forward-adjusted float bars: rows are (open, close)."""
    return [{"code": code, "date": d, "o": o, "h": max(o, c), "l": min(o, c), "c": c, "v": 100.0} for d, (o, c) in zip(DAYS, rows)]


def fframe(*groups):
    return pl.DataFrame([r for g in groups for r in g], schema={"code": pl.String, "date": pl.Date, "o": pl.Float64, "h": pl.Float64, "l": pl.Float64, "c": pl.Float64, "v": pl.Float64})


def test_adjusted_panel_limits_streak_and_forward_follow_the_fusion_definition():
    # adjusted prices 8.00 -> 8.80 -> 9.68 -> 10.65 (the limit of 9.68 is 10.648 -> 10.65): three limit-up closes, then a drop
    a = fbars("A", [(8.0, 8.0), (8.4, 8.8), (8.8, 9.68), (10.65, 10.65), (10.0, 9.9), (9.9, 9.5)])
    p = PN.build_panel_adjusted(fframe(a), DAYS).filter(pl.col("code") == "A").sort("date")
    assert p["lu"].to_list() == [False, True, True, True, False, False]
    assert p["streak"].to_list() == [0, 1, 2, 3, 0, 0]
    assert p["up_c"][1] == pytest.approx(8.8 - 0.005) and p["down_c"][1] == pytest.approx(7.2 + 0.005)      # tolerant thresholds: half a cent
    assert p["amt"][1] == pytest.approx((8.8 + 8.4 + 8.8) / 3 * 100.0)                                       # typical price x shares
    f = PN.forward(PN.build_panel_adjusted(fframe(a), DAYS), 2, len(DAYS)).filter(pl.col("code") == "A").sort("date")
    assert f.schema["entry_c"] == pl.Float64
    st = dict(zip(f["date"].to_list(), f["status"].to_list()))
    assert st[DAYS[0]] == "filled" and st[DAYS[2]] == "blocked_buy"                                         # D+1 open 10.65 is at the limit-up of 9.68
    assert f.filter(pl.col("date") == DAYS[0])["ret"][0] == pytest.approx(9.68 / 8.4 - 1)


def test_adjusted_panel_has_no_ex_day_jump_and_the_raw_panel_loses_the_limit_there():
    # a stock with a 1.00 dividend: raw 10.00 -> ex-day reference 9.00, a +10 % limit-up close of 9.90 is a raw drop of 1 % ; adjusted keeps the true +10 %
    raw = fbars("R", [(10.0, 10.0), (9.0, 9.9), (9.9, 9.9), (9.9, 9.9), (9.9, 9.9), (9.9, 9.9)])
    adj = fbars("R", [(9.0, 9.0), (9.0, 9.9), (9.9, 9.9), (9.9, 9.9), (9.9, 9.9), (9.9, 9.9)])
    pr = PN.build_panel_adjusted(fframe(raw), DAYS).filter(pl.col("code") == "R").sort("date")
    pa = PN.build_panel_adjusted(fframe(adj), DAYS).filter(pl.col("code") == "R").sort("date")
    assert pr["lu"][1] is False and pa["lu"][1] is True and pa["streak"][1] == 1
