"""Analog study: nearest historical setups of an instrument, selected by features only, with a forward-return distribution.

Why: LEGACY inventory "Analogs (相似情景)": the target's latest state is described by six standardised features
(5/20-day return, 20-day return volatility, distance to the 60-day high, volume over its 20-day mean, close over MA20),
the K nearest historical states of other stocks (and the same stock long ago) are found, de-overlapped, and the
EMPIRICAL distribution of what followed is shown. (The old file, read as historical evidence of the method, is not copied.)
Invariants (PEX04 acceptance): neighbours are chosen from feature distance ONLY - a candidate's outcome never influences
selection; eligibility depends on dates, never on results, so adding future bars cannot change an old study; an outcome is
visible only when ``outcome_end`` <= the query ``decision_as_of`` date, else it is None (immature, never 0);
same-stock cases younger than SAME_CODE_MIN_AGE bars are excluded and each stock's picks are at least ``horizon`` bars
apart (adjacent days of one stock are not independent samples); the output is an empirical frequency table, never a
calibrated probability. Features use shared InstrumentView kernels.
Policies: ``analog-knn-matured-v1`` (candidates whose outcome is already known at decision_as_of) and
``analog-knn-all-prior-v1`` (every earlier case; immature ones keep outcome None).
Not covered: sector/market-regime conditioning, learned embeddings (no model is trained).
"""
from __future__ import annotations

import heapq
import math
from collections.abc import Sequence
from dataclasses import dataclass

from ..playbooks.view import InstrumentView, ok

POLICY_MATURED = "analog-knn-matured-v1"
POLICY_ALL_PRIOR = "analog-knn-all-prior-v1"
POLICIES = (POLICY_MATURED, POLICY_ALL_PRIOR)
FEATURES = ("ret_5", "ret_20", "volatility_20", "dist_60d_high", "volume_ratio", "ma20_gap")
NEIGHBORS = 200
OVERSAMPLE = (5, 20)
SAME_CODE_MIN_AGE = 120
Z_CLIP = 5.0
MIN_HORIZON, MAX_HORIZON = 5, 60


@dataclass(frozen=True)
class Neighbor:
    instrument_id: str
    case_date: str
    index: int
    distance: float
    outcome_end: str                 # session date of the outcome bar
    forward_return: float | None     # ratio; None when immature at decision_as_of or unavailable


def features_at(v: InstrumentView, i: int) -> tuple[float, ...] | None:
    r5, r20, sd = v.ret(5)[i], v.ret(20)[i], v.ret_std(20)[i]
    h60, vr, m20, c = v.hhv("high", 60)[i], v.volume_ratio20()[i], v.ma(20)[i], v.close[i]
    if not ok(r5, r20, sd, h60, vr, m20, c) or h60 <= 0 or m20 <= 0:
        return None
    return (r5, r20, sd * 100.0, (c / h60 - 1.0) * 100.0, vr, (c / m20 - 1.0) * 100.0)


def _stats(views: Sequence[InstrumentView], eligible) -> tuple[list[float], list[float]]:
    n = 0
    s = [0.0] * 6
    ss = [0.0] * 6
    for v, j in eligible(views):
        f = features_at(v, j)
        if f is None:
            continue
        n += 1
        for k in range(6):
            s[k] += f[k]
            ss[k] += f[k] * f[k]
    if n < 2:
        return [0.0] * 6, [1.0] * 6
    mean = [x / n for x in s]
    std = [math.sqrt(max(ss[k] / n - mean[k] ** 2, 0.0)) or 1.0 for k in range(6)]
    return mean, [x if x > 1e-12 else 1.0 for x in std]


def find_neighbors(views: Sequence[InstrumentView], query: InstrumentView, query_index: int, *, horizon: int,
                   decision_date: str, policy: str = POLICY_MATURED, k: int = NEIGHBORS) -> list[Neighbor]:
    """Deterministic neighbours of ``query`` at ``query_index``; ``decision_date`` is the iso session date of the study."""
    if policy not in POLICIES:
        raise ValueError(f"unknown selection policy {policy!r}")
    if not MIN_HORIZON <= horizon <= MAX_HORIZON:
        raise ValueError(f"horizon must be within {MIN_HORIZON}..{MAX_HORIZON}")
    target = features_at(query, query_index)
    if target is None:
        return []

    def eligible(vs):
        for v in vs:
            same = v.instrument_id == query.instrument_id
            for j in range(v.n):
                d = v.dates[j]
                if d >= decision_date:
                    break                                   # a case must be strictly before the study date
                if same and j > query_index - SAME_CODE_MIN_AGE:
                    break
                if j + horizon >= v.n:
                    continue                                # an outcome bar must exist (its date may still be after the study date)
                if policy == POLICY_MATURED and v.dates[j + horizon] > decision_date:
                    continue
                yield v, j

    mean, std = _stats(views, eligible)
    z = lambda f: [max(-Z_CLIP, min(Z_CLIP, (f[x] - mean[x]) / std[x])) for x in range(6)]  # noqa: E731
    zt = z(target)
    best: list[tuple[float, str, int, int]] = []         # (distance, instrument, index, view position)
    pool = k * OVERSAMPLE[-1]
    order = {id(v): n for n, v in enumerate(views)}
    for v, j in eligible(views):
        f = features_at(v, j)
        if f is None:
            continue
        zf = z(f)
        dist = math.sqrt(sum((a - b) ** 2 for a, b in zip(zf, zt)))
        item = (-dist, v.instrument_id, -j, order[id(v)])
        if len(best) < pool:
            heapq.heappush(best, item)
        elif item > best[0]:
            heapq.heapreplace(best, item)
    cand = sorted(((-d, iid, -nj, pos) for d, iid, nj, pos in best))
    chosen: list[Neighbor] = []
    taken: dict[str, list[int]] = {}
    for dist, iid, j, pos in cand:
        if any(abs(j - o) < horizon for o in taken.get(iid, ())):
            continue
        taken.setdefault(iid, []).append(j)
        v = views[pos]
        end = v.dates[j + horizon]
        mature = end <= decision_date
        fr = None
        if mature and ok(v.close[j], v.close[j + horizon]) and v.close[j] > 0:
            fr = v.close[j + horizon] / v.close[j] - 1.0
        chosen.append(Neighbor(iid, v.dates[j], j, dist, end, fr))
        if len(chosen) >= k:
            break
    return chosen


def distribution(neighbors: Sequence[Neighbor]) -> dict:
    """Empirical table of KNOWN outcomes (immature ones are counted, not imputed). Not a calibrated probability."""
    known = sorted(n.forward_return for n in neighbors if n.forward_return is not None)
    out: dict = {"neighbors": len(neighbors), "known_outcomes": len(known), "immature": len(neighbors) - len(known)}
    if not known:
        return out | {"up_frequency": None, "mean": None, "quantiles": {}}

    def q(p: float) -> float:
        x = p * (len(known) - 1)
        lo, hi = math.floor(x), math.ceil(x)
        return known[lo] + (known[hi] - known[lo]) * (x - lo)

    return out | {"up_frequency": sum(1 for x in known if x > 0) / len(known), "mean": sum(known) / len(known),
                  "quantiles": {p: q(p / 100.0) for p in (10, 25, 50, 75, 90)}}
