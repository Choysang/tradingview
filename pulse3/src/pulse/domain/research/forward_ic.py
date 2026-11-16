"""Forward monitor statistics of a published ranking: rank IC and top-decile excess (backfill #169, M5-E04 evidence).

Why: a ranking is only worth its later outcomes. Once a horizon has been observed, the monitor compares the order of the
ranking with the realised forward returns of the same instruments. These functions are the pure part: no IO, no clock.
Method (``METHOD_VERSION``):
* rank IC = Spearman correlation between ``-alpha_rank`` (so a better rank is a higher score) and the realised gross
  forward return, over every ranked instrument that has a return; ties share the average rank; fewer than
  ``MIN_EVALUABLE`` returns, or a constant series, give ``None`` with a reason (never 0);
* top decile = the first ``ceil(0.1 * ranked)`` instruments of the ranking; instruments without a return stay in the
  decile's count (``top_n``) and are not averaged as zero (``top_evaluable`` counts the ones that were);
* excess = mean gross return of the evaluable top decile minus the median gross return of every evaluable ranked
  instrument. All returns are GROSS: no fee, tax or slippage is deducted.
* a horizon is mature when the observed session calendar holds the session ``horizon`` sessions after the decision session;
  until then the result is ``pending`` and carries no numbers.
Not covered: net-of-cost returns, ex-rights adjusted returns (the caller supplies path-guarded raw returns), regimes.
"""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from fractions import Fraction
from math import ceil
from statistics import median
from typing import Mapping, Sequence

METHOD_VERSION = "forward-ic-v1"
MIN_EVALUABLE = 5
TOP_FRACTION = Decimal("0.1")
PLACES = Decimal("0.000001")


def _q(x: Decimal | None) -> Decimal | None:
    return None if x is None else x.quantize(PLACES, rounding=ROUND_HALF_EVEN)


def average_ranks(values: Sequence[Decimal | Fraction | int]) -> list[Fraction]:
    """1-based ranks, ascending; equal values share the mean of the ranks they span."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks: list[Fraction] = [Fraction(0)] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        mean = Fraction(i + 1 + j + 1, 2)
        for k in range(i, j + 1):
            ranks[order[k]] = mean
        i = j + 1
    return ranks


def spearman(xs: Sequence[Decimal | int], ys: Sequence[Decimal | int]) -> Decimal | None:
    """Spearman rank correlation (Pearson on average ranks), 6 decimals; ``None`` for fewer than 3 pairs or a constant side."""
    if len(xs) != len(ys):
        raise ValueError("spearman needs paired series")
    n = len(xs)
    if n < 3:
        return None
    rx, ry = average_ranks(xs), average_ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx == 0 or vy == 0:
        return None
    with localcontext() as ctx:
        ctx.prec = 50
        denom = (Decimal(vx.numerator) / Decimal(vx.denominator) * Decimal(vy.numerator) / Decimal(vy.denominator)).sqrt()
        rho = (Decimal(cov.numerator) / Decimal(cov.denominator)) / denom
    return _q(max(Decimal(-1), min(Decimal(1), rho)))


def matured_window(sessions: Sequence[date], decision_session: date | None, horizon_sessions: int
                   ) -> tuple[date, date] | str:
    """(entry session, exit session) when the horizon is observed, else the pending reason code.

    Entry = the session after the decision session, exit = the session ``horizon_sessions`` after it (same convention as the
    ranking outcome of ``domain.market_state.outcomes``)."""
    cal = sorted(set(sessions))
    if decision_session is None or decision_session not in cal:
        return "DECISION_SESSION_NOT_OBSERVED"
    i = cal.index(decision_session)
    if i + horizon_sessions > len(cal) - 1:
        return "HORIZON_NOT_REACHED"
    return cal[i + 1], cal[i + horizon_sessions]


def sessions_after(sessions: Sequence[date], decision_session: date | None) -> int | None:
    if decision_session is None:
        return None
    return sum(1 for d in set(sessions) if d > decision_session)


def evaluate(ranked: Sequence[tuple[str, int]], returns: Mapping[str, Decimal | None], *,
             top_fraction: Decimal = TOP_FRACTION, min_evaluable: int = MIN_EVALUABLE) -> dict:
    """Statistics of one matured ranking. ``ranked`` = (instrument id, alpha_rank); ``returns`` = gross forward return or None."""
    ordered = sorted(ranked, key=lambda t: t[1])
    n_ranked = len(ordered)
    ev = [(iid, rk, returns.get(iid)) for iid, rk in ordered if returns.get(iid) is not None]
    out: dict = {"n_ranked": n_ranked, "n_evaluable": len(ev), "top_n": 0, "top_evaluable": 0, "rank_ic": None,
                 "top_decile_mean_gross": None, "universe_median_gross": None, "top_decile_excess": None, "missing_reason": None}
    if n_ranked == 0:
        out["missing_reason"] = "NO_RANKED_INSTRUMENT"
        return out
    k = max(1, ceil(Decimal(n_ranked) * top_fraction))
    out["top_n"] = k
    if len(ev) < min_evaluable:
        out["missing_reason"] = "TOO_FEW_EVALUABLE"
        return out
    rets = [r for _, _, r in ev]
    med = Decimal(str(median(rets)))
    out["universe_median_gross"] = _q(med)
    out["rank_ic"] = spearman([-rk for _, rk, _ in ev], rets)
    top = [returns.get(iid) for iid, _ in ordered[:k]]
    top_ok = [r for r in top if r is not None]
    out["top_evaluable"] = len(top_ok)
    if top_ok:
        mean = sum(top_ok, Decimal(0)) / len(top_ok)
        out["top_decile_mean_gross"] = _q(mean)
        out["top_decile_excess"] = _q(mean - med)
    if out["rank_ic"] is None or out["top_decile_excess"] is None:
        out["missing_reason"] = "NOT_COMPUTABLE"
    return out
