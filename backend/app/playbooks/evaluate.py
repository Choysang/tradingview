"""Playbook evaluation: backtest, in/out-of-sample split, regime attribution, fitness.

`evaluate` is the single place that turns a playbook plus the warehouse into a
`PlaybookEvaluation`. Verdicts are pure functions of the performance statistics
(`judge`), so they can be tested on constructed trade sets.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import logging
import math
import threading
import time
from typing import Any

import numpy as np
import polars as pl

from contracts.schema import (
    DataProvenance,
    EquityPoint,
    ExitRules,
    PerformanceStats,
    PlaybookBoard,
    PlaybookEvaluation,
    PlaybookRunRequest,
    PlaybookSpec,
    PlaybookSummary,
    PlaybookTrade,
    PlaybookVerdict,
    RegimePerformance,
    RegimeState,
    SizingRule,
)
from ..warehouse.store import Warehouse, active
from .backtest import ASSUMPTIONS, simulate
from .base import Playbook, catalog, resolve

LOG = logging.getLogger("pulse.playbooks")

CST = timezone(timedelta(hours=8))
SPLIT_FRACTION = 0.7
RECENT_BARS = 60
MIN_TRADES = 30
BENCHMARK_SYMBOL = "000985"

REGIME_LABELS: dict[str, dict[str, str]] = {
    "trend": {"bull": "牛市", "bear": "熊市", "range": "震荡"},
    "liquidity": {"expanding": "增量", "steady": "存量", "contracting": "缩量"},
    "sentiment": {"freezing": "冰点", "repair": "修复", "climax": "高潮", "ebb": "退潮"},
    "style": {"large": "大盘占优", "small": "小盘占优", "balanced": "均衡"},
}
DIMENSIONS = tuple(REGIME_LABELS)


# --- data context -----------------------------------------------------------------

@dataclass
class Context:
    wh: Warehouse
    version: str
    panel: pl.DataFrame
    dates: list  # sorted trading dates (datetime.date)
    regimes: dict[str, dict[str, str]]  # iso date -> {dimension: key}
    benchmark: pl.DataFrame | None
    names: dict[str, str]


_CTX: Context | None = None
_CTX_LOCK = threading.RLock()


def context(wh: Warehouse | None = None) -> Context:
    """Panel, regimes and benchmark for the warehouse; rebuilt when its version changes."""
    global _CTX
    wh = wh or active()
    version = wh.version()
    with _CTX_LOCK:
        if _CTX is not None and _CTX.version == version and _CTX.wh.root == wh.root:
            return _CTX
        from .panel import research_panel
        from .regime import regime_frame

        panel = research_panel(wh).collect()
        frame = regime_frame(wh)
        frame = frame.collect() if isinstance(frame, pl.LazyFrame) else frame
        regimes = {
            row["date"].isoformat() if hasattr(row["date"], "isoformat") else str(row["date"]): {
                dim: row[dim] for dim in DIMENSIONS
            }
            for row in frame.select("date", *DIMENSIONS).iter_rows(named=True)
        }
        index = wh.scan("index_bars")
        benchmark = None
        if index is not None:
            benchmark = (
                index.filter(pl.col("symbol") == BENCHMARK_SYMBOL).select("date", "open", "close").sort("date").collect()
            )
            if benchmark.is_empty():
                benchmark = None
        names = dict(panel.select("code", "name").unique("code", keep="last").iter_rows()) if not panel.is_empty() else {}
        _CTX = Context(
            wh=wh,
            version=version,
            panel=panel,
            dates=panel["date"].unique().sort().to_list(),
            regimes=regimes,
            benchmark=benchmark,
            names={k: v for k, v in names.items() if v},
        )
        return _CTX


def dataset_provenance(ctx: Context) -> DataProvenance:
    codes = ctx.panel["code"].n_unique() if not ctx.panel.is_empty() else 0
    last = ctx.dates[-1].isoformat() if ctx.dates else ""
    if ctx.wh.dataset == "sim":
        source, detail = "合成历史仓库（SIM）", "确定性合成日线，仅用于验证研究流程，不是市场数据。"
    else:
        source, detail = "历史仓库（真实日线）", "同步自数据源的前复权日线；回测不含停牌、除权以外的撮合细节。"
    return DataProvenance(
        mode="SIM" if ctx.wh.dataset == "sim" else "LIVE",
        source=source,
        as_of=datetime.now(CST).isoformat(timespec="seconds"),
        trade_date=last,
        detail=detail,
        coverage=codes,
    )


def current_regime_state(wh: Warehouse) -> RegimeState:
    from .regime import current_regime

    return current_regime(wh)


# --- statistics --------------------------------------------------------------------

def _round(value: float | None, digits: int = 3) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), digits)


def stats(trades: list[PlaybookTrade], equity: pl.DataFrame | None = None) -> PerformanceStats:
    """Trade statistics (percentage points) and, when an equity curve is given, curve statistics.

    `equity` needs `strategy` (NAV) and optionally `exposure`; pass the slice of
    the curve that belongs to the same period as the trades. With a `prev_nav`
    column (see `_slice_equity`) the slice's first day is measured against the
    NAV just before it; without one the first row is only a base.
    """
    out: dict[str, Any] = {"trades": len(trades)}
    if trades:
        returns = np.array([t.return_pct for t in trades], dtype=float)
        n = len(returns)
        wins, losses = returns[returns > 0], returns[returns <= 0]
        out["win_rate"] = _round(len(wins) / n * 100, 2)
        out["avg_return"] = _round(returns.mean())
        out["median_return"] = _round(np.median(returns))
        if losses.sum() < 0:
            out["profit_factor"] = _round(wins.sum() / -losses.sum(), 3)
        if n >= 2 and returns.std(ddof=1) > 0:
            out["t_stat"] = _round(returns.mean() / (returns.std(ddof=1) / math.sqrt(n)), 3)
        out["avg_holding_days"] = _round(np.mean([t.holding_days for t in trades]), 2)
    if equity is not None and equity.height >= 2:
        nav = equity["strategy"].to_numpy().astype(float)
        series = np.concatenate([[float(equity["prev_nav"][0])], nav]) if "prev_nav" in equity.columns else nav
        daily = series[1:] / series[:-1] - 1
        if len(daily) >= 2:
            growth = series[-1] / series[0]
            out["annual_return"] = _round((growth ** (252 / len(daily)) - 1) * 100 if growth > 0 else -100.0, 2)
            std = daily.std(ddof=1)
            if std > 1e-12:
                out["sharpe"] = _round(daily.mean() / std * math.sqrt(252), 3)
        peak = np.maximum.accumulate(series)
        out["max_drawdown"] = _round(float((series / peak - 1).min() * 100), 2)
        if "exposure" in equity.columns:
            out["exposure_pct"] = _round(float(equity["exposure"].mean()) * 100, 2)
    return PerformanceStats(**out)


def _slice_equity(equity: pl.DataFrame, start, end) -> pl.DataFrame:
    """Equity rows in [start, end] with the NAV just before the slice as `prev_nav`."""
    with_prev = equity.with_columns(pl.col("strategy").shift(1).alias("prev_nav"))
    mask = pl.lit(True)
    if start is not None:
        mask = mask & (pl.col("date") >= start)
    if end is not None:
        mask = mask & (pl.col("date") <= end)
    sliced = with_prev.filter(mask)
    if sliced.is_empty():
        return sliced
    first = sliced["prev_nav"][0]
    return sliced.with_columns(pl.col("prev_nav").fill_null(sliced["strategy"][0] if first is None else first))


def _stats_slice(trades: list[PlaybookTrade], equity: pl.DataFrame, start, end) -> PerformanceStats:
    return stats(trades, _slice_equity(equity, start, end))


# --- verdict and fitness ------------------------------------------------------------

def _fmt(value: float | None, suffix: str = "", digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}{suffix}"


def judge(
    full: PerformanceStats,
    in_sample: PerformanceStats,
    out_of_sample: PerformanceStats,
    by_regime: list[RegimePerformance],
) -> tuple[PlaybookVerdict, str]:
    if full.trades < MIN_TRADES:
        return "insufficient", f"全样本仅 {full.trades} 笔交易，少于 {MIN_TRADES} 笔，统计结论不可靠。"
    if out_of_sample.trades < 5:
        return "insufficient", f"样本外仅 {out_of_sample.trades} 笔交易，无法验证样本内表现是否稳健。"
    is_sharpe, oos_sharpe = in_sample.sharpe or 0.0, out_of_sample.sharpe or 0.0
    is_avg, oos_avg = in_sample.avg_return or 0.0, out_of_sample.avg_return or 0.0
    if is_sharpe > 1 and oos_sharpe < 0.3 * is_sharpe:
        return "overfit", (
            f"样本内夏普 {is_sharpe:.2f} 而样本外仅 {oos_sharpe:.2f}（不足 30%），"
            "疑似对历史过拟合。"
        )
    if is_avg > 0 and oos_avg <= 0:
        return "overfit", f"样本内平均收益 {is_avg:+.2f}% 但样本外 {oos_avg:+.2f}%，样本外失效，疑似过拟合。"
    t_full = full.t_stat or 0.0
    if oos_avg > 0 and oos_sharpe > 0.5 and t_full >= 2:
        return "effective", (
            f"样本外平均收益 {oos_avg:+.2f}%、夏普 {oos_sharpe:.2f}，全样本 t 值 {t_full:.2f}，"
            f"共 {full.trades} 笔，样本外验证有效。"
        )
    best: tuple[RegimePerformance, float] | None = None
    for item in by_regime:
        s = item.stats
        if s.trades >= 15 and (s.avg_return or 0) > 0 and (s.t_stat or 0) >= 2:
            if best is None or (s.t_stat or 0) > best[1]:
                best = (item, s.t_stat or 0)
    if best is not None:
        s = best[0].stats
        return "regime_dependent", (
            f"整体表现一般（样本外夏普 {oos_sharpe:.2f}），但在{best[0].label}"
            f"（{_DIM_NAMES[best[0].dimension]}）下 {s.trades} 笔平均 {s.avg_return:+.2f}%、t 值 {s.t_stat:.2f}，"
            "只适合该环境。"
        )
    return "weak", (
        f"样本外平均收益 {oos_avg:+.2f}%、夏普 {oos_sharpe:.2f}，全样本 t 值 {t_full:.2f}，"
        "未表现出稳定的正期望。"
    )


_DIM_NAMES = {"trend": "趋势", "liquidity": "流动性", "sentiment": "情绪", "style": "风格"}


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _avg_score(s: PerformanceStats) -> float:
    return _clip((s.avg_return or 0.0) / 2.0)


def fitness(
    full: PerformanceStats,
    out_of_sample: PerformanceStats,
    recent: PerformanceStats,
    by_regime: list[RegimePerformance],
    verdict: PlaybookVerdict,
    trend: str | None,
    sentiment: str | None,
) -> tuple[float, list[str]]:
    """0-100 suitability for the current regime, with every component listed."""
    reasons: list[str] = []
    total = 0.0

    oos_sharpe = _clip((out_of_sample.sharpe or 0.0) / 2.0)
    oos_avg = _avg_score(out_of_sample)
    oos = 0.5 * oos_sharpe + 0.5 * oos_avg
    total += 35 * oos
    reasons.append(
        f"样本外质量 {35 * oos:.1f}/35：夏普 {_fmt(out_of_sample.sharpe)}、平均收益 {_fmt(out_of_sample.avg_return, '%')}"
    )

    def regime_part(dimension: str, key: str | None, weight: float) -> None:
        nonlocal total
        label = REGIME_LABELS[dimension].get(key or "", key or "未知")
        bucket = next((r for r in by_regime if r.dimension == dimension and r.regime == key), None)
        if bucket is not None and bucket.stats.trades >= 10:
            score = _avg_score(bucket.stats)
            total += weight * score
            reasons.append(
                f"当前{_DIM_NAMES[dimension]}（{label}）{weight * score:.1f}/{weight:g}："
                f"{bucket.stats.trades} 笔平均 {_fmt(bucket.stats.avg_return, '%')}"
            )
        else:
            n = bucket.stats.trades if bucket else 0
            score = 0.6 * _avg_score(full)
            total += weight * score
            reasons.append(
                f"当前{_DIM_NAMES[dimension]}（{label}）{weight * score:.1f}/{weight:g}："
                f"该环境仅 {n} 笔（<10），回退全样本平均 {_fmt(full.avg_return, '%')} 并打 6 折"
            )

    regime_part("trend", trend, 20)
    regime_part("sentiment", sentiment, 15)

    if recent.trades >= 5:
        score = _avg_score(recent)
        total += 15 * score
        reasons.append(f"近 {RECENT_BARS} 日 {15 * score:.1f}/15：{recent.trades} 笔平均 {_fmt(recent.avg_return, '%')}")
    else:
        score = 0.5 * _avg_score(full)
        total += 15 * score
        reasons.append(
            f"近 {RECENT_BARS} 日 {15 * score:.1f}/15：仅 {recent.trades} 笔（<5），回退全样本并打 5 折"
        )

    sig = _clip((full.t_stat or 0.0) / 3.0)
    total += 10 * sig
    reasons.append(f"显著性 {10 * sig:.1f}/10：t 值 {_fmt(full.t_stat)}")
    size = _clip(full.trades / 200.0)
    total += 5 * size
    reasons.append(f"样本量 {5 * size:.1f}/5：{full.trades} 笔")

    cap = {"overfit": 25.0, "insufficient": 35.0}.get(verdict)
    if cap is not None and total > cap:
        reasons.append(f"判定为{_VERDICT_NAMES[verdict]}，适配度上限压到 {cap:g}")
        total = cap
    return round(_clip(total, 0, 100), 1), reasons


_VERDICT_NAMES = {"overfit": "过拟合", "insufficient": "样本不足"}


# --- evaluation ---------------------------------------------------------------------

def _downsample(items: list, limit: int) -> list:
    if len(items) <= limit:
        return items
    picks = np.unique(np.linspace(0, len(items) - 1, limit).round().astype(int))
    return [items[i] for i in picks]


def _requires_history_missing(panel: pl.DataFrame, columns: tuple[str, ...]) -> list[str]:
    """Columns that are blank or a static latest snapshot: not usable for a point-in-time backtest."""
    missing = []
    for column in columns:
        if column not in panel.columns or panel[column].null_count() == panel.height:
            missing.append(column)
            continue
        varies = (
            panel.group_by("code").agg(pl.col(column).n_unique().alias("n")).select((pl.col("n") > 1).any()).item()
        )
        if not varies:
            missing.append(column)
    return missing


def ebb_days(ctx: Context) -> frozenset[date]:
    """Dates whose sentiment regime is "ebb", for the sell-on-ebb exit rule."""
    return frozenset(date.fromisoformat(day) for day, regime in ctx.regimes.items() if regime.get("sentiment") == "ebb")


def run_signals(pb: Playbook, ctx: Context, params: dict[str, float] | None = None) -> pl.DataFrame:
    values = pb.params(params)
    out = pb.signal(ctx.panel.lazy(), values).select("code", "date", pl.col("score").cast(pl.Float64)).collect()
    return out.drop_nulls().filter(pl.col("score") > 0)


def _params_hash(values: dict[str, float], exits: ExitRules, sizing: SizingRule) -> str:
    payload = json.dumps([values, exits.model_dump(), sizing.model_dump()], sort_keys=True)
    return hashlib.sha1(payload.encode()).hexdigest()[:10]


def _tag_regime(trades: list[PlaybookTrade], ctx: Context, names: dict[str, str]) -> list[PlaybookTrade]:
    return [
        t.model_copy(update={
            "name": names.get(t.code),
            "regime": (ctx.regimes.get(t.entry_date) or {}).get("trend"),
        })
        for t in trades
    ]


def _regime_breakdown(trades: list[PlaybookTrade], ctx: Context) -> list[RegimePerformance]:
    out: list[RegimePerformance] = []
    for dimension in DIMENSIONS:
        groups: dict[str, list[PlaybookTrade]] = {}
        for t in trades:
            key = (ctx.regimes.get(t.entry_date) or {}).get(dimension)
            if key:
                groups.setdefault(key, []).append(t)
        for key, label in REGIME_LABELS[dimension].items():
            if key in groups:
                out.append(RegimePerformance(
                    dimension=dimension, regime=key, label=label, stats=stats(groups[key])  # type: ignore[arg-type]
                ))
    return out


def _compute(
    pb: Playbook,
    ctx: Context,
    params: dict[str, float] | None,
    exits: ExitRules,
    sizing: SizingRule,
    trend: str | None,
    sentiment: str | None,
) -> PlaybookEvaluation:
    started = time.perf_counter()
    spec = pb.spec
    last_date = ctx.dates[-1] if ctx.dates else None
    failure = ""
    try:
        signals = run_signals(pb, ctx, params)
    except Exception as exc:  # a broken signal must not take the whole board down, but it must be visible
        failure = f"（信号计算失败：{type(exc).__name__}: {exc}）"
        signals = pl.DataFrame(schema={"code": pl.Utf8, "date": pl.Date, "score": pl.Float64})
    today = int(signals.filter(pl.col("date") == last_date).height) if last_date is not None else 0
    provenance = dataset_provenance(ctx)
    period_start = ctx.dates[0].isoformat() if ctx.dates else ""
    period_end = last_date.isoformat() if last_date else ""

    blank = _requires_history_missing(ctx.panel, pb.requires_history) if pb.requires_history else []
    if not spec.available or blank:
        reason = spec.unavailable_reason or (
            f"仓库缺少这套战法需要的逐日数据（{', '.join(blank)}）：没有入库或只有最新快照，无法做无前视的回测。"
        )
        empty = PerformanceStats()
        return PlaybookEvaluation(
            spec=spec, full=empty, out_of_sample=empty, in_sample=empty, recent=empty, by_regime=[],
            fitness=None, verdict="unavailable", verdict_reason=f"未回测：{reason}仅列出今日信号。",
            signals_today=today, equity_sparkline=[], provenance=provenance,
            period_start=period_start, period_end=period_end, split_date=period_end,
            equity=[], trades=[], fitness_reasons=[], assumptions=list(ASSUMPTIONS),
            execution_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    ebb = ebb_days(ctx)
    entry = pb.spec.entry
    trades, equity = simulate(ctx.panel, signals, exits, sizing, ctx.benchmark, ebb_days=ebb, entry=entry)
    split_idx = max(1, int(len(ctx.dates) * SPLIT_FRACTION))
    split_date = ctx.dates[split_idx - 1]
    recent_start = ctx.dates[-RECENT_BARS] if len(ctx.dates) >= RECENT_BARS else ctx.dates[0]
    trades = _tag_regime(trades, ctx, ctx.names)
    trades.sort(key=lambda t: (t.entry_date, t.code))
    split_iso, recent_iso = split_date.isoformat(), recent_start.isoformat()
    recent_trades = [t for t in trades if t.entry_date >= recent_iso]

    full = stats(trades, equity)
    # Separate portfolios for each half: the in-sample run closes everything at the
    # split and the out-of-sample run starts flat, so no position straddles the
    # boundary and neither half's equity carries the other's trades.
    in_sample = stats(*simulate(
        ctx.panel, signals, exits, sizing, ctx.benchmark, end=split_date, ebb_days=ebb, entry=entry,
    ))
    if split_idx < len(ctx.dates):
        out_of_sample = stats(*simulate(
            ctx.panel, signals, exits, sizing, ctx.benchmark, start=ctx.dates[split_idx], ebb_days=ebb, entry=entry,
        ))
    else:
        out_of_sample = PerformanceStats()
    recent = _stats_slice(recent_trades, equity, recent_start, None)
    by_regime = _regime_breakdown(trades, ctx)
    verdict, verdict_reason = judge(full, in_sample, out_of_sample, by_regime)
    score, reasons = fitness(full, out_of_sample, recent, by_regime, verdict, trend, sentiment)

    nav = equity["strategy"].to_list()
    bench = equity["benchmark"].to_list()
    dd = equity["drawdown"].to_list()
    days = equity["date"].to_list()
    keep = _downsample(list(range(len(days))), 400)
    curve = [
        EquityPoint(
            date=days[i].isoformat(), strategy=round(nav[i], 3),
            benchmark=round(bench[i], 3) if bench[i] is not None else None, drawdown=round(dd[i], 3),
        )
        for i in keep
    ]
    sparkline = [round(nav[i], 2) for i in _downsample(list(range(len(nav))), 60)]
    return PlaybookEvaluation(
        spec=spec.model_copy(update={"exits": exits, "sizing": sizing}),
        full=full, out_of_sample=out_of_sample, in_sample=in_sample, recent=recent,
        by_regime=by_regime, fitness=score, verdict=verdict, verdict_reason=verdict_reason + failure,
        signals_today=today, equity_sparkline=sparkline, provenance=provenance,
        period_start=period_start, period_end=period_end, split_date=split_iso,
        equity=curve, trades=trades[-80:], fitness_reasons=reasons, assumptions=list(ASSUMPTIONS),
        execution_ms=round((time.perf_counter() - started) * 1000, 2),
    )


# --- caching: memory + warehouse docs ------------------------------------------------

_MEMORY: dict[tuple, PlaybookEvaluation] = {}
_COMPUTE_LOCK = threading.RLock()


def _regime_keys(wh: Warehouse) -> tuple[str | None, str | None]:
    try:
        state = current_regime_state(wh)
        return state.trend, state.sentiment
    except Exception:
        # Fitness then scores without the current-regime component (stated in its reasons).
        LOG.exception("current regime unavailable; fitness ignores the regime component")
        return None, None


def evaluate(playbook_id: str, request: PlaybookRunRequest | None = None) -> PlaybookEvaluation:
    """Evaluate one playbook. Default runs are cached; custom runs never are."""
    pb = resolve(playbook_id)
    if pb is None:
        raise KeyError(playbook_id)
    ctx = context()
    custom = request is not None and (request.params or request.exits or request.sizing)
    exits = (request.exits if request and request.exits else None) or pb.spec.exits
    sizing = (request.sizing if request and request.sizing else None) or pb.spec.sizing
    params = request.params if request else None
    trend, sentiment = _regime_keys(ctx.wh)
    if custom:
        return _compute(pb, ctx, params, exits, sizing, trend, sentiment)

    values = pb.params()
    digest = _params_hash(values, exits, sizing)
    key = (ctx.version, playbook_id, digest, trend, sentiment)
    with _COMPUTE_LOCK:
        cached = _MEMORY.get(key)
        if cached is not None:
            return cached
        name = f"{playbook_id}-{digest}"
        doc = ctx.wh.read_doc("evaluations", name)
        if doc and doc.get("version") == ctx.version and doc.get("regime") == [trend, sentiment]:
            try:
                result = PlaybookEvaluation.model_validate(doc["evaluation"])
                _MEMORY[key] = result
                return result
            except Exception:
                pass  # schema moved on; recompute
        result = _compute(pb, ctx, None, exits, sizing, trend, sentiment)
        for old in [k for k in _MEMORY if k[0] != ctx.version]:
            _MEMORY.pop(old, None)
        _MEMORY[key] = result
        ctx.wh.write_doc("evaluations", name, {
            "version": ctx.version, "regime": [trend, sentiment],
            "evaluation": result.model_dump(mode="json"),
        })
        return result


def summary(evaluation: PlaybookEvaluation) -> PlaybookSummary:
    return PlaybookSummary(**{name: getattr(evaluation, name) for name in PlaybookSummary.model_fields})


_BOARD: tuple[str, PlaybookBoard] | None = None


def board(force: bool = False) -> PlaybookBoard:
    global _BOARD
    started = time.perf_counter()
    ctx = context()
    trend, sentiment = _regime_keys(ctx.wh)
    key = f"{ctx.version}:{trend}:{sentiment}"
    with _COMPUTE_LOCK:
        if not force and _BOARD is not None and _BOARD[0] == key:
            return _BOARD[1]
        if force:
            _MEMORY.clear()
        items = []
        for pb in catalog():
            if force:
                items.append(_force(pb, ctx, trend, sentiment))
            else:
                items.append(evaluate(pb.spec.id))
        result = PlaybookBoard(
            provenance=dataset_provenance(ctx),
            regime=current_regime_state(ctx.wh),
            playbooks=[summary(item) for item in items],
            computed_at=datetime.now(CST).isoformat(timespec="seconds"),
            execution_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        _BOARD = (key, result)
        return result


def _force(pb: Playbook, ctx: Context, trend: str | None, sentiment: str | None) -> PlaybookEvaluation:
    """Recompute and overwrite the cache entry for one default playbook."""
    exits, sizing = pb.spec.exits, pb.spec.sizing
    result = _compute(pb, ctx, None, exits, sizing, trend, sentiment)
    digest = _params_hash(pb.params(), exits, sizing)
    _MEMORY[(ctx.version, pb.spec.id, digest, trend, sentiment)] = result
    ctx.wh.write_doc("evaluations", f"{pb.spec.id}-{digest}", {
        "version": ctx.version, "regime": [trend, sentiment], "evaluation": result.model_dump(mode="json"),
    })
    return result


def prewarm() -> None:
    """Fill the caches; meant for a background thread after startup or a sync."""
    try:
        board()
    except Exception:  # prewarm is best-effort; a request will surface the real error
        LOG.exception("playbook board prewarm failed")


def reset_caches() -> None:
    """Drop in-memory state (tests and data-dir switches)."""
    global _CTX, _BOARD
    with _CTX_LOCK:
        _CTX = None
    _BOARD = None
    _MEMORY.clear()
