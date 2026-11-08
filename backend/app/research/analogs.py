"""Analog forecast: what happened after historically similar setups.

This is the honest, dependency-free alternative to a K-line foundation model
such as Kronos. Kronos needs PyTorch and a multi-hundred-megabyte checkpoint
download, and its output would still be a point forecast with no stated
uncertainty. Here the target stock's latest state is described by six
standardised features, the K nearest historical states (of other stocks, and
of the same stock long ago) are found, and the *distribution* of what happened
to them over the next `horizon` bars is reported. Nothing is trained, nothing
is downloaded, and a wide distribution says "uncertain" in plain numbers.

Features per (code, date), all from daily bars: 5/20-day return, 20-day
volatility of daily returns, distance to the 60-day high, volume over its
20-day mean, and close over the 20-day mean.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import threading

import numpy as np
import polars as pl

from contracts.schema import AnalogExample, AnalogForecast, AnalogQuantiles, DataProvenance
from ..warehouse.store import Warehouse, active

CST = timezone(timedelta(hours=8))
NEIGHBORS = 200
EXAMPLES = 8
MAX_CANDIDATES = 300_000
SAME_CODE_MIN_AGE = 120  # bars; younger rows of the target stock overlap its present
MIN_HORIZON, MAX_HORIZON = 5, 60
SAMPLE_SEED = 20261030
Z_CLIP = 5.0  # limit-up days must not dominate a standardised distance
OVERSAMPLE = (5, 20)  # candidate pool multiples tried when de-overlapping neighbours
FEATURES = ("ret_5", "ret_20", "volatility_20", "dist_60d_high", "volume_ratio", "ma20_gap")
QUANTILES = (10, 25, 50, 75, 90)
DISCLAIMER = "相似情景不是预测，分布宽说明不确定性高。"


@dataclass(frozen=True)
class _Panel:
    """Per-row features of every bar, sorted by (code, date)."""

    codes: tuple[str, ...]  # unique codes in row order
    starts: np.ndarray  # first row of each code
    code_ids: np.ndarray  # row -> index into `codes`
    positions: np.ndarray  # bar number within the code, 0-based
    dates: np.ndarray  # datetime64[D]
    close: np.ndarray
    features: np.ndarray  # (rows, 6), NaN where undefined


_CACHE: dict[tuple[str, str], _Panel] = {}
_LOCK = threading.Lock()


def _build_panel(wh: Warehouse) -> _Panel | None:
    lazy = wh.scan("bars")
    if lazy is None:
        return None
    close, high, volume = pl.col("close"), pl.col("high"), pl.col("volume")
    over = "code"
    daily = close / close.shift(1).over(over) - 1
    frame = (
        lazy.select("code", "date", "close", "high", "volume")
        .sort(["code", "date"])
        .with_columns(
            ((close / close.shift(5).over(over) - 1) * 100).alias("ret_5"),
            ((close / close.shift(20).over(over) - 1) * 100).alias("ret_20"),
            (daily.rolling_std(20, min_samples=20).over(over) * 100).alias("volatility_20"),
            ((close / high.rolling_max(60, min_samples=60).over(over) - 1) * 100).alias("dist_60d_high"),
            (volume / volume.rolling_mean(20, min_samples=20).over(over)).alias("volume_ratio"),
            ((close / close.rolling_mean(20, min_samples=20).over(over) - 1) * 100).alias("ma20_gap"),
            pl.col("code").rle_id().alias("code_id"),
            pl.int_range(pl.len()).over(over).alias("position"),
        )
        .collect()
    )
    if frame.is_empty():
        return None
    code_ids = frame["code_id"].to_numpy().astype(np.int64)
    starts = np.flatnonzero(np.r_[True, code_ids[1:] != code_ids[:-1]])
    codes = tuple(frame["code"].to_numpy()[starts].tolist())
    matrix = np.column_stack([frame[name].cast(pl.Float64).fill_null(np.nan).to_numpy() for name in FEATURES])
    return _Panel(
        codes=codes,
        starts=starts,
        code_ids=code_ids,
        positions=frame["position"].to_numpy().astype(np.int64),
        dates=frame["date"].to_numpy().astype("datetime64[D]"),
        close=frame["close"].cast(pl.Float64).to_numpy(),
        features=matrix,
    )


def _panel(wh: Warehouse) -> _Panel | None:
    """Features are expensive on a real warehouse (millions of rows); the key
    changes whenever a table file does, so a sync invalidates it."""
    key = (str(wh.root), wh.version())
    with _LOCK:
        cached = _CACHE.get(key)
    if cached is not None:
        return cached
    built = _build_panel(wh)
    if built is None:
        return None
    with _LOCK:
        _CACHE.clear()  # one warehouse state at a time bounds memory
        _CACHE[key] = built
    return built


def _provenance(wh: Warehouse, trade_date: str, candidates: int) -> DataProvenance:
    live = wh.dataset == "live"
    return DataProvenance(
        mode="LIVE" if live else "SIM",
        source="数据仓日线 · 相似情景检索" if live else "合成历史 · 相似情景检索",
        as_of=datetime.now(CST).isoformat(timespec="seconds"),
        trade_date=trade_date,
        status="ready" if live else "fallback",
        detail="基于真实历史日线的最近邻统计。" if live else "基于合成历史，仅演示流程。",
        coverage=candidates,
    )


def _target_features(panel: _Panel, row: int) -> dict[str, float | None]:
    return {
        name: (round(float(value), 4) if np.isfinite(value) else None)
        for name, value in zip(FEATURES, panel.features[row])
    }


def _empty(wh: Warehouse, code: str, horizon: int, panel: _Panel, row: int, reason: str) -> AnalogForecast:
    trade_date = str(panel.dates[row])
    return AnalogForecast(
        provenance=_provenance(wh, trade_date, 0),
        code=code,
        as_of=trade_date,
        features=_target_features(panel, row),
        neighbors=0,
        horizon=horizon,
        path=[],
        examples=[],
        notes=[reason, DISCLAIMER],
    )


def _pick_neighbors(
    panel: _Panel, pool: np.ndarray, distance: np.ndarray, count: int, gap: int
) -> list[int]:
    """Closest candidates, skipping ones whose forward window overlaps a closer
    pick of the same stock: consecutive days of one stock are not independent
    samples and would make the distribution look tighter than it is."""
    order = np.argsort(distance, kind="stable")
    chosen: list[int] = []  # indexes into `pool`
    taken: dict[int, list[int]] = {}
    for rank in order:
        row = int(pool[rank])
        code_id, position = int(panel.code_ids[row]), int(panel.positions[row])
        if any(abs(position - other) < gap for other in taken.get(code_id, ())):
            continue
        taken.setdefault(code_id, []).append(position)
        chosen.append(int(rank))
        if len(chosen) == count:
            break
    return chosen


def forecast(code: str, horizon: int = 20, wh: Warehouse | None = None) -> AnalogForecast:
    """Nearest-neighbour forward-return distribution for `code`'s latest bar.

    Raises LookupError when the warehouse holds no bars for the code.
    """
    if not MIN_HORIZON <= horizon <= MAX_HORIZON:
        raise ValueError(f"horizon 必须在 {MIN_HORIZON} 到 {MAX_HORIZON} 之间")
    wh = wh or active()
    panel = _panel(wh)
    if panel is None or code not in panel.codes:
        raise LookupError(f"数据仓中没有 {code} 的日线")
    code_id = panel.codes.index(code)
    last_row = int(panel.starts[code_id + 1] - 1) if code_id + 1 < len(panel.starts) else len(panel.close) - 1
    target = panel.features[last_row]
    if not np.all(np.isfinite(target)):
        return _empty(wh, code, horizon, panel, last_row, "历史不足 60 根日线，特征无法计算，不给出相似情景。")

    total = len(panel.close)
    has_future = np.zeros(total, dtype=bool)
    has_future[: total - horizon] = panel.code_ids[: total - horizon] == panel.code_ids[horizon:]
    usable = has_future & np.all(np.isfinite(panel.features), axis=1)
    same_code = panel.code_ids == code_id
    usable &= ~same_code | (panel.positions <= panel.positions[last_row] - SAME_CODE_MIN_AGE)
    candidates = np.flatnonzero(usable)
    if candidates.size == 0:
        return _empty(wh, code, horizon, panel, last_row, "没有可用的历史相似样本。")
    available = int(candidates.size)
    sampled = available > MAX_CANDIDATES
    if sampled:
        rng = np.random.default_rng(SAMPLE_SEED)
        candidates = np.sort(rng.choice(candidates, size=MAX_CANDIDATES, replace=False))

    block = panel.features[candidates]
    mean, deviation = block.mean(axis=0), block.std(axis=0)
    deviation = np.where(deviation > 1e-12, deviation, 1.0)
    z_block = np.clip((block - mean) / deviation, -Z_CLIP, Z_CLIP)
    z_target = np.clip((target - mean) / deviation, -Z_CLIP, Z_CLIP)
    distance = np.sqrt(((z_block - z_target) ** 2).sum(axis=1))

    for multiple in OVERSAMPLE:
        pool_size = min(candidates.size, NEIGHBORS * multiple)
        if pool_size < candidates.size:
            near_rows = np.argpartition(distance, pool_size - 1)[:pool_size]
        else:
            near_rows = np.arange(candidates.size)
        picked = _pick_neighbors(panel, candidates[near_rows], distance[near_rows], NEIGHBORS, horizon)
        if len(picked) >= NEIGHBORS or pool_size == candidates.size:
            break
    rows = candidates[near_rows][picked]
    picked_distance = distance[near_rows][picked]

    steps = np.arange(1, horizon + 1)
    base = panel.close[rows][:, None]
    paths = (panel.close[rows[:, None] + steps[None, :]] / base - 1) * 100  # (k, horizon), percent
    quantiles = np.percentile(paths, QUANTILES, axis=0)
    final = paths[:, -1]
    closest = np.argsort(picked_distance, kind="stable")[:EXAMPLES]
    trade_date = str(panel.dates[last_row])

    notes = [
        f"在 {available:,} 条历史相似候选"
        + (f"（确定性抽样 {MAX_CANDIDATES:,} 条）" if sampled else "")
        + f"中取最相似的 {rows.size} 条（标准化欧氏距离，特征截尾 ±{Z_CLIP:g}σ），统计其后 {horizon} 个交易日的累计收益。",
        "候选来自其他股票，以及本股 120 根日线以前的历史；同一股票的相邻交易日窗口会重叠，已按预测期间隔去重。",
        "特征单位：收益、波动率（日收益标准差）、距 60 日高点、与 20 日均线偏离为 %；量比为倍数。",
        "这是最近邻情景统计，不是 K 线基础模型（如 Kronos 需要 PyTorch 与模型权重下载）；没有训练任何神经网络。",
        DISCLAIMER,
    ]
    if rows.size < NEIGHBORS:
        notes.append(f"去重后只找到 {rows.size} 条，样本偏少，结论更不稳。")
    if wh.dataset != "live":
        notes.append("当前历史为合成数据（SIM）：只演示流程，不代表真实市场。")

    return AnalogForecast(
        provenance=_provenance(wh, trade_date, available),
        code=code,
        as_of=trade_date,
        features=_target_features(panel, last_row),
        neighbors=int(rows.size),
        horizon=horizon,
        prob_up=round(float((final > 0).mean()), 4),
        mean_return=round(float(final.mean()), 3),
        path=[
            AnalogQuantiles(
                day=int(day),
                **{f"p{q}": round(float(quantiles[index, day - 1]), 3) for index, q in enumerate(QUANTILES)},
            )
            for day in steps
        ],
        examples=[
            AnalogExample(
                code=panel.codes[int(panel.code_ids[rows[i]])],
                date=str(panel.dates[rows[i]]),
                distance=round(float(picked_distance[i]), 4),
                forward_return=round(float(final[i]), 3),
            )
            for i in closest
        ],
        notes=notes,
    )
