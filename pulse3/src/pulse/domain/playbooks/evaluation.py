"""Playbook board evaluation: in-sample / out-of-sample statistics, verdict and 0-100 fitness (LEGACY_TRIAGE #16 #82 #150; pure, no IO).

Why: the old board gave every playbook a fitness ring, a verdict, the out-of-sample average and an equity sparkline (old ``playbooks.evaluate``,
read as SPECIFICATION). pulse3 stores each playbook's replay (daily NAV net of the user cost profile + trades with gross returns + entry regime)
and the sealed evaluation context with its train / validation split; this module turns those stored series into the same board figures.
Sample split (the context records it; nothing is re-chosen here): ``split_day`` = first session of the VALIDATION window. In-sample = trades
entered before it and NAV sessions before it; out-of-sample = trades entered on or after it and NAV sessions from it on (the first return of the
slice is measured against the NAV just before it). The replay is ONE continuous portfolio (the old site re-ran two separate portfolios), so a
position entered in-sample may close out-of-sample and the out-of-sample NAV carries the in-sample positions. The blind slice is never read.
When ``split_day`` is None (the experiment records no split) there is no out-of-sample figure: verdict ``None`` and the sample label stays
"in-sample only" - an in-sample number is never presented as validated.
Statistics (``EVALUATION_VERSION``): per sample trades, win rate and mean of the closed trades' GROSS returns (ratio, fees are inside the NAV),
t = mean / (sample std / sqrt(n)) (n >= 2, std > 0), Sharpe = mean / sample std of the daily NAV returns * sqrt(252) (risk-free 0; the daily
return needs two adjacent sessions with a NAV, a missing mark is skipped, never spread).
Verdict (the old rule order, thresholds unchanged; returns here are ratios, the old ones percent points):
  insufficient: full < 30 closed trades or out-of-sample < 5.   overfit: in-sample Sharpe > 1 and out-of-sample Sharpe < 0.3 x in-sample, or
  in-sample mean > 0 and out-of-sample mean <= 0.   effective: out-of-sample mean > 0, out-of-sample Sharpe > 0.5 and full t >= 2.
  regime_dependent: a regime bucket of >= 15 trades with mean > 0 and t >= 2.   weak: otherwise.
  Added (ledger E082): ``effective`` is confirmed with a moving-block bootstrap (block = mean holding sessions, by session) of the out-of-sample
  daily returns; a RELIABLE test (>= 10 blocks) with p >= 0.05 downgrades it to ``weak``; an unreliable one (< 10 blocks) is reported only and
  does not change the verdict (the ledger: fewer than 10 blocks are report-only). The style-matched baseline of E062 (``research.baseline``)
  needs same-day pool returns by float-cap and volatility quintile, which are not stored with a replay, so it is NOT applied here.
Fitness 0-100 = out-of-sample quality 35 (half Sharpe/2 clipped, half mean/2% clipped) + current-trend bucket 20 + current-sentiment bucket 15
+ last-60-sessions 15 + significance 10 (t/3 clipped) + sample size 5 (trades/200 clipped); a bucket / recent window with too few trades falls
back to the full-sample mean with a 0.6 / 0.5 discount; ``overfit`` caps at 25 and ``insufficient`` at 35. Every component is listed in
``reasons``. A missing Sharpe / current regime scores that component 0 and says so (it is never silently filled).
Not covered: separate in/out portfolios, benchmark-relative curves, the matched baseline (see above), the hard 52-playbook ``requires`` gate (the
caller supplies ``unavailable``).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from pulse.domain.market_state.regime import LABELS
from pulse.domain.research import bootstrap as BS

EVALUATION_VERSION = "playbook-board-v1"
SPLIT_NOTE = ("样本外 = 验证时段（评价上下文封存的切分点之后入场的交易与净值）；整段回放是同一个连续账户，"
              "样本内入场的持仓可能在样本外才平仓；盲测时段没有被读取。")
PPY = 252
MIN_TRADES = 30
MIN_OOS_TRADES = 5
MIN_REGIME_TRADES = 15
MIN_FIT_BUCKET = 10
MIN_FIT_RECENT = 5
RECENT_SESSIONS = 60
AVG_FULL_SCORE = Decimal("0.02")          # a 2% mean gross return per trade is a full return score
BLOCK_P_MAX = 0.05
DIMENSIONS = ("trend", "liquidity", "sentiment", "style")
DIM_TEXT = {"trend": "趋势", "liquidity": "流动性", "sentiment": "情绪", "style": "风格"}
VERDICT_TEXT = {"effective": "有效", "regime_dependent": "依赖环境", "weak": "偏弱", "overfit": "疑似过拟合", "insufficient": "样本不足"}
D = Decimal


@dataclass(frozen=True)
class TradeIn:
    """A CLOSED trade: entry session, gross return ratio, holding sessions and the regime tag read at entry (None = unknown)."""
    entry_day: str
    ret: Decimal
    holding: int | None = None
    regime: Mapping[str, object] | None = None


@dataclass(frozen=True)
class Sample:
    trades: int
    win_rate: Decimal | None
    avg_return: Decimal | None
    t_stat: Decimal | None
    sharpe: Decimal | None
    sessions: int                       # daily returns behind the Sharpe


@dataclass(frozen=True)
class RegimeStat:
    dimension: str
    regime: str
    label: str
    stats: Sample


@dataclass(frozen=True)
class BlockInfo:
    p_value: float | None
    blocks: int
    block: int
    reliable: bool
    reason: str | None


@dataclass(frozen=True)
class Evaluation:
    version: str
    split_day: str | None
    full: Sample
    in_sample: Sample
    out_of_sample: Sample | None
    recent: Sample
    by_regime: tuple[RegimeStat, ...]
    verdict: str | None
    verdict_reason: str
    fitness: Decimal | None
    reasons: tuple[str, ...]
    block: BlockInfo | None
    missing: tuple[str, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------------------------------------
def daily_returns(nav: Sequence[tuple[str, Decimal | None]], initial: Decimal, start: str | None = None, end: str | None = None) -> list[Decimal]:
    """Returns of the sessions in ``[start, end)`` (ISO strings; None = open). The previous mark is the one of the previous session of the WHOLE
    series (``initial`` before the first); a session whose own or previous NAV is missing has no return."""
    out: list[Decimal] = []
    prev: Decimal | None = initial
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = 50, ROUND_HALF_EVEN
        for day, v in nav:
            inside = (start is None or day >= start) and (end is None or day < end)
            if inside and v is not None and prev is not None and prev > 0:
                out.append(v / prev - 1)
            prev = v
    return out


def _mean_std(xs: Sequence[Decimal]) -> tuple[Decimal, Decimal | None]:
    n = len(xs)
    mean = sum(xs, D(0)) / n
    if n < 2:
        return mean, None
    var = sum(((x - mean) ** 2 for x in xs), D(0)) / (n - 1)
    return mean, var.sqrt()


def sample_stats(trades: Sequence[TradeIn], returns: Sequence[Decimal]) -> Sample:
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = 50, ROUND_HALF_EVEN
        n = len(trades)
        win = avg = t = None
        if n:
            rs = [x.ret for x in trades]
            win = D(sum(1 for r in rs if r > 0)) / n
            avg, std = _mean_std(rs)
            if std is not None and std > 0:
                t = avg / (std / D(n).sqrt())
        sharpe = None
        if len(returns) >= 2:
            m, s = _mean_std(returns)
            if s is not None and s > D("1e-12"):
                sharpe = m / s * D(PPY).sqrt()
        return Sample(n, win, avg, t, sharpe, len(returns))


def regime_bucket(tag: Mapping[str, object] | None, dim: str) -> str | None:
    """Bucket of one dimension, or None when the tag is missing, the dimension's basis is not ``ok`` or the label is unmeasured."""
    if tag is None:
        return None
    basis = tag.get("basis")
    if not isinstance(basis, Mapping) or basis.get(dim) != "ok":
        return None
    label = str(tag.get(dim))
    return label if label in LABELS[dim] and label != "unknown" else None


def regime_stats(trades: Sequence[TradeIn]) -> list[RegimeStat]:
    out: list[RegimeStat] = []
    for dim in DIMENSIONS:
        groups: dict[str, list[TradeIn]] = {}
        for t in trades:
            b = regime_bucket(t.regime, dim)
            if b is not None:
                groups.setdefault(b, []).append(t)
        for key, label in LABELS[dim].items():
            if key in groups:
                out.append(RegimeStat(dim, key, label, sample_stats(groups[key], ())))
    return out


# --------------------------------------------------------------------------------------------------------
# verdict and fitness
# --------------------------------------------------------------------------------------------------------
def _f(v: Decimal | None, digits: int = 2, pct: bool = False) -> str:
    if v is None:
        return "缺失"
    return f"{float(v) * 100:+.{digits}f}%" if pct else f"{float(v):.{digits}f}"


def judge(full: Sample, ins: Sample, oos: Sample, by_regime: Sequence[RegimeStat], block: BlockInfo | None) -> tuple[str, str]:
    if full.trades < MIN_TRADES:
        return "insufficient", f"全样本仅 {full.trades} 笔已平仓交易，少于 {MIN_TRADES} 笔，统计结论不可靠。"
    if oos.trades < MIN_OOS_TRADES:
        return "insufficient", f"样本外仅 {oos.trades} 笔交易，无法验证样本内表现是否稳健。"
    is_sh, oos_sh = ins.sharpe or D(0), oos.sharpe or D(0)
    is_avg, oos_avg = ins.avg_return or D(0), oos.avg_return or D(0)
    if is_sh > 1 and oos_sh < D("0.3") * is_sh:
        return "overfit", f"样本内夏普 {float(is_sh):.2f} 而样本外仅 {float(oos_sh):.2f}（不足 30%），疑似对历史过拟合。"
    if is_avg > 0 and oos_avg <= 0:
        return "overfit", f"样本内平均收益 {_f(is_avg, 2, True)} 但样本外 {_f(oos_avg, 2, True)}，样本外失效，疑似过拟合。"
    t_full = full.t_stat or D(0)
    if oos_avg > 0 and oos_sh > D("0.5") and t_full >= 2:
        text = (f"样本外平均收益 {_f(oos_avg, 2, True)}、夏普 {float(oos_sh):.2f}，全样本 t 值 {float(t_full):.2f}，共 {full.trades} 笔，样本外验证有效。")
        if block is not None and block.p_value is not None:
            if block.reliable and block.p_value >= BLOCK_P_MAX:
                return "weak", text.replace("样本外验证有效。", "") + f"但块自助（块长 {block.block} 个交易日，{block.blocks} 块）p = {block.p_value:.3f} ≥ {BLOCK_P_MAX}，不能确认为有效（E082）。"
            if not block.reliable:
                text += f"块自助只有 {block.blocks} 块（少于 {BS.MIN_BLOCKS}），p = {block.p_value:.3f} 仅供参考，不据此改判（E082）。"
            else:
                text += f"块自助 p = {block.p_value:.3f}（{block.blocks} 块）。"
        return "effective", text
    best: tuple[RegimeStat, Decimal] | None = None
    for r in by_regime:
        s = r.stats
        if s.trades >= MIN_REGIME_TRADES and (s.avg_return or D(0)) > 0 and (s.t_stat or D(0)) >= 2:
            if best is None or (s.t_stat or D(0)) > best[1]:
                best = (r, s.t_stat or D(0))
    if best is not None:
        s = best[0].stats
        return "regime_dependent", (f"整体表现一般（样本外夏普 {float(oos_sh):.2f}），但在{best[0].label}（{DIM_TEXT[best[0].dimension]}）下 "
                                    f"{s.trades} 笔平均 {_f(s.avg_return, 2, True)}、t 值 {float(best[1]):.2f}，只适合该环境。")
    return "weak", f"样本外平均收益 {_f(oos_avg, 2, True)}、夏普 {float(oos_sh):.2f}，全样本 t 值 {float(t_full):.2f}，未表现出稳定的正期望。"


def _clip(x: Decimal, lo: Decimal = D(0), hi: Decimal = D(1)) -> Decimal:
    return max(lo, min(hi, x))


def _avg_score(s: Sample) -> Decimal:
    return _clip((s.avg_return or D(0)) / AVG_FULL_SCORE)


def fitness(full: Sample, oos: Sample, recent: Sample, by_regime: Sequence[RegimeStat], verdict: str, trend: str | None,
            sentiment: str | None) -> tuple[Decimal, list[str], list[str]]:
    """(score 0-100 rounded to 1 decimal, per-component reasons, names of the inputs that were missing)."""
    reasons: list[str] = []
    missing: list[str] = []
    total = D(0)
    if oos.sharpe is None:
        missing.append("oos_sharpe")
    q_oos = D("0.5") * _clip((oos.sharpe or D(0)) / 2) + D("0.5") * _avg_score(oos)
    total += 35 * q_oos
    reasons.append(f"样本外质量 {float(35 * q_oos):.1f}/35：夏普 {_f(oos.sharpe)}、平均收益 {_f(oos.avg_return, 2, True)}")

    def regime_part(dim: str, key: str | None, weight: int) -> None:
        nonlocal total
        label = LABELS[dim].get(key or "", key or "未知")
        if key is None:
            missing.append(f"current_{dim}")
        bucket = next((r for r in by_regime if r.dimension == dim and r.regime == key), None)
        if bucket is not None and bucket.stats.trades >= MIN_FIT_BUCKET:
            score = _avg_score(bucket.stats)
            total += weight * score
            reasons.append(f"当前{DIM_TEXT[dim]}（{label}）{float(weight * score):.1f}/{weight}：{bucket.stats.trades} 笔平均 {_f(bucket.stats.avg_return, 2, True)}")
        else:
            n = 0 if bucket is None else bucket.stats.trades
            score = D("0.6") * _avg_score(full)
            total += weight * score
            reasons.append(f"当前{DIM_TEXT[dim]}（{label}）{float(weight * score):.1f}/{weight}：该环境仅 {n} 笔（<{MIN_FIT_BUCKET}），回退全样本平均 {_f(full.avg_return, 2, True)} 并打 6 折")

    regime_part("trend", trend, 20)
    regime_part("sentiment", sentiment, 15)
    if recent.trades >= MIN_FIT_RECENT:
        score = _avg_score(recent)
        total += 15 * score
        reasons.append(f"近 {RECENT_SESSIONS} 日 {float(15 * score):.1f}/15：{recent.trades} 笔平均 {_f(recent.avg_return, 2, True)}")
    else:
        score = D("0.5") * _avg_score(full)
        total += 15 * score
        reasons.append(f"近 {RECENT_SESSIONS} 日 {float(15 * score):.1f}/15：仅 {recent.trades} 笔（<{MIN_FIT_RECENT}），回退全样本并打 5 折")
    sig = _clip((full.t_stat or D(0)) / 3)
    total += 10 * sig
    reasons.append(f"显著性 {float(10 * sig):.1f}/10：t 值 {_f(full.t_stat)}")
    size = _clip(D(full.trades) / 200)
    total += 5 * size
    reasons.append(f"样本量 {float(5 * size):.1f}/5：{full.trades} 笔")
    cap = {"overfit": D(25), "insufficient": D(35)}.get(verdict)
    if cap is not None and total > cap:
        reasons.append(f"判定为{VERDICT_TEXT[verdict]}，适配度上限压到 {cap}")
        total = cap
    return _clip(total, D(0), D(100)).quantize(D("0.1"), rounding=ROUND_HALF_EVEN), reasons, missing


# --------------------------------------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------------------------------------
def block_info(oos_nav: Sequence[tuple[str, Decimal | None]], initial: Decimal, split_day: str, trades: Sequence[TradeIn], *,
               seed: int = BS.DEFAULT_SEED) -> BlockInfo | None:
    """Block-bootstrap p of "the mean daily net return of the out-of-sample sessions is <= 0" (block = mean holding sessions, at least 1)."""
    days = [d for d, _ in oos_nav if d >= split_day]
    rets: list[tuple[str, Decimal]] = []
    prev: Decimal | None = initial
    for d, v in oos_nav:
        if d >= split_day and v is not None and prev is not None and prev > 0:
            rets.append((d, v / prev - 1))
        prev = v
    if not rets:
        return None
    holds = [t.holding for t in trades if t.holding is not None]
    block = max(1, round(sum(holds) / len(holds))) if holds else 1
    res = BS.block_bootstrap_p(days, [d for d, _ in rets], [r for _, r in rets], [1] * len(rets), block=block, seed=seed + block)
    return BlockInfo(res.p_value, res.blocks, res.block, res.reliable, res.reason)


def evaluate(trades: Sequence[TradeIn], nav: Sequence[tuple[str, Decimal | None]], initial: Decimal, split_day: str | None, *,
             trend: str | None, sentiment: str | None) -> Evaluation:
    """Board figures of one stored replay. ``trades`` are the CLOSED trades only; ``nav`` is the daily NAV in session order."""
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = 50, ROUND_HALF_EVEN
        tr = sorted(trades, key=lambda t: t.entry_day)
        sessions = [d for d, _ in nav]
        full = sample_stats(tr, daily_returns(nav, initial))
        recent_start = sessions[-RECENT_SESSIONS] if len(sessions) >= RECENT_SESSIONS else (sessions[0] if sessions else None)
        recent = sample_stats([t for t in tr if recent_start is not None and t.entry_day >= recent_start],
                              daily_returns(nav, initial, recent_start))
        by_regime = regime_stats(tr)
        if split_day is None:
            ins = full
            return Evaluation(EVALUATION_VERSION, None, full, ins, None, recent, tuple(by_regime), None,
                              "实验没有记录样本外切分：只有样本内数字，不给出判定。", None, (), None, ("split_day",))
        ins = sample_stats([t for t in tr if t.entry_day < split_day], daily_returns(nav, initial, None, split_day))
        oos = sample_stats([t for t in tr if t.entry_day >= split_day], daily_returns(nav, initial, split_day))
        blk = block_info(nav, initial, split_day, [t for t in tr if t.entry_day >= split_day]) if oos.sessions >= 2 else None
        verdict, reason = judge(full, ins, oos, by_regime, blk)
        score, reasons, missing = fitness(full, oos, recent, by_regime, verdict, trend, sentiment)
        return Evaluation(EVALUATION_VERSION, split_day, full, ins, oos, recent, tuple(by_regime), verdict, reason, score, tuple(reasons), blk,
                          tuple(missing))


def downsample(items: Sequence, limit: int) -> list:
    """At most ``limit`` evenly spaced items, always keeping the first and the last."""
    n = len(items)
    if n <= limit:
        return list(items)
    picks = sorted({round(i * (n - 1) / (limit - 1)) for i in range(limit)})
    return [items[i] for i in picks]

