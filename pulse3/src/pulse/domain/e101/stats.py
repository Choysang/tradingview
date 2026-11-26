"""E101 statistics: H1 (D0 market return vs same-weekday non-event days), H2 (R gap against the surprise sign), H3 / H4 (group excess over the E062 baseline).

Why: pre-registration sections 1, 3, 4. The forward judge and the historical robustness run call these functions on the same records, so a number is computed one way only.
All p-values: E082 moving-block bootstrap (block 5 sessions, seed 20261125, 10,000 draws), one-sided, slots = every session of the window; the bootstrapped value is oriented so that
the pre-registered direction is positive (``block_bootstrap_p`` tests "mean <= 0").
Inputs are plain records: ``market`` = session -> (equal-weight return, equal-weight open gap) of the main board; events are ``rules.Event``; group records are
(event key, decision day, group, code, excess) per filled, matched name.
Not covered: building the records (``application.bf_e101``), Holm and the size rule (``rules.judge``).
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from pulse.domain.e101 import rules as R
from pulse.domain.research import bootstrap as BS


@dataclass(frozen=True)
class Effect:
    effect: float | None
    p_value: float | None
    n: int                         # events (H1, H2) or matched names (H3, H4)
    days: int                      # decision days carrying the statistic
    blocks: int
    reliable: bool
    note: str | None = None
    extra: Mapping | None = None

    def wire(self) -> dict:
        return {k: getattr(self, k) for k in ("effect", "p_value", "n", "days", "blocks", "reliable", "note", "extra")}


def _boot(slots: Sequence[str], per_day: Mapping[str, tuple[int, float]], sign: int) -> BS.BootstrapResult:
    days = sorted(per_day)
    return BS.block_bootstrap_p(list(slots), days, [sign * per_day[d][1] for d in days], [per_day[d][0] for d in days], block=R.BLOCK, seed=R.SEED, n_boot=R.N_BOOT)


def _day_means(values: Iterable[tuple[str, float]]) -> dict[str, tuple[int, float]]:
    acc: dict[str, list[float]] = defaultdict(list)
    for d, v in values:
        acc[d].append(v)
    return {d: (len(v), sum(v) / len(v)) for d, v in acc.items()}


def weekday_controls(market_r: Mapping[str, float], slots: Sequence[str], event_days: set[str]) -> dict[int, float]:
    """Mean market return of the non-event slots per ISO weekday."""
    acc: dict[int, list[float]] = defaultdict(list)
    for d in slots:
        if d not in event_days and d in market_r:
            acc[date.fromisoformat(d).isoweekday()].append(market_r[d])
    return {w: sum(v) / len(v) for w, v in acc.items() if v}


def h1(market_r: Mapping[str, float], events: Sequence[R.Event], slots: Sequence[str], event_days: set[str]) -> Effect:
    """effect = mean over events of (D0 return - same-weekday control); bootstrap of (control - D0 return), weight = events per D0."""
    ctl = weekday_controls(market_r, slots, event_days)
    diffs, absd, absc, skipped = [], [], [], 0
    for e in events:
        d = e.d0.isoformat()
        w = e.d0.isoweekday()
        if d not in market_r or w not in ctl:
            skipped += 1
            continue
        diffs.append((d, market_r[d] - ctl[w]))
        absd.append(abs(market_r[d]))
    if not diffs:
        return Effect(None, None, 0, 0, 0, False, "no_event_with_market_return", {"skipped": skipped})
    non = [abs(market_r[s]) for s in slots if s not in event_days and s in market_r]
    effect = sum(v for _, v in diffs) / len(diffs)
    bs = _boot(slots, _day_means(diffs), -1)
    extra = {"skipped": skipped, "mean_abs_d0": sum(absd) / len(absd), "mean_abs_non_event": (sum(non) / len(non)) if non else None,
             "non_event_days": len(non), "controls_by_weekday": ctl}
    return Effect(effect, bs.p_value, len(diffs), len({d for d, _ in diffs}), bs.blocks, bs.reliable, bs.reason, extra)


def h2(gaps: Mapping[str, float], events: Sequence[tuple[R.Event, float]], slots: Sequence[str]) -> Effect:
    """``events`` = (event, s) with a consensus; s = 0 carries no sign and is left out. effect = mean of sign(s) x R gap; bootstrap of -(sign x gap) on R days."""
    vals = []
    for e, s in events:
        if s == 0 or e.r is None or e.r.isoformat() not in gaps:
            continue
        vals.append((e.r.isoformat(), (1.0 if s > 0 else -1.0) * gaps[e.r.isoformat()]))
    if not vals:
        return Effect(None, None, 0, 0, 0, False, "no_event_with_consensus_and_gap")
    effect = sum(v for _, v in vals) / len(vals)
    bs = _boot(slots, _day_means(vals), -1)
    return Effect(effect, bs.p_value, len(vals), len({d for d, _ in vals}), bs.blocks, bs.reliable, bs.reason)


GroupRec = tuple[str, str, str, str, float]            # (event key, decision day, group, code, excess)


def group_excess(recs: Sequence[GroupRec], group: str, slots: Sequence[str]) -> Effect:
    """Name-weighted mean excess of ``group``; a name of one decision day is counted once even when two events share that day (same leg, same baseline)."""
    seen: dict[tuple[str, str], float] = {}
    for _, day, g, code, ex in recs:
        if g == group:
            seen.setdefault((day, code), ex)
    per = _day_means((day, ex) for (day, _), ex in seen.items())
    if not per:
        return Effect(None, None, 0, 0, 0, False, "no_matched_names")
    n = sum(v[0] for v in per.values())
    effect = sum(v[0] * v[1] for v in per.values()) / n
    bs = _boot(slots, per, 1)
    return Effect(effect, bs.p_value, n, len(per), bs.blocks, bs.reliable, bs.reason)


def two_groups(recs: Sequence[GroupRec], slots: Sequence[str]) -> tuple[dict[str, Effect], float | None]:
    """Both groups and the intersection-union p (the larger p; None when a group is untested)."""
    eff = {g: group_excess(recs, g, slots) for g in R.GROUPS}
    ps = [e.p_value for e in eff.values()]
    return eff, (None if any(p is None for p in ps) else max(ps))
