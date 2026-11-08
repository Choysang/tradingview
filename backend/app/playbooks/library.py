"""The 战法 library: mainstream A-share selection theories as pure signal functions.

Every signal sees the whole research panel (see base.py), looks only at
completed bars (shifts are always positive: no `shift(-k)`), and returns the
(code, date) rows where the rule fires with a positive `score` (a strength the
backtester uses to rank same-day candidates). Entry is the next open — that is
the engine's job, not the signal's.

Attribution is explicit: academic factors cite the paper; widely circulated
practice (游资 打板 conventions, 均线/MACD/KDJ/BOLL folklore) is labelled
"社区经验（未经学术验证）" because no peer-reviewed evidence is claimed. Thresholds
are defaults chosen to be explainable, not optimised; each has a parameter range.
"""
from __future__ import annotations

import polars as pl

from contracts.schema import ExitRules, PlaybookParam, PlaybookSpec, SizingRule
from .base import Playbook, register

MIN_CROSS_SECTION = 20  # a decile of fewer than 20 liquid names is not a decile
COMMUNITY = "社区经验（未经学术验证）"
CODE = "code"


# --- building blocks -------------------------------------------------------------


def c(name: str) -> pl.Expr:
    return pl.col(name)


def lag(expr: pl.Expr, bars: int = 1) -> pl.Expr:
    """Value `bars` completed bars ago, per code."""
    return expr.shift(bars).over(CODE)


def roll_max(expr: pl.Expr, window: int) -> pl.Expr:
    return expr.rolling_max(window).over(CODE)


def volume_ratio20() -> pl.Expr:
    """Volume against the *prior* 20-bar average (today does not dilute its own baseline)."""
    return c("volume") / lag(c("vol_ma20"))


def emit(panel: pl.LazyFrame, hit: pl.Expr, score: pl.Expr) -> pl.LazyFrame:
    return (
        panel.select(CODE, "date", hit.fill_null(False).alias("_hit"), score.cast(pl.Float64).alias("score"))
        .filter(pl.col("_hit") & pl.col("score").is_not_null() & pl.col("score").is_finite() & (pl.col("score") > 0))
        .select(CODE, "date", "score")
    )


def first_day_of(period: pl.Expr) -> pl.Expr:
    """True on the first trading date (in the panel) of each period, for every code."""
    return c("date") == c("date").min().over(period)


MONTH = c("date").dt.year() * 100 + c("date").dt.month()
WEEK = c("date").dt.iso_year() * 100 + c("date").dt.week()


def decile(
    panel: pl.LazyFrame,
    metric: pl.Expr,
    eligible: pl.Expr,
    *,
    highest: bool,
    period: pl.Expr,
    share_pct: float,
) -> pl.LazyFrame:
    """Top (or bottom) `share_pct` % of the eligible names on the first date of each
    period; score = 1 - (rank - 1) / n, so rank 1 scores 1."""
    masked = pl.when(eligible & metric.is_not_null() & metric.is_finite()).then(metric).otherwise(None)
    ranked = panel.with_columns(masked.alias("_m")).with_columns(
        c("_m").rank("ordinal", descending=highest).over("date").alias("_r"),
        c("_m").count().over("date").alias("_n"),
    )
    keep = (
        first_day_of(period)
        & c("_r").is_not_null()
        & (c("_n") >= MIN_CROSS_SECTION)
        & (c("_r") <= (c("_n") * (share_pct / 100)).ceil())
    )
    return emit(ranked, keep, 1 - (c("_r") - 1) / c("_n"))


def liquid(min_amount: float) -> pl.Expr:
    """20-day mean turnover (亿元) at least `min_amount`."""
    return c("amount").rolling_mean(20).over(CODE) >= min_amount


def param(key: str, label: str, value: float, low: float, high: float, step: float) -> PlaybookParam:
    return PlaybookParam(key=key, label=label, value=value, min=low, max=high, step=step)


def short_exits(days: int = 3, stop: float | None = 7, target: float | None = None, trail: float | None = None) -> ExitRules:
    return ExitRules(holding_days=days, stop_loss_pct=stop, take_profit_pct=target, trailing_stop_pct=trail)


def equal(positions: int = 5, per_day: int = 2) -> SizingRule:
    return SizingRule(method="equal", max_positions=positions, max_new_per_day=per_day)


def add(spec: PlaybookSpec, signal, requires_history: tuple[str, ...] = ()) -> None:
    register(Playbook(spec=spec, signal=signal, requires_history=requires_history))


# --- short / sentiment (打板与情绪) -------------------------------------------------


def _first_board(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
    ratio = volume_ratio20()
    hit = (
        c("is_limit_up") & (c("limit_streak") == 1) & ~c("one_word")
        & (ratio >= p["vol_min"]) & (ratio <= p["vol_max"])
        & (c("close") >= c("high_60_prev"))
    )
    return emit(panel, hit, ratio)


add(PlaybookSpec(
    id="first_board_relay", name="首板接力", style="sentiment", horizon="short",
    summary="放量首次涨停并创 60 日新高，次日开盘介入，博弈二板溢价。",
    rule="当日首次涨停（limit_streak=1）、非一字板；成交量 / 前 20 日均量 ∈ [1.5, 6]；收盘 ≥ 前 60 日最高价。得分 = 量比。次日开盘买入，持有 3 日，止损 7%。",
    source=COMMUNITY, params=[
        param("vol_min", "量比下限", 1.5, 0.5, 4, 0.1), param("vol_max", "量比上限", 6, 3, 15, 0.5)],
    exits=short_exits(3, 7), sizing=equal(5, 2)), _first_board)


def _two_to_three(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
    hit = (c("limit_streak") == int(p["streak"])) & ~c("one_word")
    return emit(panel, hit, c("amount"))


add(PlaybookSpec(
    id="two_to_three", name="二进三", style="sentiment", horizon="short",
    summary="已连续两板且非一字的个股，次日博弈三板晋级。",
    rule="当日 limit_streak = 2（可调）且非一字板。得分 = 当日成交额（亿元，偏好流动性好的）。次日开盘买入，持有 2 日，止损 8%。",
    source=COMMUNITY, params=[param("streak", "连板数", 2, 2, 4, 1)],
    exits=short_exits(2, 8), sizing=equal(4, 2)), _two_to_three)


def _broken_reflex(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
    hit = (
        (lag(c("limit_streak"), 2) >= p["min_streak"]) & ~lag(c("is_limit_up"))
        & (c("close") > lag(c("high"))) & (c("pct_chg") > p["min_pct"])
    )
    return emit(panel, hit, c("pct_chg"))


add(PlaybookSpec(
    id="broken_board_reflex", name="断板反包", style="sentiment", horizon="short",
    summary="连板股断板后次日放量收复前日高点，视为分歧转一致的反包。",
    rule="前天 limit_streak ≥ 2 且昨日未涨停（断板）；今日收盘 > 昨日最高价且涨幅 > 3%。得分 = 当日涨幅。次日开盘买入，持有 3 日，止损 8%。",
    source=COMMUNITY, params=[
        param("min_streak", "断板前连板数", 2, 2, 5, 1), param("min_pct", "反包涨幅下限(%)", 3, 1, 9, 0.5)],
    exits=short_exits(3, 8), sizing=equal(4, 2)), _broken_reflex)


def _limit_pullback(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
    tolerance = 1 + p["tolerance"] / 100
    scores = []
    for k in range(3, 9):
        hit_k = (
            lag(c("is_limit_up"), k) & (c("close") >= lag(c("low"), k)) & (c("close") <= lag(c("low"), k) * tolerance)
            & (c("volume") < p["max_volume"] * lag(c("volume"), k)) & (c("close") >= c("ma10"))
        )
        scores.append(pl.when(hit_k).then(lag(c("volume"), k) / c("volume")))
    return emit(panel, pl.max_horizontal(scores).is_not_null(), pl.max_horizontal(scores))


add(PlaybookSpec(
    id="limit_pullback", name="涨停回踩", style="sentiment", horizon="short",
    summary="涨停后缩量回踩到涨停日最低价附近且不破 10 日线，博弈主力护盘后的二次上攻。",
    rule="3–8 个交易日前有一根涨停；今日收盘 ∈ [该涨停日最低价, 其 ×1.03]；今日成交量 < 0.6 × 该涨停日成交量；收盘 ≥ 10 日均线。得分 = 涨停日量 / 今日量（越缩量越高）。次日开盘买入，持有 5 日，止损 6%。",
    source=COMMUNITY, params=[
        param("tolerance", "距最低价容忍(%)", 3, 1, 8, 0.5), param("max_volume", "缩量比上限", 0.6, 0.3, 1, 0.05)],
    exits=short_exits(5, 6), sizing=equal(5, 2)), _limit_pullback)


def _dragon_return(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
    peak = lag(roll_max(c("high"), 20))
    hit = (
        (lag(roll_max(c("limit_streak"), 20)) >= p["min_streak"])
        & (lag(c("close")) <= peak * (1 - p["pullback"] / 100))
        & (c("pct_chg") > 0) & (c("volume") > lag(c("vol_ma5"))) & (c("close") > c("ma5"))
    )
    return emit(panel, hit, c("volume") / lag(c("vol_ma5")))


add(PlaybookSpec(
    id="dragon_return", name="龙回头", style="sentiment", horizon="short",
    summary="曾走出 4 板以上的龙头股回调 20% 以上后放量重新站上 5 日线。",
    rule="前 20 个交易日内最高 limit_streak ≥ 4；昨日收盘较该期间最高价回撤 ≥ 20%；今日上涨、成交量 > 前 5 日均量、收盘 > 5 日均线。得分 = 今量 / 前 5 日均量。次日开盘买入，持有 5 日，止损 8%。该信号稀有：合成数据 780 日中仅个位数次。",
    source=COMMUNITY, params=[
        param("min_streak", "峰值连板数", 4, 3, 7, 1), param("pullback", "回撤下限(%)", 20, 10, 40, 1)],
    exits=short_exits(5, 8), sizing=equal(3, 1)), _dragon_return)


# --- swing: trend and breakout ----------------------------------------------------


def _turtle(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
    hit = c("close") > c("high_55_prev") * (1 + p["min_margin"] / 100)
    return emit(panel, hit, (c("close") / c("high_55_prev") - 1) * 100)


add(PlaybookSpec(
    id="turtle_55", name="海龟突破", style="trend", horizon="swing",
    summary="收盘突破前 55 日最高价（唐奇安通道上轨），用追踪止损持有趋势。",
    rule="收盘 > 前 55 日最高价（不含当日）× (1 + 最小突破幅度)。得分 = 突破幅度(%)。次日开盘买入；原版以 20 日低点离场，这里用 10% 追踪止损近似；最长持有 60 日，初始止损 10%。",
    source="Richard Dennis 海龟交易法则（Faith, Way of the Turtle, 2007），A 股适用性未经学术验证",
    params=[param("min_margin", "最小突破幅度(%)", 0, 0, 5, 0.5)],
    exits=short_exits(60, 10, None, 10), sizing=equal(8, 2)), _turtle)


def _platform_breakout(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
    box = (c("high_20_prev") - c("low_20_prev")) / c("low_20_prev") * 100
    ratio = volume_ratio20()
    hit = (box < p["max_range"]) & (c("close") > c("high_20_prev")) & (ratio > p["vol_mult"])
    return emit(panel, hit, ratio)


add(PlaybookSpec(
    id="platform_breakout", name="平台突破", style="breakout", horizon="swing",
    summary="20 日窄幅整理后放量突破平台上沿（Sequoia / InStock 选股器的经典思路）。",
    rule="前 20 日（不含当日）振幅 (最高/最低 − 1) < 15%；收盘 > 前 20 日最高价；成交量 > 2 × 前 20 日均量。得分 = 量比。次日开盘买入，持有 15 日，止损 8%，追踪止损 10%。",
    source="Sequoia / InStock 开源选股器的“放量突破平台”规则（社区经验，未经学术验证）",
    source_url="https://github.com/myhhub/stock",
    params=[param("max_range", "平台振幅上限(%)", 15, 5, 30, 1), param("vol_mult", "放量倍数", 2, 1.2, 5, 0.1)],
    exits=short_exits(15, 8, None, 10), sizing=equal(6, 2)), _platform_breakout)


def _ma_bull_pullback(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
    hit = (
        (c("ma10") > c("ma20")) & (c("ma20") > c("ma60")) & (c("ma60") > lag(c("ma60"), 10))
        & (c("low") <= c("ma20") * (1 + p["touch"] / 100)) & (c("close") >= c("ma20")) & (c("pct_chg") > 0)
    )
    return emit(panel, hit, (c("ma20") / c("ma60") - 1) * 100)


add(PlaybookSpec(
    id="ma_bull_pullback", name="均线多头回踩", style="trend", horizon="swing",
    summary="多头排列的上升趋势中，回踩 20 日线后收阳，顺势低吸。",
    rule="MA10 > MA20 > MA60 且 MA60 较 10 日前上升；今日最低价 ≤ MA20 × 1.015；收盘 ≥ MA20 且当日上涨。得分 = MA20 / MA60 − 1（趋势强度，%）。次日开盘买入，持有 10 日，止损 6%，追踪止损 8%。",
    source=COMMUNITY, params=[param("touch", "触及 MA20 容忍(%)", 1.5, 0.5, 4, 0.5)],
    exits=short_exits(10, 6, None, 8), sizing=equal(6, 2)), _ma_bull_pullback)


def _golden_cross(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
    hit = (c("ma20") > c("ma60")) & (lag(c("ma20")) <= lag(c("ma60"))) & (c("close") > c("ma60"))
    return emit(panel, hit, (c("ma20") / c("ma60") - 1) * 1000)


add(PlaybookSpec(
    id="golden_cross_20_60", name="双均线金叉", style="trend", horizon="swing",
    summary="20 日线上穿 60 日线（中期金叉），确认趋势转多。",
    rule="今日 MA20 > MA60 且昨日 MA20 ≤ MA60，同时收盘 > MA60。得分 = (MA20/MA60 − 1) × 1000。次日开盘买入，持有 30 日，止损 8%，追踪止损 12%。",
    source=COMMUNITY, params=[],
    exits=short_exits(30, 8, None, 12), sizing=equal(8, 2)), _golden_cross)


def _macd_zero_cross(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
    hit = (
        (c("macd_dif") > 0) & (c("macd_dea") > 0) & (c("macd_dif") > c("macd_dea"))
        & (lag(c("macd_dif")) <= lag(c("macd_dea")))
    )
    return emit(panel, hit, c("macd_hist") / c("close") * 100)


add(PlaybookSpec(
    id="macd_above_zero_cross", name="MACD 零轴上金叉", style="trend", horizon="swing",
    summary="DIF、DEA 均在零轴上方时 DIF 上穿 DEA，视为强势中的二次加速。",
    rule="DIF > 0 且 DEA > 0；今日 DIF > DEA 且昨日 DIF ≤ DEA（12/26/9，EMA 以首日收盘为种子，同通达信）。得分 = MACD 柱 / 收盘 × 100。次日开盘买入，持有 15 日，止损 7%，追踪止损 10%。",
    source=COMMUNITY, params=[],
    exits=short_exits(15, 7, None, 10), sizing=equal(8, 2)), _macd_zero_cross)


def _boll_squeeze(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
    width = (c("boll_upper") - c("boll_lower")) / c("boll_mid")
    floor = width.rolling_quantile(p["squeeze"] / 100, window_size=120).over(CODE)
    hit = (lag(width) <= lag(floor)) & (c("close") > c("boll_upper"))
    return emit(panel, hit, (c("close") / c("boll_upper") - 1) * 100)


add(PlaybookSpec(
    id="boll_squeeze_breakout", name="布林收口突破", style="volatility", horizon="swing",
    summary="布林带宽收缩到历史低位（波动压缩）后向上突破上轨。",
    rule="昨日带宽 ((上轨−下轨)/中轨) 处于其最近 120 日分布的最低 10% 以内；今日收盘 > 布林上轨（20 日，2 倍总体标准差）。得分 = 收盘 / 上轨 − 1（%）。次日开盘买入，持有 15 日，止损 7%，追踪止损 10%。",
    source="John Bollinger 的 squeeze 概念（Bollinger on Bollinger Bands, 2001），阈值为社区经验，未经学术验证",
    params=[param("squeeze", "带宽分位(%)", 10, 3, 25, 1)],
    exits=short_exits(15, 7, None, 10), sizing=equal(6, 2)), _boll_squeeze)


def _new_high_250(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
    breakout = c("close") > c("high_250_prev")
    recent = lag(breakout.cast(pl.Int8).rolling_max(int(p["cooldown"])).over(CODE)).fill_null(0)
    return emit(panel, breakout & (recent == 0), (c("close") / c("high_250_prev") - 1) * 100)


add(PlaybookSpec(
    id="new_high_250", name="一年新高", style="momentum", horizon="swing",
    summary="收盘创 250 日新高（52 周高点效应），首次突破才计入，避免在趋势里反复触发。",
    rule="收盘 > 前 250 日最高价，且此前 5 个交易日内没有出现过同样的新高。得分 = 突破幅度(%)。次日开盘买入，持有 40 日，止损 10%，追踪止损 12%。",
    source="George & Hwang (2004), The 52-Week High and Momentum Investing, Journal of Finance",
    source_url="https://doi.org/10.1111/j.1540-6261.2004.00695.x",
    params=[param("cooldown", "冷却期(交易日)", 5, 1, 20, 1)],
    exits=short_exits(40, 10, None, 12), sizing=equal(8, 2)), _new_high_250)


# --- long / factor (monthly or weekly rebalance, liquid names only) ------------------


def _momentum(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
    return decile(panel, c("ret_250") - c("ret_20"), liquid(p["min_amount"]),
                  highest=True, period=MONTH, share_pct=p["top_pct"])


add(PlaybookSpec(
    id="momentum_12_1", name="截面动量 12-1", style="momentum", horizon="long",
    summary="按过去 12 个月剔除最近 1 个月的收益排序，做多最强的十分位。",
    rule="每月第一个交易日；流动性过滤：20 日均成交额 ≥ 1 亿元；指标 = ret_250 − ret_20（%），取截面最高的 10%。得分 = 1 − (名次−1)/N。持有 20 日，不设价格止损。注意：A 股中长期动量弱于美股，短期反转更显著。",
    source="Jegadeesh & Titman (1993), Returns to Buying Winners and Selling Losers, Journal of Finance",
    source_url="https://doi.org/10.1111/j.1540-6261.1993.tb04702.x",
    params=[param("top_pct", "入选比例(%)", 10, 5, 30, 1), param("min_amount", "最低日均成交额(亿)", 1, 0.2, 5, 0.1)],
    exits=short_exits(20, None), sizing=equal(20, 20)), _momentum)


def _low_vol(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
    volatility = c("pct_chg").rolling_std(60).over(CODE)
    return decile(panel, volatility, liquid(p["min_amount"]),
                  highest=False, period=MONTH, share_pct=p["top_pct"])


add(PlaybookSpec(
    id="low_volatility", name="低波动", style="volatility", horizon="long",
    summary="做多过去 60 日日收益波动率最低的十分位（低波动异象）。",
    rule="每月第一个交易日；20 日均成交额 ≥ 1 亿元；指标 = 近 60 个交易日日涨跌幅标准差，取截面最低的 10%。得分 = 1 − (名次−1)/N。持有 20 日，不设价格止损。",
    source="Ang, Hodrick, Xing & Zhang (2006), The Cross-Section of Volatility and Expected Returns, Journal of Finance",
    source_url="https://doi.org/10.1111/j.1540-6261.2006.00836.x",
    params=[param("top_pct", "入选比例(%)", 10, 5, 30, 1), param("min_amount", "最低日均成交额(亿)", 1, 0.2, 5, 0.1)],
    exits=short_exits(20, None), sizing=equal(20, 20)), _low_vol)


def _short_reversal(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
    return decile(panel, c("ret_5"), liquid(p["min_amount"]),
                  highest=False, period=WEEK, share_pct=p["top_pct"])


add(PlaybookSpec(
    id="short_reversal", name="短期反转", style="reversal", horizon="short",
    summary="每周买入过去一周跌幅最大的十分位，A 股散户占比高、短期反转效应尤其显著。",
    rule="每周第一个交易日；20 日均成交额 ≥ 1 亿元；指标 = ret_5（%），取截面最低的 10%。得分 = 1 − (名次−1)/N。持有 5 日，不设价格止损。",
    source="Jegadeesh (1990), Evidence of Predictable Behavior of Security Returns, Journal of Finance",
    source_url="https://doi.org/10.1111/j.1540-6261.1990.tb05110.x",
    params=[param("top_pct", "入选比例(%)", 10, 5, 30, 1), param("min_amount", "最低日均成交额(亿)", 1, 0.2, 5, 0.1)],
    exits=short_exits(5, None), sizing=equal(20, 20)), _short_reversal)


def _small_cap(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
    tradable = (
        liquid(p["min_amount"]) & (c("days_listed") >= 250)
    )
    return decile(panel, c("cap_est"), tradable, highest=False, period=MONTH, share_pct=p["top_pct"])


add(PlaybookSpec(
    id="small_cap", name="小市值", style="value", horizon="long",
    summary="做多估算总市值最小的十分位（规模溢价），剔除仓库里不满 250 根日线的次新股。",
    rule="每月第一个交易日；20 日均成交额 ≥ 0.5 亿元（小市值股成交额天然偏低，门槛低于其它因子的 1 亿）；剔除仓库内不满 250 根日线的股票（历史 ST 状态未知，不按今天的名称回溯剔除，以免前视）；指标 = cap_est = 总市值 × 收盘 / 最新收盘，即用最新股本固定不变来近似历史市值，取截面最小的 10%。得分 = 1 − (名次−1)/N。持有 20 日，不设价格止损。",
    source="Banz (1981), The relationship between return and market value of common stocks, Journal of Financial Economics",
    source_url="https://doi.org/10.1016/0304-405X(81)90018-0",
    params=[param("top_pct", "入选比例(%)", 10, 5, 30, 1), param("min_amount", "最低日均成交额(亿)", 0.5, 0.1, 5, 0.1)],
    exits=short_exits(20, None), sizing=equal(20, 20)), _small_cap)


def _value(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
    ok = liquid(p["min_amount"]) & (c("pe_ttm") > 0) & (c("pb") > 0)
    cheap = panel.with_columns(
        pl.when(ok).then(1 / c("pe_ttm")).alias("_ep"),
        pl.when(ok).then(1 / c("pb")).alias("_bp"),
    ).with_columns(
        c("_ep").rank("average").over("date").alias("_ep_rank"),
        c("_bp").rank("average").over("date").alias("_bp_rank"),
        c("_ep").count().over("date").alias("_count"),
    ).with_columns(((c("_ep_rank") + c("_bp_rank")) / c("_count")).alias("_value"))
    return decile(cheap, c("_value"), ok, highest=True, period=MONTH, share_pct=p["top_pct"])


add(PlaybookSpec(
    id="value_pe_pb", name="低估值 PE+PB", style="value", horizon="long",
    summary="按盈利收益率 (1/PE) 与账面市值比 (1/PB) 的截面排名之和选最便宜的十分位。",
    rule="每月第一个交易日；20 日均成交额 ≥ 1 亿元；PE_TTM > 0 且 PB > 0；指标 = rank(1/PE) + rank(1/PB)，取截面最高的 10%。得分 = 1 − (名次−1)/N。限制：仓库只保存最新一期 PE/PB，没有历史时点基本面，因此该战法只能对当前最新交易日选股，不参与历史回测（requires_history）。",
    source="Fama & French (1992), The Cross-Section of Expected Stock Returns, Journal of Finance",
    source_url="https://doi.org/10.1111/j.1540-6261.1992.tb04398.x",
    params=[param("top_pct", "入选比例(%)", 10, 5, 30, 1), param("min_amount", "最低日均成交额(亿)", 1, 0.2, 5, 0.1)],
    exits=short_exits(20, None), sizing=equal(20, 20),
    unavailable_reason="历史时点 PE/PB 未入库：只能对当前最新交易日选股，不做回测"), _value, requires_history=("pe_ttm", "pb"))


# --- reversal / oscillator -------------------------------------------------------------


def _rsi_oversold(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
    hit = (lag(c("rsi_6")) < p["threshold"]) & (c("rsi_6") > lag(c("rsi_6"))) & (c("pct_chg") > 0)
    return emit(panel, hit, p["threshold"] - lag(c("rsi_6")))


add(PlaybookSpec(
    id="rsi_oversold", name="RSI 超卖反弹", style="reversal", horizon="short",
    summary="RSI(6) 昨日跌入超卖区，今日回升并收阳，博弈技术性反弹。",
    rule="昨日 RSI(6) < 20（Wilder 平滑）；今日 RSI(6) 高于昨日且当日上涨。得分 = 20 − 昨日 RSI(6)（越超卖越高）。次日开盘买入，持有 4 日，止损 6%。",
    source="Wilder (1978), New Concepts in Technical Trading Systems；20 阈值为社区经验，未经学术验证",
    params=[param("threshold", "超卖阈值", 20, 10, 35, 1)],
    exits=short_exits(4, 6), sizing=equal(6, 3)), _rsi_oversold)


def _kdj_low(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
    hit = (c("kdj_k") > c("kdj_d")) & (lag(c("kdj_k")) <= lag(c("kdj_d"))) & (c("kdj_k") < p["zone"])
    return emit(panel, hit, p["zone"] - c("kdj_k"))


add(PlaybookSpec(
    id="kdj_golden_low", name="KDJ 低位金叉", style="reversal", horizon="short",
    summary="K 线在低位（< 30）上穿 D 线，视为超跌反弹启动。",
    rule="今日 K > D 且昨日 K ≤ D，且今日 K < 30（KDJ 9/3/3，K/D 以 50 为种子，同通达信）。得分 = 30 − K。次日开盘买入，持有 5 日，止损 6%。",
    source=COMMUNITY, params=[param("zone", "低位区(K 上限)", 30, 15, 50, 1)],
    exits=short_exits(5, 6), sizing=equal(6, 3)), _kdj_low)


def _boll_rebound(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
    hit = (lag(c("close")) < lag(c("boll_lower"))) & (c("close") > c("boll_lower")) & (c("close") > c("open"))
    return emit(panel, hit, (c("close") / c("boll_lower") - 1) * 100)


add(PlaybookSpec(
    id="boll_lower_rebound", name="布林下轨反弹", style="reversal", horizon="short",
    summary="收盘曾跌破布林下轨，次日收回轨内并收阳，均值回归。",
    rule="昨日收盘 < 昨日布林下轨；今日收盘 > 今日下轨且收阳（收盘 > 开盘）。得分 = 收盘 / 下轨 − 1（%）。次日开盘买入，持有 5 日，止损 6%。",
    source=COMMUNITY, params=[],
    exits=short_exits(5, 6), sizing=equal(6, 3)), _boll_rebound)


def _dry_pullback(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
    drop3 = (c("close") / lag(c("close"), 3) - 1) * 100
    prior = lag(c("vol_ma20"))
    hit = (
        (c("ma20") > c("ma60")) & (c("close") > c("ma60")) & (drop3 <= -p["pullback"])
        & (c("volume") <= p["shrink"] * prior)
    )
    return emit(panel, hit, prior / c("volume"))


add(PlaybookSpec(
    id="volume_dry_pullback", name="缩量回调", style="volume", horizon="short",
    summary="上升趋势中的三日急跌若伴随明显缩量，说明抛压衰竭，博弈回升。",
    rule="MA20 > MA60 且收盘 > MA60；近 3 日累计跌幅 ≥ 3%；今日成交量 ≤ 0.6 × 前 20 日均量。得分 = 前 20 日均量 / 今日成交量（越缩量越高）。次日开盘买入，持有 5 日，止损 6%。",
    source=COMMUNITY, params=[
        param("pullback", "3 日跌幅下限(%)", 3, 1, 10, 0.5), param("shrink", "缩量比上限", 0.6, 0.3, 1, 0.05)],
    exits=short_exits(5, 6), sizing=equal(6, 3)), _dry_pullback)


# --- volume ----------------------------------------------------------------------------


def _low_surge(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
    ratio = volume_ratio20()
    hit = (c("close") <= p["far"] * c("high_250_prev")) & (ratio >= p["vol_mult"]) & (c("pct_chg") > p["min_pct"])
    return emit(panel, hit, ratio)


add(PlaybookSpec(
    id="low_volume_surge", name="底部放量", style="volume", horizon="swing",
    summary="远离 250 日高点的低位出现放量大阳，视为资金进场的启动信号。",
    rule="收盘 ≤ 0.7 × 前 250 日最高价；成交量 ≥ 3 × 前 20 日均量；当日涨幅 > 4%。得分 = 量比。次日开盘买入，持有 20 日，止损 8%，追踪止损 12%。",
    source=COMMUNITY, params=[
        param("far", "距高点比例上限", 0.7, 0.4, 0.9, 0.05), param("vol_mult", "放量倍数", 3, 1.5, 8, 0.5),
        param("min_pct", "涨幅下限(%)", 4, 2, 9, 0.5)],
    exits=short_exits(20, 8, None, 12), sizing=equal(6, 2)), _low_surge)

