"""Buy rules: no chasing a gap and a resting pullback order, filled at the price worked out on paper."""
from __future__ import annotations

import polars as pl

from backend.app.playbooks import exits as exit_lab
from backend.app.playbooks.backtest import simulate
from contracts.schema import EntryRules, ExitRules
from test_exit_rules import BASE, ONE, SIGNAL, ZERO, day, panel


def trades_for(bars: list[dict], signals: pl.DataFrame = SIGNAL, **entry):
    trades, _ = simulate(panel(bars), signals, ExitRules(**BASE), ONE, costs=ZERO, entry=EntryRules(**entry))
    return trades


def test_default_entry_buys_the_next_open():
    trade = trades_for([{"open": 10}, {"open": 10.8, "close": 11}, {"open": 11}])[0]
    assert trade.entry_date == day(1).isoformat() and trade.entry_price == 10.8


def test_no_chase_skips_a_gap_above_the_limit_and_buys_below_it():
    assert trades_for([{"open": 10}, {"open": 10.4, "close": 11}, {"open": 11}], max_gap_pct=3) == []
    trade = trades_for([{"open": 10}, {"open": 10.2, "close": 11}, {"open": 11}], max_gap_pct=3)[0]
    assert trade.entry_price == 10.2


def test_pullback_fills_at_the_limit_when_the_low_touches_it():
    trade = trades_for([{"open": 10}, {"open": 10.1, "low": 9.7, "close": 10.3}, {"open": 10.5}], pullback_pct=2)[0]
    assert trade.entry_date == day(1).isoformat() and trade.entry_price == 9.8


def test_pullback_fills_at_the_open_when_it_opens_below_the_limit():
    trade = trades_for([{"open": 10}, {"open": 9.5, "close": 9.9}, {"open": 10}], pullback_pct=2)[0]
    assert trade.entry_price == 9.5


def test_pullback_skips_when_the_low_never_reaches_the_order():
    assert trades_for([{"open": 10}, {"open": 10.1, "low": 9.9, "close": 10.4}, {"open": 10.5}], pullback_pct=2) == []


def test_a_skipped_signal_frees_the_slot_for_the_next_one():
    bars = [{"open": 10}, {"open": 10.5, "close": 11}, {"open": 11}]
    signals = pl.DataFrame({"code": ["A", "Z"], "date": [day(0), day(0)], "score": [2.0, 1.0]})
    trades = trades_for(bars, signals, max_gap_pct=3)
    assert [t.code for t in trades] == ["Z"]


def test_entry_text_names_the_order_price():
    assert exit_lab.entry_text(EntryRules()) == "次日开盘买；一字涨停买不到就放弃。"
    assert "9.80 元" in exit_lab.entry_text(EntryRules(pullback_pct=2), close=10.0)
    assert "10.30 元" in exit_lab.entry_text(EntryRules(max_gap_pct=3), close=10.0)


def test_entry_variants_are_offered_to_every_horizon():
    from backend.app.playbooks.base import resolve

    for playbook_id in ("first_board_relay", "turtle_55"):
        keys = {key for key, _, _ in exit_lab.variants_for(resolve(playbook_id))}
        assert {"no_chase", "pullback"} <= keys
