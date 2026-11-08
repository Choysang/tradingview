"""Walk-forward rotation: which playbooks work *now*, and does following them pay.

No playbook works in every market. The rotation re-elects playbooks every STEP
trading days from their completed trades in a trailing window, follows the top K
for the next STEP days, and stays in cash when none has a positive record. Each
election uses only trades that exited before the decision day, so the whole walk
is point-in-time. The test of the idea is the next period's trades of the elected
playbooks against all playbooks traded all the time.

The configuration (window, top K, method) is chosen in time, not in hindsight:
only periods that end by the split date (the first SPLIT_FRACTION of the dates)
pick it, by the Newey-West t of their excess, and only periods that start after
the split test it. A split by stocks would not be independent: every stock lives
through the same market, so the same months drive both halves. The live
election follows the same STEP anchors as the walk, so what is followed today is
what was tested. Trades force-closed at the end of the data are not completed
trades and never enter the book.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math
import threading
import time

import polars as pl

from contracts.schema import RotationConfig, RotationPeriod, RotationReport, RotationScore, RotationStats
from .backtest import simulate
from .base import Playbook, catalog
from .evaluate import (
    REGIME_LABELS, SPLIT_FRACTION, Context, _requires_history_missing, context, dataset_provenance, ebb_days,
    run_signals,
)

STEP = 20  # re-elect about monthly
WINDOWS = (20, 60, 120)
TOP_KS = (1, 3, 5)
DEFAULT = ("trailing", 20, 3)  # used only when no configuration has an in-sample t
REGIME_LOOKBACK = 250
MIN_TRADES = 5
SHRINK = 5.0  # pseudo-trades at zero return: a few lucky trades do not top the table
VALID_T = 1.5
VALID_HIT = 0.55
MIN_OOS_PERIODS = 6
NW_LAG = 1  # holdings of one period run into the next

_LOCK = threading.RLock()
_BOOK: tuple[tuple, dict[str, pl.DataFrame]] | None = None
_REPORT: tuple[tuple, RotationReport] | None = None


@dataclass(frozen=True)
class Period:
    start: date
    end: date
    chosen: tuple[str, ...]
    selected: float | None  # mean of the chosen playbooks' mean trade return; 0.0 when in cash
    baseline: float | None  # same over every playbook that traded


# --- trades ------------------------------------------------------------------------

def trade_book(ctx: Context, playbooks: list[Playbook] | None = None) -> dict[str, pl.DataFrame]:
    """Full-period trades of every backtestable playbook: frame(code, entry, exit, ret, trend, sentiment)."""
    global _BOOK
    playbooks = playbooks if playbooks is not None else catalog()
    key = (ctx.version, tuple(pb.spec.id for pb in playbooks))
    with _LOCK:
        if _BOOK is not None and _BOOK[0] == key:
            return _BOOK[1]
        book: dict[str, pl.DataFrame] = {}
        for pb in playbooks:
            if not pb.spec.available or (
                pb.requires_history and _requires_history_missing(ctx.panel, pb.requires_history)
            ):
                continue
            signals = run_signals(pb, ctx)
            trades, _ = simulate(
                ctx.panel, signals, pb.spec.exits, pb.spec.sizing, ctx.benchmark, ebb_days=ebb_days(ctx),
                entry=pb.spec.entry,
            )
            trades = [t for t in trades if t.exit_reason != "end"]
            book[pb.spec.id] = _frame(
                [t.code for t in trades], [t.entry_date for t in trades], [t.exit_date for t in trades],
                [t.return_pct for t in trades], ctx,
            )
        _BOOK = (key, book)
        return book


def _frame(codes: list[str], entries: list[str], exits: list[str], returns: list[float], ctx: Context) -> pl.DataFrame:
    regimes = [ctx.regimes.get(day) or {} for day in entries]
    return pl.DataFrame(
        {
            "code": codes, "entry": entries, "exit": exits, "ret": returns,
            "trend": [r.get("trend") for r in regimes], "sentiment": [r.get("sentiment") for r in regimes],
        },
        schema={
            "code": pl.Utf8, "entry": pl.Utf8, "exit": pl.Utf8, "ret": pl.Float64,
            "trend": pl.Utf8, "sentiment": pl.Utf8,
        },
    ).with_columns(pl.col("entry").str.to_date(), pl.col("exit").str.to_date())


# --- scoring and the walk ---------------------------------------------------------------

def scores(
    book: dict[str, pl.DataFrame], start: date, decide: date, regime: dict[str, str] | None = None
) -> dict[str, tuple[float, int, float]]:
    """playbook -> (shrunk mean, trades, raw mean) over trades that entered on or after
    `start` and exited on or before `decide`; with `regime`, only trades that entered
    in the same trend and sentiment state."""
    out: dict[str, tuple[float, int, float]] = {}
    for pid, frame in book.items():
        sub = frame.filter((pl.col("entry") >= start) & (pl.col("exit") <= decide))
        if regime is not None:
            sub = sub.filter((pl.col("trend") == regime.get("trend")) & (pl.col("sentiment") == regime.get("sentiment")))
        n = sub.height
        if n < MIN_TRADES:
            continue
        total = float(sub["ret"].sum())
        out[pid] = (total / (n + SHRINK), n, total / n)
    return out


def elect(table: dict[str, tuple[float, int, float]], top_k: int) -> tuple[str, ...]:
    ranked = sorted(((s, pid) for pid, (s, _, _) in table.items() if s > 0), key=lambda item: (-item[0], item[1]))
    return tuple(pid for _, pid in ranked[:top_k])


def _period_mean(book: dict[str, pl.DataFrame], ids: tuple[str, ...] | list[str], start: date, end: date) -> float | None:
    means = []
    for pid in ids:
        sub = book[pid].filter(pl.col("entry").is_between(start, end))
        if sub.height:
            means.append(float(sub["ret"].mean()))
    return sum(means) / len(means) if means else None


def _warm(method: str, window: int) -> int:
    return REGIME_LOOKBACK if method == "regime" else window


def election(
    book: dict[str, pl.DataFrame], dates: list[date], regimes: dict[str, dict[str, str]],
    method: str, window: int, i: int,
) -> dict[str, tuple[float, int, float]] | None:
    """The score table decided on the close of dates[i - 1]; None when the regime is unknown."""
    decide = dates[i - 1]
    state = regimes.get(decide.isoformat()) if method == "regime" else None
    if method == "regime" and not state:
        return None
    return scores(book, dates[i - _warm(method, window)], decide, state)


def walk(
    book: dict[str, pl.DataFrame], dates: list[date], regimes: dict[str, dict[str, str]],
    method: str, window: int, top_k: int,
) -> list[Period]:
    periods: list[Period] = []
    for i in range(_warm(method, window), len(dates) - 1, STEP):
        table = election(book, dates, regimes, method, window, i)
        if table is None:
            continue
        chosen = elect(table, top_k)
        start, end = dates[i], dates[min(i + STEP, len(dates)) - 1]
        baseline = _period_mean(book, list(book), start, end)
        selected = _period_mean(book, chosen, start, end) if chosen else 0.0  # nothing works: cash
        if selected is None:  # elected playbooks did not trade this period: cash
            selected = 0.0
        periods.append(Period(start, end, chosen, selected, baseline))
    return periods


def newey_west_t(diffs: list[float], lag: int = NW_LAG) -> float | None:
    """t of the mean with Bartlett-weighted autocovariances up to `lag`."""
    n = len(diffs)
    if n < 3:
        return None
    mean = sum(diffs) / n
    dev = [d - mean for d in diffs]
    var = sum(x * x for x in dev) / n
    for k in range(1, min(lag, n - 1) + 1):
        var += 2 * (1 - k / (lag + 1)) * sum(dev[i] * dev[i - k] for i in range(k, n)) / n
    return mean / math.sqrt(var / n) if var > 0 else None


def period_stats(periods: list[Period]) -> RotationStats:
    paired = [(p, p.selected, p.baseline) for p in periods if p.selected is not None and p.baseline is not None]
    diffs = [s - b for _, s, b in paired]
    invested = [s - b for p, s, b in paired if p.chosen]
    n = len(diffs)
    return RotationStats(
        periods=n,
        selected_avg=_r(sum(s for _, s, _ in paired) / n) if n else None,
        baseline_avg=_r(sum(b for _, _, b in paired) / n) if n else None,
        excess_avg=_r(sum(diffs) / n) if n else None,
        hit_rate=_r(sum(d > 0 for d in diffs) / n * 100, 1) if n else None,
        t_stat=_r(newey_west_t(diffs), 2), cash_periods=sum(not p.chosen for p, _, _ in paired),
        invested_periods=len(invested), invested_excess_avg=_r(sum(invested) / len(invested)) if invested else None,
    )


def summarise(periods: list[Period], method: str, window: int, top_k: int, split: date) -> RotationConfig:
    label = f"同周期 · 近 {REGIME_LOOKBACK} 日 · 前 {top_k}" if method == "regime" else f"近 {window} 日 · 前 {top_k}"
    return RotationConfig(
        key=f"{method}-{window}-{top_k}", label=label, method=method, window=window, top_k=top_k,  # type: ignore[arg-type]
        in_sample=period_stats([p for p in periods if p.end <= split]),
        out_of_sample=period_stats([p for p in periods if p.start > split]),
    )


def choose(configs: list[RotationConfig]) -> RotationConfig:
    """The configuration with the best in-sample t; the fixed default when none has one."""
    rated = [c for c in configs if c.in_sample.t_stat is not None]
    if not rated:
        key = "-".join(str(part) for part in DEFAULT)
        return next(c for c in configs if c.key == key)
    return max(rated, key=lambda c: (c.in_sample.t_stat, c.key))


def _r(value: float | None, digits: int = 3) -> float | None:
    return None if value is None or not math.isfinite(value) else round(value, digits)


def _num(value: float | None, spec: str) -> str:
    return "—" if value is None else format(value, spec)


def validated(stats: RotationStats) -> bool:
    return (
        stats.periods >= MIN_OOS_PERIODS and stats.excess_avg is not None and stats.excess_avg > 0
        and (stats.t_stat or 0) >= VALID_T and (stats.hit_rate or 0) >= VALID_HIT * 100
    )


# --- the report ---------------------------------------------------------------------------

def current_anchor(n_dates: int, method: str, window: int) -> int | None:
    """Index i of the latest election on the walk's STEP grid (decided on the close of dates[i - 1])."""
    warm = _warm(method, window)
    if n_dates < warm:
        return None
    return warm + (n_dates - warm) // STEP * STEP


def current_scores(
    ctx: Context, book: dict[str, pl.DataFrame], playbooks: list[Playbook], config: RotationConfig
) -> tuple[list[RotationScore], int | None]:
    """The election in force today, on the same anchors as the walk, with every playbook's standing."""
    i = current_anchor(len(ctx.dates), config.method, config.window)
    table = election(book, ctx.dates, ctx.regimes, config.method, config.window, i) if i is not None else None
    if table is None:
        return [], i
    chosen = set(elect(table, config.top_k))
    specs = {pb.spec.id: pb.spec for pb in playbooks}
    rows = []
    for pid in book:
        score, n, raw = table.get(pid, (None, 0, None))
        spec = specs[pid]
        rows.append(RotationScore(
            playbook_id=pid, name=spec.name, horizon=spec.horizon, trades=n,
            avg_return=_r(raw), score=_r(score), selected=pid in chosen, window=_warm(config.method, config.window),
        ))
    rows.sort(key=lambda r: (not r.selected, -(r.score if r.score is not None else -1e9), r.playbook_id))
    return rows, i


def report(force: bool = False, ctx: Context | None = None, playbooks: list[Playbook] | None = None) -> RotationReport:
    global _REPORT
    started = time.perf_counter()
    ctx = ctx or context()
    playbooks = playbooks if playbooks is not None else catalog()
    key = (ctx.version, tuple(pb.spec.id for pb in playbooks))
    with _LOCK:
        if not force and _REPORT is not None and _REPORT[0] == key:
            return _REPORT[1]
        book = trade_book(ctx, playbooks)
        split = ctx.dates[max(1, int(len(ctx.dates) * SPLIT_FRACTION)) - 1] if ctx.dates else date.min
        configs, walks = [], {}
        grid = [("trailing", w, k) for w in WINDOWS for k in TOP_KS] + [("regime", REGIME_LOOKBACK, k) for k in TOP_KS]
        for method, window, top_k in grid:
            periods = walk(book, ctx.dates, ctx.regimes, method, window, top_k)
            config = summarise(periods, method, window, top_k, split)
            configs.append(config)
            walks[config.key] = periods
        chosen = choose(configs)
        oos = chosen.out_of_sample
        ok = validated(oos)
        hit, excess, t_stat = _num(oos.hit_rate, "g"), _num(oos.excess_avg, "+.2f"), _num(oos.t_stat, "g")
        verdict = (
            f"轮换有效：样本内选出「{chosen.label}」，每 {STEP} 个交易日重选；"
            f"样本外 {oos.periods} 期里有 {hit}% 跑赢全部战法同时做，平均每笔多 {excess}%（t = {t_stat}）。"
            if ok else
            f"轮换未通过检验：样本内选出「{chosen.label}」，样本外"
            + (f" {oos.periods} 期跑赢 {hit}%，平均每笔超额 {excess}%，t = {t_stat}" if oos.periods else "期数为 0")
            + f"；门槛是至少 {MIN_OOS_PERIODS} 期、超额为正、t ≥ {VALID_T:g}、跑赢期数 ≥ {VALID_HIT * 100:g}%。"
        )
        names = {pb.spec.id: pb.spec.name for pb in playbooks}
        current, anchor = current_scores(ctx, book, playbooks, chosen)
        decided_on = ctx.dates[anchor - 1].isoformat() if anchor is not None else None
        next_in = anchor + STEP - len(ctx.dates) if anchor is not None else None
        result = RotationReport(
            provenance=dataset_provenance(ctx), step=STEP, default_key=chosen.key, split_date=split.isoformat(),
            decided_on=decided_on, next_in_days=next_in, configs=configs,
            periods=[
                RotationPeriod(
                    start=p.start.isoformat(), end=p.end.isoformat(), chosen=[names.get(pid, pid) for pid in p.chosen],
                    selected_return=_r(p.selected), baseline_return=_r(p.baseline),
                )
                for p in walks[chosen.key]
            ],
            current=current, validated=ok, verdict=verdict,
            notes=[
                f"每 {STEP} 个交易日推选一次，只用推选日之前已经平仓的交易；没有战法近期为正时空仓（该期收益记 0）。",
                f"排序分 = 窗口内每笔收益之和 ÷（笔数 + {SHRINK:g}），少于 {MIN_TRADES} 笔不参与，防止几笔运气好的交易排到前面。",
                "比较对象是同期全部战法一起做（各战法每笔平均收益的等权平均），收益是每笔扣成本后的平均值，不是组合净值。",
                f"按时间切分：{split.isoformat()} 及以前结束的期只用来选设置（选样本内 t 值最高的），之后开始的期只用来检验；"
                "按股票切分不独立，因为所有股票经历同一段行情。",
                f"t 值按 Newey-West（滞后 {NW_LAG} 期）计算，因为一期的持仓会延续到下一期；“有持仓”一栏去掉了空仓期，"
                "单看推选战法本身是否更好。",
                "当前推选与逐期检验用同一组推选日；持有期长于窗口的战法（例如近 20 日窗口里持有 20 日的长线战法）"
                "窗口内没有平仓交易，不会被推选。",
                "数据末日被强制平仓的交易不算已完成的交易，不参与推选和检验。",
                "“同周期”方法只看过去一年里与推选日趋势、情绪相同时的交易。",
            ],
            execution_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        _REPORT = (key, result)
        return result


def reset_caches() -> None:
    global _BOOK, _REPORT
    with _LOCK:
        _BOOK = None
        _REPORT = None


REGIME_NAMES = REGIME_LABELS  # re-exported for the API layer
