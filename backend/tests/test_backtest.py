"""Backtest determinism, cost accounting and no-lookahead behaviour."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from backend.app.research.backtest import run
from backend.app.research.strategies import CATALOG, catalog, resolve
from contracts.schema import BacktestRequest, DataProvenance, OHLCBar


def _bars(count: int = 200) -> list[OHLCBar]:
    bars = []
    price = 20.0
    day = date(2026, 1, 5)
    for index in range(count):
        step = 0.4 if index % 7 < 4 else -0.3
        opening = round(price + step * 0.3, 2)
        closing = round(price + step, 2)
        bars.append(
            OHLCBar(
                date=(day + timedelta(days=index)).isoformat(),
                open=opening,
                high=round(max(opening, closing) + 0.2, 2),
                low=round(min(opening, closing) - 0.2, 2),
                close=closing,
                volume=1_000_000,
            )
        )
        price = closing
    return bars


def _provenance() -> DataProvenance:    return DataProvenance(
        mode="SIM",
        source="test",
        as_of="2026-10-20T10:00:00+08:00",
        trade_date="2026-10-20",
        detail="deterministic fixture",
        coverage=200,
    )


def test_catalog_entries_are_resolvable() -> None:
    assert catalog().strategies
    for strategy in catalog().strategies:
        assert resolve(strategy.id) is strategy
        assert strategy.signal in {"momentum", "breakout", "mean_reversion"}
        assert 0 < strategy.holding_days <= 20


def test_unknown_strategy_is_rejected() -> None:
    with pytest.raises(LookupError):
        run(BacktestRequest(strategy_id="does_not_exist"), _bars(), _provenance())


def test_insufficient_history_is_rejected() -> None:
    with pytest.raises(ValueError):
        run(BacktestRequest(lookback=20, holding_days=20), _bars(30), _provenance())


def test_backtest_is_deterministic() -> None:
    request = BacktestRequest(strategy_id="breakout_volume", lookback=20, holding_days=5)
    first = run(request, _bars(), _provenance())
    second = run(request, _bars(), _provenance())
    assert first.equity == second.equity
    assert first.trades == second.trades
    assert first.total_return == second.total_return


def test_equity_matches_final_return() -> None:
    result = run(BacktestRequest(strategy_id="momentum_ignition"), _bars(), _provenance())
    assert result.equity[-1].strategy == pytest.approx(result.total_return, abs=0.01)
    assert result.max_drawdown <= 0
    assert 0 <= result.exposure_pct <= 100


def test_costs_and_trade_arithmetic() -> None:
    result = run(BacktestRequest(strategy_id="momentum_ignition", holding_days=3), _bars(), _provenance())
    assert result.trade_count == len(result.trades)
    if result.trades:
        assert result.total_costs > 0
        for trade in result.trades:
            assert trade.shares % 100 == 0
            assert trade.costs > 0
            assert trade.entry_date < trade.exit_date
        assert result.win_rate is not None


def test_no_lookahead_execution() -> None:
    """A signal from bar i-1 must fill at bar i's open, never at bar i-1's close."""
    result = run(BacktestRequest(strategy_id="breakout_volume", lookback=10, holding_days=2), _bars(), _provenance())
    bars = {bar.date: bar for bar in _bars()}
    for trade in result.trades:
        bar = bars[trade.entry_date]
        assert trade.entry_price >= bar.open


def test_stamp_duty_only_affects_sell_side() -> None:
    base = run(BacktestRequest(strategy_id="momentum_ignition", stamp_duty_bps=0), _bars(), _provenance())
    taxed = run(BacktestRequest(strategy_id="momentum_ignition", stamp_duty_bps=10), _bars(), _provenance())
    assert taxed.total_return < base.total_return
    assert taxed.total_costs > base.total_costs


def test_breakout_signal_can_actually_fire() -> None:
    """A Donchian breakout compares to previous highs, not today's own high."""
    result = run(
        BacktestRequest(strategy_id="breakout_volume", lookback=10, holding_days=3),
        _bars(),
        _provenance(),
    )
    assert result.trade_count > 0
    assert result.total_return != 0


def test_all_catalog_strategies_execute() -> None:
    for strategy_id in CATALOG:
        result = run(
            BacktestRequest(strategy_id=strategy_id, lookback=15, holding_days=4),
            _bars(),
            _provenance(),
        )
        assert len(result.equity) == len(_bars())
        assert result.assumptions


def _gap_bars(entry_gap: float, exit_gap: float) -> list[OHLCBar]:
    """Rising series with a forced limit gap at entry and at exit."""
    bars: list[OHLCBar] = []
    price = 10.0
    day = date(2026, 1, 5)
    for index in range(10):
        opening = round(price * (1 + entry_gap if index == 6 else 1), 2) if index == 6 else round(price, 2)
        closing = round(price * 1.01, 2)
        if index == 7:
            opening = round(bars[6].close * (1 + exit_gap), 2)
        bars.append(
            OHLCBar(
                date=(day + timedelta(days=index)).isoformat(),
                open=opening,
                high=round(max(opening, closing) + 0.1, 2),
                low=round(min(opening, closing) - 0.1, 2),
                close=closing,
                volume=1_000_000,
            )
        )
        price = closing
    return bars


def test_limit_up_open_blocks_the_buy() -> None:
    """Opening at the limit means the queue is already full: no fill."""
    result = run(
        BacktestRequest(strategy_id="momentum_ignition", code="600000", lookback=5, holding_days=1),
        _gap_bars(entry_gap=0.10, exit_gap=0.0),
        _provenance(),
    )
    assert any("跳过买入" in line for line in result.assumptions)
    assert "跳过买入 0 次" not in " ".join(result.assumptions)


def test_limit_down_open_defers_the_sell() -> None:
    result = run(
        BacktestRequest(strategy_id="momentum_ignition", code="600000", lookback=5, holding_days=1),
        _gap_bars(entry_gap=0.0, exit_gap=-0.10),
        _provenance(),
    )
    assert any("顺延卖出" in line for line in result.assumptions)
