"""Panel definitions of the fusion lane on small synthetic bars (limit flags, ST flag, listing age, caps, tradable, grids)."""
from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from pulse.domain.fusion import panel as P

D0 = date(2024, 1, 1)


def weekdays(n: int, start: date = D0) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def bars(code: str, closes: list[float], *, highs=None, lows=None, opens=None, volume=1e8, cap: float | None = 50.0,
         list_date: date | None = date(2000, 1, 3), has_total=True) -> pl.DataFrame:
    n = len(closes)
    days = weekdays(n)
    return pl.DataFrame({
        "code": [code] * n, "date": days, "open": opens or closes, "high": highs or closes, "low": lows or closes, "close": closes,
        "volume": [volume] * n, "float_cap_100m": [cap] * n, "has_total_cap": [has_total] * n, "list_date": [list_date] * n},
        schema={"code": pl.String, "date": pl.Date, "open": pl.Float64, "high": pl.Float64, "low": pl.Float64, "close": pl.Float64,
                "volume": pl.Float64, "float_cap_100m": pl.Float64, "has_total_cap": pl.Boolean, "list_date": pl.Date})


def test_amount_is_typical_price_times_volume_in_100_millions():
    long = P.build_long(bars("600001", [10.0, 10.0], highs=[11.0, 11.0], lows=[9.0, 9.0], volume=2e6))
    assert long["amount"][0] == pytest.approx((11 + 9 + 10) / 3 * 2e6 / 1e8)


def test_only_main_board_codes_survive():
    long = P.build_long(pl.concat([bars("600001", [10.0] * 3), bars("300001", [10.0] * 3), bars("688001", [10.0] * 3)]))
    assert long["code"].unique().to_list() == ["600001"]


def test_limit_up_flags_use_the_rounded_limit_price_and_half_a_cent():
    closes = [10.0, 11.0, 12.1, 12.0]           # +10%, +10%, then down
    highs = [10.0, 11.0, 12.1, 12.3]
    long = P.build_long(bars("600001", closes, highs=highs))
    assert long["is_limit_up"].to_list() == [False, True, True, False]
    assert long["limit_streak"].to_list() == [0, 1, 2, 0]
    assert long["touched_limit"].to_list() == [False, True, True, False]


def test_broken_board_open_at_limit_and_limit_down():
    #               prev 10 -> day1: high touches 11.0 but closes 10.5 (broken); day2 opens at the floor and closes there
    long = P.build_long(bars("600001", [10.0, 10.5, 9.45], highs=[10.0, 11.0, 9.45], lows=[10.0, 10.0, 9.45],
                             opens=[10.0, 10.0, 9.45]))
    assert long["is_broken"].to_list() == [False, True, False]
    assert long["is_limit_down"].to_list() == [False, False, True]       # 10.5 * 0.9 = 9.45
    assert long["open_at_floor"].to_list() == [False, False, True]
    assert long["open_at_limit"].to_list() == [False, False, False]
    assert long["pct_chg"][1] == pytest.approx(5.0)
    assert long["prev_close"].to_list() == [None, 10.0, 10.5]


def test_prev_close_skips_missing_sessions_it_is_the_previous_row():
    b = bars("600001", [10.0, 10.0, 11.0]).filter(pl.col("date") != weekdays(3)[1])
    long = P.build_long(b)
    assert long["prev_close"].to_list() == [None, 10.0]
    assert long["is_limit_up"].to_list() == [False, True]


def test_price_st_flag_needs_two_exact_five_percent_closes_and_no_wide_move():
    flat = [10.0] * 30
    # two closes on the exact +5% limit (at the high) inside the window
    seq = flat + [10.5, 11.03, 11.03, 11.03]
    long = P.build_long(bars("600001", seq, highs=[10.0] * 30 + [10.5, 11.03, 11.03, 11.03]))
    assert long["st_like"][31]              # two hits: 10.5 (+5%) and 11.03 (10.5 * 1.05 = 11.025 -> 11.03 within a tick)
    assert not long["st_like"][30]           # one hit is not enough
    # a +6% day inside the window clears the flag
    wide = P.build_long(bars("600001", flat + [10.6, 10.6 * 1.05], highs=[10.0] * 30 + [10.6, 10.6 * 1.05]))
    assert not wide["st_like"][-1]


def test_days_listed_counts_rows_plus_weekdays_before_the_first_bar():
    first = weekdays(3)[0]
    long = P.build_long(bars("600001", [10.0] * 3, list_date=first - timedelta(days=7)))   # a week = 5 weekdays before the first bar
    assert long["days_listed"].to_list() == [6, 7, 8]
    # without a listing date: long listed when trading on the frame's first date, else its own row count
    two = pl.concat([bars("600001", [10.0] * 3, list_date=None), bars("600002", [10.0] * 2, list_date=None).with_columns(
        (pl.col("date") + pl.duration(days=7)).alias("date"))])
    out = P.build_long(two)
    assert out.filter(pl.col("code") == "600001")["days_listed"][0] == 1 + P.LONG_LISTED
    assert out.filter(pl.col("code") == "600002")["days_listed"].to_list() == [1, 2]


def test_float_cap_is_scaled_by_close_over_last_close_and_needs_the_total_cap():
    long = P.build_long(bars("600001", [10.0, 20.0, 40.0], cap=80.0))
    assert long["float_cap"].to_list() == pytest.approx([20.0, 40.0, 80.0])
    none = P.build_long(bars("600001", [10.0, 20.0], cap=80.0, has_total=False))
    assert none["float_cap"].to_list() == [None, None]
    assert P.build_long(bars("600001", [10.0, 20.0], cap=None))["float_cap"].to_list() == [None, None]


def test_tradable_rule_liquidity_listing_age_price_and_st():
    n = 140
    good = P.build_long(bars("600001", [10.0] * n))
    t = good["tradable"].to_list()
    assert not any(t[:19]) and t[19]                                   # 20 rows of amount 10 >= 0.5 and listed long ago
    cheap = P.build_long(bars("600002", [1.9] * n))
    assert not any(cheap["tradable"].to_list())                        # close < 2
    thin = P.build_long(bars("600003", [10.0] * n, volume=1e5))
    assert not any(thin["tradable"].to_list())                         # amount 0.01
    young = P.build_long(bars("600004", [10.0] * n, list_date=weekdays(1)[0]))
    ty = young["tradable"].to_list()
    assert not ty[118] and ty[119] and young["days_listed"][119] == 120   # needs 120 sessions


def test_retradable_recomputes_rolling_parts_inside_the_cut_but_keeps_other_columns():
    long = P.build_long(bars("600001", [10.0] * 60))
    cut = P.retradable(long.filter(pl.col("date") >= long["date"][30]))
    assert not any(cut["tradable"].to_list()[:19]) and cut["tradable"][19]
    assert cut["days_listed"].to_list() == long.filter(pl.col("date") >= long["date"][30])["days_listed"].to_list()


def test_grids_nan_for_missing_bars_and_mkt_is_the_mean_over_tradable_names():
    a, b = bars("600001", [10.0, 10.0, 10.0]), bars("600002", [10.0, 11.0], list_date=None)
    long = P.build_long(pl.concat([a, b]))
    p = P.to_panel(long)
    assert p.codes == ["600001", "600002"] and p.T == 3
    assert math.isnan(p.f["close"][2, 1])
    assert p.f["is_limit_up"].dtype == np.float32 and p.f["close"].dtype == np.float32
    assert p.f["turn"][0, 0] == pytest.approx(10.0 * 1e8 / 1e8 / 50.0 * 100, rel=1e-5)


def test_label_is_the_demeaned_forward_return_and_blocked_names_are_missing():
    T, N = 8, 3
    o = np.full((T, N), 10.0, dtype=np.float32)
    o[:, 0] = [10, 10, 11, 12, 13, 14, 15, 16]
    f = {"open": o, "open_at_limit": np.zeros((T, N), dtype=np.float32), "tradable": np.ones((T, N), dtype=np.float32),
         "close": o.copy()}
    f["open_at_limit"][2, 1] = 1.0                       # stock 1 opens sealed at the limit on day 2
    p = P.Panel([date(2024, 1, 1) + timedelta(days=i) for i in range(T)], ["a", "b", "c"], f)
    y = p.label(2)
    # day 1: buy open[2], sell open[4]: a 11 -> 13 (+18.18%); b blocked (NaN); c 0%; mean of finite = 9.0909
    assert y[1, 0] == pytest.approx(18.1818 - 9.0909, abs=1e-3) and math.isnan(y[1, 1]) and y[1, 2] == pytest.approx(-9.0909, abs=1e-3)
    assert math.isnan(y[T - 3, 0])                       # no open two sessions after the next one


def test_cgo_is_close_over_the_turnover_weighted_reference_minus_one():
    close = np.full((70, 1), 10.0, dtype=np.float32)
    close[65, 0] = 11.0
    turn = np.full((70, 1), 10.0, dtype=np.float32)      # 10 percent a day
    cgo = P.capital_gains_overhang(close, turn)
    assert math.isnan(cgo[58, 0]) and cgo[59, 0] == pytest.approx(0.0, abs=1e-6)
    assert cgo[65, 0] == pytest.approx(11.0 / (0.1 * 11.0 + 0.9 * 10.0) - 1, rel=1e-5)


def test_prospect_value_is_negative_for_symmetric_returns_and_needs_enough_bars():
    r = np.tile(np.array([[0.05], [-0.05]], dtype=np.float32), (30, 1))
    tk = P.prospect_value(r, 60)
    assert math.isnan(tk[58, 0]) and tk[59, 0] < 0                     # loss aversion
    sparse = r.copy()
    sparse[:20, 0] = np.nan                                             # fewer than 45 finite bars
    assert math.isnan(P.prospect_value(sparse, 60)[59, 0])
