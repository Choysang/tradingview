"""Panel-level helpers: cross-sectional ranks, market-wide daily aggregates and weekly/monthly period lines.

Why: some playbooks (decile factors, composites, sentiment-cycle timing) need other stocks' same-day values. Those columns
are computed HERE once per panel, from same-date-and-earlier data only, and attached to every InstrumentView, so the signal
functions stay pure per-instrument functions (and the truncation test covers them).
Invariants: a rank on date d uses only values at date d; a rolling quantile of market counts uses dates <= d (the rule
decides whether d itself is included - see ``ice_flags``); a cross-section thinner than ``min_names`` yields no rank
(None), never a rank of a handful of names; weekly/monthly values become visible only on the first bar of the NEXT
period (so a panel cut mid-period gives the same answer as the full one).
Not covered: any data outside the price panel (sector membership, fundamentals, seats).
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date

from pulse.domain.features import indicators as ind

from .spec import XSpec
from .view import InstrumentView, ok

ICE_WINDOW = 250


def attach_cross_sections(views: Sequence[InstrumentView], sessions: Sequence[str], specs: Sequence[XSpec]) -> None:
    """Fill ``view.xs`` for every spec (idempotent per key)."""
    pos = [{d: i for i, d in enumerate(v.dates)} for v in views]
    for spec in specs:
        if views and f"{spec.key}.{next(iter(spec.metrics))}" in views[0].xs:
            continue
        for v in views:
            for m in spec.metrics:
                v.xs[f"{spec.key}.{m}"] = [None] * v.n
            if spec.composite:
                v.xs[f"{spec.key}.score"] = [None] * v.n
            if spec.date_flag:
                v.xs[f"{spec.key}.flag"] = [None] * v.n
        for d in sessions:
            rows: list[tuple[int, int, dict[str, float]]] = []
            for k, (v, p) in enumerate(zip(views, pos)):
                i = p.get(d)
                if i is None or not spec.eligible(v, i):
                    continue
                vals = {}
                good = True
                for m, fn in spec.metrics.items():
                    x = fn(v, i)
                    if x is None or not math.isfinite(x):
                        good = False
                        break
                    vals[m] = x
                if good:
                    rows.append((k, i, vals))
            n = len(rows)
            if n < spec.min_names:
                continue
            if spec.date_flag:
                flag = spec.date_flag([vals for _, _, vals in rows])
                for k, i, _ in rows:
                    views[k].xs[f"{spec.key}.flag"][i] = flag
            ranks: dict[str, list[int]] = {}
            for m in spec.metrics:
                order = sorted(range(n), key=lambda r: (rows[r][2][m], views[rows[r][0]].instrument_id))
                rk = [0] * n
                for place, r in enumerate(order):
                    rk[r] = place + 1
                ranks[m] = rk
                for r, (k, i, _) in enumerate(rows):
                    views[k].xs[f"{spec.key}.{m}"][i] = (rk[r], n)
            if spec.composite:
                score = [sum(ranks[m][r] / n for m in spec.composite) / len(spec.composite) for r in range(n)]
                order = sorted(range(n), key=lambda r: (-score[r], views[rows[r][0]].instrument_id))
                for place, r in enumerate(order):
                    k, i, _ = rows[r]
                    views[k].xs[f"{spec.key}.score"][i] = (place + 1, n)


def market_aggregates(views: Sequence[InstrumentView], sessions: Sequence[str], *, high_streak: float = 5,
                      ice_pct: float = 90, min_history: int = 60) -> dict[str, dict[str, bool | None]]:
    """Per-date market flags used by ``ice_point_reflex`` and ``high_low_switch`` (all from dates <= d, then lagged where the
    rule says "yesterday"). Returns {"ice": {date: bool|None}, "switch": {date: bool|None}}."""
    ld = {d: 0 for d in sessions}
    lu = {d: 0 for d in sessions}
    top = {d: 0.0 for d in sessions}
    fb = {d: 0 for d in sessions}
    for v in views:
        dn, up, st = v.limit_down(), v.limit_up(), v.limit_streak()
        for i, d in enumerate(v.dates):
            if dn[i]:
                ld[d] += 1
            if up[i]:
                lu[d] += 1
            if st[i] is not None:
                top[d] = max(top[d], st[i])
                if st[i] == 1:
                    fb[d] += 1
    ice: dict[str, bool | None] = {}
    switch: dict[str, bool | None] = {}
    series = [ld[d] for d in sessions]
    for k, d in enumerate(sessions):
        if k == 0:
            ice[d] = switch[d] = None
            continue
        hist = series[max(0, k - ICE_WINDOW):k]                    # up to YESTERDAY, inclusive of yesterday
        if len(hist) < min_history:
            ice[d] = None
        else:
            s = sorted(hist)
            q = s[min(len(s) - 1, math.ceil(ice_pct / 100.0 * len(s)) - 1)]
            ice[d] = bool(ld[sessions[k - 1]] >= q and ld[sessions[k - 1]] > 0 and lu[d] > lu[sessions[k - 1]])
        switch[d] = bool(top[d] < high_streak and top[sessions[k - 1]] >= high_streak and fb[d] >= fb[sessions[k - 1]])
    return {"ice": ice, "switch": switch}


def pearson(x: Sequence[float | None], y: Sequence[float | None], n: int, i: int) -> float | None:
    """Rolling correlation over the n bars ending at i."""
    if i + 1 < n:
        return None
    a, b = x[i - n + 1:i + 1], y[i - n + 1:i + 1]
    if any(t is None for t in a) or any(t is None for t in b):
        return None
    ma, mb = sum(a) / n, sum(b) / n
    sa = math.sqrt(sum((t - ma) ** 2 for t in a))
    sb = math.sqrt(sum((t - mb) ** 2 for t in b))
    if sa <= 0 or sb <= 0:
        return None
    return sum((s - ma) * (t - mb) for s, t in zip(a, b)) / (sa * sb)


def _period_key(day: str, kind: str):
    d = date.fromisoformat(day)
    return d.isocalendar()[:2] if kind == "w" else (d.year, d.month)


def period_lines(v: InstrumentView, kind: str, min_periods: int) -> dict[str, list]:
    """Weekly ("w") / monthly ("m") MACD(12,26,9)/MA5-10 state of the latest COMPLETED period, visible from the first bar of the
    next period: lists ``up`` (DIF>DEA), ``golden`` (DIF crossed above DEA in that period), ``ma_golden`` (MA5 crossed above
    MA10), ``fresh`` (this bar is the first on which that period is known)."""
    keys = [_period_key(d, kind) for d in v.dates]
    closes: list[float | None] = []
    ends: list[int] = []                                   # bar index of each period's last bar
    for i, k in enumerate(keys):
        if i + 1 == len(keys) or keys[i + 1] != k:
            closes.append(v.close[i])
            ends.append(i)
    m = ind.macd(closes, 12, 26, 9, "sma_seed", 1)
    ma5, ma10 = ind.ma(closes, 5), ind.ma(closes, 10)
    n = v.n
    out = {"up": [None] * n, "golden": [None] * n, "ma_golden": [None] * n, "fresh": [False] * n}
    period_of_bar: list[int] = []
    p = 0
    for i, k in enumerate(keys):
        period_of_bar.append(p)
        if ends[p] == i:
            p += 1
    last_known = -1
    for i in range(n):
        done = period_of_bar[i] - 1                         # last completed period at bar i
        if done < min_periods - 1 or done < 1:
            continue
        dif, dea, pd, pe = m["macd"][done], m["signal"][done], m["macd"][done - 1], m["signal"][done - 1]
        if ok(dif, dea):
            out["up"][i] = dif > dea
            out["golden"][i] = bool(ok(pd, pe) and pd <= pe and dif > dea)
        if ok(ma5[done], ma10[done], ma5[done - 1], ma10[done - 1]):
            out["ma_golden"][i] = bool(ma5[done - 1] <= ma10[done - 1] and ma5[done] > ma10[done])
        if done != last_known:
            out["fresh"][i] = True
            last_known = done
    return out
