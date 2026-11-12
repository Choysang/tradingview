"""Forward outcome of a published ranking (PEX02 ``pulse.ranking_outcome``): realised returns, no hindsight.

Why: the old site's 以史为鉴 compared recorded picks with later returns. Here a ranking is immutable; its
outcome is a separate artifact computed from the unified raw daily bars once the horizon has been observed,
and *pending* until then. Nothing is filled early and the original ranking is never rewritten.
Method (``OUTCOME_VERSION``): the ranking is formed on session s (``decision_session``). Entry = raw open of the
next session s+1; exit = raw close of session s+h where h is the horizon in sessions; the ranking is pending
until session s+h exists in the observed calendar. Gross return = exit / entry - 1. Net of the declared
round-trip cost ratio. Excess = gross - cost - median(gross over every evaluable ranked instrument).
Opportunity denominator: the preregistered top-N (and bottom-N) of the ranking; instruments that cannot be
evaluated (no entry/exit bar, or a price path with an impossible move such as ex-rights) stay in the
denominator of ``*_over_opportunities`` and are counted in ``unevaluable``; they are never averaged as zero.
The bottom-N group is reported as a long-only descriptive return of the avoided names; it is NOT converted
into an unverified short return.
Not covered: ex-rights adjusted returns, order / fill simulation (see the execution kernel), fees beyond the
declared ratio.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from statistics import median
from typing import Any, Mapping, Sequence

from .daily import Bar

OUTCOME_VERSION = "ranking-forward-outcome-v1"
D = Decimal
PATH_GUARD = D("0.111")                 # a daily raw move beyond 11.1% on the main board is not a price move
TOP_N = 10


@dataclass(frozen=True)
class PathResult:
    iid: str
    gross: Decimal | None
    reason: str | None                  # None when evaluable


def _q(x: Decimal | None, places: str = "0.000001") -> Decimal | None:
    return None if x is None else x.quantize(D(places), rounding=ROUND_HALF_EVEN)


def path_return(iid: str, bars: Sequence[Bar], entry_day: date, exit_day: date) -> PathResult:
    by = {b.d: b for b in bars}
    e, x = by.get(entry_day), by.get(exit_day)
    if e is None or e.o is None or e.o <= 0:
        return PathResult(iid, None, "NO_ENTRY_BAR")
    if x is None or x.c is None or x.c <= 0:
        return PathResult(iid, None, "NO_EXIT_BAR")
    inside = [b for b in bars if entry_day <= b.d <= exit_day and b.c is not None]
    prev = None
    for b in sorted(inside, key=lambda b: b.d):
        if prev is not None and prev.c and abs(b.c / prev.c - 1) > PATH_GUARD:        # type: ignore[operator]
            return PathResult(iid, None, "UNVERIFIABLE_PRICE_PATH")
        prev = b
    return PathResult(iid, x.c / e.o - 1, None)


def metric(name: str, value: Decimal | None, unit: str, n: int, missing: str | None, version: str = OUTCOME_VERSION) -> dict[str, Any]:
    return {"name": name, "definition_version": version, "value": value, "unit": unit, "sample_count": n,
            "missing_reason": None if value is not None else (missing or "NOT_COMPUTABLE")}


def evaluate(*, ranking_id: str, release_id: str, horizon_id: str, horizon_sessions: int, decision_session: date,
             ranked: Sequence[tuple[str, int]], series: Mapping[str, Sequence[Bar]], sessions: Sequence[date],
             cost_ratio: Decimal, cost_version: str, top_n: int = TOP_N) -> dict[str, Any]:
    """Outcome body for one ranking at the observed calendar ``sessions`` (ascending)."""
    cal = sorted(set(sessions))
    head = {"ranking_id": ranking_id, "release_id": release_id, "horizon_id": horizon_id, "horizon_sessions": horizon_sessions,
            "decision_session": decision_session, "top_n": top_n, "method_version": OUTCOME_VERSION, "cost_version": cost_version,
            "cost_ratio": cost_ratio, "observed_through": cal[-1] if cal else None}
    if decision_session not in cal:
        return {**head, "status": "pending", "entry_session": None, "exit_session": None, "metrics": _pending_metrics(top_n),
                "unevaluable": {}, "reason": "DECISION_SESSION_NOT_OBSERVED"}
    if cal.index(decision_session) + horizon_sessions > len(cal) - 1:
        return {**head, "status": "pending", "entry_session": None, "exit_session": None, "metrics": _pending_metrics(top_n),
                "unevaluable": {}, "reason": "HORIZON_NOT_REACHED"}
    i = cal.index(decision_session)
    entry_day, exit_day = cal[i + 1], cal[i + horizon_sessions]
    ordered = sorted(ranked, key=lambda t: t[1])
    results = {iid: path_return(iid, series.get(iid, ()), entry_day, exit_day) for iid, _ in ordered}
    gross_all = [r.gross for r in results.values() if r.gross is not None]
    med = D(str(median(gross_all))) if gross_all else None
    top, bottom = [iid for iid, _ in ordered[:top_n]], [iid for iid, _ in ordered[-top_n:]]

    def group(ids: Sequence[str], tag: str) -> list[dict[str, Any]]:
        ok = [results[i].gross for i in ids if results[i].gross is not None]
        excess = [g - cost_ratio - med for g in ok] if med is not None else []
        n_all = len(ids)
        mean_gross = sum(ok, D(0)) / len(ok) if ok else None
        mean_ex = sum(excess, D(0)) / len(excess) if excess else None
        hits = sum(1 for e in excess if e > 0)
        return [
            metric(f"{tag}_mean_gross_return", _q(mean_gross), "ratio", len(ok), "NO_EVALUABLE_INSTRUMENT"),
            metric(f"{tag}_mean_excess_return", _q(mean_ex), "ratio", len(excess), "NO_EVALUABLE_INSTRUMENT"),
            metric(f"{tag}_hit_rate_over_evaluable", _q(D(hits) / len(excess)) if excess else None, "ratio", len(excess), "NO_EVALUABLE_INSTRUMENT"),
            metric(f"{tag}_hit_rate_over_opportunities", _q(D(hits) / n_all) if n_all else None, "ratio", n_all, "NO_OPPORTUNITY"),
            metric(f"{tag}_opportunity_count", D(n_all), "count", n_all, None),
            metric(f"{tag}_evaluable_count", D(len(ok)), "count", n_all, None),
        ]

    reasons: dict[str, int] = {}
    for iid in top + bottom:
        r = results[iid].reason
        if r:
            reasons[r] = reasons.get(r, 0) + 1
    metrics = (group(top, "top") + group(bottom, "avoid_descriptive_long_only")
               + [metric("universe_median_gross_return", _q(med), "ratio", len(gross_all), "NO_EVALUABLE_INSTRUMENT"),
                  metric("universe_evaluable_count", D(len(gross_all)), "count", len(ordered), None)])
    return {**head, "status": "resolved", "entry_session": entry_day, "exit_session": exit_day, "metrics": metrics,
            "unevaluable": reasons, "reason": None,
            "members": [{"instrument_id": iid, "alpha_rank": rk, "gross_return": _q(results[iid].gross), "reason": results[iid].reason}
                        for iid, rk in ordered[:top_n]]}


def _pending_metrics(top_n: int) -> list[dict[str, Any]]:
    names = ["top_mean_gross_return", "top_mean_excess_return", "top_hit_rate_over_evaluable", "top_hit_rate_over_opportunities",
             "avoid_descriptive_long_only_mean_gross_return", "avoid_descriptive_long_only_mean_excess_return",
             "universe_median_gross_return"]
    return [metric(n, None, "ratio", 0, "HORIZON_NOT_REACHED") for n in names]
