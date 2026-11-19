"""Forward experiments on the frozen score: outcomes, matched cells, coverage gate, log semantics, X20 path, OS20 helpers."""
from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from pulse.domain.fusion import forward as FW

D = [date(2026, 11, 2) + timedelta(days=i) for i in range(30)]


def frame(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows)


def test_outcomes_buy_at_the_next_open_and_sell_at_the_close_h_rows_later_and_exclude_sealed_opens():
    rows = []
    for j, code in enumerate(["a", "b"]):
        for i in range(8):
            rows.append({"code": code, "date": D[i], "open": 10.0 + i, "close": 20.0 + i, "tradable": True,
                         "open_at_limit": code == "b" and i == 3})
    out = FW.outcomes(frame(rows)).filter(pl.col("code") == "a").sort("date")
    # day 0: buy open[1] = 11, sell close[1 + 4]... r5 = close[t+5] / open[t+1] - 1
    assert out["r5"][0] == pytest.approx((25.0 / 11.0 - 1) * 100)
    b = FW.outcomes(frame(rows)).filter(pl.col("code") == "b").sort("date")
    assert bool(b["in_pool"][2]) is False and bool(b["in_pool"][0]) is True      # day 3 opens sealed: the signal of day 2 is not buyable
    assert bool(b["in_pool"][7]) is False                                         # no next bar
    assert out["r5"][4] is None                                                   # no close five rows later


def test_matched_cell_falls_back_to_the_size_cell_when_the_cell_has_fewer_than_five_names():
    rows = []
    for k in range(12):
        rows.append({"code": f"c{k}", "date": D[0], "in_pool": True, "size5": 1.0, "vol5": 1.0 if k < 8 else 2.0, "r5": float(k)})
    out = FW.matched_cells(frame(rows).with_columns(pl.col("size5").cast(pl.Float64)), 5)
    big = out.filter(pl.col("vol5") == 1.0)["m_sv"].unique().to_list()
    small = out.filter(pl.col("vol5") == 2.0)["m_sv"].unique().to_list()
    assert big == [pytest.approx(3.5)]                     # eight names: the cell mean of 0..7
    assert small == [pytest.approx(5.5)]                   # four names are fewer than five: the size cell mean of all twelve


def test_coverage_status_thresholds_are_98_and_95_percent_of_the_previous_session():
    by = {D[0]: {f"c{i}" for i in range(100)}, D[1]: {f"c{i}" for i in range(99)}, D[2]: {f"c{i}" for i in range(95)},
          D[3]: {f"c{i}" for i in range(50)}}
    got = FW.coverage_status(by)
    assert got[D[1]] == (0.99, "ok")
    assert got[D[2]][1] == "degraded" and got[D[2]][0] == pytest.approx(95 / 99, abs=1e-6)
    assert got[D[3]][1] == "refuse"


def test_log_semantics_latest_row_wins_and_flagged_dates_are_rewritten_once_unflagged():
    assert FW.needs_row(None, False) and FW.needs_row(None, True)
    assert not FW.needs_row({"flagged": False}, False) and not FW.needs_row({"flagged": True}, True)
    assert FW.needs_row({"flagged": True}, False)
    rows = [{"date": "2026-11-02", "v": 1}, {"date": "2026-11-02", "v": 2}, {"date": "2026-11-03", "v": 3}]
    assert FW.latest_by_date(rows) == {"2026-11-02": {"date": "2026-11-02", "v": 2}, "2026-11-03": {"date": "2026-11-03", "v": 3}}


def test_monitor_new_rows_flag_a_window_with_a_refused_session_and_replace_it_later():
    import polars as pl_

    daily = pl_.DataFrame({"date": [D[0]], "n": [100], "ic": [0.05], "decile_spread": [1.0], "top_ex": [0.1], "top_sv": [0.2]})
    days = D[:8]
    bad = {D[3]: (0.5, "refuse")}
    r = FW.monitor_new_rows(daily, days, bad, {})
    assert r[0]["flagged"] is True and r[0]["coverage_fail_days"] == [D[3].isoformat()]
    # the window of date D0 is D1..D5: a refusal outside it does not flag
    assert FW.monitor_new_rows(daily, days, {D[7]: (0.5, "refuse")}, {})[0]["flagged"] is False
    # the gap is filled later: a new row replaces the flagged one
    again = FW.monitor_new_rows(daily, days, {}, {D[0].isoformat(): r[0]})
    assert again[0]["flagged"] is False and again[0]["replaces_flagged"] is True
    assert FW.monitor_new_rows(daily, days, {}, {D[0].isoformat(): again[0]}) == []


def test_rebalance_cost_is_sold_plus_bought_weight_times_the_side_cost():
    assert FW.rebalance_cost(set(), {"a", "b"}, 0.001) == pytest.approx(0.001)           # first build: only the bought weight
    assert FW.rebalance_cost({"a", "b"}, {"a", "b"}, 0.001) == 0.0
    assert FW.rebalance_cost({"a", "b"}, {"b", "c"}, 0.001) == pytest.approx((0.5 + 0.5) * 0.001)
    assert FW.rebalance_cost({"a"}, set(), 0.001) == 0.0


def test_x20_basket_drops_the_lowest_fifth_and_pays_the_rebalance_cost_against_the_equal_weight_pool():
    codes = ["a", "b", "c", "d", "e"]
    days = D[:3]
    rows = []
    for d in days:
        for code in codes:
            rows.append({"code": code, "date": d, "open": 10.0, "close": 10.0 if d == days[1] and code != "a" else (12.0 if code == "a" and d == days[1] else 10.0)})
    panel = frame(rows)
    pool = frame([{"code": code, "date": days[0], "q": q} for code, q in zip(codes, [0.1, 0.3, 0.5, 0.7, 0.9])])
    path = FW.x20_path(panel, pool)
    assert [r["date"] for r in path] == [days[1].isoformat(), days[2].isoformat()]
    first = path[0]
    side = FW.X20_SIDE
    assert first["n_x20"] == 4 and first["n_ew"] == 5 and first["trade_day"] is True
    assert first["x20"] == pytest.approx(1 - 1 * side, abs=1e-6)                          # nothing sold, everything bought: one side cost
    assert first["ew"] == pytest.approx((1 - side) * (1 + 0.2 / 5), abs=1e-6)             # a closes +20% and is only in the pool
    assert first["excess_pct"] == pytest.approx((first["x20"] / first["ew"] - 1) * 100, abs=1e-3)
    assert "trade_day" not in path[1]


def test_adj_quantile_subtracts_the_industry_mean_when_five_names_exist_else_the_pool_mean():
    rows, pop = [], []
    for k in range(10):
        ind = "big" if k < 6 else "small"
        for i, d in enumerate(D[:2]):
            rows.append({"code": f"c{k}", "date": d, "close": 10.0 * (1 + (0.01 * k if i == 1 else 0.0))})
            pop.append({"code": f"c{k}", "date": d, "industry": ind})
    panel = frame(rows)
    q = FW.adj_quantile(panel, frame(pop), 1).sort("code")
    assert q.height == 10 and q["q"].min() > 0 and q["q"].max() == 1.0
    # in the small industry (4 names < 5) the pool mean is used: a plain cross-section rank of 1-day returns
    assert q.filter(pl.col("code") == "c9")["q"][0] == 1.0


def test_block_bootstrap_p_is_zero_for_a_series_that_is_always_positive_and_one_for_negative():
    pos = np.full(40, 0.5)
    assert FW.block_boot_p(pos, 10, 500, 1) == 0.0
    assert FW.block_boot_p(-pos, 10, 500, 1) == 1.0
    assert FW.block_boot_p(np.array([np.nan]), 10, 10, 1) is None
    x = np.random.default_rng(2).normal(0, 1, 60)
    assert FW.block_boot_p(x, 10, 500, 7) == FW.block_boot_p(x, 10, 500, 7)                # seeded: repeatable


def test_nw_t_matches_the_plain_t_for_an_uncorrelated_series_and_needs_ten_values():
    x = np.array([1.0, -1.0] * 10)
    assert FW.nw_t(x[:9], 5) is None
    assert FW.nw_t(np.full(20, 2.0), 5) is None                                          # no variance
    t = FW.nw_t(x + 0.5, 0)
    assert t == pytest.approx((0.5) / math.sqrt(1 / 20), rel=1e-9)


def test_os20_summary_and_row_shape():
    rows = [{"date": "2026-11-02", "mkt20": -7.5, "ex_OS20_GAP": 1.0, "ex_OS20_NOGAP": 0.5, "raw_OS20_GAP": 0.2, "n_OS20_GAP": 100, "flagged": False},
            {"date": "2026-11-03", "mkt20": 2.0, "ex_OS20_GAP": 3.0, "ex_OS20_NOGAP": 1.5, "raw_OS20_GAP": 1.0, "n_OS20_GAP": 120, "flagged": False}]
    s = FW.os20_summary(rows)
    assert s["dates"] == 2 and s["ex_gap"] == 2.0 and s["diff_gap_nogap"] == 1.0 and s["mean_n_gap"] == 110.0
    assert s["net_raw_gap"] == pytest.approx(0.6 - FW.OS20_COST)
    assert s["ex_gap_down"]["dates"] == 1 and s["ex_gap_up"]["mean"] == 3.0
    r = FW.os20_row({"date": D[0], "n_OS20_GAP": 3, "ex_OS20_GAP": 1.23456, "raw_OS20_GAP": None}, {D[0]: -3.14159})
    assert r["n_OS20_GAP"] == 3 and r["ex_OS20_GAP"] == 1.235 and r["raw_OS20_GAP"] is None and r["mkt20"] == -3.142
    assert r["n_OS5_GAP"] == 0


def test_monitor_summary_counts_unflagged_dates_and_reports_what_remains_to_the_formal_check():
    rows = [{"date": f"d{i}", "ic": 0.05, "decile_spread": 1.0, "top_sv": 0.1, "flagged": i % 4 == 0} for i in range(12)]
    s = FW.monitor_summary(rows)
    assert s["decision_dates"] == 12 and s["unflagged"] == 9 and s["remaining"] == 91 and s["mean_ic"] == 0.05
