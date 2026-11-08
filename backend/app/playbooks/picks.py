"""Daily picks: an ensemble of the playbooks that fit, plus an honest track record.

Pipeline for one trade date
    board verdicts -> per-horizon playbook weights -> each playbook's signals on the
    latest bar, percentile-ranked inside the playbook -> per stock weighted mean
    rank (the ensemble) -> blend with technical / sentiment / trend sub-scores ->
    exclusions, risk flags, exits -> top 10 per horizon and top 20 overall.

Track record has two kinds and never mixes them
    recorded   every stored report, forward returns once the holding window has passed
    simulated  out-of-sample replay: weights are fixed from IN-SAMPLE trades only,
               then the ensemble is rebuilt on every 5th OOS date using data up to
               that date and scored on what happened next
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
import threading

import numpy as np
import polars as pl

from contracts.schema import (
    DailyReport,
    EntryRules,
    ExitRules,
    PerformanceStats,
    PickReason,
    PlaybookSummary,
    PlaybookWeight,
    ReportIndex,
    ReportIndexItem,
    RotationScore,
    StockPick,
    TrackRecordPoint,
    TrackRecordResponse,
    TrackRecordSummary,
)
from . import exits as exit_lab, rotation
from .backtest import DEFAULT_COSTS, price_grid, simulate
from .base import Playbook, catalog

LOG = logging.getLogger("pulse.picks")
from .evaluate import (
    CST,
    SPLIT_FRACTION,
    Context,
    _requires_history_missing,
    board,
    context,
    current_regime_state,
    dataset_provenance,
    ebb_days,
    run_signals,
    stats,
)

HORIZONS = ("short", "swing", "long")
HORIZON_NAMES = {"short": "短线", "swing": "波段", "long": "长线"}
VERDICT_NAMES = {
    "effective": "有效", "regime_dependent": "依赖周期", "weak": "偏弱", "overfit": "过拟合",
    "insufficient": "样本不足", "unavailable": "无法回测",
}
HOLD = {"short": 3, "swing": 10, "long": 40}
TOP_PER_HORIZON = 10
TOP_OVERALL = 20
MIN_FITNESS = 40.0
REGIME_DEPENDENT_FACTOR = 0.7
UNTESTED_WEIGHT = 0.25
REPLAY_STEP = 5
MIN_AMOUNT = 0.5  # 亿元
MIN_BARS = 60
REBALANCE_CYCLE = 25  # bars; a calendar month has at most ~23 trading days
# Bump when the report gains content (2: rotation voting and sell plans; 3: bear mode), so
# stored reports built by older code are rebuilt instead of served without it.
REPORT_FORMAT = 3
# Bear mode (docs/research/2026-11-01-technical-bear.md): while the benchmark closes below its
# 60-day mean, technical bottom-fishing and chasing lost money in down months, and only
# low-volatility playbooks held up. Short and swing pause; long keeps the defensive styles.
BEAR_MA = 60
BEAR_HORIZONS = ("short", "swing")
DEFENSIVE_STYLES = frozenset({"volatility"})
PHASE_SCORE = {"freezing": 0.2, "repair": 0.6, "climax": 1.0, "ebb": 0.4}
BLEND = {  # ensemble, sentiment, technical, trend
    "short": (0.50, 0.35, 0.15, 0.0),
    "swing": (0.50, 0.15, 0.35, 0.0),
    "long": (0.50, 0.0, 0.15, 0.35),
}
DISCLAIMER = "研究用途，不构成投资建议"
DAY_COLUMNS = (
    "name", "close", "pct_chg", "amount", "days_listed", "limit_streak", "volume", "vol_ma20", "ma5", "ma10",
    "ma20", "ma60", "ma120", "macd_dif", "macd_dea", "macd_hist", "high_60_prev", "ret_20", "ret_60", "ret_120",
    "volatility_20", "atr_14", "one_word",
)


# --- weights ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Weight:
    playbook_id: str
    horizon: str
    weight: float
    untested: bool = False


def weights_from_board(summaries: list[PlaybookSummary]) -> list[Weight]:
    """Who votes, and how loudly: fit playbooks by fitness, untestable value playbooks at a fixed low weight."""
    out: list[Weight] = []
    for s in summaries:
        if s.verdict in ("effective", "regime_dependent") and (s.fitness or 0) >= MIN_FITNESS:
            factor = REGIME_DEPENDENT_FACTOR if s.verdict == "regime_dependent" else 1.0
            out.append(Weight(s.spec.id, s.spec.horizon, round((s.fitness or 0) / 100 * factor, 4)))
        elif s.verdict == "unavailable" and s.spec.style == "value":
            out.append(Weight(s.spec.id, "long", UNTESTED_WEIGHT, untested=True))
    return out


def weights_from_rotation(current: list[RotationScore], summaries: list[PlaybookSummary]) -> list[Weight]:
    """Elected playbooks vote by their trailing score relative to the best one; value keeps its fixed low weight."""
    chosen = [r for r in current if r.selected and (r.score or 0) > 0]
    top = max((r.score or 0.0 for r in chosen), default=0.0)
    out = [Weight(r.playbook_id, r.horizon, round((r.score or 0.0) / top, 4)) for r in chosen]
    out += [
        Weight(s.spec.id, "long", UNTESTED_WEIGHT, untested=True)
        for s in summaries if s.verdict == "unavailable" and s.spec.style == "value"
    ]
    return out


# --- the ensemble on one day -------------------------------------------------------------

def _pct_rank(name: str) -> pl.Expr:
    return (pl.col(name).rank("average") / pl.col(name).count()).fill_null(0.0)


def _day_scores(day: pl.DataFrame, phase: str | None) -> pl.DataFrame:
    """technical / sentiment / trend sub-scores (0-100) for every stock on the day."""
    missing = [c for c in DAY_COLUMNS if c not in day.columns]
    day = day.with_columns(*(pl.lit(None).alias(c) for c in missing))
    num = lambda n: pl.col(n).cast(pl.Float64)  # noqa: E731
    ma_alignment = (
        (num("ma5") > num("ma10")).fill_null(False).cast(pl.Float64)
        + (num("ma10") > num("ma20")).fill_null(False).cast(pl.Float64)
        + (num("ma20") > num("ma60")).fill_null(False).cast(pl.Float64)
    ) / 3
    macd = (
        pl.when((num("macd_dif") > num("macd_dea")) & (num("macd_hist") > 0)).then(1.0)
        .when(num("macd_dif") > num("macd_dea")).then(0.6)
        .when(num("macd_dif") > 0).then(0.3)
        .otherwise(0.0)
    )
    position = ((num("close") / num("high_60_prev") - 0.8) / 0.2).clip(0, 1).fill_null(0.0)
    streak = (num("limit_streak").fill_null(0).clip(0, 5) / 5)
    surge = ((num("volume") / num("vol_ma20") - 1) / 2).clip(0, 1).fill_null(0.0)
    above_long = (num("close") > num("ma120")).fill_null(False).cast(pl.Float64)
    phase_score = PHASE_SCORE.get(phase or "", 0.5)
    return day.with_columns(
        ((ma_alignment + macd.fill_null(0.0) + position + _pct_rank("ret_20")) / 4 * 100).alias("technical"),
        ((streak + surge + phase_score) / 3 * 100).alias("sentiment"),
        ((_pct_rank("ret_60") + _pct_rank("ret_120") + above_long) / 3 * 100).alias("trend"),
    )


def _eligible(day: pl.DataFrame, exclude_st: bool) -> pl.DataFrame:
    # Names are today's: excluding ST by name is right for today's report and
    # look-ahead for a past date, so the replay turns it off.
    st = pl.col("name").fill_null("").str.to_uppercase().str.contains("ST") if exclude_st else pl.lit(False)
    return day.filter(
        ~st
        & (pl.col("days_listed").fill_null(0) >= MIN_BARS)
        & (pl.col("amount").fill_null(0) >= MIN_AMOUNT)
    )


def ensemble_day(
    day: pl.DataFrame,
    signals: dict[str, pl.DataFrame],
    weights: list[Weight],
    phase: str | None,
    exclude_st: bool = True,
) -> tuple[dict[str, pl.DataFrame], dict[str, pl.DataFrame]]:
    """Rank candidates for each horizon from one day's panel rows and that day's signals.

    `signals` maps playbook id -> frame(code, score) for the day. Returns
    (per-horizon ranked candidates, per-horizon contributions long frame).
    Uses nothing but the rows it is given, which is what makes the replay point-in-time.
    """
    scored = _eligible(_day_scores(day, phase), exclude_st)
    ranked: dict[str, pl.DataFrame] = {}
    contributions: dict[str, pl.DataFrame] = {}
    for horizon in HORIZONS:
        parts = []
        voters: list[Weight] = []
        for w in (w for w in weights if w.horizon == horizon and w.weight > 0):
            frame = signals.get(w.playbook_id)
            if frame is None or frame.is_empty():
                continue
            voters.append(w)
            frame = frame.select("code", "score").unique("code", keep="first")
            parts.append(frame.with_columns(
                pl.lit(w.playbook_id).alias("playbook_id"),
                pl.lit(w.weight).alias("weight"),
                (pl.col("score").rank("average") / pl.len()).alias("rank_pct"),
            ))
        # An untested playbook only adjusts the ranking of names tested ones put
        # forward; alone it never produces a list.
        if all(w.untested for w in voters):
            ranked[horizon] = pl.DataFrame()
            contributions[horizon] = pl.DataFrame()
            continue
        long = pl.concat(parts)
        contributions[horizon] = long
        # Normalised by every voter's weight, so a playbook that did not pick a
        # name counts as zero for it: weights decide who gets the slots, and a
        # name picked by a weak playbook alone cannot match one the fittest picked.
        total = sum(w.weight for w in voters)
        tested = [w.playbook_id for w in voters if not w.untested]
        agg = long.group_by("code").agg(
            ((pl.col("weight") * pl.col("rank_pct")).sum() / total).alias("ensemble"),
            pl.len().alias("agreement"),
            pl.col("playbook_id").sort_by(pl.col("weight") * pl.col("rank_pct"), descending=True).first().alias("lead"),
            pl.col("playbook_id").is_in(tested).any().alias("_tested"),
        ).filter(pl.col("_tested")).drop("_tested")
        e, s, t, r = BLEND[horizon]
        joined = scored.join(agg, on="code", how="inner").with_columns(
            (
                (e * pl.col("ensemble") + (s * pl.col("sentiment") + t * pl.col("technical") + r * pl.col("trend")) / 100)
                * 100
            ).clip(0, 100).alias("score")
        )
        ranked[horizon] = joined.sort(["score", "code"], descending=[True, False])
    return ranked, contributions


def overall_from(per_horizon: dict[str, pl.DataFrame]) -> pl.DataFrame:
    """Top 20 across horizons, one row per code at its best horizon."""
    parts = [
        frame.head(TOP_PER_HORIZON).with_columns(pl.lit(h).alias("horizon"))
        for h, frame in per_horizon.items() if not frame.is_empty()
    ]
    if not parts:
        return pl.DataFrame()
    stacked = pl.concat(parts, how="diagonal_relaxed").sort(["score", "code"], descending=[True, False])
    return stacked.unique("code", keep="first", maintain_order=True).head(TOP_OVERALL)


# --- the daily report ---------------------------------------------------------------------

def _empty_signals() -> pl.DataFrame:
    return pl.DataFrame(schema={"code": pl.Utf8, "score": pl.Float64})


def _window(pb: Playbook) -> int:
    """How many bars a signal stays actionable. Long factor playbooks rebalance
    monthly or weekly, so until the next rebalance their latest list is still the
    holding; short and swing signals are only actionable on their own day."""
    return max(pb.spec.exits.holding_days, REBALANCE_CYCLE) if pb.spec.horizon == "long" else 1


def _as_of(frame: pl.DataFrame, dates: list, index: int, window: int) -> pl.DataFrame:
    """The signal list in force on dates[index]: the latest signal day within the
    window, never later than dates[index] (point-in-time)."""
    low = dates[max(0, index - window + 1)]
    recent = frame.filter((pl.col("date") >= low) & (pl.col("date") <= dates[index]))
    if recent.is_empty():
        return _empty_signals()
    last = recent["date"].max()
    return recent.filter(pl.col("date") == last).select("code", "score")


def _signals_on(
    playbooks: dict[str, Playbook], ids: set[str], ctx: Context, index: int
) -> tuple[dict[str, pl.DataFrame], list[str]]:
    """Each playbook's list in force on dates[index], plus the ones that failed.
    One broken signal must not sink the report, but the report must say so."""
    out: dict[str, pl.DataFrame] = {}
    failed: list[str] = []
    for pid in ids:
        try:
            frame = run_signals(playbooks[pid], ctx)
        except Exception as exc:
            LOG.exception("signal %s failed", pid)
            failed.append(f"{playbooks[pid].spec.name}（{type(exc).__name__}）")
            out[pid] = _empty_signals()
            continue
        out[pid] = _as_of(frame, ctx.dates, index, _window(playbooks[pid]))
    return out, failed


def _flags(row: dict, vol_cut: float | None, recent_down: set[str]) -> list[str]:
    flags = []
    if row.get("one_word"):
        flags.append("今日一字板，明日开盘大概率买不到")
    close, ma20 = row.get("close"), row.get("ma20")
    if close and ma20 and close > ma20 * 1.15:
        flags.append(f"偏离 20 日线 {(close / ma20 - 1) * 100:.0f}%，短线过热")
    if vol_cut is not None and (row.get("volatility_20") or 0) >= vol_cut > 0:
        flags.append("波动率处于全市场前 10%")
    if row["code"] in recent_down:
        flags.append("近 10 日出现过跌停")
    if (row.get("amount") or 0) < 1:
        flags.append("日成交额不足 1 亿，流动性偏低")
    return flags


def _stop_loss(close: float | None, atr: float | None, stop_pct: float | None) -> float | None:
    if close is None:
        return None
    candidates = []
    if stop_pct:
        candidates.append(close * (1 - stop_pct / 100))
    if atr and atr > 0:
        candidates.append(close - 2 * atr)
    return round(max(candidates), 2) if candidates else None


def _build_pick(
    rank: int, row: dict, horizon: str, contrib: pl.DataFrame, summaries: dict[str, PlaybookSummary],
    untested: set[str], themes: dict[str, str], vol_cut: float | None, recent_down: set[str],
    exits_by_id: dict[str, ExitRules] | None = None, elected: dict[str, RotationScore] | None = None,
    entry_by_id: dict[str, EntryRules] | None = None,
) -> StockPick:
    rows = contrib.filter(pl.col("code") == row["code"]).sort(pl.col("weight") * pl.col("rank_pct"), descending=True)
    reasons: list[PickReason] = []
    for r in rows.iter_rows(named=True):
        summary = summaries[r["playbook_id"]]
        basis = summary.out_of_sample if summary.out_of_sample.trades >= 10 else summary.full
        record = (elected or {}).get(r["playbook_id"])
        if r["playbook_id"] in untested:
            note = "未经历史回测：仓库无逐日基本面，仅按当前估值排名，固定低权重"
        elif record is not None and record.avg_return is not None:
            note = (
                f"信号强度分位 {round(r['rank_pct'] * 100)}%（原始分 {float(r['score']):.2f}）；"
                f"轮换推选：近 {record.window} 日 {record.trades} 笔平均 {record.avg_return:+.2f}%"
            )
        else:
            note = f"信号强度分位 {round(r['rank_pct'] * 100)}%（原始分 {float(r['score']):.2f}）；{summary.verdict_reason}"
        reasons.append(PickReason(
            playbook_id=r["playbook_id"], playbook_name=summary.spec.name, weight=round(r["weight"], 3),
            # The percentile within that playbook's signals that day, the number the ensemble averages.
            signal=round(float(r["rank_pct"]), 4), win_rate=basis.win_rate, avg_return=basis.avg_return, note=note,
        ))
    lead = summaries[row["lead"]]
    exits = (exits_by_id or {}).get(lead.spec.id, lead.spec.exits)
    entry = (entry_by_id or {}).get(lead.spec.id, lead.spec.entry)
    close, atr = row.get("close"), row.get("atr_14")
    take = round(close * (1 + exits.take_profit_pct / 100), 2) if close and exits.take_profit_pct else None
    code = row["code"]
    theme = themes.get(code)
    return StockPick(
        rank=rank, code=code, name=row.get("name") or code, horizon=horizon,  # type: ignore[arg-type]
        score=round(float(row["score"]), 2),
        technical=round(float(row["technical"]), 1), sentiment=round(float(row["sentiment"]), 1),
        trend=round(float(row["trend"]), 1), agreement=int(row["agreement"]),
        close=close, pct_chg=row.get("pct_chg"),
        stop_loss=_stop_loss(close, atr, exits.stop_loss_pct), take_profit=take,
        holding_days=exits.holding_days, entry_note=exit_lab.entry_text(entry, close), reasons=reasons,
        risk_flags=_flags(row, vol_cut, recent_down), themes=[theme] if theme else [],
        exit_plan=exit_lab.plan_text(exits),
    )


def _themes(ctx: Context) -> dict[str, str]:
    lazy = ctx.wh.scan("securities")
    if lazy is None or "theme" not in lazy.collect_schema().names():
        return {}
    frame = lazy.select("code", "theme").drop_nulls().collect()
    return dict(frame.iter_rows())


def market_mode(ctx: Context, day) -> tuple[str, str | None]:
    """("bear", detail) when the benchmark's latest close on or before `day` is below the mean of its
    last BEAR_MA closes, else ("normal", None). The detail names the close's date, so an index that
    lags the stock bars by a day shows it."""
    if ctx.benchmark is None:
        return "normal", None
    rows = ctx.benchmark.filter((pl.col("date") <= day) & pl.col("close").is_not_null()).tail(BEAR_MA)
    if rows.height < BEAR_MA:
        return "normal", None
    close, mean = float(rows["close"][-1]), float(rows["close"].mean())
    if close >= mean:
        return "normal", None
    return "bear", f"中证全指 {rows['date'][-1].isoformat()} 收盘 {close:.2f}，低于 {BEAR_MA} 日均线 {mean:.2f}"


def apply_market_mode(weights: list[Weight], playbooks: dict[str, Playbook], mode: str) -> list[Weight]:
    """In bear mode only long playbooks of a defensive style (and the untested value playbook) vote."""
    if mode != "bear":
        return weights
    return [
        w for w in weights
        if w.horizon == "long" and (w.untested or (w.playbook_id in playbooks and playbooks[w.playbook_id].spec.style in DEFENSIVE_STYLES))
    ]


def bear_notes(detail: str, weights_all: list[Weight], weights: list[Weight], idle: list[str]) -> list[str]:
    """Replace the idle notes of the horizons bear mode pauses, and say why once."""
    dropped = {w.horizon for w in weights_all if not w.untested} - {w.horizon for w in weights if not w.untested}
    paused = [h for h in HORIZONS if h in BEAR_HORIZONS or h in dropped]
    kept = [n for n in idle if not any(n.startswith(f"{HORIZON_NAMES[h]}：") for h in paused)]
    general = (
        f"熊市模式：{detail}。短线、波段暂停技术抄底和追涨买点，长线只保留低波动类防守战法；指数重新站上 {BEAR_MA} 日线后恢复。"
        "依据：2026-11 检验，这类行情里 K 线见底形态、MACD 底背离、追涨突破在下跌月平均亏损（docs/research/2026-11-01-technical-bear.md）。"
    )
    return [general] + [f"{HORIZON_NAMES[h]}：熊市模式，今日暂停{HORIZON_NAMES[h]}推荐。{detail}。" for h in paused] + kept


def idle_notes(
    summaries: list[PlaybookSummary], weights: list[Weight], current: list[RotationScore] | None = None
) -> list[str]:
    """Say which horizons sit out today, and how far the closest playbook is from qualifying.
    With `current` (rotation mode) the yardstick is the trailing-window record instead of fitness."""
    voting = {w.horizon for w in weights if not w.untested}
    notes = []
    window = next((r.window for r in current or []), 0)
    for horizon in (h for h in HORIZONS if h not in voting):
        name = HORIZON_NAMES[horizon]
        if current is not None:
            rows = [r for r in current if r.horizon == horizon and r.avg_return is not None]
            best = max(rows, key=lambda r: r.score if r.score is not None else float("-inf"), default=None)
            closest = f"近 {window} 日表现最好的是「{best.name}」：{best.trades} 笔，平均 {best.avg_return:+.2f}%。" if best else ""
            notes.append(
                f"{name}：近 {window} 日轮换推选里没有{name}战法（近期交易没有为正，或排不进推选名额），"
                f"今日不给出推荐，观望。{closest}"
            )
            continue
        tested = [s for s in summaries if s.spec.horizon == horizon and s.fitness is not None]
        best = max(tested, key=lambda s: s.fitness or 0.0, default=None)
        closest = (
            f"最接近的是「{best.spec.name}」：适配度 {best.fitness:.0f}，结论{VERDICT_NAMES.get(best.verdict, best.verdict)}。"
            if best else ""
        )
        notes.append(
            f"{HORIZON_NAMES[horizon]}：当前周期没有战法通过检验（样本外有效或限定环境有效，且适配度 ≥ {MIN_FITNESS:g}），"
            f"今日不给出推荐，观望。{closest}"
        )
    return notes


def build_report(ctx: Context, summaries: list[PlaybookSummary], regime) -> DailyReport:
    latest = ctx.dates[-1]
    by_id = {s.spec.id: s for s in summaries}
    registry = {pb.spec.id: pb for pb in catalog()}
    rot = rotation.report(ctx=ctx, playbooks=list(registry.values()))
    selection = "rotation" if rot.validated else "fitness"
    weights_all = weights_from_rotation(rot.current, summaries) if rot.validated else weights_from_board(summaries)
    mode, mode_detail = market_mode(ctx, latest)
    weights = apply_market_mode(weights_all, registry, mode)
    elected = {r.playbook_id: r for r in rot.current} if rot.validated else None
    rules = {w.playbook_id: exit_lab.live_rules(registry[w.playbook_id], ctx) for w in weights if not w.untested}
    exits_by_id = {pid: r[0] for pid, r in rules.items()}
    entry_by_id = {pid: r[1] for pid, r in rules.items()}
    day = ctx.panel.filter(pl.col("date") == latest)
    signals, failed = _signals_on(registry, {w.playbook_id for w in weights}, ctx, len(ctx.dates) - 1)
    ranked, contributions = ensemble_day(day, signals, weights, regime.sentiment)

    vol = day["volatility_20"].drop_nulls() if "volatility_20" in day.columns else pl.Series([], dtype=pl.Float64)
    vol_cut = float(vol.quantile(0.9)) if len(vol) else None
    recent_dates = ctx.dates[-10:]
    recent_down = set(
        ctx.panel.filter(pl.col("date").is_in(recent_dates) & pl.col("is_limit_down"))["code"].unique().to_list()
    ) if "is_limit_down" in ctx.panel.columns else set()
    themes = _themes(ctx)
    untested = {w.playbook_id for w in weights if w.untested}

    picks: dict[str, list[StockPick]] = {}
    rows_by_h: dict[str, list[dict]] = {}
    for horizon in HORIZONS:
        frame = ranked[horizon]
        rows = frame.head(TOP_PER_HORIZON).to_dicts() if not frame.is_empty() else []
        rows_by_h[horizon] = rows
        picks[horizon] = [
            _build_pick(
                i, row, horizon, contributions[horizon], by_id, untested, themes, vol_cut, recent_down,
                exits_by_id, elected, entry_by_id,
            )
            for i, row in enumerate(rows, 1)
        ]
    best = overall_from(ranked)
    overall: list[StockPick] = []
    for i, row in enumerate(best.to_dicts() if not best.is_empty() else [], 1):
        h = row["horizon"]
        overall.append(_build_pick(
            i, row, h, contributions[h], by_id, untested, themes, vol_cut, recent_down, exits_by_id, elected,
            entry_by_id,
        ))

    used = {w.playbook_id: w for w in weights}
    weight_rows = [
        PlaybookWeight(
            playbook_id=s.spec.id, name=s.spec.name, horizon=s.spec.horizon,
            weight=used[s.spec.id].weight if s.spec.id in used else 0.0, fitness=s.fitness, verdict=s.verdict,
        )
        for s in summaries
    ]
    prov = dataset_provenance(ctx)
    notes = [
        f"数据源：{prov.source}，覆盖 {prov.coverage} 只股票，最新交易日 {latest.isoformat()}。",
        (
            f"当前环境：{regime.label}。投票战法来自轮换推选（{rot.decided_on} 收盘推选，{rot.next_in_days} 个交易日后重选），"
            f"共 {sum(not w.untested for w in weights)} 个；轮换设置在样本内选出，并在之后的时段通过逐期检验。"
            if selection == "rotation" else
            f"当前环境：{regime.label}。参与投票的战法 {sum(not w.untested for w in weights)} 个"
            f"（适配度 ≥ {MIN_FITNESS:g} 且样本外有效或限定环境有效）。"
        ),
        rot.verdict,
        "排除：ST、上市不足 60 个交易日、当日无行情、成交额低于 0.5 亿。",
        "止损 = max(收盘价×(1-止损%), 收盘价-2×ATR14)，取自贡献最大的战法的离场规则；"
        "战法本身不设价格止损时（如低风险复合），止损价只是 2×ATR14 的风险参考，回测没有用它。",
        "卖出计划取自贡献最大的战法：卖法实验室在样本内选出、且样本外也更好的卖法才替换默认卖法。",
    ]
    idle = idle_notes(summaries, weights_all, rot.current if selection == "rotation" else None)
    notes += bear_notes(mode_detail or "", weights_all, weights, idle) if mode == "bear" else idle
    if failed:
        notes.append(f"以下战法今日信号计算失败，未参与投票：{'、'.join(sorted(failed))}。")
    long_tested = any(w.horizon == "long" and not w.untested for w in weights)
    if long_tested:
        notes.append("长线因子战法按月或按周调仓：两次调仓之间沿用最近一次调仓日的名单。")
    if untested and long_tested:
        notes.append("低估值类战法仓库无历史基本面，未经历史回测，只以固定低权重参与长线排序。")
    if prov.mode == "SIM":
        notes.append("警告：当前为 SIM 合成数据，结论仅用于验证流程，不是市场判断。")
    notes.append(DISCLAIMER + "。")
    return DailyReport(
        trade_date=latest.isoformat(), generated_at=datetime.now(CST).isoformat(timespec="seconds"),
        provenance=prov, regime=regime, weights=weight_rows,
        short=picks["short"], swing=picks["swing"], long=picks["long"], overall=overall, notes=notes,
        selection=selection,  # type: ignore[arg-type]
        market_mode=mode,  # type: ignore[arg-type]
        market_detail=mode_detail,
    )


_REPORT_LOCK = threading.RLock()


def generate_report(force: bool = False) -> DailyReport:
    """The report for the warehouse's latest trade date; idempotent unless `force`."""
    ctx = context()
    latest = ctx.dates[-1].isoformat()
    with _REPORT_LOCK:
        if not force:
            existing = report(latest, ctx)
            built = ctx.wh.read_doc("report_versions", latest) or {}
            # Same day, same data, same report format: reuse. New bars for the day (an
            # after-close sync) or a newer report format rebuild it.
            if existing is not None and built.get("version") == ctx.version and built.get("format") == REPORT_FORMAT:
                return existing
        summaries = board().playbooks
        result = build_report(ctx, summaries, current_regime_state(ctx.wh))
        ctx.wh.write_doc("reports", latest, result.model_dump(mode="json"))
        ctx.wh.write_doc("report_versions", latest, {"version": ctx.version, "format": REPORT_FORMAT})
        return result


def report(trade_date: str, ctx: Context | None = None) -> DailyReport | None:
    ctx = ctx or context()
    doc = ctx.wh.read_doc("reports", trade_date)
    if doc is None:
        return None
    try:
        return DailyReport.model_validate(doc)
    except Exception:
        return None  # an older schema: treat as missing so it can be regenerated


def latest_report() -> DailyReport | None:
    ctx = context()
    for name in reversed(ctx.wh.list_docs("reports")):
        found = report(name, ctx)
        if found is not None:
            return found
    return None


def report_index() -> ReportIndex:
    ctx = context()
    items = []
    for name in reversed(ctx.wh.list_docs("reports")):
        found = report(name, ctx)
        if found is not None:
            items.append(ReportIndexItem(
                trade_date=found.trade_date, generated_at=found.generated_at, regime_label=found.regime.label,
                top=[f"{p.name} {p.code}" for p in found.overall[:3]],
            ))
    return ReportIndex(reports=items)


# --- track record ---------------------------------------------------------------------------

class Prices:
    """Forward-return lookups on dense grids of the whole panel."""

    def __init__(self, ctx: Context) -> None:
        self.dates = np.array(ctx.dates, dtype="datetime64[D]")
        self.codes = sorted(ctx.panel["code"].unique().to_list())
        self.index = {c: i for i, c in enumerate(self.codes)}
        self.grid = price_grid(ctx.panel, self.codes, self.dates, ("open", "close", "open_at_limit"))
        self.bench = self.bench_open = None
        if ctx.benchmark is not None:
            joined = (
                pl.DataFrame({"date": ctx.dates}).join(ctx.benchmark.unique("date"), on="date", how="left")
                .sort("date").with_columns(pl.col("close").forward_fill())
            )
            self.bench = joined["close"].to_numpy().astype(float)
            if "open" in joined.columns:
                self.bench_open = joined["open"].cast(pl.Float64).to_numpy().astype(float)
        self.buy = (1 + DEFAULT_COSTS.slippage_bp / 1e4) * (1 + DEFAULT_COSTS.commission_bp / 1e4)
        self.sell = (1 - DEFAULT_COSTS.slippage_bp / 1e4) * (1 - (DEFAULT_COSTS.commission_bp + DEFAULT_COSTS.stamp_bp) / 1e4)

    def date_index(self, d) -> int | None:
        i = int(np.searchsorted(self.dates, np.datetime64(d)))
        return i if i < len(self.dates) and self.dates[i] == np.datetime64(d) else None

    def forward(self, code: str, signal_idx: int, hold: int) -> tuple[float, float | None] | None:
        """(net return %, benchmark return %) from the next open to the close `hold` bars later, or None."""
        ci = self.index.get(code)
        entry, exit_ = signal_idx + 1, signal_idx + 1 + hold
        if ci is None or exit_ >= len(self.dates):
            return None
        g = self.grid
        if not (g["present"][ci, entry] and g["present"][ci, exit_]) or g["open_at_limit"][ci, entry]:
            return None
        buy_px, sell_px = g["open"][ci, entry], g["close"][ci, exit_]
        if not buy_px > 0 or not np.isfinite(sell_px):
            return None
        ret = (sell_px * self.sell / (buy_px * self.buy) - 1) * 100
        # Benchmark over the same window as the stock: entry-day open to exit-day close.
        bench = None
        start = self.bench_open[entry] if self.bench_open is not None else np.nan
        if self.bench is not None and np.isfinite(start) and start > 0:
            bench = (self.bench[exit_] / start - 1) * 100
        return float(ret), (float(bench) if bench is not None else None)


@dataclass
class _Outcome:
    trade_date: str
    horizon: str
    picks: int
    returns: list[float]
    excesses: list[float]


def _outcomes(prices: Prices, trade_date: str, picks_by_horizon: dict[str, list[tuple[str, str]]]) -> list[_Outcome]:
    """Score (code, pick horizon) lists per track horizon (short/swing/long/overall)."""
    idx = prices.date_index(datetime.fromisoformat(trade_date).date())
    out = []
    for horizon, items in picks_by_horizon.items():
        returns, excesses = [], []
        for code, pick_horizon in items:
            if idx is None:
                continue
            res = prices.forward(code, idx, HOLD[pick_horizon])
            if res is None:
                continue
            returns.append(res[0])
            if res[1] is not None:
                excesses.append(res[0] - res[1])  # excess per pick; mean excess = mean ret - mean benchmark
        out.append(_Outcome(trade_date, horizon, len(items), returns, excesses))
    return out


def _points(outcomes: list[_Outcome], kind: str) -> list[TrackRecordPoint]:
    points = []
    for o in outcomes:
        if o.picks == 0:
            continue
        n = len(o.returns)
        avg = float(np.mean(o.returns)) if n else None
        excess = float(np.mean(o.excesses)) if o.excesses and len(o.excesses) == n else None
        points.append(TrackRecordPoint(
            trade_date=o.trade_date, horizon=o.horizon, kind=kind, picks=o.picks, matured=n,  # type: ignore[arg-type]
            avg_return=round(avg, 3) if avg is not None else None,
            hit_rate=round(float(np.mean(np.array(o.returns) > 0)) * 100, 1) if n else None,
            benchmark_return=round(avg - excess, 3) if avg is not None and excess is not None else None,
            excess_return=round(excess, 3) if excess is not None else None,
        ))
    return points


def _summaries(outcomes: list[_Outcome], kind: str) -> list[TrackRecordSummary]:
    out = []
    for horizon in (*HORIZONS, "overall"):
        group = [o for o in outcomes if o.horizon == horizon]
        returns = [r for o in group for r in o.returns]
        excess = [x for o in group for x in o.excesses]
        out.append(TrackRecordSummary(
            horizon=horizon, kind=kind,  # type: ignore[arg-type]
            reports=sum(1 for o in group if o.returns), matured_picks=len(returns),
            avg_return=round(float(np.mean(returns)), 3) if returns else None,
            hit_rate=round(float(np.mean(np.array(returns) > 0)) * 100, 1) if returns else None,
            excess_return=round(float(np.mean(excess)), 3) if excess and len(excess) == len(returns) else None,
        ))
    return out


def recorded_outcomes(ctx: Context, prices: Prices) -> list[_Outcome]:
    outcomes: list[_Outcome] = []
    for name in ctx.wh.list_docs("reports"):
        found = report(name, ctx)
        if found is None:
            continue
        groups = {h: [(p.code, h) for p in getattr(found, h)] for h in HORIZONS}
        groups["overall"] = [(p.code, p.horizon) for p in found.overall]
        outcomes += _outcomes(prices, found.trade_date, groups)
    return outcomes


def in_sample_weight(s: PerformanceStats) -> float:
    """Weight a playbook earns from in-sample trades alone (0 = does not vote)."""
    if s.trades < 30 or (s.avg_return or 0) <= 0 or (s.t_stat or 0) < 2:
        return 0.0
    return float(min(1.0, max(0.2, (s.t_stat or 0) / 5)))


def in_sample_weights(
    ctx: Context, split_date, registry: list[Playbook] | None = None, failed: list[str] | None = None
) -> list[Weight]:
    """Weights fixed on trades that were entered and closed on or before `split_date`.

    The panel is cut at the split before anything runs, so out-of-sample rows
    cannot influence a weight, however the signal functions are written.
    """
    cut = Context(
        wh=ctx.wh, version=ctx.version, panel=ctx.panel.filter(pl.col("date") <= split_date),
        dates=[d for d in ctx.dates if d <= split_date], regimes=ctx.regimes, benchmark=ctx.benchmark, names=ctx.names,
    )
    weights = []
    for pb in registry if registry is not None else catalog():
        if not pb.spec.available or (pb.requires_history and _requires_history_missing(cut.panel, pb.requires_history)):
            continue
        try:
            signals = run_signals(pb, cut)
        except Exception as exc:
            LOG.exception("in-sample signal %s failed", pb.spec.id)
            if failed is not None:
                failed.append(f"{pb.spec.name}（{type(exc).__name__}）")
            continue
        trades, equity = simulate(
            cut.panel, signals, pb.spec.exits, pb.spec.sizing, cut.benchmark, end=split_date, ebb_days=ebb_days(cut),
            entry=pb.spec.entry,
        )
        w = in_sample_weight(stats(trades, equity))
        if w > 0:
            weights.append(Weight(pb.spec.id, pb.spec.horizon, round(w, 4)))
    return weights


def simulated_outcomes(
    ctx: Context, prices: Prices, registry: list[Playbook] | None = None, failed: list[str] | None = None
) -> tuple[list[_Outcome], list[Weight], str]:
    split_idx = max(1, int(len(ctx.dates) * SPLIT_FRACTION))
    split_date = ctx.dates[split_idx - 1]
    pbs = registry if registry is not None else catalog()
    weights = in_sample_weights(ctx, split_date, pbs, failed)
    replay = ctx.dates[split_idx::REPLAY_STEP]
    if not weights or not replay:
        return [], weights, split_date.isoformat()
    by_id = {pb.spec.id: pb for pb in pbs}
    all_signals = {w.playbook_id: run_signals(by_id[w.playbook_id], ctx) for w in weights}
    position = {d: i for i, d in enumerate(ctx.dates)}
    days = {k[0]: v for k, v in ctx.panel.filter(pl.col("date").is_in(replay)).partition_by("date", as_dict=True).items()}
    outcomes: list[_Outcome] = []
    for d in replay:
        phase = (ctx.regimes.get(d.isoformat()) or {}).get("sentiment")
        today = {pid: _as_of(frame, ctx.dates, position[d], _window(by_id[pid])) for pid, frame in all_signals.items()}
        voting = apply_market_mode(weights, by_id, market_mode(ctx, d)[0])  # the same bear rule as the live report
        ranked, _ = ensemble_day(days[d], today, voting, phase, exclude_st=False)
        groups: dict[str, list[tuple[str, str]]] = {
            h: [(c, h) for c in ranked[h].head(TOP_PER_HORIZON)["code"].to_list()] if not ranked[h].is_empty() else []
            for h in HORIZONS
        }
        best = overall_from(ranked)
        groups["overall"] = list(zip(best["code"].to_list(), best["horizon"].to_list())) if not best.is_empty() else []
        outcomes += _outcomes(prices, d.isoformat(), groups)
    return outcomes, weights, split_date.isoformat()


_SIM_CACHE: dict[str, tuple[list[TrackRecordPoint], list[TrackRecordSummary], str]] = {}


def track_record() -> TrackRecordResponse:
    ctx = context()
    prices = Prices(ctx)
    recorded = recorded_outcomes(ctx, prices)
    with _REPORT_LOCK:
        cached = _SIM_CACHE.get(ctx.version)
        if cached is None:
            failed: list[str] = []
            outcomes, weights, split = simulated_outcomes(ctx, prices, failed=failed)
            names = {pb.spec.id: pb.spec.name for pb in catalog()}
            detail = "、".join(f"{names.get(w.playbook_id, w.playbook_id)}({w.weight:.2f})" for w in weights) or "无"
            note = f"模拟回放：权重仅由 {split} 及以前的样本内交易确定（{detail}），之后每 {REPLAY_STEP} 个交易日重建一次组合。"
            note += "回放不按今天的名称剔除 ST（历史 ST 状态未知）。"
            if failed:
                note += f"信号计算失败、未参与回放：{'、'.join(failed)}。"
            cached = (_points(outcomes, "simulated"), _summaries(outcomes, "simulated"), note)
            _SIM_CACHE.clear()
            _SIM_CACHE[ctx.version] = cached
    sim_points, sim_summary, sim_note = cached
    rec_points = _points(recorded, "recorded")
    matured = sum(p.matured for p in rec_points if p.horizon != "overall")
    notes = [
        sim_note,
        "收益 = 次日开盘买入至持有期（短线 3、波段 10、长线 40 个交易日）后收盘，已扣佣金、印花税与滑点；基准为中证全指同一窗口（买入日开盘至卖出日收盘），超额 = 收益 - 基准。",
        f"实盘记录：已存档 {len({p.trade_date for p in rec_points})} 份报告，到期的荐股 {matured} 只；未到期的不计入。",
        "模拟回放的是历史样本外，实盘记录才是真实发布后的结果，两者不混合。",
    ]
    if dataset_provenance(ctx).mode == "SIM":
        notes.append("警告：当前为 SIM 合成数据，战绩仅用于验证流程。")
    return TrackRecordResponse(
        provenance=dataset_provenance(ctx),
        summary=[*_summaries(recorded, "recorded"), *sim_summary],
        points=[*rec_points, *sim_points],
        notes=notes,
    )


def reset_caches() -> None:
    _SIM_CACHE.clear()


__all__ = [
    "generate_report", "latest_report", "report", "report_index", "track_record", "ensemble_day",
    "weights_from_board", "in_sample_weights", "in_sample_weight",
]
