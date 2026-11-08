"""Market regime detection: trend x liquidity x sentiment cycle x size style.

The frame is built from the research panel (breadth, limit-up statistics,
market turnover) and the index bars. Every rule only uses data up to the day it
labels, so a regime label on day D never changes when later days arrive —
except through the 5-day mode smoother, which is trailing as well.

Units: breadth_ma20, broken_rate, premium are percentages (0-100 / %).
Extra columns beyond base.py (index_ma60, ma60_slope, lu_percentile, style_spread)
are kept so explanations and diagnostics can quote the actual numbers.
"""
from __future__ import annotations

from datetime import datetime, timezone
import threading

import numpy as np
import polars as pl

from contracts.schema import DataProvenance, RegimeHistoryResponse, RegimePoint, RegimeState
from ..warehouse.store import Warehouse
from .panel import research_panel

INDEX_NAMES = {"000985": "中证全指", "000300": "沪深300", "000852": "中证1000"}
PRIMARY_INDEX = "000985"
LARGE_INDEX, SMALL_INDEX = "000300", "000852"

SLOPE_BARS = 20
BULL_SLOPE, BEAR_SLOPE = 2.0, -2.0
BULL_BREADTH, BEAR_BREADTH = 55.0, 45.0
SMOOTH_DAYS = 5
EXPANDING, CONTRACTING = 1.15, 0.85
PERCENTILE_WINDOW = 250
MIN_PERCENTILE_OBS = 30
FREEZING_PCT, CLIMAX_PCT = 20.0, 80.0
STYLE_SPREAD = 3.0

TREND_LABEL = {"bull": "牛市", "bear": "熊市", "range": "震荡"}
LIQUIDITY_LABEL = {"expanding": "增量", "steady": "存量", "contracting": "缩量"}
SENTIMENT_LABEL = {"freezing": "冰点", "repair": "修复", "climax": "高潮", "ebb": "退潮"}
STYLE_LABEL = {"small": "小盘占优", "large": "大盘占优", "balanced": "风格均衡"}

RULES = [
    "指数：优先使用 000985 中证全指，缺失时取第一个可用指数；指数也缺失时用全部个股涨跌幅均值合成等权指数。",
    "趋势：牛市 = 指数收盘 > MA60 且 MA60 的 20 日斜率 > +2% 且 breadth_ma20（收盘站上 20 日线的个股占比）> 55%；熊市 = 收盘 < MA60 且斜率 < −2% 且 breadth_ma20 < 45%；其余为震荡。再对原始判定取最近 5 个交易日的众数（并列取最近者）以避免来回跳变。",
    "流动性：全市场成交额的 20 日均值 / 120 日均值 > 1.15 为增量，< 0.85 为缩量，其余为存量。",
    "情绪：涨停家数 limit_up_count、炸板率 = 炸板 /（涨停 + 炸板）、最高连板 max_height、昨日涨停股今日平均涨幅 premium；p = 涨停家数在最近 250 日内的分位数。冰点 = p < 20 且 premium ≤ 0；高潮 = p > 80 且 max_height ≥ 其 250 日 80 分位；否则涨停家数 5 日均值较上一日上升为修复，不升为退潮。",
    "风格：中证1000 近 20 日涨幅 − 沪深300 近 20 日涨幅 > +3 个百分点为小盘占优，< −3 为大盘占优，其余（或缺少这两个指数）为均衡。",
    "历史不足时：MA60/斜率/分位数样本不足的日子，趋势按震荡、流动性按存量、分位数缺失时不判冰点与高潮。",
]

_CACHE: dict[str, pl.DataFrame] = {}
_LOCK = threading.Lock()


# --- pure helpers (also unit-tested) -------------------------------------------------


def mode_smooth(raw: list[str], window: int = SMOOTH_DAYS) -> list[str]:
    """Trailing mode of the last `window` labels; ties go to the most recent label."""
    out: list[str] = []
    for index in range(len(raw)):
        recent = raw[max(0, index - window + 1) : index + 1]
        counts: dict[str, int] = {}
        for label in recent:
            counts[label] = counts.get(label, 0) + 1
        best = max(counts.values())
        out.append(next(label for label in reversed(recent) if counts[label] == best))
    return out


def classify_sentiment(
    limit_up: np.ndarray, height: np.ndarray, premium: np.ndarray
) -> tuple[list[str], list[float | None]]:
    """Sentiment phase per day plus the rolling 250-day percentile of limit_up_count."""
    count = len(limit_up)
    mean5 = np.full(count, np.nan)
    for index in range(4, count):
        mean5[index] = limit_up[index - 4 : index + 1].mean()
    phases: list[str] = []
    percentiles: list[float | None] = []
    for index in range(count):
        window = slice(max(0, index - PERCENTILE_WINDOW + 1), index + 1)
        if index + 1 < MIN_PERCENTILE_OBS:
            percentile, height_cut = None, None
        else:
            percentile = float((limit_up[window] <= limit_up[index]).mean() * 100)
            height_cut = float(np.percentile(height[window], 80))
        percentiles.append(percentile)
        gain = premium[index] if not np.isnan(premium[index]) else 0.0
        if percentile is not None and percentile < FREEZING_PCT and gain <= 0:
            phases.append("freezing")
        elif percentile is not None and percentile > CLIMAX_PCT and height[index] >= height_cut:
            phases.append("climax")
        elif index >= 5 and mean5[index] > mean5[index - 1]:
            phases.append("repair")
        else:
            phases.append("ebb")
    return phases, percentiles


# --- frame -----------------------------------------------------------------------------


def _index_frame(wh: Warehouse, dates: pl.DataFrame, panel: pl.DataFrame) -> tuple[pl.DataFrame, str]:
    """(date, index_close) on the panel's dates, and the index display name."""
    bars = wh.read("index_bars")
    symbols = sorted(bars["symbol"].unique().to_list()) if bars is not None and not bars.is_empty() else []
    if symbols:
        symbol = PRIMARY_INDEX if PRIMARY_INDEX in symbols else symbols[0]
        series = bars.filter(pl.col("symbol") == symbol).select("date", pl.col("close").alias("index_close"))
        name = INDEX_NAMES.get(symbol, symbol)
    else:
        mean_pct = panel.group_by("date").agg(pl.col("pct_chg").mean().fill_null(0).alias("m")).sort("date")
        series = mean_pct.select("date", ((1 + pl.col("m") / 100).cum_prod() * 1000).alias("index_close"))
        name = "等权指数（由个股合成）"
    joined = dates.join(series, on="date", how="left").sort("date")
    return joined.with_columns(pl.col("index_close").forward_fill()), name


def _style_spread(wh: Warehouse, dates: pl.DataFrame) -> pl.DataFrame:
    """small - large 20-day return difference (percentage points), null when an index is missing."""
    bars = wh.read("index_bars")
    empty = dates.select("date", pl.lit(None, dtype=pl.Float64).alias("style_spread"))
    if bars is None or bars.is_empty():
        return empty
    wide = {}
    for key, symbol in (("small", SMALL_INDEX), ("large", LARGE_INDEX)):
        one = bars.filter(pl.col("symbol") == symbol).sort("date")
        if one.is_empty():
            return empty
        wide[key] = one.select("date", ((pl.col("close") / pl.col("close").shift(SLOPE_BARS) - 1) * 100).alias(key))
    merged = wide["small"].join(wide["large"], on="date", how="inner")
    spread = merged.select("date", (pl.col("small") - pl.col("large")).alias("style_spread"))
    return dates.join(spread, on="date", how="left")


def _daily_stats(panel: pl.DataFrame) -> pl.DataFrame:
    flagged = panel.lazy().with_columns(pl.col("is_limit_up").shift(1).over("code").fill_null(False).alias("_prev_lu"))
    return (
        flagged.group_by("date")
        .agg(
            pl.when(pl.col("ma20").is_not_null().sum() > 0)
            .then((pl.col("close") > pl.col("ma20")).sum() / pl.col("ma20").is_not_null().sum() * 100)
            .alias("breadth_ma20"),
            pl.col("amount").sum().alias("market_amount"),
            pl.col("is_limit_up").sum().cast(pl.Int64).alias("limit_up_count"),
            pl.col("is_broken").sum().cast(pl.Int64).alias("_broken"),
            pl.col("limit_streak").max().cast(pl.Int64).alias("max_height"),
            pl.col("pct_chg").filter(pl.col("_prev_lu")).mean().alias("premium"),
        )
        .sort("date")
        .collect()
    )


def _build(wh: Warehouse) -> tuple[pl.DataFrame, str]:
    panel = research_panel(wh).collect()
    if panel.is_empty():
        raise ValueError("research panel is empty; sync daily bars first")
    stats = _daily_stats(panel)
    index, name = _index_frame(wh, stats.select("date"), panel)
    frame = stats.join(index, on="date").join(_style_spread(wh, stats.select("date")), on="date").sort("date")
    frame = frame.with_columns(
        pl.col("index_close").rolling_mean(60).alias("index_ma60"),
        pl.col("market_amount").rolling_mean(20).alias("_a20"),
        pl.col("market_amount").rolling_mean(120).alias("_a120"),
        (pl.col("limit_up_count") + pl.col("_broken") > 0).alias("_has_board"),
    ).with_columns(
        ((pl.col("index_ma60") / pl.col("index_ma60").shift(SLOPE_BARS) - 1) * 100).alias("ma60_slope"),
        (pl.col("_a20") / pl.col("_a120")).alias("amount_ratio"),
        pl.when(pl.col("_has_board"))
        .then(pl.col("_broken") / (pl.col("limit_up_count") + pl.col("_broken")) * 100)
        .otherwise(0.0)
        .alias("broken_rate"),
    )
    close, ma60, slope, breadth = (frame[k].to_numpy() for k in ("index_close", "index_ma60", "ma60_slope", "breadth_ma20"))
    raw: list[str] = []
    for i in range(frame.height):
        if np.isnan(ma60[i]) or np.isnan(slope[i]):
            raw.append("range")
        elif close[i] > ma60[i] and slope[i] > BULL_SLOPE and breadth[i] > BULL_BREADTH:
            raw.append("bull")
        elif close[i] < ma60[i] and slope[i] < BEAR_SLOPE and breadth[i] < BEAR_BREADTH:
            raw.append("bear")
        else:
            raw.append("range")
    ratio = frame["amount_ratio"].to_numpy()
    liquidity = [
        "steady" if np.isnan(r) else "expanding" if r > EXPANDING else "contracting" if r < CONTRACTING else "steady"
        for r in ratio
    ]
    sentiment, percentiles = classify_sentiment(
        frame["limit_up_count"].to_numpy().astype(float),
        frame["max_height"].to_numpy().astype(float),
        frame["premium"].cast(pl.Float64).fill_null(float("nan")).to_numpy(),
    )
    spread = frame["style_spread"].cast(pl.Float64).fill_null(float("nan")).to_numpy()
    style = ["balanced" if np.isnan(s) else "small" if s > STYLE_SPREAD else "large" if s < -STYLE_SPREAD else "balanced" for s in spread]
    out = frame.select(
        "date",
        pl.Series("trend", mode_smooth(raw), dtype=pl.Utf8),
        pl.Series("liquidity", liquidity, dtype=pl.Utf8),
        pl.Series("sentiment", sentiment, dtype=pl.Utf8),
        pl.Series("style", style, dtype=pl.Utf8),
        "index_close", "breadth_ma20", "limit_up_count", "broken_rate", "max_height", "amount_ratio", "premium",
        "index_ma60", "ma60_slope",
        pl.Series("lu_percentile", percentiles, dtype=pl.Float64),
        "style_spread",
    )
    return out, name


def _cached(wh: Warehouse) -> tuple[pl.DataFrame, str]:
    key = wh.version()
    with _LOCK:
        hit = _CACHE.get(key)
    if hit is None:
        hit = _build(wh)
        with _LOCK:
            _CACHE.clear()
            _CACHE[key] = hit
    return hit


def regime_frame(wh: Warehouse) -> pl.DataFrame:
    """One row per trade date; columns documented in playbooks/base.py (plus diagnostics)."""
    return _cached(wh)[0]


# --- API objects -----------------------------------------------------------------------


def _num(value: object, digits: int = 2) -> float | None:
    if value is None:
        return None
    number = float(value)  # type: ignore[arg-type]
    return None if np.isnan(number) else round(number, digits)


def _point(row: dict) -> RegimePoint:
    return RegimePoint(
        date=row["date"].isoformat(),
        index_close=_num(row["index_close"]),
        trend=row["trend"],
        liquidity=row["liquidity"],
        sentiment=row["sentiment"],
        style=row["style"],
        breadth_ma20=_num(row["breadth_ma20"], 1),
        limit_up_count=int(row["limit_up_count"]) if row["limit_up_count"] is not None else None,
        broken_rate=_num(row["broken_rate"], 1),
        max_height=int(row["max_height"]) if row["max_height"] is not None else None,
        amount_ratio=_num(row["amount_ratio"], 3),
        premium=_num(row["premium"], 2),
    )


def _label(row: dict) -> str:
    return " · ".join(
        (TREND_LABEL[row["trend"]], LIQUIDITY_LABEL[row["liquidity"]], SENTIMENT_LABEL[row["sentiment"]], STYLE_LABEL[row["style"]])
    )


def _explain(row: dict, index_name: str, dataset: str) -> list[str]:
    def fmt(value: object, pattern: str, fallback: str = "无数据") -> str:
        number = _num(value, 6)
        return fallback if number is None else format(number, pattern)

    sentences = [
        f"趋势：{index_name}收盘 {fmt(row['index_close'], '.2f')}，MA60 为 {fmt(row['index_ma60'], '.2f')}"
        f"（20 日斜率 {fmt(row['ma60_slope'], '+.1f')}%），站上 20 日线的个股占 {fmt(row['breadth_ma20'], '.0f')}%，判定为{TREND_LABEL[row['trend']]}。",
        f"量能：全市场成交额 20 日均值 / 120 日均值 = {fmt(row['amount_ratio'], '.2f')}，判定为{LIQUIDITY_LABEL[row['liquidity']]}。",
        f"情绪：涨停 {row['limit_up_count']} 家（近 250 日分位 {fmt(row['lu_percentile'], '.0f')}%），炸板率 {fmt(row['broken_rate'], '.0f')}%，"
        f"最高 {row['max_height']} 板，昨日涨停股今日平均涨幅 {fmt(row['premium'], '+.1f')}%，处于{SENTIMENT_LABEL[row['sentiment']]}阶段。",
    ]
    if _num(row["style_spread"]) is None:
        sentences.append("风格：缺少中证1000 或沪深300 指数，按风格均衡处理。")
    else:
        sentences.append(
            f"风格：中证1000 较沪深300 近 20 日涨幅差 {fmt(row['style_spread'], '+.1f')} 个百分点，{STYLE_LABEL[row['style']]}。"
        )
    if dataset != "live":
        sentences.append("数据来自合成（SIM）历史，仅用于验证流程，不代表真实市场。")
    return sentences


def current_regime(wh: Warehouse) -> RegimeState:
    frame, name = _cached(wh)
    row = frame.row(frame.height - 1, named=True)
    return RegimeState(**_point(row).model_dump(), label=_label(row), explanation=_explain(row, name, wh.dataset))


def regime_history(wh: Warehouse, days: int = 500) -> RegimeHistoryResponse:
    frame, name = _cached(wh)
    tail = frame.tail(max(1, days))
    codes, _, end = wh.coverage()
    live = wh.dataset == "live"
    return RegimeHistoryResponse(
        provenance=DataProvenance(
            mode="LIVE" if live else "SIM",
            source="Pulse 历史仓库（真实日线）" if live else "Pulse 合成历史（SIM）",
            as_of=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            trade_date=end or "",
            detail=f"{frame.height} 个交易日，指数为{name}；市场状态由 {codes} 只股票的日线统计得出",
            coverage=codes,
        ),
        index_name=name,
        current=current_regime(wh),
        points=[_point(row) for row in tail.iter_rows(named=True)],
        rules=RULES,
    )
