"""Same-day style-matched baseline (ledger E062; the old ``sameday_base.add_style`` / ``evaluate_matched`` as SPECIFICATION).

Why: E062 ("works", verdict on the signals: fails) showed that most of the excess of "effective" daily signals is size / volatility style return, so
the standard baseline for every future "effective" verdict is the SAME-DAY pool mean of names in the SAME float-cap quintile x SAME 20-day
volatility quintile, optionally also the same industry. This module is that rule as a pure utility (no IO, no clock, no float: ``Decimal`` ratios).
Rules (``BASELINE_VERSION``):
  * Pool: for one decision day, the tradable names that have a known forward return (the caller applies the tradability filter; the same filter
    applies to signals and baseline). The signal names are part of the pool (as in the old script).
  * Quintile of a value among the non-missing values of the day: ``ceil(5 * rank / count)`` clipped to 1..5, ``rank`` = average rank (1-based; ties
    share the mean of their positions), ``count`` = number of non-missing values. A missing value has no quintile (the name cannot be matched).
  * Three baselines per name, all means of the pool's forward return on the same day: ``all`` (whole pool), ``size`` (same size quintile), ``sv``
    (same size x volatility cell, the main one), ``svi`` (same size x volatility x industry cell). A cell with fewer than ``MIN_CELL`` (5) names
    falls back to the coarser cell: svi -> sv -> size. A missing industry is its own group ``NA`` (the old script's fill).
  * Excess of a signal name = its return minus its baseline. ``matched_excess`` returns the mean over all signal names (name-weighted, as in the
    old report) and the per-day means with the signal counts, which is what ``bootstrap.block_bootstrap_p`` consumes.
  * Pre-registered survival rule (E062): ``survives`` = PRE matched excess (sv) > 0 with p < 0.05 AND DEV matched excess (sv) >= 0.
Missing is missing: a signal name without a quintile (no cap or no volatility) is skipped and counted in ``unmatched``, never given a baseline of 0.
Not covered: the tradability filter, the float-cap and volatility computations (inputs), costs (both sides pay the same, so they do not change the
excess), the bootstrap itself (``bootstrap.py``).
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

BASELINE_VERSION = "same-day-style-matched-v1"
MIN_CELL = 5
NO_INDUSTRY = "NA"
D = Decimal


@dataclass(frozen=True)
class PoolName:
    """One pool name on one day: forward return (ratio) and the quintiles of its style (None = could not be classified)."""
    key: str
    ret: Decimal
    size5: int | None
    vol5: int | None
    industry: str | None = None


@dataclass(frozen=True)
class DayExcess:
    day: str
    k: int
    ex_all: Decimal
    ex_sv: Decimal
    ex_svi: Decimal


@dataclass(frozen=True)
class MatchedExcess:
    n: int                      # signal names matched
    unmatched: int              # signal names with no quintile or no pool entry (not counted in n)
    days: int
    ex_all: Decimal | None
    ex_sv: Decimal | None
    ex_svi: Decimal | None
    by_day: tuple[DayExcess, ...]


def quintiles(values: Sequence[Decimal | None]) -> list[int | None]:
    """Quintile 1..5 of every non-missing value among the non-missing values (average rank; see the module docstring)."""
    present = [(i, v) for i, v in enumerate(values) if v is not None]
    out: list[int | None] = [None] * len(values)
    count = len(present)
    if count == 0:
        return out
    order = sorted(present, key=lambda t: t[1])
    j = 0
    while j < count:
        k = j
        while k + 1 < count and order[k + 1][1] == order[j][1]:
            k += 1
        rank = D(j + k + 2) / 2                         # mean of the 1-based positions j+1 .. k+1
        q = min(5, max(1, math.ceil(rank * 5 / count)))
        for m in range(j, k + 1):
            out[order[m][0]] = q
        j = k + 1
    return out


def _mean(xs: Sequence[Decimal]) -> Decimal:
    return sum(xs, D(0)) / len(xs)


def matched_means(pool: Sequence[PoolName], *, min_cell: int = MIN_CELL) -> dict[str, dict[str, Decimal]]:
    """Per pool name key: its ``all`` / ``size`` / ``sv`` / ``svi`` baseline means (names without both quintiles get only ``all``)."""
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = 50, ROUND_HALF_EVEN
        out: dict[str, dict[str, Decimal]] = {}
        if not pool:
            return out
        m_all = _mean([p.ret for p in pool])
        by_s: dict[int, list[Decimal]] = {}
        by_sv: dict[tuple[int, int], list[Decimal]] = {}
        by_svi: dict[tuple[int, int, str], list[Decimal]] = {}
        for p in pool:
            if p.size5 is None or p.vol5 is None:
                continue
            by_s.setdefault(p.size5, []).append(p.ret)
            by_sv.setdefault((p.size5, p.vol5), []).append(p.ret)
            by_svi.setdefault((p.size5, p.vol5, p.industry or NO_INDUSTRY), []).append(p.ret)
        for p in pool:
            row = {"all": m_all}
            if p.size5 is not None and p.vol5 is not None:
                size = _mean(by_s[p.size5])
                cell = by_sv[(p.size5, p.vol5)]
                sv = _mean(cell) if len(cell) >= min_cell else size
                icell = by_svi[(p.size5, p.vol5, p.industry or NO_INDUSTRY)]
                svi = _mean(icell) if len(icell) >= min_cell else sv
                row.update({"size": size, "sv": sv, "svi": svi})
            out[p.key] = row
        return out


def matched_excess(signals_by_day: Mapping[str, Sequence[str]], pool_by_day: Mapping[str, Sequence[PoolName]], *,
                   min_cell: int = MIN_CELL) -> MatchedExcess:
    """Excess of the signal names over the three same-day baselines (``all``, ``sv``, ``svi``), overall and per day.

    ``signals_by_day`` maps a day to the keys of the names signalled on it; a key absent from that day's pool, or without both quintiles, is
    ``unmatched``."""
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = 50, ROUND_HALF_EVEN
        tot = {"all": D(0), "sv": D(0), "svi": D(0)}
        n = unmatched = 0
        by_day: list[DayExcess] = []
        for day in sorted(signals_by_day):
            pool = pool_by_day.get(day, ())
            means = matched_means(pool, min_cell=min_cell)
            ret = {p.key: p.ret for p in pool}
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
                by_day.append(DayExcess(day, k, day_sum["all"] / k, day_sum["sv"] / k, day_sum["svi"] / k))
                for name in tot:
                    tot[name] += day_sum[name]
                n += k
        if n == 0:
            return MatchedExcess(0, unmatched, 0, None, None, None, ())
        return MatchedExcess(n, unmatched, len(by_day), tot["all"] / n, tot["sv"] / n, tot["svi"] / n, tuple(by_day))


def survives(pre_ex_sv: Decimal | None, pre_p: float | None, dev_ex_sv: Decimal | None) -> bool:
    """E062 pre-registered rule: PRE matched excess > 0 with p < 0.05 and DEV matched excess >= 0. Any missing figure -> False (never a pass)."""
    if pre_ex_sv is None or pre_p is None or dev_ex_sv is None:
        return False
    return pre_ex_sv > 0 and pre_p < 0.05 and dev_ex_sv >= 0
