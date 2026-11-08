"""Sell rules from the 2026-10 survey, scaling out and 做T: each worked out on paper, costs off."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from backend.app.playbooks import exits as exit_lab
from backend.app.playbooks.backtest import simulate
from contracts.schema import ExitRules, SizingRule
from test_exit_rules import BASE, ONE, SIGNAL, ZERO, day, one_trade, panel

TWO = SizingRule(method="equal", max_positions=2, max_new_per_day=1)  # half the cash stays free for a 正T


def trade_with(bars: list[dict], sizing: SizingRule = ONE, **rules):
    trades, _ = simulate(panel(bars), SIGNAL, ExitRules(**{**BASE, **rules}), sizing, costs=ZERO)
    assert len(trades) == 1
    return trades[0]


# --- sell rules ---------------------------------------------------------------------------


def test_broken_board_sells_next_open():
    bars = [{"open": 10}, {"open": 10, "close": 10.2}, {"open": 10.5, "high": 11.2, "close": 10.6, "broken": True},
            {"open": 10.4}, {"open": 10}]
    trade = one_trade(bars, sell_on_broken=True)
    assert trade.exit_reason == "broken" and trade.exit_date == day(3).isoformat() and trade.exit_price == 10.4


def test_long_upper_shadow_on_heavy_volume_sells_only_in_profit():
    shadow = {"open": 10.5, "high": 12, "low": 10.4, "close": 10.6, "volume": 300}
    bars = [{"open": 10}, {"open": 10}, shadow, {"open": 10.4}, {"open": 10}]
    trade = one_trade(bars, sell_on_shadow=True)
    assert trade.exit_reason == "shadow" and trade.exit_date == day(3).isoformat()
    losing = [{"open": 10}, {"open": 11}, shadow, {"open": 10.4}, {"open": 10}]  # bought at 11, close 10.6
    assert one_trade(losing, sell_on_shadow=True).exit_reason != "shadow"
    quiet = [{"open": 10}, {"open": 10}, {**shadow, "volume": 150}, {"open": 10.4}, {"open": 10}]
    assert one_trade(quiet, sell_on_shadow=True).exit_reason != "shadow"


def test_close_far_above_the_20_day_line_sells_next_open():
    bars = [{"open": 10}, {"open": 10, "ma20": 9.5}, {"open": 10.2, "close": 10.5, "ma20": 9.0}, {"open": 10.3}, {"open": 10}]
    trade = one_trade(bars, bias_exit_pct=15)  # 10.5 > 9.0 x 1.15 = 10.35; on day 1, 10 < 9.5 x 1.15
    assert trade.exit_reason == "bias" and trade.exit_date == day(3).isoformat() and trade.exit_price == 10.3


# --- scaling out ----------------------------------------------------------------------------


def test_scale_out_sells_half_at_the_target_and_keeps_the_rest():
    bars = [{"open": 10}, {"open": 10}, {"open": 10.2, "high": 10.6, "close": 10.4}, {"open": 10.4, "close": 10.8},
            {"open": 10.8, "close": 11}]
    trade = trade_with(bars, holding_days=3, scale_out_pct=5)
    # 100,000 shares at 10: half sold at 10.5, half at the time exit (day 4 close 11).
    assert trade.exit_reason == "time" and trade.return_pct == pytest.approx(7.5)


# --- 做T --------------------------------------------------------------------------------------


def t_bars(day2: dict) -> list[dict]:
    """Signal day 0, entry at the day-1 open of 10, a T day 2, a quiet day 3, time exit at the day-4 close of 10."""
    return [{"open": 10}, {"open": 10, "close": 10}, {"open": 10, **day2}, {"open": 10.1, "close": 10.1}, {"open": 10, "close": 10}]


def test_reverse_t_sells_half_at_the_resting_price_and_buys_back_at_the_close():
    trade = trade_with(t_bars({"high": 10.4, "low": 10, "close": 10.1}), holding_days=3, t_high_pct=3)
    # 50,000 shares sold at 10.3, bought back at 10.1: +10,000 on a 1,000,000 position.
    assert trade.return_pct == pytest.approx(1.0) and trade.exit_reason == "time" and trade.t_trades == 1


def test_reverse_t_buys_back_only_what_its_money_affords_when_fully_invested():
    trade = trade_with(t_bars({"high": 10.4, "low": 10, "close": 10.35}), holding_days=3, t_high_pct=3)
    # 515,000 from 50,000 at 10.3 buys 49,700 at 10.35 (605 left); 99,700 shares sold at 10.
    assert trade.return_pct == pytest.approx((99_700 * 10 + 605) / 1_000_000 * 100 - 100)


def sealed_limit_up(day3: dict) -> list[dict]:
    bars = t_bars({"high": 11, "low": 10, "close": 11, "limit_up": True})
    bars[3] = day3
    bars[4] = {"open": 11, "close": 11}
    return bars


def test_reverse_t_buys_back_at_the_next_open_after_a_sealed_limit_up():
    bars = sealed_limit_up({"open": 11.3, "close": 11})  # below that day's own 倒T price (11 x 1.03): no second T
    trade = trade_with(bars, TWO, holding_days=3, t_high_pct=3)
    # 50,000 held; 25,000 sold at 10.3, bought back at the day-3 open of 11.3; all 50,000 sold at 11.
    assert trade.return_pct == pytest.approx(5.0) and trade.t_trades == 1
    # Fully invested, the 515,000 of sale money buys back 45,500 at 11.3 (850 left); 95,500 sold at 11.
    alone = trade_with(bars, holding_days=3, t_high_pct=3)
    assert alone.return_pct == pytest.approx((95_500 * 11 + 850) / 1_000_000 * 100 - 100)


def test_reverse_t_waits_while_the_open_is_at_the_limit():
    bars = sealed_limit_up({"open": 12.1, "close": 12.1, "limit_up": True, "open_at_limit": True})
    bars[4] = {"open": 12, "close": 12}
    trade = trade_with(bars, TWO, holding_days=3, t_high_pct=3)
    # 25,000 sold at 10.3, no buy at the day-3 limit open, bought back at the day-4 open of 12; 50,000 sold at 12.
    assert trade.return_pct == pytest.approx((600_000 + 257_500 - 300_000) / 500_000 * 100 - 100) and trade.t_trades == 1


def test_a_reverse_t_that_cannot_buy_back_one_lot_ends_the_trade():
    bars = [{"open": 9000}, {"open": 9000, "close": 9000}, {"open": 9000, "high": 9900, "close": 9900, "limit_up": True},
            {"open": 10300, "close": 10300}, {"open": 10300, "close": 10300}]
    trade = trade_with(bars, holding_days=3, t_high_pct=3, t_share=1)
    # 100 shares bought for 900,000 (100,000 left); sold at 9,270; 1,027,000 cannot buy 100 at 10,300.
    assert trade.exit_reason == "t_out" and trade.exit_date == day(3).isoformat()
    assert trade.return_pct == pytest.approx(3.0)


def test_an_exit_with_a_leg_open_sells_only_the_shares_held_and_returns_the_sale_money():
    bars = sealed_limit_up({"open": 11.3, "close": 11})
    bars[2]["ma20"] = 9.0  # close 11 > 9 x 1.15: the bias rule sells at the day-3 open
    trade = trade_with(bars, TWO, holding_days=3, t_high_pct=3, bias_exit_pct=15)
    # 25,000 sold at 10.3 on day 2; the other 25,000 at the day-3 open of 11.3; nothing bought back.
    assert trade.exit_reason == "bias" and trade.return_pct == pytest.approx((257_500 + 282_500) / 500_000 * 100 - 100)


def test_forward_t_buys_low_and_sells_base_shares_at_the_close():
    trade = trade_with(t_bars({"high": 10, "low": 9.6, "close": 9.9}), TWO, holding_days=3, t_low_pct=3)
    # 50,000 shares held; 25,000 bought at 9.7, 25,000 sold at 9.9: +5,000 on 500,000.
    assert trade.return_pct == pytest.approx(1.0)


def test_forward_t_is_skipped_without_cash():
    trade = trade_with(t_bars({"high": 10, "low": 9.6, "close": 9.9}), ONE, holding_days=3, t_low_pct=3)
    assert trade.return_pct == pytest.approx(0.0) and trade.t_trades == 0


def test_forward_t_sells_at_the_next_tradable_open_after_a_sealed_limit_down():
    bars = t_bars({"high": 10, "low": 9.0, "close": 9.0, "limit_down": True})
    bars[3] = {"open": 8.1, "close": 8.1, "limit_down": True, "open_at_floor": True}  # locked: cannot sell
    bars[4] = {"open": 8.5, "close": 10}
    trade = trade_with(bars, TWO, holding_days=3, t_low_pct=3)
    # 25,000 bought at 9.7 on day 2, sold at the day-4 open of 8.5; the 50,000 base shares sold at 10.
    assert trade.return_pct == pytest.approx((500_000 - 25_000 * 9.7 + 25_000 * 8.5) / 500_000 * 100 - 100)


def test_both_resting_orders_fill_on_a_wide_day_whatever_came_first():
    trade = trade_with(t_bars({"high": 10.4, "low": 9.6, "close": 10}), TWO, holding_days=3, t_high_pct=3, t_low_pct=3)
    # 25,000 sold at 10.3 and 25,000 bought at 9.7: +15,000 on 500,000.
    assert trade.return_pct == pytest.approx(3.0)


def test_no_t_on_the_entry_bar_or_when_the_price_only_touches_the_order():
    entry_day = [{"open": 10}, {"open": 10, "high": 10.6, "close": 10}, {"open": 10, "close": 10}, {"open": 10, "close": 10},
                 {"open": 10, "close": 10}]
    assert trade_with(entry_day, holding_days=3, t_high_pct=3).return_pct == pytest.approx(0.0)
    touch = t_bars({"high": 10.3, "low": 10, "close": 10.1})  # exactly at 10.3: not traded through
    assert trade_with(touch, holding_days=3, t_high_pct=3).return_pct == pytest.approx(0.0)


def test_scale_out_comes_first_and_the_t_uses_the_shares_left():
    trade = trade_with(t_bars({"high": 10.4, "low": 10, "close": 10.1}), TWO, holding_days=3, scale_out_pct=3, t_high_pct=3)
    # 25,000 scaled out at 10.3; 12,500 of the rest sold at 10.3 and bought back at 10.1; 25,000 sold at 10.
    assert trade.return_pct == pytest.approx((257_500 + 2_500 + 250_000) / 500_000 * 100 - 100)


def random_panel(seed: int, n_codes: int = 4, n_days: int = 80) -> pl.DataFrame:
    """Random walks with limit-up and limit-down closes, for checks that must hold on any path."""
    rng = np.random.default_rng(seed)
    rows = []
    for k in range(n_codes):
        prev = 10.0
        for i in range(n_days):
            pct = float(np.clip(rng.normal(0, 0.045), -0.1, 0.1))
            close = round(prev * (1 + pct), 2)
            up, down = pct >= 0.0995, pct <= -0.0995
            o = round(prev * (1 + float(np.clip(rng.normal(0, 0.02), -0.1, 0.1))), 2)
            high = max(o, close) * (1 if up else 1 + abs(rng.normal(0, 0.02)))
            low = min(o, close) * (1 if down else 1 - abs(rng.normal(0, 0.02)))
            rows.append({
                "code": f"C{k}", "date": date(2026, 1, 5) + timedelta(days=i), "open": o, "high": high, "low": low,
                "close": close, "prev_close": prev, "atr_14": 0.3, "ma5": prev, "volume": 100.0, "vol_ma5": 100.0,
                "is_limit_up": up, "is_limit_down": down, "open_at_limit": bool(up and rng.random() < 0.3),
                "open_at_floor": bool(down and rng.random() < 0.3), "ma20": prev, "is_broken": False,
            })
            prev = close
    return pl.DataFrame(rows)


@pytest.mark.parametrize("seed", range(6))
def test_t_trades_never_spend_more_cash_than_the_book_has(seed):
    panel_ = random_panel(seed)
    signals = panel_.select("code", "date").with_columns(score=pl.lit(1.0))
    sizing = SizingRule(method="equal", max_positions=3, max_new_per_day=3)
    rules = ExitRules(**{**BASE, "holding_days": 6}, t_high_pct=2, t_low_pct=2, scale_out_pct=4)
    trades, equity = simulate(panel_, signals, rules, sizing)
    assert trades and sum(t.t_trades for t in trades) > 0
    # Exposure above 1 would mean cash below zero: a T that borrowed money.
    assert equity["exposure"].max() <= 1 + 1e-9
    assert equity["strategy"].min() > 0


def test_rules_off_leave_the_trade_unchanged():
    bars = t_bars({"high": 10.4, "low": 9.6, "close": 10.1})
    assert trade_with(bars, holding_days=3).return_pct == pytest.approx(0.0)


# --- lab and plan text ----------------------------------------------------------------------


def test_plan_text_names_the_new_rules():
    plan = exit_lab.plan_text(ExitRules(
        holding_days=10, stop_loss_pct=None, scale_out_pct=10, t_high_pct=3, t_low_pct=3, sell_on_broken=True,
        sell_on_shadow=True, bias_exit_pct=15,
    ))
    text = "\n".join(plan)
    for phrase in ("先卖一半", "做T", "卖出 5 成底仓", "炸板", "长上影", "乖离过大"):
        assert phrase in text, phrase
    assert plan[-1].startswith("以上都没触发")


def test_lab_offers_t_and_stop_width_to_every_playbook_and_board_rules_to_limit_plays_only():
    from backend.app.playbooks.base import resolve

    relay = {key for key, _, _ in exit_lab.variants_for(resolve("first_board_relay"))}
    reversal = {key for key, _, _ in exit_lab.variants_for(resolve("rsi_oversold"))}
    trend = {key for key, _, _ in exit_lab.variants_for(resolve("turtle_55"))}
    assert {"t_high", "t_low", "t_both", "stop_tight", "stop_wide"} <= relay & reversal & trend
    assert "broken" in relay and "broken" not in reversal
    assert {"bias", "chandelier_wide", "chandelier_tight", "scale_half"} <= trend
