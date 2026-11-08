"""Alphalens-style factor research on a (code, date, OHLCV) panel.

Per date the factor is rank-correlated with the forward return (Spearman IC),
names are bucketed into quantiles, and a long-short curve is built from
non-overlapping samples. All cross-sectional work runs as Polars window
expressions. Factors are signed so that a higher value should predict a higher
forward return; a negative IC then means the idea did not work, not a typo.
"""
from __future__ import annotations

import math
import time

import polars as pl

from contracts.schema import (
    DataProvenance,
    FactorInfo,
    FactorLabRequest,
    FactorLabResponse,
    ICPoint,
    MarketHistoryPoint,
    QuantileReturn,
)

MIN_NAMES = 10  # fewer stocks than this makes a rank correlation meaningless

CATALOG: list[FactorInfo] = [
    FactorInfo(key="momentum_20", label="20 日动量", description="收盘价 / 20 日前收盘价 - 1，追强势。"),
    FactorInfo(key="reversal_5", label="5 日反转", description="-(收盘价 / 5 日前收盘价 - 1)，近 5 日跌得多的预期反弹。"),
    FactorInfo(key="volatility_20", label="20 日低波", description="-(近 20 日日收益率标准差)，低波动异象：波动越小得分越高。"),
    FactorInfo(key="volume_surge", label="放量倍数", description="今日成交量 / 前 20 日平均成交量。"),
    FactorInfo(key="dist_high_60", label="距 60 日高点", description="收盘价 / 近 60 日最高价 - 1，越接近新高得分越高。"),
    FactorInfo(key="ma_gap_20", label="MA20 乖离", description="收盘价 / 20 日均价 - 1。"),
]


def _factor_expr(key: str) -> pl.Expr:
    close = pl.col("close")
    if key == "momentum_20":
        return close / close.shift(20).over("code") - 1
    if key == "reversal_5":
        return -(close / close.shift(5).over("code") - 1)
    if key == "volatility_20":
        return -pl.col("ret1").rolling_std(20).over("code")
    if key == "volume_surge":
        return pl.col("volume") / pl.col("volume").shift(1).rolling_mean(20).over("code")
    if key == "dist_high_60":
        return close / pl.col("high").rolling_max(60).over("code") - 1
    if key == "ma_gap_20":
        return close / close.rolling_mean(20).over("code") - 1
    raise ValueError(f"未知因子：{key}")


def _prepare(panel: pl.DataFrame, request: FactorLabRequest) -> pl.DataFrame:
    """Factor and forward return per row, restricted to the last `window` usable dates."""
    close = pl.col("close")
    frame = (
        panel.sort("code", "date")
        .with_columns(ret1=close / close.shift(1).over("code") - 1)
        .with_columns(
            factor=_factor_expr(request.factor),
            fwd=close.shift(-request.horizon).over("code") / close - 1,
        )
        .filter(
            pl.col("factor").is_not_null() & pl.col("factor").is_finite()
            & pl.col("fwd").is_not_null() & pl.col("fwd").is_finite()
        )
    )
    if not frame.height:
        return frame
    earliest = frame["date"].unique().sort().tail(request.window)[0]
    return frame.filter(pl.col("date") >= earliest)


def _daily_ic(frame: pl.DataFrame) -> list[ICPoint]:
    ranked = frame.with_columns(
        rf=pl.col("factor").rank("average").over("date"),
        rr=pl.col("fwd").rank("average").over("date"),
    )
    rows = ranked.group_by("date").agg(pl.corr("rf", "rr").alias("ic"), pl.len().alias("n")).sort("date")
    points = []
    for day, ic, n in rows.iter_rows():
        valid = n >= MIN_NAMES and ic is not None and math.isfinite(ic)
        points.append(ICPoint(date=day, ic=round(ic, 4) if valid else None))
    return points


def _ic_stats(points: list[ICPoint]) -> dict[str, float | None]:
    values = [p.ic for p in points if p.ic is not None]
    empty: dict[str, float | None] = {"ic_mean": None, "ic_std": None, "icir": None, "ic_t_stat": None, "ic_positive_ratio": None}
    if len(values) < 2:
        return empty
    mean = sum(values) / len(values)
    std = (sum((v - mean) ** 2 for v in values) / (len(values) - 1)) ** 0.5
    stats: dict[str, float | None] = {
        "ic_mean": round(mean, 4),
        "ic_std": round(std, 4),
        "icir": None,
        "ic_t_stat": None,
        "ic_positive_ratio": round(sum(1 for v in values if v > 0) / len(values) * 100, 1),
    }
    if std > 1e-12:
        stats["icir"] = round(mean / std, 3)
        stats["ic_t_stat"] = round(mean / std * math.sqrt(len(values)), 2)
    return stats


def _buckets(frame: pl.DataFrame, quantiles: int) -> pl.DataFrame:
    """Rank-bucket each date into 1..quantiles (quantiles = highest factor). Ties break by seeded random."""
    size = max(MIN_NAMES, quantiles)
    rank = pl.col("factor").rank("random", seed=0).over("date").cast(pl.Int64)
    count = pl.len().over("date").cast(pl.Int64)
    return (
        frame.filter(count >= size)
        .with_columns(q=((rank - 1) * quantiles // count + 1).cast(pl.Int64))
        .group_by("date", "q")
        .agg(pl.col("fwd").mean().alias("mean"), pl.len().alias("k"), pl.col("code"))
    )


def _quantile_returns(per_bucket: pl.DataFrame, quantiles: int) -> list[QuantileReturn]:
    out = []
    for q in range(1, quantiles + 1):
        rows = per_bucket.filter(pl.col("q") == q)
        if rows.height:
            out.append(QuantileReturn(quantile=q, mean_return=round(rows["mean"].mean() * 100, 3), count=int(rows["k"].sum())))
    return out


def _long_short(per_bucket: pl.DataFrame, quantiles: int, horizon: int) -> tuple[list[MarketHistoryPoint], float | None]:
    """Compound top-minus-bottom on every `horizon`-th date so holding periods never overlap."""
    lookup = {(d, q): (m, set(codes)) for d, q, m, _, codes in per_bucket.iter_rows()}
    days = sorted(per_bucket["date"].unique().to_list())[::horizon]
    curve: list[MarketHistoryPoint] = []
    growth = 1.0
    changed: list[float] = []
    previous: set[str] | None = None
    for day in days:
        top, bottom = lookup.get((day, quantiles)), lookup.get((day, 1))
        if top is None or bottom is None:
            continue
        growth *= 1 + (top[0] - bottom[0])
        curve.append(MarketHistoryPoint(time=day, value=round((growth - 1) * 100, 3)))
        if previous is not None and top[1]:
            changed.append(1 - len(previous & top[1]) / len(top[1]))
        previous = top[1]
    turnover = round(sum(changed) / len(changed) * 100, 1) if changed else None
    return curve, turnover


def _notes(request: FactorLabRequest, universe: int, days: int, rows: int, provenance: DataProvenance) -> list[str]:
    notes = [
        f"样本：{universe} 只股票 × {days} 个评估日，共 {rows} 个观测；因子与未来 {request.horizon} 日收益均由面板日线计算。",
        f"重叠处理：未来 {request.horizon} 日收益在相邻交易日高度重叠，逐日 IC 序列存在自相关，t 值偏乐观；"
        f"多空曲线每隔 {request.horizon} 个交易日取样一次以避免持仓重叠，分位收益为逐日均值。",
        f"IC 为 Spearman 秩相关，少于 {MIN_NAMES} 只股票的日期记空；分组为 {request.quantiles} 档，第 {request.quantiles} 档为因子值最高档，同值按固定随机种子打散。",
        "未扣除交易成本、涨跌停无法成交与停牌，也未做行业与市值中性化。",
    ]
    if provenance.mode == "SIM":
        notes.append("当前为合成日线（SIM），IC 与分组收益没有任何经济含义，仅用于验证计算路径。")
    else:
        notes.append(
            f"股票池规则：仅含已缓存真实日线的 {provenance.coverage} 只；预热按今日成交额前 300 只，存在选择偏差"
            "（按今天的强势股回看历史，动量与距新高类因子的 IC 会被高估）。"
        )
    return notes


def run(panel: pl.DataFrame, request: FactorLabRequest, provenance: DataProvenance) -> FactorLabResponse:
    started = time.perf_counter()
    frame = _prepare(panel, request) if panel.height else panel.clear()
    universe = panel["code"].n_unique() if panel.height else 0
    ic_series = _daily_ic(frame) if frame.height else []
    buckets = _buckets(frame, request.quantiles) if frame.height else None
    quantile_returns = _quantile_returns(buckets, request.quantiles) if buckets is not None and buckets.height else []
    curve, turnover = _long_short(buckets, request.quantiles, request.horizon) if buckets is not None and buckets.height else ([], None)
    notes = _notes(request, universe, len(ic_series), frame.height, provenance)
    if not ic_series:
        notes.insert(0, "面板中没有足够的历史样本（需要因子窗口加未来收益窗口），无法评估。")
    return FactorLabResponse(
        provenance=provenance,
        request=request,
        catalog=CATALOG,
        universe=universe,
        days=len(ic_series),
        execution_ms=round((time.perf_counter() - started) * 1000, 2),
        ic_series=ic_series,
        quantile_returns=quantile_returns,
        long_short=curve,
        top_turnover=turnover,
        notes=notes,
        **_ic_stats(ic_series),
    )
