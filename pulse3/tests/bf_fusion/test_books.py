"""Frozen book rules on a tiny market: entry at the next open, T+1, hold days, lots, costs, slots, gate and decision rows."""
from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pytest

from pulse.domain.fusion import books as BK

DAYS = [date(2026, 11, 2) + timedelta(days=i) for i in range(14)]
CODES = ["600001", "600002", "600003"]


def market(opens, closes=None, *, at_limit=None, lock=None):
    """Grids for the three stocks; ``opens``/``closes`` are [T, 3]."""
    o = np.array(opens, dtype=np.float64)
    c = o.copy() if closes is None else np.array(closes, dtype=np.float64)
    z = np.zeros_like(o)
    return {"open": o, "close": c, "open_at_limit": z.copy() if at_limit is None else at_limit,
            "is_limit_down": z.copy() if lock is None else lock, "open_at_floor": z.copy() if lock is None else lock}


def sim(grid, signals, *, hold=2, slots=2, per_day=1, start=DAYS[0], end=None):
    return BK.simulate(DAYS, CODES, grid, signals, hold=hold, slots=slots, per_day=per_day, start=start, end=end)


def flat_prices(T=14, price=10.0):
    return [[price] * 3 for _ in range(T)]


def lots(equity: float, slots: int, price: float, cash: float) -> int:
    return int(min(equity / slots, cash) / (price * BK.BUY_FEE) // BK.LOT) * BK.LOT


def test_a_signal_is_bought_at_the_next_open_in_whole_lots_and_sold_at_the_close_after_the_hold():
    g = market(flat_prices())
    run = sim(g, {DAYS[0]: ["600001"]}, hold=2)
    t = run.trades[0]
    shares = lots(1_000_000, 2, 10.0, 1_000_000)
    assert shares == 49900 and (t.code, t.entry_date, t.exit_date, t.exit_reason) == ("600001", DAYS[1], DAYS[3], "time")
    assert t.holding_days == 2
    # round trip on flat prices loses exactly the two fees
    assert t.return_pct == pytest.approx((BK.SELL_FEE / BK.BUY_FEE - 1) * 100, abs=1e-4)
    cost = shares * 10.0 * BK.BUY_FEE
    assert run.nav[-1] == pytest.approx((1_000_000 - cost + shares * 10.0 * BK.SELL_FEE) / 1e6 * 100, rel=1e-9)


def test_t_plus_one_the_entry_day_cannot_be_the_exit_day_and_hold_counts_sessions():
    g = market(flat_prices())
    assert sim(g, {DAYS[0]: ["600001"]}, hold=1).trades[0].exit_date == DAYS[2]


def test_names_that_open_sealed_at_the_limit_or_have_no_bar_are_skipped_and_the_next_name_is_taken():
    at = np.zeros((14, 3))
    at[1, 0] = 1
    o = np.array(flat_prices())
    o[1, 1] = np.nan
    g = market(o, at_limit=at)
    run = sim(g, {DAYS[0]: ["600001", "600002", "600003"]}, hold=2)
    assert [t.code for t in run.trades] == ["600003"]


def test_slots_and_new_names_per_day_are_limits_and_a_held_name_is_not_bought_twice():
    g = market(flat_prices())
    sig = {d: ["600001", "600002", "600003"] for d in DAYS[:6]}
    run = sim(g, sig, hold=5, slots=2, per_day=2)
    entries = sorted((t.entry_date, t.code) for t in run.trades)
    assert entries[:2] == [(DAYS[1], "600001"), (DAYS[1], "600002")]       # two names on the first day
    assert max(sum(1 for t in run.trades if t.entry_date <= d < t.exit_date) for d in DAYS) <= 2


def test_a_locked_limit_down_delays_the_sale_to_the_next_open_that_is_not_locked():
    lock = np.zeros((14, 3))
    lock[3, 0] = 1                                   # exit day: closed and opened at the floor
    o = np.array(flat_prices())
    c = o.copy()
    c[3, 0] = 9.0
    o[4, 0] = 8.8
    g = market(o, c, lock=lock)
    t = sim(g, {DAYS[0]: ["600001"]}, hold=2).trades[0]
    assert t.exit_date == DAYS[4] and t.exit_price == 8.8 and t.exit_reason == "time"


def test_positions_open_on_the_last_day_close_at_that_close_with_reason_end():
    g = market(flat_prices())
    run = sim(g, {DAYS[0]: ["600001"]}, hold=5, end=DAYS[3])
    assert run.trades[0].exit_reason == "end" and run.trades[0].exit_date == DAYS[3]
    assert run.held_at_end() == ["600001"]


def test_gate_compares_the_previous_nav_with_the_mean_of_the_twenty_before_it():
    nav = np.array([100.0] * 20 + [90.0, 90.0, 120.0])
    w = BK.gate(nav, 20, 0.25)
    assert np.isnan(w).sum() == 0 and w[:20].tolist() == [1.0] * 20
    # day 20: previous nav 100 vs mean of days 0..19 = 100 -> not below; day 21: previous 90 < mean(100*19 + 90)/20 -> gated
    assert w[20] == 1.0 and w[21] == 0.25 and w[22] == 0.25


def test_keep_count_rounds_half_to_even_like_the_old_log():
    assert BK.keep_count(1.0, 4) == 4 and BK.keep_count(0.25, 4) == 1 and BK.keep_count(0.5, 2) == 1 and BK.keep_count(0.25, 2) == 0


def test_top_signals_orders_by_score_with_ties_in_code_order_and_drops_non_finite():
    scores = {DAYS[0]: (["b", "a", "c", "d"], np.array([1.0, 1.0, 2.0, np.nan]))}
    assert BK.top_signals(scores, 3)[DAYS[0]] == ["c", "b", "a"]          # a stable sort keeps the input order of equal scores


def test_gated_signals_cut_the_list_on_gated_days_only():
    sig = {DAYS[0]: ["a", "b", "c", "d"], DAYS[1]: ["a", "b", "c", "d"]}
    out = BK.gated_signals(sig, {DAYS[0]: 0.25, DAYS[1]: 1.0}, 4)
    assert out == {DAYS[0]: ["a"], DAYS[1]: ["a", "b", "c", "d"]}
    assert BK.gated_signals(sig, {DAYS[0]: 0.25}, 2)[DAYS[0]] == []         # round(0.5) = 0 stops all buying


def test_decision_row_lists_the_names_the_book_will_try_to_buy_not_already_held():
    g = market(flat_prices(), None)
    scores = {d: (CODES, np.array([3.0, 2.0, 1.0])) for d in DAYS}
    rule = BK.BookRule("t", 2, 0.25, "rule text", slots=2, offer=3)
    gated, raw, w, sig = BK.run_book(rule, DAYS, CODES, g, scores, start=DAYS[0], end=DAYS[9])
    row = BK.decision_row(rule, gated, raw, w, sig, scores_digest="abc")
    assert row["date"] == DAYS[9].isoformat() and row["offer_top10"] == CODES
    assert set(row["holdings"]) <= set(CODES) and all(c not in row["holdings"] for c in row["try_buy"])
    assert row["rule"] == "rule text" and row["scores_sha256_16"] == "abc"
    assert row["new_buys_allowed"] == rule.per_day or row["gate_w"] < 1
    assert math.isfinite(row["nav"]) and row["ungated_nav"] > 90


def test_the_run_is_deterministic():
    g = market(flat_prices())
    sig = {d: ["600001", "600003"] for d in DAYS[:8]}
    a, b = sim(g, sig), sim(g, sig)
    assert a.trades == b.trades and np.array_equal(a.nav, b.nav)


def test_book_rules_are_the_pre_registered_ones():
    assert (BK.MAIN.hold, BK.MAIN.gate_low, BK.MAIN.per_day, BK.MAIN.slots, BK.MAIN.offer, BK.MAIN.look) == (5, 0.25, 4, 20, 40, 20)
    assert (BK.H10.hold, BK.H10.gate_low, BK.H10.per_day) == (10, 0.5, 2)
    assert BK.FORWARD_START == date(2024, 8, 20) and BK.FROZEN == date(2026, 11, 1)
    assert BK.COMMISSION_BP == 2.5 and BK.STAMP_BP == 5.0 and BK.SLIPPAGE_BP == 5.0
