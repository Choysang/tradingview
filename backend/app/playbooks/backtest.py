"""Portfolio backtester with A-share constraints and Hikyuu-style exits/sizing.

`simulate` turns a signal frame (code, date, score) into trades and a daily
equity curve. It is deliberately a small day loop over dense numpy price grids:
one row per code that has a signal, one column per trading day, so 750 days of
5,000 codes with ~20k signals stays well under two seconds.

Every rule below is also returned as text by `ASSUMPTIONS`, so an evaluation can
show exactly what was assumed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date, datetime
import math

import numpy as np
import polars as pl

from contracts.schema import EntryRules, ExitRules, PlaybookTrade, SizingRule

LOT = 100
INITIAL_CASH = 1_000_000.0
MAX_WEIGHT_MULTIPLE = 2.0  # volatility sizing never exceeds 2x the equal weight


@dataclass(frozen=True)
class Costs:
    commission_bp: float = 2.5  # both sides
    stamp_bp: float = 5.0  # sells only
    slippage_bp: float = 5.0  # both sides
    transfer_bp: float = 0.0  # 过户费, both sides


DEFAULT_COSTS = Costs()  # the engine baseline; the frozen main book and earlier lab results use it
# The user's account (decision 2026-11-08): 50,000 yuan per buy, commission 万5 a side (the 5-yuan minimum does not
# bind at that size: 25 yuan), stamp duty 万5 on sells, 过户费 万0.1 a side, slippage 5 bp a side: 25.2 bp a round trip.
USER_COSTS = Costs(commission_bp=5.0, stamp_bp=5.0, slippage_bp=5.0, transfer_bp=0.1)
USER_ORDER_YUAN = 50_000.0

ASSUMPTIONS: tuple[str, ...] = (
    "信号产生于已完成的日线 d，次一交易日开盘买入；该日停牌（无行情）或开盘即涨停（一字板）则放弃该信号。",
    "买入规则（按战法开启）：不追高在次日开盘高于信号日收盘×(1+x%) 时放弃；回踩买在信号日收盘×(1-x%) 挂单，开盘已低于挂单价按开盘价成交，盘中最低价触及按挂单价成交，否则放弃。放弃的名额留给分数更低的下一只。",
    "每日候选按信号分数从高到低排序，最多新开 max_new_per_day 只，同时持仓不超过 max_positions；以 100 股为一手向下取整。",
    "等权：单笔目标市值 = 当前权益 / max_positions；波动率加权：按 1/(ATR14/收盘价) 在当日新开仓之间分配，总额与等权一致，单笔不超过等权的 2 倍。",
    "T+1：买入当日不可卖出；自次日起检查离场。同一根 K 线内先检查开盘的“次日不及预期”，再检查止损、止盈（保守）。",
    "止损：最低价触及 入场价×(1-止损%) 时按 min(开盘价, 止损价) 成交；止盈：最高价触及 入场价×(1+止盈%) 时按 max(开盘价, 止盈价) 成交。",
    "移动止损：收盘价低于 持仓期最高收盘价×(1-回撤%) 时，次日开盘离场。时间离场：持有满 holding_days 个交易日后按当日收盘价卖出。",
    "卖出规则（按战法开启）：次日不及预期在买入次日开盘价低于买入价×(1-x%) 时按开盘价卖；断板、炸板（触及涨停未封住）、跌破 5 日线、放量滞涨（量 > 5 日均量 2 倍且涨幅 < 2%）、冲高回落（最高涨幅 ≥ 7% 而收盘 < 3%）、高位长上影（持仓盈利、上影线 > 当日振幅一半且量 > 5 日均量 2 倍）、乖离过大（收盘 > 20 日线×(1+x%)）、吊灯止损（收盘 < 持仓期最高收盘 − N×ATR14）、情绪退潮，都在收盘时判定、次日开盘卖；买入当天收盘也会判定（次日卖出符合 T+1）。",
    "分批止盈（按战法开启）：自买入次日起、当天没有触发离场时（数据最后一天不做），盘中最高价越过 买入价×(1+x%) 时按 max(开盘价, 该价) 卖出一半（整手），只做一次；余下的仓位照常按其它规则卖出，交易收益把两部分合计。",
    "做T（按战法开启，自买入次日起、当天没有触发离场时；数据最后一天不做）：倒T 在盘中最高价越过 昨收×(1+x%) 时按 max(开盘价, 该价) 卖出 t_share 的底仓，收盘价买回；正T 在盘中最低价跌破 昨收×(1-x%) 时按 min(开盘价, 该价) 买入同样数量，收盘价卖出同样数量的底仓；两个价位同一天都触及时，高位卖、低位买都成交，与日内先后顺序无关。价格要越过挂单价 0.1% 才算成交。每一腿单独计成本，T 的盈亏计入该笔交易。",
    "做T 的资金：正T 的买入只能用当天开盘建仓之后剩下的现金（同一天收盘才到账的卖出款不能用），不够就不做；倒T 的卖出款留在这只股票上，不给新开仓用，买回时只买得起的整手，买不回的部分从持仓中减掉，不透支；一股都买不回时，这笔交易以“做T 未买回”结束。等待买回的卖出款计入仓位占用。收盘封死涨停买不回、封死跌停卖不出时，顺延到次一个可成交的开盘（开盘即涨停不买回、跌停封死不卖出）。还有没完成的一腿时，不再做T，也不分批止盈。",
    "离场当日若跌停封死（跌停且开盘即跌停），无法卖出，顺延至下一个可成交的交易日开盘价。",
    "成本：佣金双边 2.5 bp，印花税仅卖出 5 bp，滑点双边各 5 bp；收益率 return_pct 为扣除全部成本后的净收益。",
    "权益按每日收盘价逐日盯市；停牌日沿用最近收盘价。数据末日仍持有的仓位按当日收盘价平仓，原因记为 end。",
    "基准为指数收盘价按首日重基为 100（无指数数据时为空）；回撤为相对历史峰值的百分比。",
    "样本内与样本外是两个独立组合：样本内在分割日全部平仓，样本外从次日空仓开始，任何持仓都不跨越分割日。",
    "幸存者偏差：仓库只含同步时仍在上市的股票，之前已退市的股票不在历史里，结果偏乐观。",
    "历史 ST 状态未知：涨跌停一律按板块比例（10% / 20% / 30%）判定，不按今天的名称回溯剔除 ST。2026-07-06 以前主板 ST 的 5% 涨跌停不被识别；"
    "从这天起交易所把主板 ST 涨跌幅放宽到 10%（日线可见），此后与板块比例一致。",
    "价格为前复权：涨停价由复权后的昨收计算，分红较多的低价股可能出现一分钱级别的误判。成交额为 典型价×成交量 的估算值。",
    "days_listed 是上市交易日数：仓库首日已在交易的股票视为老股（记为 10000 + 已有日线根数），之后才出现的股票按日线根数计；仓库起点前几个月内上市的股票会被当成老股，误差很小。均线等指标仍需要足够的日线，仓库最初一段时间为空。",
)


def _as_date(value: str | Date | datetime | None) -> Date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, Date):
        return value
    return Date.fromisoformat(str(value)[:10])


@dataclass
class _Position:
    ci: int
    entry_idx: int
    entry_px: float  # raw open
    shares: int
    cost: float  # cash paid including fees
    high_close: float
    last_px: float
    pending: str | None = None  # exit reason waiting for the next executable open
    flows: float = 0.0  # net cash from partial sells and T legs (in: +, out: -)
    scaled: bool = False  # the one-off half sell (分批止盈) has happened
    t_short: int = 0  # 倒T shares sold and not yet bought back (a sealed limit-up close)
    t_long: int = 0  # 正T shares bought and not yet sold (a sealed limit-down close)
    t_cash: float = 0.0  # 倒T sale money held for the buy-back, not spendable on new entries
    t_count: int = 0

    def held(self) -> int:
        return self.shares - self.t_short + self.t_long


def code_index(codes: list[str]) -> pl.DataFrame:
    return pl.DataFrame({"code": codes, "_ci": np.arange(len(codes), dtype=np.int64)})


GRID_FIELDS = (
    "open", "high", "low", "close", "is_limit_down", "open_at_limit", "open_at_floor", "atr_14",
    "is_limit_up", "prev_close", "ma5", "volume", "vol_ma5", "ma20", "is_broken",
)
TOUCH = 0.001  # a resting order fills only when the price trades 0.1% through it


def price_grid(
    panel: pl.DataFrame, codes: list[str], dates: np.ndarray, fields: tuple[str, ...] = GRID_FIELDS
) -> dict[str, np.ndarray]:
    """Dense (code x day) arrays; also `present` (a bar exists) and `floor_lock` when its inputs are asked for."""
    wanted = list(fields)
    frame = panel.select(["code", "date", *[c for c in wanted if c in panel.columns]])
    frame = frame.filter(
        pl.col("code").is_in(codes) & pl.col("date").is_between(dates[0].item(), dates[-1].item())
    )
    frame = frame.join(code_index(codes), on="code", how="inner")
    ci = frame["_ci"].to_numpy()
    di = np.searchsorted(dates, frame["date"].to_numpy().astype("datetime64[D]"))
    shape = (len(codes), len(dates))
    out: dict[str, np.ndarray] = {}
    for name in wanted:
        if name not in frame.columns:
            out[name] = np.full(shape, False if name.startswith(("is_", "open_at")) else np.nan)
            continue
        values = frame[name]
        if name.startswith(("is_", "open_at")):
            arr = np.zeros(shape, dtype=bool)
            arr[ci, di] = values.fill_null(False).to_numpy()
        else:
            arr = np.full(shape, np.nan)
            arr[ci, di] = values.cast(pl.Float64).to_numpy()
        out[name] = arr
    out["present"] = np.zeros(shape, dtype=bool)
    out["present"][ci, di] = True
    if "is_limit_down" in out and "open_at_floor" in out:
        out["floor_lock"] = out["is_limit_down"] & out["open_at_floor"]
    return out


def simulate(
    panel: pl.DataFrame,
    signals: pl.DataFrame,
    exits: ExitRules,
    sizing: SizingRule,
    benchmark: pl.DataFrame | None = None,
    start: str | Date | None = None,
    end: str | Date | None = None,
    costs: Costs = DEFAULT_COSTS,
    ebb_days: frozenset[Date] = frozenset(),
    entry: EntryRules | None = None,
) -> tuple[list[PlaybookTrade], pl.DataFrame]:
    """Run the portfolio. Returns (trades, equity).

    `ebb_days` are the dates whose sentiment regime is "ebb"; only used when
    `exits.sell_on_ebb` is on. `entry` limits the next-open buy (no chasing a
    gap, or a resting order below the signal close); None buys at the open.

    `equity` has columns date, strategy (NAV, 100 at start), benchmark (rebased
    to 100, null without a benchmark), drawdown (% from peak, <= 0) and
    exposure (invested share of equity, 0-1).
    """
    start_d, end_d = _as_date(start), _as_date(end)
    calendar = panel.select(pl.col("date").unique().sort())["date"]
    if start_d is not None:
        calendar = calendar.filter(calendar >= start_d)
    if end_d is not None:
        calendar = calendar.filter(calendar <= end_d)
    day_list = calendar.to_list()
    n_days = len(day_list)
    if n_days == 0:
        return [], pl.DataFrame(schema={"date": pl.Date, "strategy": pl.Float64, "benchmark": pl.Float64,
                                        "drawdown": pl.Float64, "exposure": pl.Float64})
    dates = calendar.to_numpy().astype("datetime64[D]")

    sig = (
        signals.select("code", "date", "score")
        .filter(pl.col("date").is_between(day_list[0], day_list[-1]))
        .sort("score", descending=True)
        .unique(subset=["code", "date"], keep="first", maintain_order=True)
    )
    codes = sorted(set(sig["code"].to_list()) & set(panel["code"].unique().to_list()))
    sig = sig.filter(pl.col("code").is_in(codes)) if codes else sig.clear()

    grid = price_grid(panel, codes, dates) if codes else {}
    by_day: dict[int, list[tuple[int, float]]] = {}
    if codes:
        sig = sig.join(code_index(codes), on="code", how="inner").sort("score", descending=True)
        s_di = np.searchsorted(dates, sig["date"].to_numpy().astype("datetime64[D]"))
        for c, d, score in zip(sig["_ci"].to_list(), s_di.tolist(), sig["score"].to_list()):
            by_day.setdefault(d, []).append((c, float(score)))  # score-descending

    buy_fee = (1 + costs.slippage_bp / 1e4) * (1 + (costs.commission_bp + costs.transfer_bp) / 1e4)
    sell_fee = (1 - costs.slippage_bp / 1e4) * (1 - (costs.commission_bp + costs.stamp_bp + costs.transfer_bp) / 1e4)
    stop = exits.stop_loss_pct / 100 if exits.stop_loss_pct else None
    take = exits.take_profit_pct / 100 if exits.take_profit_pct else None
    trail = exits.trailing_stop_pct / 100 if exits.trailing_stop_pct else None
    scale = exits.scale_out_pct / 100 if exits.scale_out_pct else None
    t_up = exits.t_high_pct / 100 if exits.t_high_pct else None
    t_down = exits.t_low_pct / 100 if exits.t_low_pct else None
    bias = exits.bias_exit_pct / 100 if exits.bias_exit_pct else None
    weak = exits.weak_open_pct / 100 if exits.weak_open_pct else None
    max_gap = entry.max_gap_pct / 100 if entry is not None and entry.max_gap_pct is not None else None
    pullback = entry.pullback_pct / 100 if entry is not None and entry.pullback_pct else None

    cash = INITIAL_CASH
    t_budget = 0.0
    close_backs: list[_Position] = []  # 倒T sold today, bought back at today's close
    positions: list[_Position] = []
    trades: list[PlaybookTrade] = []
    nav: list[float] = []
    exposure: list[float] = []

    def close_position(pos: _Position, day: int, fill: float, reason: str) -> None:
        nonlocal cash
        proceeds = pos.held() * fill * sell_fee
        cash += proceeds + pos.t_cash
        pos.t_cash = 0.0
        entry_date, exit_date = day_list[pos.entry_idx], day_list[day]
        trades.append(PlaybookTrade(
            code=codes[pos.ci],
            entry_date=entry_date.isoformat(),
            exit_date=exit_date.isoformat(),
            entry_price=round(pos.entry_px, 4),
            exit_price=round(fill, 4),
            return_pct=round(((proceeds + pos.flows) / pos.cost - 1) * 100, 4),
            holding_days=day - pos.entry_idx,
            t_trades=pos.t_count,
            exit_reason=reason,  # type: ignore[arg-type]
        ))

    def equity_value() -> float:
        return cash + sum(p.held() * p.last_px + p.t_cash for p in positions)

    def fill_price(ci: int, t: int) -> float | None:
        """Today's buy price for yesterday's signal, or None when the entry rule passes on it."""
        o = float(grid["open"][ci, t])
        if max_gap is None and pullback is None:
            return o
        ref = float(grid["close"][ci, t - 1])
        if not (math.isfinite(ref) and ref > 0):
            return None
        if max_gap is not None and o > ref * (1 + max_gap):
            return None
        if pullback is not None:
            limit = ref * (1 - pullback)
            if o <= limit:
                return o
            return limit if grid["low"][ci, t] <= limit else None
        return o

    def flow(pos: _Position, amount: float) -> None:
        nonlocal cash
        cash += amount
        pos.flows += amount

    def affordable(money: float, price: float) -> int:
        """Whole lots that `money` buys at `price` including costs."""
        return int(max(money - 1e-6, 0.0) / (price * buy_fee)) // LOT * LOT if price > 0 else 0

    def buy_back(pos: _Position, price: float) -> None:
        """Close a 倒T: buy the sold shares back with their sale money plus free cash, whole lots only."""
        nonlocal cash
        back = min(pos.t_short, affordable(pos.t_cash + cash, price))
        pay = back * price * buy_fee
        cash += pos.t_cash - pay
        pos.flows -= pay
        pos.shares -= pos.t_short - back  # what cannot be bought back is gone
        pos.t_cash, pos.t_short = 0.0, 0

    def settle_t_legs(pos: _Position, t: int) -> None:
        """Legs that a sealed close left open are done at today's open, when it is tradable."""
        o = float(grid["open"][pos.ci, t])
        if pos.t_short and not grid["open_at_limit"][pos.ci, t]:
            buy_back(pos, o)
            if not pos.shares:
                close_position(pos, t, o, "t_out")
        if pos.t_long and not grid["floor_lock"][pos.ci, t]:
            flow(pos, pos.t_long * o * sell_fee)
            pos.t_long = 0

    def scale_out(pos: _Position, o: float, h: float) -> None:
        target = pos.entry_px * (1 + scale)
        half = pos.shares // 2 // LOT * LOT
        if pos.scaled or pos.t_short or pos.t_long or half < LOT or h < target * (1 + TOUCH):
            return
        flow(pos, half * max(o, target) * sell_fee)
        pos.shares -= half
        pos.scaled = True

    def do_t(pos: _Position, t: int, o: float, h: float, l: float, c: float) -> None:
        """One T per held day: sell high and/or buy low at resting prices, the other leg at the close.

        A buy leg may only spend `t_budget`, the cash left after today's opening entries: money
        that arrives at today's close cannot pay for an order filled earlier in the day. A 倒T
        buy-back waits in `close_backs` until every position has had its intraday trades."""
        nonlocal t_budget
        prev = float(grid["prev_close"][pos.ci, t])
        k = int(pos.shares * exits.t_share) // LOT * LOT
        if not (math.isfinite(prev) and prev > 0) or k < LOT:
            return
        up = prev * (1 + t_up) if t_up is not None else None
        down = prev * (1 - t_down) if t_down is not None else None
        hit_up = up is not None and h >= up * (1 + TOUCH)
        hit_down = down is not None and l <= down * (1 - TOUCH)
        sell_px = max(o, up) if hit_up else 0.0
        buy_px = min(o, down) if hit_down else 0.0
        if hit_down and t_budget < k * buy_px * buy_fee:
            hit_down = False  # no free cash for the buy leg
        if not (hit_up or hit_down):
            return
        pos.t_count += 1
        if hit_up and hit_down:  # both resting orders filled; the shares end where they began
            t_budget -= k * buy_px * buy_fee
            flow(pos, k * sell_px * sell_fee - k * buy_px * buy_fee)
        elif hit_up:
            pos.t_short, pos.t_cash = k, k * sell_px * sell_fee
            pos.flows += pos.t_cash
            if not grid["is_limit_up"][pos.ci, t]:  # sealed at the limit: buy back at the next tradable open
                close_backs.append(pos)
        else:
            t_budget -= k * buy_px * buy_fee
            flow(pos, -k * buy_px * buy_fee)
            if grid["is_limit_down"][pos.ci, t]:
                pos.t_long = k  # sealed at the floor: sell at the next tradable open
            else:
                flow(pos, k * c * sell_fee)

    def armed(ci: int, t: int, c: float, high_close: float, entry_px: float) -> str | None:
        """A sell rule that fired on the close of bar t; it executes at the next open."""
        g = grid
        if exits.chandelier_atr and math.isfinite(g["atr_14"][ci, t]) and c < high_close - exits.chandelier_atr * g["atr_14"][ci, t]:
            return "chandelier"
        if exits.sell_if_not_limit and not g["is_limit_up"][ci, t]:
            return "not_limit"
        if exits.sell_on_broken and g["is_broken"][ci, t]:
            return "broken"
        if bias is not None and math.isfinite(g["ma20"][ci, t]) and c > g["ma20"][ci, t] * (1 + bias):
            return "bias"
        if exits.sell_on_shadow and c > entry_px:
            top, bottom = g["high"][ci, t], g["low"][ci, t]
            upper = top - max(g["open"][ci, t], c)
            if top > bottom and upper / (top - bottom) > 0.5 and g["volume"][ci, t] > 2 * g["vol_ma5"][ci, t]:
                return "shadow"
        if exits.sell_below_ma5 and math.isfinite(g["ma5"][ci, t]) and c < g["ma5"][ci, t]:
            return "below_ma5"
        prev = g["prev_close"][ci, t]
        if math.isfinite(prev) and prev > 0:
            if exits.sell_on_stall and g["volume"][ci, t] > 2 * g["vol_ma5"][ci, t] and c / prev - 1 < 0.02:
                return "stall"
            if exits.sell_on_fade and g["high"][ci, t] / prev - 1 >= 0.07 and c / prev - 1 < 0.03:
                return "fade"
        if exits.sell_on_ebb and day_list[t] in ebb_days:
            return "ebb"
        return None

    for t in range(n_days):
        if codes:
            open_, high, low, close = grid["open"], grid["high"], grid["low"], grid["close"]
            present, floor_lock = grid["present"], grid["floor_lock"]

            # A) exits that were armed on an earlier bar execute at today's open.
            kept: list[_Position] = []
            for pos in positions:
                if pos.pending is not None and present[pos.ci, t] and not floor_lock[pos.ci, t]:
                    close_position(pos, t, float(open_[pos.ci, t]), pos.pending)
                else:
                    if (pos.t_short or pos.t_long) and present[pos.ci, t]:
                        settle_t_legs(pos, t)
                    if pos.shares:
                        kept.append(pos)
            positions = kept

            # B) entries at today's open from yesterday's completed-bar signals.
            if t >= 1 and (t - 1) in by_day and len(positions) < sizing.max_positions:
                held = {p.ci for p in positions}
                accepted: list[int] = []
                fills: list[float] = []
                slots = sizing.max_positions - len(positions)
                for ci, _ in by_day[t - 1]:
                    if len(accepted) >= min(sizing.max_new_per_day, slots):
                        break
                    if ci in held or not present[ci, t] or grid["open_at_limit"][ci, t]:
                        continue
                    if not (open_[ci, t] > 0):
                        continue
                    fill = fill_price(ci, t)
                    if fill is None:
                        continue
                    accepted.append(ci)
                    fills.append(fill)
                if accepted:
                    equity_now = equity_value()
                    base = equity_now / sizing.max_positions
                    weights = np.ones(len(accepted))
                    if sizing.method == "volatility":
                        atr, ref = grid["atr_14"][accepted, t - 1], close[accepted, t - 1]
                        inv = ref / atr
                        if np.all(np.isfinite(inv)) and np.all(inv > 0):
                            weights = np.minimum(inv / inv.mean(), MAX_WEIGHT_MULTIPLE)
                    for ci, price, weight in zip(accepted, fills, weights.tolist()):
                        per_share = price * buy_fee
                        lots = int(min(base * weight, cash) / per_share // LOT)
                        shares = lots * LOT
                        if shares < LOT:
                            continue
                        cost = shares * per_share
                        cash -= cost
                        positions.append(_Position(ci, t, price, shares, cost, price, price))

            # C) intraday / close exits for positions bought before today (T+1).
            t_budget = cash
            close_backs = []
            kept = []
            for pos in positions:
                ci = pos.ci
                if not present[ci, t]:
                    kept.append(pos)
                    continue
                if pos.entry_idx >= t or pos.pending is not None:
                    # Entry bar (T+1: no exit today) or an exit already armed: mark to market.
                    # A sell rule may still fire on the entry bar's close: it sells tomorrow.
                    pos.last_px = float(close[ci, t])
                    pos.high_close = max(pos.high_close, pos.last_px)
                    if pos.pending is None:
                        pos.pending = armed(ci, t, pos.last_px, pos.high_close, pos.entry_px)
                    kept.append(pos)
                    continue
                o, h, l, c = (float(open_[ci, t]), float(high[ci, t]), float(low[ci, t]), float(close[ci, t]))
                locked = bool(floor_lock[ci, t])
                reason: str | None = None
                fill = 0.0
                # 不及预期 is decided at the open, before the day's low can reach a stop.
                if weak is not None and t == pos.entry_idx + 1 and o < pos.entry_px * (1 - weak):
                    reason, fill = "weak_open", o
                elif stop is not None and l <= pos.entry_px * (1 - stop):
                    reason, fill = "stop", min(o, pos.entry_px * (1 - stop))
                elif take is not None and h >= pos.entry_px * (1 + take):
                    reason, fill = "target", max(o, pos.entry_px * (1 + take))
                elif t - pos.entry_idx >= exits.holding_days:
                    reason, fill = "time", c
                if reason is not None:
                    if locked:
                        pos.pending = reason  # cannot sell a locked limit-down; retry next open
                        pos.last_px = c
                        kept.append(pos)
                    else:
                        close_position(pos, t, fill, reason)
                    continue
                if scale is not None and t < n_days - 1:
                    scale_out(pos, o, h)
                if (t_up is not None or t_down is not None) and t < n_days - 1 and not (pos.t_short or pos.t_long):
                    do_t(pos, t, o, h, l, c)
                pos.last_px = c
                pos.high_close = max(pos.high_close, c)
                if trail is not None and c < pos.high_close * (1 - trail):
                    pos.pending = "trailing"
                else:
                    pos.pending = armed(ci, t, c, pos.high_close, pos.entry_px)
                kept.append(pos)
            positions = kept
            for pos in close_backs:
                buy_back(pos, float(close[pos.ci, t]))
                if not pos.shares:
                    close_position(pos, t, float(close[pos.ci, t]), "t_out")
            positions = [p for p in positions if p.shares]

            if t == n_days - 1:
                for pos in positions:
                    last = float(close[pos.ci, t]) if present[pos.ci, t] else pos.last_px
                    close_position(pos, t, last, "end")
                positions = []

        value = equity_value()
        nav.append(value)
        # 倒T sale money waiting for the buy-back is still tied to the stock: it counts as invested.
        exposure.append(sum(p.held() * p.last_px + p.t_cash for p in positions) / value if value > 0 else 0.0)

    strategy = np.array(nav) / INITIAL_CASH * 100.0
    peak = np.maximum.accumulate(strategy)
    equity = pl.DataFrame({
        "date": day_list,
        "strategy": strategy,
        "benchmark": _benchmark_curve(benchmark, calendar),
        "drawdown": (strategy / peak - 1) * 100.0,
        "exposure": exposure,
    })
    return trades, equity


def _benchmark_curve(benchmark: pl.DataFrame | None, calendar: pl.Series) -> list[float | None]:
    if benchmark is None or benchmark.is_empty():
        return [None] * len(calendar)
    column = "close" if "close" in benchmark.columns else "index_close"
    joined = (
        pl.DataFrame({"date": calendar})
        .join(benchmark.select("date", pl.col(column).alias("b")).unique("date"), on="date", how="left")
        .sort("date")
        .with_columns(pl.col("b").forward_fill().backward_fill())
    )
    values = joined["b"].to_numpy().astype(float)
    if not len(values) or not math.isfinite(values[0]) or values[0] == 0:
        return [None] * len(calendar)
    return (values / values[0] * 100.0).tolist()
