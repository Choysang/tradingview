"""Deterministic event-driven backtest. Every number is derived from the bars.

Execution model, stated explicitly in `assumptions`:
- a signal is evaluated on a completed bar's close;
- the order fills at the next bar's open with slippage;
- commission is charged on both sides, stamp duty on the sell side;
- position size is rounded down to whole 100-share lots.
No random numbers and no pre-computed returns are used anywhere.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from contracts.schema import (
    BacktestRequest,
    BacktestResponse,
    DataProvenance,
    EquityPoint,
    OHLCBar,
    TradeRecord,
)
from . import tearsheet
from .strategies import resolve

CST = timezone(timedelta(hours=8))
LOT = 100
TRADING_DAYS = 252


def _signal(kind: str, closes: list[float], highs: list[float], index: int, lookback: int) -> bool:
    if index < lookback:
        return False
    window = closes[index - lookback + 1 : index + 1]
    mean = sum(window) / len(window)
    if kind == "momentum":
        return closes[index] > mean and closes[index] > closes[index - 1]
    if kind == "breakout":
        # Compare against the previous N highs. Including today's high would make
        # the condition close >= high, which almost never holds.
        return closes[index] >= max(highs[index - lookback : index])
    return closes[index] < mean * 0.97


def _limit_ratio(code: str) -> float:
    if code.startswith(("30", "68")):
        return 0.20
    if code.startswith(("4", "8")):
        return 0.30
    return 0.10


def _blocked_by_limit(code: str, previous_close: float, opening: float, rising: bool) -> bool:
    """A gap at the limit means the order queue cannot be filled at the open."""
    if previous_close <= 0:
        return False
    gap = opening / previous_close - 1
    edge = _limit_ratio(code) - 0.005
    return gap >= edge if rising else gap <= -edge


def run(request: BacktestRequest, bars: list[OHLCBar], provenance: DataProvenance) -> BacktestResponse:
    strategy = resolve(request.strategy_id)
    if strategy is None:
        raise LookupError(f"未知策略：{request.strategy_id}")
    if len(bars) < request.lookback + request.holding_days + 2:
        raise ValueError(
            f"历史数据不足：需要至少 {request.lookback + request.holding_days + 2} 根日线，当前 {len(bars)} 根。"
        )

    closes = [bar.close for bar in bars]
    highs = [bar.high for bar in bars]
    commission_rate = request.commission_bps / 10_000
    slippage = request.slippage_bps / 10_000
    stamp_rate = request.stamp_duty_bps / 10_000

    cash = request.initial_cash
    shares = 0
    entry_price = 0.0
    entry_cost = 0.0
    entry_index = -1
    entry_commission = 0.0
    trades: list[TradeRecord] = []
    equity: list[EquityPoint] = []
    total_costs = 0.0
    days_in_market = 0
    blocked_entries = 0
    blocked_exits = 0
    peak = request.initial_cash
    stop_pct, target_pct, trail_pct = request.stop_loss_pct, request.take_profit_pct, request.trailing_stop_pct
    has_exit_rules = any(value is not None for value in (stop_pct, target_pct, trail_pct))
    exit_counts = {"holding": 0, "stop_loss": 0, "take_profit": 0, "trailing_stop": 0, "final_close": 0}
    pending_reason: str | None = None  # a triggered exit waiting for the next sellable open
    hold_peak = 0.0  # highest close since entry, for the trailing stop

    def sell(bar: OHLCBar, raw_price: float, reason: str) -> None:
        nonlocal cash, shares, entry_index, total_costs, pending_reason
        price = round(raw_price * (1 - slippage), 4)
        proceeds = shares * price
        commission = proceeds * commission_rate
        stamp = proceeds * stamp_rate
        cash += proceeds - commission - stamp
        total_costs += commission + stamp
        pnl = proceeds - commission - stamp - entry_cost - entry_commission
        trades.append(
            TradeRecord(
                entry_date=bars[entry_index].date,
                exit_date=bar.date,
                entry_price=round(entry_price, 4),
                exit_price=price,
                shares=shares,
                pnl=round(pnl, 2),
                return_pct=round(pnl / (entry_cost + entry_commission) * 100, 3),
                costs=round(commission + stamp + entry_commission, 2),
            )
        )
        exit_counts[reason] += 1
        shares = 0
        entry_index = -1
        pending_reason = None

    for index, bar in enumerate(bars):
        exited_intrabar = False
        # Exits are evaluated only after the entry bar (T+1): entry happens later
        # in this loop body, so a fresh position is never checked on its own bar.
        if shares:
            # A limit-down open means the queue cannot sell; the exit waits.
            floor_open = _blocked_by_limit(request.code, closes[index - 1], bar.open, rising=False)
            reason = pending_reason
            if reason is None and index - entry_index >= request.holding_days:
                reason = "holding"
            if reason is not None:
                if floor_open:
                    blocked_exits += 1
                else:
                    sell(bar, bar.open, reason)
            elif has_exit_rules:
                # Stop before target: when one bar spans both, assume the worse.
                stop_price = entry_price * (1 - stop_pct / 100) if stop_pct is not None else None
                target_price = entry_price * (1 + target_pct / 100) if target_pct is not None else None
                hit: tuple[str, float] | None = None
                if stop_price is not None and bar.low <= stop_price:
                    hit = ("stop_loss", min(bar.open, stop_price))
                elif target_price is not None and bar.high >= target_price:
                    hit = ("take_profit", max(bar.open, target_price))
                if hit is not None:
                    if floor_open:
                        blocked_exits += 1
                        pending_reason = hit[0]
                    else:
                        sell(bar, hit[1], hit[0])
                        # Bought at the open and stopped out the same day would break T+1.
                        exited_intrabar = True

        # Enter at this bar's open when yesterday's close produced a signal.
        if not shares and index > 0 and not exited_intrabar:
            if _signal(strategy.signal, closes, highs, index - 1, request.lookback):
                if _blocked_by_limit(request.code, closes[index - 1], bar.open, rising=True):
                    # Opening at the limit means the buy queue is already full.
                    blocked_entries += 1
                else:
                    price = round(bar.open * (1 + slippage), 4)
                    budget = cash / (1 + commission_rate)
                    lots = int(budget // (price * LOT))
                    if lots > 0:
                        shares = lots * LOT
                        entry_cost = shares * price
                        entry_commission = entry_cost * commission_rate
                        cash -= entry_cost + entry_commission
                        total_costs += entry_commission
                        entry_price = price
                        entry_index = index
                        hold_peak = bar.close
                        pending_reason = None

        if shares and trail_pct is not None:
            hold_peak = max(hold_peak, bar.close)
            if pending_reason is None and bar.close < hold_peak * (1 - trail_pct / 100):
                # Decided on the close, so the sale can only happen at the next open.
                pending_reason = "trailing_stop"

        position_value = shares * bar.close
        value = cash + position_value
        peak = max(peak, value)
        if shares:
            days_in_market += 1
        equity.append(
            EquityPoint(
                date=bar.date,
                strategy=round((value / request.initial_cash - 1) * 100, 3),
                benchmark=round((bar.close / closes[0] - 1) * 100, 3),
                drawdown=round((value / peak - 1) * 100, 3),
            )
        )

    # Close any open position at the last close so the curve and trades agree.
    if shares:
        sell(bars[-1], bars[-1].close, "final_close")

    final_value = cash
    if equity:
        # The last point must agree with the headline return after liquidation.
        last = equity[-1]
        final_return = (final_value / request.initial_cash - 1) * 100
        peak = max(peak, final_value)
        equity[-1] = EquityPoint(
            date=last.date,
            strategy=round(final_return, 3),
            benchmark=last.benchmark,
            drawdown=round((final_value / peak - 1) * 100, 3),
        )
    daily_returns = [
        (1 + equity[index].strategy / 100) / (1 + equity[index - 1].strategy / 100) - 1
        for index in range(1, len(equity))
    ]
    sharpe = None
    if len(daily_returns) > 2:
        mean = sum(daily_returns) / len(daily_returns)
        variance = sum((value - mean) ** 2 for value in daily_returns) / (len(daily_returns) - 1)
        deviation = variance ** 0.5
        if deviation > 1e-9:
            sharpe = round(mean / deviation * TRADING_DAYS ** 0.5, 3)
    wins = sum(1 for trade in trades if trade.pnl > 0)

    return BacktestResponse(
        run_id=uuid4().hex[:12],
        created_at=datetime.now(CST).isoformat(timespec="seconds"),
        provenance=provenance,
        request=request,
        total_return=round((final_value / request.initial_cash - 1) * 100, 3),
        benchmark_return=round((closes[-1] / closes[0] - 1) * 100, 3),
        max_drawdown=round(min(point.drawdown for point in equity), 3),
        sharpe_ratio=sharpe,
        win_rate=round(wins / len(trades) * 100, 1) if trades else None,
        trade_count=len(trades),
        total_costs=round(total_costs, 2),
        exposure_pct=round(days_in_market / len(equity) * 100, 1),
        equity=equity,
        trades=trades,
        assumptions=_assumptions(request, strategy, provenance, blocked_entries, blocked_exits, exit_counts),
        metrics=tearsheet.metrics(equity, trades, request.initial_cash),
        monthly_returns=tearsheet.monthly(equity),
    )


def _assumptions(
    request: BacktestRequest,
    strategy,
    provenance: DataProvenance,
    blocked_entries: int,
    blocked_exits: int,
    exit_counts: dict[str, int],
) -> list[str]:
    notes = [
        f"信号：{strategy.name}（{strategy.signal}），回看 {request.lookback} 根日线，收盘后产生。",
        f"成交：次日开盘价成交，滑点 {request.slippage_bps} 基点；持仓 {request.holding_days} 个交易日后开盘卖出。",
        f"费用：双边佣金 {request.commission_bps} 基点，卖出印花税 {request.stamp_duty_bps} 基点，均从现金扣除。",
        f"仓位：按 100 股整手下单，初始资金 {request.initial_cash:,.0f} 元。",
        f"数据：{provenance.source}（{provenance.mode}）。{provenance.detail}",
        "涨跌停排队：开盘即在涨停价（主板 9.5% / 创业板科创板 19.5% / 北交所 29.5% 以上）视为买不到，"
        f"跳过买入 {blocked_entries} 次；跌停开盘视为卖不出，顺延卖出 {blocked_exits} 次。",
        "未模拟盘中封单排队、停牌与分红除权，也未使用融资融券。",
        "回测仅用于验证规则，不构成任何投资建议。",
    ]
    rules = [
        text
        for text, value in (
            (f"固定止损 {request.stop_loss_pct}%", request.stop_loss_pct),
            (f"固定止盈 {request.take_profit_pct}%", request.take_profit_pct),
            (f"移动止损 {request.trailing_stop_pct}%", request.trailing_stop_pct),
        )
        if value is not None
    ]
    if rules:
        notes.insert(
            -1,
            f"离场规则：{'、'.join(rules)}（相对成交价）。T+1：入场当日不检查；同一根 K 线内先判止损再判止盈，"
            "止损按 min(开盘, 止损价) 成交、止盈按 max(开盘, 止盈价) 成交；移动止损在收盘低于持有期最高收盘价×(1−幅度) "
            "后于下一根开盘卖出；跌停开盘无法卖出则顺延。止损后不会在同一根 K 线再入场。",
        )
        notes.insert(
            -1,
            "离场原因统计：到期 {holding}、止损 {stop_loss}、止盈 {take_profit}、移动止损 {trailing_stop}、期末平仓 {final_close}。".format(**exit_counts),
        )
    return notes
