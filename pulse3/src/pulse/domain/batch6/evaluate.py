"""Evaluation of one batch 6 hypothesis in one segment, Holm correction and the pass rule (pre-registration sections 1, 2, 4).

Why: every hypothesis is judged the same way: matched excess over the same-day style-matched baseline (E062, ``research.baseline``), a one-sided moving-block bootstrap
(E082, ``research.bootstrap``; block = holding sessions, 10,000 draws, seed 20261118), Holm over the family of seven hypotheses, and a practical-size threshold of the round-trip
cost.
Rules (literal readings, recorded in section 7 notes):
  * excess = mean over signal names of (name return - same-day matched baseline mean of the pool), pool = in-scope names with a known filled return that day, both
    sides gross: the round-trip cost is paid by signal and baseline alike and cancels, so ``excess`` is the same before and after cost; the signal's own mean return
    is reported after cost (``mean_ret_net`` = mean - 0.252 %);
  * direction: the hypothesis has an expected sign s (+1 / -1); the one-sided p-value tests "s * excess <= 0" (block bootstrap on s * excess); p is the share of draws
    with a statistic <= 0 (None when there is nothing to test);
  * practical size: ``abs(excess) > 0.252 %`` ("after cost the magnitude exceeds 0.252 %"; for a positive hypothesis this is excess after cost > 0);
  * Holm: family size 7; when only some hypotheses have a p-value the others count as untested (p = 1) and sort last, so ``p_adj`` of a tested one is
    ``max_{j<=i} (7 - j + 1) p_(j)`` over the tested ones (conservative: it assumes all seven are tested);
  * pass (E093 / E094): PRE: sign correct AND practical size AND Holm-adjusted p < 0.05 AND the PRE block bootstrap is reliable (>= 10 blocks, E082);
    DEV: the sign of the excess equals the expected sign. A missing figure is never a pass.
Not covered: the pool, signal and fill construction (application), the forward-only criteria (40 signal days).
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from pulse.domain.batch6 import rules as R
from pulse.domain.research import baseline as BL
from pulse.domain.research import bootstrap as BS

D = Decimal


def dec(x: float) -> Decimal:
    return D(repr(float(x)))


@dataclass(frozen=True)
class SegmentResult:
    hypothesis: str
    segment: str
    sign: int
    hold: int
    n_signals: int                    # signal names generated (with a decision session in this segment)
    n_attempted: int                  # signals whose D+1 / entry session is observed
    n_filled: int                     # filled and with an observed exit
    fill_rate: float | None
    n_unfilled: Mapping[str, int]     # status -> count (blocked_buy / no_bar / pending_exit / not_in_pool)
    n_matched: int
    signal_days: int
    mean_ret_gross: float | None
    mean_ret_net: float | None
    mean_baseline: float | None
    excess: float | None              # matched excess (sv), gross = after cost (cost cancels)
    excess_all: float | None
    excess_svi: float | None
    p_value: float | None             # one-sided, direction-aware
    blocks: int
    reliable: bool
    note: str | None


def excess_from_means(signals_by_day: Mapping[str, Sequence[str]], pool_by_day: Mapping[str, Sequence[BL.PoolName]],
                      means_by_day: Mapping[str, Mapping[str, Mapping[str, Decimal]]]) -> BL.MatchedExcess:
    """``baseline.matched_excess`` with the per-day ``matched_means`` supplied by the caller (they are expensive: one pass per pool name over its cells, and the same pool serves
    several hypotheses). Same arithmetic, same result (unit-tested against ``matched_excess``)."""
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = 50, ROUND_HALF_EVEN
        return _excess_from_means(signals_by_day, pool_by_day, means_by_day)


def _excess_from_means(signals_by_day, pool_by_day, means_by_day) -> BL.MatchedExcess:
    tot = {"all": D(0), "sv": D(0), "svi": D(0)}
    n = unmatched = 0
    by_day: list[BL.DayExcess] = []
    for day in sorted(signals_by_day):
        means = means_by_day.get(day, {})
        ret = {p.key: p.ret for p in pool_by_day.get(day, ())}
        day_sum = {"all": D(0), "sv": D(0), "svi": D(0)}
        k = 0
        for key in signals_by_day[day]:
            m = means.get(key)
            if m is None or "sv" not in m:
                unmatched += 1
                continue
            for name in day_sum:
                day_sum[name] += ret[key] - m[name]
            k += 1
        if k:
            by_day.append(BL.DayExcess(day, k, day_sum["all"] / k, day_sum["sv"] / k, day_sum["svi"] / k))
            for name in tot:
                tot[name] += day_sum[name]
            n += k
    if n == 0:
        return BL.MatchedExcess(0, unmatched, 0, None, None, None, ())
    return BL.MatchedExcess(n, unmatched, len(by_day), tot["all"] / n, tot["sv"] / n, tot["svi"] / n, tuple(by_day))


def evaluate_segment(*, hypothesis: str, segment: str, sign: int, hold: int, all_days: Sequence[str], signals_by_day: Mapping[str, Sequence[str]],
                     pool_by_day: Mapping[str, Sequence[BL.PoolName]], status_counts: Mapping[str, int], n_signals: int,
                     means_by_day: Mapping[str, Mapping[str, Mapping[str, Decimal]]] | None = None, seed: int | None = None) -> SegmentResult:
    """``signals_by_day``: only FILLED signals with an observed exit. ``status_counts``: counts of the other signal statuses."""
    if means_by_day is None:
        means_by_day = {d: BL.matched_means(pool_by_day.get(d, ())) for d in signals_by_day}
    me = excess_from_means(signals_by_day, pool_by_day, means_by_day)
    attempted = sum(status_counts.get(k, 0) for k in ("filled", "blocked_buy", "no_bar", "pending_exit", "flag_unknown"))
    filled = status_counts.get("filled", 0)
    rate = filled / attempted if attempted else None
    unf = {k: v for k, v in status_counts.items() if k != "filled" and v}
    if me.n == 0:
        return SegmentResult(hypothesis, segment, sign, hold, n_signals, attempted, filled, rate, unf, 0, 0, None, None, None, None, None, None, None, 0, False, "no_signals")
    rets: list[Decimal] = []
    bases: list[Decimal] = []
    for day, keys in signals_by_day.items():
        by = {p.key: p.ret for p in pool_by_day.get(day, ())}
        means = means_by_day.get(day, {})
        for k in keys:
            m = means.get(k)
            if m is not None and "sv" in m:
                rets.append(by[k])
                bases.append(m["sv"])
    mean_ret = sum(rets, D(0)) / len(rets)
    mean_base = sum(bases, D(0)) / len(bases)
    days = [d.day for d in me.by_day]
    series = [sign * d.ex_sv for d in me.by_day]
    bs = BS.block_bootstrap_p(list(all_days), days, series, [d.k for d in me.by_day], block=hold, seed=R.SEED if seed is None else seed, n_boot=R.N_BOOT)
    return SegmentResult(hypothesis, segment, sign, hold, n_signals, attempted, filled, rate, unf, me.n, len(days), float(mean_ret), float(mean_ret - R.COST),
                         float(mean_base), float(me.ex_sv), float(me.ex_all), float(me.ex_svi), bs.p_value, bs.blocks, bs.reliable, bs.reason)


def holm(p_values: Mapping[str, float | None], family: int = R.FAMILY_SIZE) -> dict[str, float | None]:
    """Holm-adjusted p-values. Hypotheses with p None are untested (treated as 1, sorted last, no adjusted value). Family size is fixed (7)."""
    tested = sorted(((k, v) for k, v in p_values.items() if v is not None), key=lambda kv: (kv[1], kv[0]))
    out: dict[str, float | None] = {k: None for k in p_values}
    run = 0.0
    for i, (k, p) in enumerate(tested):
        run = max(run, min(1.0, (family - i) * p))
        out[k] = run
    return out


@dataclass(frozen=True)
class Verdict:
    hypothesis: str
    pre_sign_ok: bool | None
    pre_size_ok: bool | None
    pre_p_adj: float | None
    pre_significant: bool | None
    pre_reliable: bool | None
    dev_sign_ok: bool | None
    passed: bool
    reason: str


def judge(hyp: str, pre: SegmentResult | None, dev: SegmentResult | None, p_adj: float | None) -> Verdict:
    sign = R.HYPOTHESES[hyp][1]
    if pre is None or pre.excess is None:
        return Verdict(hyp, None, None, p_adj, None, None, None, False, "no PRE result")
    sign_ok = pre.excess * sign > 0
    size_ok = abs(pre.excess) > float(R.COST)
    sig = p_adj is not None and p_adj < R.ALPHA
    dev_ok = None if dev is None or dev.excess is None else dev.excess * sign > 0
    passed = bool(sign_ok and size_ok and sig and pre.reliable and dev_ok)
    bits = [f"PRE sign {'ok' if sign_ok else 'wrong'}", f"size {'ok' if size_ok else 'below 0.252%'}",
            f"Holm p {'n/a' if p_adj is None else f'{p_adj:.4f}'} {'<' if sig else '>='} 0.05", "reliable" if pre.reliable else "fewer than 10 blocks",
            "DEV " + ("n/a" if dev_ok is None else "same sign" if dev_ok else "opposite sign")]
    return Verdict(hyp, sign_ok, size_ok, p_adj, sig, pre.reliable, dev_ok, passed, "; ".join(bits))


def build_pool(rows: Sequence[tuple[str, float, float | None, float | None]]) -> list[BL.PoolName]:
    """Pool of one day from (code, return, size value, volatility value): quintiles among the day's names (missing value = no quintile)."""
    size_q = BL.quintiles([None if r[2] is None else dec(r[2]) for r in rows])
    vol_q = BL.quintiles([None if r[3] is None else dec(r[3]) for r in rows])
    return [BL.PoolName(r[0], dec(r[1]), size_q[i], vol_q[i]) for i, r in enumerate(rows)]


def percent_rank_top(values: Mapping[str, float], share: float) -> set[str]:
    """Names in the top ``share`` of the values (ties at the cut are all included): the top ceil(share x n) by value, plus names equal to the last one taken."""
    if not values:
        return set()
    k = max(1, math.ceil(share * len(values) - 1e-9))
    order = sorted(values.items(), key=lambda kv: (-kv[1], kv[0]))
    cut = order[min(k, len(order)) - 1][1]
    return {c for c, v in values.items() if v >= cut}
