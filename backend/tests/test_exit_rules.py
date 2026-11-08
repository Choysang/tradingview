"""Sell rules: each fires on the bar described in its contract text and sells at the price worked out on paper."""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from backend.app.playbooks.backtest import Costs, simulate
from contracts.schema import ExitRules, SizingRule

ZERO = Costs(0, 0, 0)
D0 = date(2026, 3, 2)
ONE = SizingRule(method="equal", max_positions=1, max_new_per_day=1)
BASE = dict(holding_days=10, stop_loss_pct=None, take_profit_pct=None, trailing_stop_pct=None)


def day(i: int) -> date:
    return D0 + timedelta(days=i)


def panel(bars: list[dict]) -> pl.DataFrame:
    """Code A follows `bars`; filler Z keeps the calendar. Missing fields get neutral values."""
    rows = []
    prev = None
    for i, b in enumerate(bars):
        close = b.get("close", b["open"])
        rows.append({
            "code": "A", "date": day(i), "open": float(b["open"]), "high": float(b.get("high", max(b["open"], close))),
            "low": float(b.get("low", min(b["open"], close))), "close": float(close),
            "prev_close": float(prev if prev is not None else b["open"]), "atr_14": float(b.get("atr", 1.0)),
            "ma5": float(b.get("ma5", 0.0)), "volume": float(b.get("volume", 100.0)), "vol_ma5": float(b.get("vol_ma5", 100.0)),
            "is_limit_up": b.get("limit_up", False), "is_limit_down": b.get("limit_down", False),
            "open_at_limit": b.get("open_at_limit", False), "open_at_floor": b.get("open_at_floor", False),
            "ma20": float(b.get("ma20", float("nan"))), "is_broken": b.get("broken", False),
        })
        prev = close
    for i in range(len(bars)):
        rows.append({
            "code": "Z", "date": day(i), "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "prev_close": 10.0,
            "atr_14": 0.1, "ma5": 10.0, "volume": 1.0, "vol_ma5": 1.0, "is_limit_up": False, "is_limit_down": False,
            "open_at_limit": False, "open_at_floor": False, "ma20": 10.0, "is_broken": False,
        })
    return pl.DataFrame(rows)


SIGNAL = pl.DataFrame({"code": ["A"], "date": [day(0)], "score": [1.0]})


def one_trade(bars: list[dict], ebb_days: frozenset = frozenset(), **rules):
    trades, _ = simulate(panel(bars), SIGNAL, ExitRules(**{**BASE, **rules}), ONE, costs=ZERO, ebb_days=ebb_days)
    assert len(trades) == 1
    return trades[0]


def test_weak_open_sells_at_the_open_of_the_day_after_entry():
    bars = [{"open": 10}, {"open": 10}, {"open": 9.7, "close": 10.5}, {"open": 11}]
    trade = one_trade(bars, weak_open_pct=2)
    assert trade.exit_reason == "weak_open" and trade.exit_date == day(2).isoformat() and trade.exit_price == 9.7


def test_weak_open_sells_at_the_open_before_the_day_low_reaches_the_stop():
    bars = [{"open": 10}, {"open": 10}, {"open": 9.7, "low": 9.0, "close": 9.2}, {"open": 9.2}]
    trade = one_trade(bars, weak_open_pct=2, stop_loss_pct=6)
    assert trade.exit_reason == "weak_open" and trade.exit_price == 9.7


def test_weak_open_is_only_checked_on_the_first_sellable_day():
    bars = [{"open": 10}, {"open": 10}, {"open": 10.1}, {"open": 9.0}, {"open": 9.0}]
    trade = one_trade(bars, weak_open_pct=2)
    assert trade.exit_reason != "weak_open"


def test_not_limit_on_the_entry_bar_sells_the_next_open():
    bars = [{"open": 10, "limit_up": True}, {"open": 11, "close": 11.5}, {"open": 11.2}, {"open": 12}]
    trade = one_trade(bars, sell_if_not_limit=True)
    assert trade.exit_reason == "not_limit" and trade.exit_date == day(2).isoformat() and trade.exit_price == 11.2


def test_a_board_that_holds_keeps_the_position():
    bars = [{"open": 10}, {"open": 11, "close": 12.1, "limit_up": True}, {"open": 12.5, "close": 13.3, "limit_up": True},
            {"open": 13.0, "close": 12.8}, {"open": 12.6}]
    trade = one_trade(bars, sell_if_not_limit=True)
    assert trade.exit_reason == "not_limit" and trade.exit_date == day(4).isoformat()


def test_close_below_ma5_sells_next_open():
    bars = [{"open": 10}, {"open": 10, "ma5": 9}, {"open": 10, "close": 9.5, "ma5": 9.8}, {"open": 9.4}]
    trade = one_trade(bars, sell_below_ma5=True)
    assert trade.exit_reason == "below_ma5" and trade.exit_date == day(3).isoformat() and trade.exit_price == 9.4


def test_volume_stall_sells_next_open():
    bars = [{"open": 10}, {"open": 10, "close": 10.1, "volume": 300, "vol_ma5": 100}, {"open": 10.2}]
    trade = one_trade(bars, sell_on_stall=True)
    assert trade.exit_reason == "stall" and trade.exit_date == day(2).isoformat()


def test_fade_from_a_big_intraday_high_sells_next_open():
    bars = [{"open": 10}, {"open": 10.2, "high": 10.9, "close": 10.2}, {"open": 10.0}]
    trade = one_trade(bars, sell_on_fade=True)
    assert trade.exit_reason == "fade" and trade.exit_price == 10.0


def test_chandelier_trails_the_highest_close_by_atr_multiples():
    bars = [{"open": 10}, {"open": 10, "close": 12, "atr": 0.5}, {"open": 12, "close": 11.2, "atr": 0.5}, {"open": 11.0}]
    trade = one_trade(bars, chandelier_atr=1.5)  # 11.2 < 12 - 1.5 * 0.5 = 11.25
    assert trade.exit_reason == "chandelier" and trade.exit_price == 11.0


def test_ebb_day_sells_next_open():
    bars = [{"open": 10}, {"open": 10}, {"open": 10.5}, {"open": 10.4}]
    trade = one_trade(bars, ebb_days=frozenset({day(2)}), sell_on_ebb=True)
    assert trade.exit_reason == "ebb" and trade.exit_date == day(3).isoformat()


def test_rules_off_change_nothing():
    bars = [{"open": 10}, {"open": 10, "close": 9, "ma5": 11, "volume": 900}, {"open": 9}, {"open": 9}]
    trade = one_trade(bars)
    assert trade.exit_reason == "end"
