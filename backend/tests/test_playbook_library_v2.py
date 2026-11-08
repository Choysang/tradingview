"""Playbook library v2 (sentiment cycle, price-volume factors, timing): hand-built panels.

Each playbook gets a small panel constructed so the rule fires on exactly the intended rows and
stays silent on near-misses. Truncation no-look-ahead and SIM coverage for these ids are already
exercised by the parametrised tests in test_playbook_library.py (they iterate the whole catalog).
"""
from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, timedelta

import polars as pl
import pytest

from backend.app.playbooks import base

NEW_IDS = (
    "ice_point_reflex", "divergence_to_consensus", "first_negative", "high_low_switch",
    "volume_price_divergence", "low_amplitude", "overnight_gap_reversal", "low_turnover_low_vol",
    "microcap_timed",
)

START = date(2026, 3, 2)  # a Monday: index 7, 14, 21 are Mondays; index 30 is April 1st

FLOAT_COLUMNS = (
    "open high low close prev_close volume amount ma5 vol_ma5 ret_5 ret_20 volatility_20 "
    "float_cap total_cap cap_est"
).split()
BOOL_COLUMNS = ("is_limit_up", "is_limit_down", "one_word")
SCHEMA = {
    "code": pl.String, "date": pl.Date,
    **{name: pl.Float64 for name in FLOAT_COLUMNS},
    **{name: pl.Boolean for name in BOOL_COLUMNS},
    "limit_streak": pl.Int64, "days_listed": pl.Int64,
}
DEFAULTS = {
    "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "prev_close": 10.0, "volume": 100.0, "amount": 5.0,
    "ma5": 10.0, "vol_ma5": 100.0, "ret_5": 0.0, "ret_20": 0.0, "volatility_20": 1.0,
    "float_cap": 100.0, "total_cap": 100.0, "cap_est": 100.0,
    "is_limit_up": False, "is_limit_down": False, "one_word": False, "limit_streak": 0, "days_listed": 200,
}


def day(index: int) -> date:
    return START + timedelta(days=index)


def bars(code: str, count: int, per_day: Callable[[int], dict] | None = None, **const) -> list[dict]:
    rows = []
    for i in range(count):
        row = {"code": code, "date": day(i), **DEFAULTS, **const}
        if per_day is not None:
            row.update(per_day(i))
        rows.append(row)
    return rows


def frame(rows: list[dict]) -> pl.LazyFrame:
    return pl.DataFrame(rows, schema=SCHEMA).sort("code", "date").lazy()


def run(playbook_id: str, rows: list[dict], **overrides: float) -> pl.DataFrame:
    pb = base.resolve(playbook_id)
    return pb.signal(frame(rows), pb.params(overrides)).collect()


def hits(signals: pl.DataFrame) -> set[tuple[str, date]]:
    return {(row["code"], row["date"]) for row in signals.iter_rows(named=True)}


# --- registry ---------------------------------------------------------------------------


def test_new_playbooks_registered_once_with_chinese_rules():
    ids = [pb.spec.id for pb in base.catalog()]
    assert len(ids) == len(set(ids))
    for playbook_id in NEW_IDS:
        pb = base.resolve(playbook_id)
        assert pb is not None, playbook_id
        for text in (pb.spec.name, pb.spec.summary, pb.spec.rule, pb.spec.source):
            assert text and re.search(r"[一-鿿]", text), (playbook_id, text)
        assert pb.requires_history == ()
    styles = {base.resolve(i).spec.style for i in NEW_IDS}
    assert styles <= {"sentiment", "trend", "momentum", "reversal", "breakout", "volume", "volatility", "value"}


# --- sentiment cycle --------------------------------------------------------------------


def ice_panel(days: int = 70, ice_at: int = 65, limit_downs: int = 3, rivals_on_ice_day: bool = False,
              one_word: bool = False) -> list[dict]:
    rows = bars("A", days, lambda i: {
        "is_limit_up": True, "limit_streak": 1, "amount": 7.0, "one_word": one_word} if i == ice_at + 1 else {})
    for k in range(limit_downs):
        rows += bars(f"F{k}", days, lambda i: {"is_limit_down": True} if i == ice_at else {})
    if rivals_on_ice_day:  # a limit-up on the ice day itself: today's count no longer exceeds yesterday's
        rows += bars("E", days, lambda i: {"is_limit_up": True, "limit_streak": 1} if i == ice_at else {})
    return rows


def test_ice_point_reflex_fires_on_first_board_after_an_ice_day():
    signals = run("ice_point_reflex", ice_panel())
    assert hits(signals) == {("A", day(66))}
    assert signals["score"].to_list() == [7.0]


@pytest.mark.parametrize("rows", [
    ice_panel(limit_downs=0),                 # no limit-downs at all: not an ice point
    ice_panel(ice_at=40),                     # fewer than 60 days of history behind the ice day
    ice_panel(rivals_on_ice_day=True),        # limit-ups did not grow on the day after
    ice_panel(one_word=True),                 # the first board is a 一字板, unbuyable
], ids=["no-ice", "short-history", "no-rebound", "one-word"])
def test_ice_point_reflex_near_misses_are_silent(rows):
    assert run("ice_point_reflex", rows).is_empty()


def divergence_panel(prev_volume: float = 200.0, volume: float = 100.0, prev_one_word: bool = False,
                     streak: int = 2) -> list[dict]:
    def per_day(i: int) -> dict:
        if i == 1:
            return {"is_limit_up": True, "limit_streak": streak - 1, "volume": prev_volume, "vol_ma5": 100.0,
                    "one_word": prev_one_word}
        if i == 2:
            return {"is_limit_up": True, "limit_streak": streak, "volume": volume}
        return {}
    return bars("A", 3, per_day)


def test_divergence_to_consensus_fires_and_scores_by_streak():
    signals = run("divergence_to_consensus", divergence_panel(streak=3))
    assert hits(signals) == {("A", day(2))}
    assert signals["score"].to_list() == [3.0]


@pytest.mark.parametrize("rows", [
    divergence_panel(volume=150.0),            # 150 >= 0.7 * 200: not shrunk enough
    divergence_panel(prev_volume=140.0),       # 140 < 1.5 * 100: yesterday was not a volume-divergence board
    divergence_panel(prev_one_word=True),      # yesterday was a 一字板
], ids=["not-shrunk", "not-divergent", "one-word-yesterday"])
def test_divergence_to_consensus_near_misses_are_silent(rows):
    assert run("divergence_to_consensus", rows).is_empty()


def first_negative_panel(**today) -> list[dict]:
    def per_day(i: int) -> dict:
        if i == 1:
            return {"is_limit_up": True, "limit_streak": today.pop("prev_streak", 3)}
        if i == 2:
            return {"open": 10.0, "close": 9.8, "ma5": 9.5, **today}
        return {}
    return bars("A", 3, per_day)


def test_first_negative_fires_after_a_chain_of_three_boards():
    signals = run("first_negative", first_negative_panel())
    assert hits(signals) == {("A", day(2))}
    assert signals["score"].to_list() == [3.0]


@pytest.mark.parametrize("rows", [
    first_negative_panel(prev_streak=2),       # chain shorter than 3
    first_negative_panel(close=10.3),          # not a black candle
    first_negative_panel(is_limit_down=True),  # a 跌停 is a collapse, not a first negative
    first_negative_panel(ma5=9.9),             # closed below the 5-day line
], ids=["short-chain", "up-candle", "limit-down", "below-ma5"])
def test_first_negative_near_misses_are_silent(rows):
    assert run("first_negative", rows).is_empty()


def high_low_panel(day1_top_streak: int = 0, day1_first_boards: int = 3) -> list[dict]:
    """Day 0: one 5-board dragon and two first boards. Day 1: candidates A/B/C (float 10/30/50), D is a 一字板."""
    rows = bars("X", 2, lambda i: {"is_limit_up": i == 0 or day1_top_streak > 0,
                                   "limit_streak": 5 if i == 0 else day1_top_streak})
    for code in ("P", "Q"):
        rows += bars(code, 2, lambda i: {"is_limit_up": True, "limit_streak": 1} if i == 0 else {})
    first_board = {"is_limit_up": True, "limit_streak": 1}
    candidates = [("A", 10.0), ("B", 30.0), ("C", 50.0)][:day1_first_boards]
    for code, cap in candidates:
        rows += bars(code, 2, lambda i: first_board if i == 1 else {}, float_cap=cap)
    if day1_first_boards == 3:
        rows += bars("D", 2, lambda i: {**first_board, "one_word": True} if i == 1 else {}, float_cap=1.0)
    return rows


def test_high_low_switch_picks_small_first_boards_below_the_median():
    signals = run("high_low_switch", high_low_panel())
    assert hits(signals) == {("A", day(1)), ("B", day(1))}  # median of {10, 30, 50} is 30; D is 一字板
    scores = dict(zip(signals["code"], signals["score"]))
    assert scores["A"] == pytest.approx(1 / 10) and scores["B"] == pytest.approx(1 / 30)


@pytest.mark.parametrize("rows", [
    high_low_panel(day1_top_streak=5),         # the dragon is still alive
    high_low_panel(day1_first_boards=1),       # day 1 has 1 first board vs 3 on day 0 (X's chain + P, Q)
], ids=["dragon-alive", "fewer-first-boards"])
def test_high_low_switch_near_misses_are_silent(rows):
    assert run("high_low_switch", rows).is_empty()


# --- weekly price-volume / amplitude / overnight factors -------------------------------------


def linear(i: int, start: float, step: float) -> float:
    return start + step * i


def test_volume_price_divergence_needs_both_low_correlation_and_low_return():
    rows: list[dict] = []
    # corr = -1 (most negative) but a strong 5-day gain: passes the correlation cut, fails the return cut.
    rows += bars("N1", 22, lambda i: {"close": 10.0 + i, "volume": 1000.0 - 50 * i}, ret_5=5.0)
    rows += bars("T0", 22, lambda i: {"close": 10.0 + i, "volume": 1000.0 - 50 * i + 9 * (i % 2)}, ret_5=-10.0)
    rows += bars("T1", 22, lambda i: {"close": 10.0 + i, "volume": 1000.0 - 50 * i + 20 * (i % 3)}, ret_5=-9.0)
    # the weakest 5-day return but a positive correlation: fails the correlation cut.
    rows += bars("N2", 22, lambda i: {"close": 10.0 + i, "volume": 100.0 + 10 * i}, ret_5=-30.0)
    for k in range(20):
        rows += bars(f"Z{k:02d}", 22, lambda i, k=k: {"close": 10.0 + i, "volume": 100.0 + 10 * i + k % 5}, ret_5=0.1 * k)
    signals = run("volume_price_divergence", rows)
    # Monday index 14 has no 20-day liquidity history yet; the signal day is Monday index 21.
    assert hits(signals) == {("T0", day(21)), ("T1", day(21))}
    scores = dict(zip(signals["code"], signals["score"]))
    assert 1.9 < scores["T0"] <= 2.0 and scores["T0"] > scores["T1"] > 1.0  # 1 - corr, more negative scores higher


def amplitude_rows() -> list[dict]:
    def shaped(close_last: float, half_range: float):
        def per_day(i: int) -> dict:
            close = close_last if i == 21 else 10.0
            prev = close_last if i > 21 else (10.0 if i != 21 else 10.0)
            return {"close": close, "open": close, "prev_close": prev if i else close,
                    "high": close + 10.0 * half_range if i != 21 else close * (1 + half_range),
                    "low": close - 10.0 * half_range if i != 21 else close * (1 - half_range)}
        return per_day

    rows: list[dict] = []
    rows += bars("N3", 22, shaped(10.0, 0.0075))   # smallest amplitude but flat close: position 0.5, fails the gate
    rows += bars("S0", 22, shaped(9.85, 0.01))     # tiny amplitude, closes near the bottom of its 20-day range
    rows += bars("S1", 22, shaped(9.85, 0.0105))
    rows += bars("N4", 22, shaped(9.0, 0.5))       # closes at the bottom but amplitude is huge
    for k in range(20):
        rows += bars(f"Z{k:02d}", 22, shaped(10.0, 0.05 + 0.001 * k))
    return rows


def test_low_amplitude_requires_low_mean_amplitude_and_low_range_position():
    signals = run("low_amplitude", amplitude_rows())
    assert hits(signals) == {("S0", day(21)), ("S1", day(21))}
    scores = dict(zip(signals["code"], signals["score"]))
    assert 0.9 < scores["S1"] < scores["S0"] < 1.0  # 1 / (1 + mean amplitude), smaller amplitude scores higher


def test_overnight_gap_reversal_picks_the_worst_overnight_sums_among_liquid_names():
    def gap(factor: float):
        return lambda i: {"open": 10.0 * factor, "prev_close": 10.0}
    rows: list[dict] = []
    for k in range(3):
        rows += bars(f"O{k}", 22, gap(0.99 - 0.001 * k))
    rows += bars("ILL", 22, gap(0.98), amount=0.1)       # worst overnight, but not liquid
    for k in range(20):
        rows += bars(f"Z{k:02d}", 22, gap(1.001 + 0.0001 * k))
    signals = run("overnight_gap_reversal", rows)
    assert hits(signals) == {(f"O{k}", day(21)) for k in range(3)}
    scores = dict(zip(signals["code"], signals["score"]))
    assert scores["O2"] > scores["O1"] > scores["O0"]  # O2 has the lowest sum, hence rank 1


# --- monthly factors ---------------------------------------------------------------------------


def turnover_rows(null_cap_for: str | None = "Z") -> list[dict]:
    rows: list[dict] = []
    for i in range(7):  # low turnover and low volatility, the intended picks
        rows += bars(f"G{i}", 31, volume=1e6 * (1 + 0.1 * i), volatility_20=1.0 + 0.1 * i)
    rows += bars("X", 31, volume=2e6, volatility_20=9.0)         # low turnover, high volatility
    rows += bars("Y", 31, volume=1e8, volatility_20=1.8)         # high turnover, low volatility
    rows += bars("Z", 31, volume=1.0, volatility_20=0.1, **({"float_cap": None} if null_cap_for else {}))
    for k in range(15):
        rows += bars(f"N{k:02d}", 31, volume=5e6 + 1e4 * k, volatility_20=3.0 + 0.01 * k)
    return rows


def test_low_turnover_low_vol_needs_both_ranks_and_skips_null_float_cap():
    signals = run("low_turnover_low_vol", turnover_rows())
    # April 1st is index 30; March's first day (index 0) has no 20-day history yet.
    assert hits(signals) == {(f"G{i}", day(30)) for i in range(7)}
    scores = dict(zip(signals["code"], signals["score"]))
    assert all(0 < value <= 1 for value in scores.values())
    assert scores["G0"] > scores["G3"] > scores["G6"]


def microcap_rows(small_ret: float, large_ret: float) -> list[dict]:
    rows: list[dict] = []
    for i in range(40):
        ret = small_ret if i < 4 else large_ret if i >= 36 else 0.0
        rows += bars(f"C{i:02d}", 31, float_cap=10.0 + 10.0 * i, ret_20=ret, days_listed=60 if i == 0 else 200)
    return rows


def test_microcap_timed_on_picks_smallest_tradable_names():
    signals = run("microcap_timed", microcap_rows(small_ret=5.0, large_ret=1.0))
    # 5% of the 39 tradable names is 2; C00 is a recent listing (days_listed 60) and is skipped.
    assert hits(signals) == {("C01", day(30)), ("C02", day(30))}
    scores = dict(zip(signals["code"], signals["score"]))
    assert scores["C01"] == pytest.approx(1 / 20) and scores["C02"] == pytest.approx(1 / 30)


def test_microcap_timed_off_when_microcaps_lag_large_caps():
    assert run("microcap_timed", microcap_rows(small_ret=-5.0, large_ret=1.0)).is_empty()
