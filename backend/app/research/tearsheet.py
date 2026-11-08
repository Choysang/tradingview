"""Tear-sheet statistics (quantstats / pyfolio style) from a backtest's equity curve.

Everything derives from the `EquityPoint` list and the `TradeRecord` list, so the
numbers can be re-checked by hand. Returns are percentage points; ratios are plain
numbers; unknowns (too short a curve, no losing trade) stay None, never 0.
"""
from __future__ import annotations

from contracts.schema import BacktestMetrics, EquityPoint, MonthlyReturn, TradeRecord

TRADING_DAYS = 252


def _values(equity: list[EquityPoint], initial_cash: float) -> list[float]:
    """Portfolio value at each bar's close. equity[0] is already the first bar."""
    return [initial_cash * (1 + point.strategy / 100) for point in equity]


def _returns(values: list[float]) -> list[float]:
    return [values[i] / values[i - 1] - 1 for i in range(1, len(values)) if values[i - 1] > 0]


def _annualised(growth: float, periods: int) -> float | None:
    if periods < 1 or growth <= 0:
        return None
    return (growth ** (TRADING_DAYS / periods) - 1) * 100


def _std(values: list[float]) -> float:
    mean = sum(values) / len(values)
    return (sum((v - mean) ** 2 for v in values) / (len(values) - 1)) ** 0.5


def _underwater(values: list[float]) -> tuple[float, int]:
    """Max drawdown (fraction, <= 0) and the longest stretch of bars below the prior peak."""
    peak, worst, run, longest = values[0], 0.0, 0, 0
    for value in values[1:]:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
        run = run + 1 if value < peak else 0
        longest = max(longest, run)
    return worst, longest


def _holding_days(trades: list[TradeRecord], equity: list[EquityPoint]) -> float | None:
    index = {point.date: i for i, point in enumerate(equity)}
    spans = [index[t.exit_date] - index[t.entry_date] for t in trades if t.entry_date in index and t.exit_date in index]
    return round(sum(spans) / len(spans), 2) if spans else None


def _max_losing_streak(trades: list[TradeRecord]) -> int:
    streak = longest = 0
    for trade in trades:
        streak = streak + 1 if trade.pnl < 0 else 0
        longest = max(longest, streak)
    return longest


def _r(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def metrics(equity: list[EquityPoint], trades: list[TradeRecord], initial_cash: float) -> BacktestMetrics:
    values = _values(equity, initial_cash)
    periods = len(equity) - 1  # returns between consecutive bars
    daily = _returns(values)
    annual = _annualised(values[-1] / values[0], periods) if equity else None
    bench = _annualised((1 + equity[-1].benchmark / 100) / (1 + equity[0].benchmark / 100), periods) if equity else None
    volatility = _std(daily) * TRADING_DAYS ** 0.5 * 100 if len(daily) > 2 else None

    sortino = None
    if len(daily) > 2:
        downside = (sum(min(r, 0.0) ** 2 for r in daily) / len(daily)) ** 0.5
        if downside > 1e-12:
            sortino = sum(daily) / len(daily) / downside * TRADING_DAYS ** 0.5

    worst, underwater_days = _underwater(values) if equity else (0.0, 0)
    calmar = annual / abs(worst * 100) if annual is not None and worst < 0 else None

    gains = sum(t.pnl for t in trades if t.pnl > 0)
    losses = sum(t.pnl for t in trades if t.pnl < 0)
    returns = [t.return_pct for t in trades]
    return BacktestMetrics(
        annual_return=_r(annual),
        annual_volatility=_r(volatility),
        benchmark_annual_return=_r(bench),
        sortino_ratio=_r(sortino),
        calmar_ratio=_r(calmar),
        max_drawdown_days=underwater_days if equity else None,
        profit_factor=_r(gains / abs(losses)) if losses < 0 else None,
        expectancy=_r(sum(returns) / len(returns)) if returns else None,
        avg_holding_days=_holding_days(trades, equity),
        best_trade=max(returns) if returns else None,
        worst_trade=min(returns) if returns else None,
        max_consecutive_losses=_max_losing_streak(trades),
    )


def monthly(equity: list[EquityPoint]) -> list[MonthlyReturn]:
    """Month-end strategy value over the previous month-end (the first month starts at 1.0)."""
    ends: dict[str, float] = {}
    for point in equity:
        ends[point.date[:7]] = 1 + point.strategy / 100
    out, previous = [], 1.0
    for month, value in ends.items():
        out.append(MonthlyReturn(month=month, value=round((value / previous - 1) * 100, 3)))
        previous = value
    return out
