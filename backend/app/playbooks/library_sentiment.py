"""战法库 v2: 情绪周期、价量/振幅/隔夜因子与择时（9 套），沿用 library.py 的积木。

Same contract as library.py: signals are pure `(panel, params) -> [code, date, score]`, use
completed bars only (positive shifts, trailing windows) and never `shift(-k)`. Market-wide
conditions are computed per date from the panel itself — `.over("date")` windows or a
`group_by("date")` aggregate joined back (the join is followed by a sort, Polars 2.0 does not
keep row order) — and only ever read that date and earlier ones.

Scores must be positive for the engine; where a rule says "score = -x" the stored score is a
monotone positive transform of it (`1 - x`, `1 / (1 + x)`, or a shifted rank sum), which ranks
same-day candidates identically.
"""
from __future__ import annotations

import polars as pl

from .library import (
    COMMUNITY, CODE, MIN_CROSS_SECTION, MONTH, WEEK, add, c, decile, emit, equal, first_day_of, lag,
    liquid, param, short_exits,
)
from contracts.schema import PlaybookSpec

ICE_WINDOW = 250  # trailing trading days over which "the limit-down count is extreme" is judged


# --- building blocks -------------------------------------------------------------


def with_market(panel: pl.LazyFrame, daily: pl.LazyFrame, columns: list[str]) -> pl.LazyFrame:
    """Join per-date market columns back and restore (code, date) order for later `.over(code)` windows."""
    return panel.join(daily.select("date", *columns), on="date", how="left").sort(CODE, "date")


def pick_bottom(
    panel: pl.LazyFrame,
    metrics: list[tuple[pl.Expr, float]],
    eligible: pl.Expr,
    period: pl.Expr,
    score: pl.Expr,
    gate: pl.Expr | None = None,
) -> pl.LazyFrame:
    """On the first date of each period, names that rank in the bottom `share` % of *every*
    metric among the eligible set (ranks are per date, over the same eligible names).

    `score` may use `_m{i}` (masked metric i), `_r{i}` (its ascending ordinal rank) and `_n`
    (eligible count). `gate` is an extra boolean that is not rank-based."""
    ok = eligible
    for metric, _ in metrics:
        ok = ok & metric.is_not_null() & metric.is_finite()
    frame = panel.with_columns(ok.alias("_ok")).with_columns(
        *(pl.when(c("_ok")).then(metric).alias(f"_m{i}") for i, (metric, _) in enumerate(metrics))
    ).with_columns(
        *(c(f"_m{i}").rank("ordinal").over("date").alias(f"_r{i}") for i in range(len(metrics))),
        c("_ok").sum().over("date").alias("_n"),
    )
    keep = first_day_of(period) & c("_ok") & (c("_n") >= MIN_CROSS_SECTION)
    for i, (_, share) in enumerate(metrics):
        keep = keep & (c(f"_r{i}") <= (c("_n") * (share / 100)).ceil())
    if gate is not None:
        keep = keep & gate
    return emit(frame, keep, score)


def float_cap_pit() -> pl.Expr:
    """Float cap as of each row's day: the snapshot float cap scaled by close / latest close (the latest
    share count held constant). The raw snapshot contains the price move from that day to today, so ranking
    history by it would let the future decide who was small. The panel's `cap_est / total_cap` is exactly
    close / latest close, so a panel cut at D keeps the same value on every day up to D. Null when total_cap
    is unknown."""
    return c("float_cap") * c("cap_est") / c("total_cap")


def first_boards(panel: pl.LazyFrame) -> pl.Expr:
    """First limit-up of a chain that is buyable (not a 一字板)."""
    return (c("limit_streak") == 1) & ~c("one_word")


# --- sentiment (打板与情绪周期) ---------------------------------------------------------


def _ice_point_reflex(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
    daily = (
        panel.group_by("date").agg(c("is_limit_down").sum().alias("_ld"), c("is_limit_up").sum().alias("_lu"))
        .sort("date")
        .with_columns(
            c("_ld").rolling_quantile(p["ice_pct"] / 100, window_size=ICE_WINDOW, min_samples=int(p["min_history"])).alias("_q")
        )
        .with_columns(
            ((c("_ld") >= c("_q")) & (c("_ld") > 0)).shift(1).alias("_ice_prev"),
            (c("_lu") > c("_lu").shift(1)).alias("_lu_up"),
        )
        .with_columns((c("_ice_prev") & c("_lu_up")).alias("_ice"))
    )
    return emit(with_market(panel, daily, ["_ice"]), c("_ice") & first_boards(panel), c("amount"))


add(PlaybookSpec(
    id="ice_point_reflex", name="冰点反包", style="sentiment", horizon="short",
    summary="全市场跌停家数处于近一年极端高位的“冰点”之后，涨停家数回升的那天，买入首板博弈情绪修复。",
    rule="冰点日 = 昨日全市场跌停家数（>0）≥ 其近 250 个交易日（含昨日，至少 60 日历史）分布的 90% 分位；且今日全市场涨停家数 > 昨日涨停家数。满足时，当日 limit_streak = 1 且非一字板的个股入选。得分 = 当日成交额（亿元）。次日开盘买入，持有 2 日，止损 6%。",
    source=f"{COMMUNITY}：冰点反核 / 情绪周期打法",
    params=[param("ice_pct", "跌停家数分位(%)", 90, 80, 99, 1), param("min_history", "最少历史天数", 60, 20, 250, 10)],
    exits=short_exits(2, 6), sizing=equal(5, 2)), _ice_point_reflex)


def _divergence_to_consensus(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
    divergent_prev = (
        lag(c("is_limit_up")) & ~lag(c("one_word"))
        & (lag(c("volume")) >= p["div_mult"] * lag(c("vol_ma5")))
    )
    hit = divergent_prev & c("is_limit_up") & (c("volume") < p["shrink"] * lag(c("volume")))
    return emit(panel, hit, c("limit_streak"))


add(PlaybookSpec(
    id="divergence_to_consensus", name="分歧转一致", style="sentiment", horizon="short",
    summary="昨日放量分歧板，今日缩量再封涨停：筹码在分歧中完成换手，转为一致。",
    rule="昨日涨停且非一字板，昨日成交量 ≥ 1.5 × 昨日的 5 日均量（vol_ma5 含昨日本身，放量分歧板）；今日再次涨停且今日成交量 < 0.7 × 昨日成交量（缩量一致）。得分 = 今日 limit_streak。次日开盘买入，持有 2 日，止损 7%。",
    source=COMMUNITY, params=[
        param("div_mult", "分歧放量倍数", 1.5, 1.1, 4, 0.1), param("shrink", "缩量比上限", 0.7, 0.3, 1, 0.05)],
    exits=short_exits(2, 7), sizing=equal(4, 2)), _divergence_to_consensus)


def _first_negative(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
    hit = (
        (lag(c("limit_streak")) >= p["min_streak"]) & (c("close") < c("open"))
        & ~c("is_limit_down") & (c("close") >= c("ma5"))
    )
    return emit(panel, hit, lag(c("limit_streak")))


add(PlaybookSpec(
    id="first_negative", name="首阴反抽", style="sentiment", horizon="short",
    summary="连板股第一次收阴（未跌停、仍在 5 日线上），视为强势股的正常分歧，博弈反抽。",
    rule="昨日 limit_streak ≥ 3；今日收盘 < 开盘（阴线）、未跌停、收盘 ≥ 5 日均线。得分 = 昨日 limit_streak。次日开盘买入，持有 3 日，止损 7%。",
    source=COMMUNITY, params=[param("min_streak", "昨日连板数下限", 3, 2, 6, 1)],
    exits=short_exits(3, 7), sizing=equal(4, 2)), _first_negative)


def _high_low_switch(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
    daily = (
        panel.group_by("date").agg(
            c("limit_streak").max().alias("_top"),
            (c("limit_streak") == 1).sum().alias("_fb"),
        )
        .sort("date")
        .with_columns(
            ((c("_top") < p["high_streak"]) & (c("_top").shift(1) >= p["high_streak"])
             & (c("_fb") >= c("_fb").shift(1))).alias("_switch"),
        )
    )
    joined = with_market(panel, daily, ["_switch"]).with_columns(float_cap_pit().alias("_fc"))
    cap = pl.when(first_boards(panel) & (c("_fc") > 0)).then(c("_fc"))
    median_cap = cap.median().over("date")
    hit = c("_switch") & first_boards(panel) & (c("_fc") <= median_cap)
    return emit(joined, hit, 1 / c("_fc"))


add(PlaybookSpec(
    id="high_low_switch", name="高低切换", style="sentiment", horizon="short",
    summary="高标集体断板、首板数量不减时，资金从高位切向低位，买入小盘首板。",
    rule="今日全市场最高 limit_streak < 5 且昨日 ≥ 5（高标全部断板）；今日全市场首板家数（limit_streak = 1，含一字板）≥ 昨日首板家数。满足时，当日首板（limit_streak = 1）、非一字板且当日流通市值 ≤ 当日这类首板流通市值中位数的个股入选。得分 = 1 / 当日流通市值（越小越高）。当日流通市值 = float_cap × cap_est / total_cap = float_cap × 当日收盘 / 最新收盘（最新快照股本固定不变，不含此后的价格涨跌，避免用未来决定当时的大小；total_cap 为空则跳过）。次日开盘买入，持有 2 日，止损 6%。",
    source=COMMUNITY, params=[param("high_streak", "高标连板数", 5, 3, 8, 1)],
    exits=short_exits(2, 6), sizing=equal(5, 2)), _high_low_switch)


# --- price-volume / amplitude / overnight factors (Alpha191-style, weekly) -------------------


def _volume_price_divergence(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
    corr = pl.rolling_corr(c("close"), c("volume"), window_size=10).over(CODE)
    return pick_bottom(
        panel, [(corr, p["corr_pct"]), (c("ret_5"), p["ret_pct"])], liquid(p["min_amount"]), WEEK,
        score=1 - c("_m0"),
    )


add(PlaybookSpec(
    id="volume_price_divergence", name="量价背离反转", style="reversal", horizon="short",
    summary="近 10 日收盘价与成交量强负相关（量价背离）且近 5 日跌幅居前的流动性股票，每周博弈反转。",
    rule="每周第一个交易日；20 日均成交额 ≥ 1 亿元的股票中，指标 1 = 近 10 日收盘价与成交量的皮尔逊相关系数，取截面最低的 10%；指标 2 = ret_5（%），取截面最低的 20%；两个条件的名次都在同一批流动性股票内计算，须同时满足。得分 = 1 − 相关系数（即 −相关系数的正向平移，越负越高）。持有 5 日，不设价格止损。",
    source="Alpha191 风格的价量相关性因子（国泰君安短周期价量 alpha），间接引用 arXiv:2601.06499，未核对原文；在 A 股上的有效性未经本系统验证",
    source_url="https://arxiv.org/abs/2601.06499",
    params=[
        param("corr_pct", "相关系数入选比例(%)", 10, 5, 30, 1), param("ret_pct", "ret_5 入选比例(%)", 20, 10, 50, 1),
        param("min_amount", "最低日均成交额(亿)", 1, 0.2, 5, 0.1)],
    exits=short_exits(5, None), sizing=equal(20, 20)), _volume_price_divergence)


def _low_amplitude(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
    amplitude = ((c("high") - c("low")) / c("prev_close")).rolling_mean(10).over(CODE)
    top = c("high").rolling_max(20).over(CODE)
    bottom = c("low").rolling_min(20).over(CODE)
    position = pl.when(top > bottom).then((c("close") - bottom) / (top - bottom))
    return pick_bottom(
        panel, [(amplitude, p["top_pct"])], liquid(p["min_amount"]), WEEK,
        score=1 / (1 + c("_m0")), gate=position <= p["pos_max"] / 100,
    )


add(PlaybookSpec(
    id="low_amplitude", name="低振幅", style="volatility", horizon="short",
    summary="近 10 日平均振幅处于全市场最低、且收盘贴近 20 日区间低位的流动性股票，每周入选。",
    rule="每周第一个交易日；20 日均成交额 ≥ 1 亿元的股票中，日振幅 = (最高 − 最低) / 前收盘，取其近 10 日均值，截面最低的 10%；且收盘位于近 20 日最高-最低区间的最低 30% 以内（(收盘 − 20 日最低) / (20 日最高 − 20 日最低) ≤ 0.3，区间含当日）。得分 = 1 / (1 + 平均振幅)（即 −平均振幅 的正向变换）。持有 5 日，不设价格止损。",
    source="Alpha191 风格的振幅 / 位置因子，在 A 股上的有效性未经本系统验证",
    params=[
        param("top_pct", "入选比例(%)", 10, 5, 30, 1), param("pos_max", "区间位置上限(%)", 30, 10, 60, 5),
        param("min_amount", "最低日均成交额(亿)", 1, 0.2, 5, 0.1)],
    exits=short_exits(5, None), sizing=equal(20, 20)), _low_amplitude)


def _overnight_gap_reversal(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
    overnight = (c("open") / c("prev_close") - 1).rolling_sum(20).over(CODE)
    return decile(panel, overnight, liquid(p["min_amount"]), highest=False, period=WEEK, share_pct=p["top_pct"])


add(PlaybookSpec(
    id="overnight_gap_reversal", name="隔夜收益反转", style="reversal", horizon="swing",
    summary="近 20 日累计隔夜收益最差的流动性股票，每周入选，押注隔夜与日内收益方向相反。",
    rule="每周第一个交易日；20 日均成交额 ≥ 1 亿元的股票中，隔夜收益 = 开盘 / 前收盘 − 1，取近 20 日之和，截面最低的 10%。得分 = 1 − (名次−1)/N（名次与 −隔夜和 同序）。持有 10 日，不设价格止损。",
    source="学界关于 A 股隔夜收益与日内收益走势相反的论断（待本系统检验，此处不引用具体数字）",
    params=[param("top_pct", "入选比例(%)", 10, 5, 30, 1), param("min_amount", "最低日均成交额(亿)", 1, 0.2, 5, 0.1)],
    exits=short_exits(10, None), sizing=equal(20, 20)), _overnight_gap_reversal)


# --- long: low turnover/low vol, microcap timing (monthly) -----------------------------------


def _low_turnover_low_vol(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
    # float_shares = float_cap * 1e8 / latest close; the panel's own `cap_est = total_cap * close / latest
    # close` carries that latest close without reading future rows, so latest close = close * total_cap / cap_est.
    proxy = c("volume") * c("close") * c("total_cap") / (c("cap_est") * c("float_cap") * 1e8)
    turnover = proxy.rolling_mean(20).over(CODE)
    eligible = liquid(p["min_amount"]) & (c("float_cap") > 0) & (c("cap_est") > 0)
    return pick_bottom(
        panel, [(turnover, p["top_pct"]), (c("volatility_20"), p["top_pct"])], eligible, MONTH,
        score=1 - (c("_r0") + c("_r1") - 2) / (2 * c("_n")),
    )


add(PlaybookSpec(
    id="low_turnover_low_vol", name="低换手低波动", style="volatility", horizon="long",
    summary="近 20 日换手与波动率同时处于截面最低 30% 的流动性股票，月度调仓。",
    rule="每月第一个交易日；20 日均成交额 ≥ 1 亿元且 float_cap 非空的股票中，换手代理 = 成交量 / 流通股本，流通股本 = float_cap(亿) × 1e8 / 最新收盘价（快照股本，忽略历史股本变动），取近 20 日均值，截面最低的 30%；同时 volatility_20 截面最低的 30%（两个名次在同一批股票内计算）。得分 = 1 − (两个名次之和 − 2) / (2N)（即 −名次之和 的正向平移，越低越高）。持有 20 日，不设价格止损。float_cap 为空的股票跳过。",
    source=f"{COMMUNITY}；低换手 / 低波动异象，在 A 股上的有效性未经本系统验证",
    params=[param("top_pct", "入选比例(%)", 30, 10, 50, 5), param("min_amount", "最低日均成交额(亿)", 1, 0.2, 5, 0.1)],
    exits=short_exits(20, None), sizing=equal(20, 20)), _low_turnover_low_vol)


def _microcap_timed(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
    share = p["regime_pct"] / 100
    frame = (
        panel.with_columns(float_cap_pit().alias("_pit"))
        .with_columns(pl.when(c("_pit").is_not_null() & (c("_pit") > 0) & c("ret_20").is_not_null()).then(c("_pit")).alias("_fc"))
        .with_columns(
            c("_fc").rank("ordinal").over("date").alias("_rs"),
            c("_fc").rank("ordinal", descending=True).over("date").alias("_rl"),
            c("_fc").count().over("date").alias("_nc"),
        )
        .with_columns(
            pl.when(c("_rs") <= (c("_nc") * share).ceil()).then(c("ret_20")).mean().over("date").alias("_small_ret"),
            pl.when(c("_rl") <= (c("_nc") * share).ceil()).then(c("ret_20")).mean().over("date").alias("_large_ret"),
        )
        .with_columns(
            pl.when(c("_nc") >= MIN_CROSS_SECTION).then(c("_small_ret") - c("_large_ret")).alias("_rel")
        )
    )
    tradable = liquid(p["min_amount"]) & (c("days_listed") >= p["min_days"]) & (c("_pit") > 0)
    return pick_bottom(
        frame, [(c("_pit"), p["top_pct"])], tradable, MONTH,
        score=1 / c("_pit"), gate=c("_rel") >= 0,
    )


add(PlaybookSpec(
    id="microcap_timed", name="微盘择时", style="value", horizon="long",
    summary="小市值策略加开关：微盘相对大盘股的 20 日强度不为负才持有，否则空仓。",
    rule="开关：每个交易日，当日流通市值最小 10% 的股票 ret_20 等权均值 − 最大 10% 的股票 ret_20 等权均值（只用当日及以前数据，截面少于 20 只则关闭），≥ 0 为开。仅当月第一个交易日开关为开时：在 20 日均成交额 ≥ 0.5 亿元、days_listed ≥ 120 的股票中，取当日流通市值最小的 5%；关则当月无信号。当日流通市值 = float_cap × cap_est / total_cap = float_cap × 当日收盘 / 最新收盘（最新快照股本固定不变，不含此后的价格涨跌；total_cap 为空则跳过）。得分 = 1 / 当日流通市值。持有 20 日，不设价格止损。",
    source=f"{COMMUNITY}；在 small_cap（Banz, 1981 规模溢价）基础上加相对强弱开关，开关本身未经学术验证",
    params=[
        param("top_pct", "入选比例(%)", 5, 2, 20, 1), param("regime_pct", "强弱对比分位(%)", 10, 5, 20, 1),
        param("min_amount", "最低日均成交额(亿)", 0.5, 0.1, 5, 0.1), param("min_days", "最少上市交易日", 120, 60, 250, 10)],
    exits=short_exits(20, None), sizing=equal(20, 20)), _microcap_timed)
