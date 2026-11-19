"""E077 tail-of-day picks and E078 first/last half hour (pre-registration ``2026-11-08-batch4-preregistration.md``, sections 3 and 4).

Why: two forward experiments the old server runs every session from the 60 s snapshots; they move to pulse3 with the rules UNCHANGED.
E077 pool = main board, name without ST, price >= 2, below the limit-up price, previous close > 0, at the 14:50 snapshot. Signal (all
together): change in [3, 5] percent at 14:30 AND 14:50, volume ratio >= 1, turnover in [5, 10] percent, float cap <= 200 (亿元),
a close >= +9.7 percent among the 20 sessions before the day; the top 5 by volume ratio (ties by code). Buy at the 14:50 price, sell at
the next session's 10:00 price; the control is the same float-cap quintile of the pool; cost 0.252 percent round trip.
E078: x = 10:00 / previous close - 1, x2 = 10:00 / open - 1, y = close / 14:30 - 1 as means over the codes present at every time point.
Invariants: Decimal arithmetic; a missing price is None and is skipped by a mean, never read as 0; the inputs are the snapshots a caller
selected at or after the clock time (``application.bf_fwdlog_inputs``), so nothing later than the decision is read.
Not covered: choosing the snapshot (the caller does), the 40-day / 60-day decision statistics (``stats``).
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, Decimal
from typing import Mapping, Sequence

from .common import D, HUNDRED, ZERO, mean, pct, q

PCT_LO, PCT_HI = D(3), D(5)
VR_MIN = D(1)
TO_LO, TO_HI = D(5), D(10)
CAP_MAX = D(200)
PRICE_MIN = D(2)
LIMIT_MOVE = D("0.097")
LOOK = 20
TOP = 5
COST_PCT = D("0.252")         # percent, round trip (USER_COSTS)
START = date(2026, 11, 2)     # first decision day of the old log
E077_CHECK_AT, E078_CHECK_AT = 40, 60
E077_MIN_SIGNAL_DAYS = 25


@dataclass(frozen=True)
class Quote:
    """The columns of one recorded snapshot these rules read."""
    code: str
    name: str | None
    price: Decimal | None
    prev_close: Decimal | None = None
    open: Decimal | None = None
    limit_up: Decimal | None = None
    turnover: Decimal | None = None
    float_cap: Decimal | None = None
    volume_ratio: Decimal | None = None


def limit_history(closes: Mapping[str, Sequence[tuple[date, Decimal | None]]], day: date) -> frozenset[str]:
    """Codes with a close up by at least 9.7 percent on some session among the ``LOOK`` sessions before ``day``.

    ``closes``: main-board code -> (date, close) rows. The sessions are the last LOOK + 1 distinct dates before ``day`` that any code has; the
    return of a row is against the previous row of the same code inside that window, so the first session only seeds the next one."""
    dates = sorted({d for rows in closes.values() for d, _ in rows if d < day})[-(LOOK + 1):]
    if len(dates) < 2:
        return frozenset()
    window = set(dates)
    first = dates[0]
    out = set()
    for code, rows in closes.items():
        prev = None
        for d, c in sorted((r for r in rows if r[0] in window and r[1] is not None), key=lambda r: r[0]):
            if prev is not None and d > first and prev > ZERO and c / prev - 1 >= LIMIT_MOVE:
                out.add(code)
                break
            prev = c
    return frozenset(out)


def quintiles(float_caps: Mapping[str, Decimal | None]) -> dict[str, int | None]:
    """cap5 = ceil(average rank / count * 5) clipped to 1..5 over the pool's non-missing float caps; a missing cap has no quintile."""
    vals = {c: v for c, v in float_caps.items() if v is not None}
    n = len(vals)
    ordered = sorted(vals.values())
    out: dict[str, int | None] = {c: None for c in float_caps}
    for c, v in vals.items():
        lt = bisect.bisect_left(ordered, v)
        eq = bisect.bisect_right(ordered, v) - lt
        rank = D(lt) + D(eq + 1) / D(2)                       # average rank of ties
        raw = (rank * D(5) / D(n)).to_integral_value(rounding=ROUND_CEILING)
        out[c] = int(min(5, max(1, raw)))
    return out


def e077_pool(q1430: Mapping[str, Quote], q1450: Mapping[str, Quote]) -> dict[str, dict]:
    """The pool at the 14:50 snapshot with its 14:30 / 14:50 change and cap quintile (code -> fields)."""
    pool: dict[str, dict] = {}
    for code, b in q1450.items():
        if b.name is None or "ST" in b.name or b.price is None or b.prev_close is None or b.limit_up is None:
            continue
        if not (b.price >= PRICE_MIN and b.prev_close > ZERO and b.price < b.limit_up):
            continue
        a = q1430.get(code)
        pool[code] = {"q": b, "pct30": None if a is None else pct(a.price, a.prev_close), "pct50": pct(b.price, b.prev_close)}
    cap5 = quintiles({c: v["q"].float_cap for c, v in pool.items()})
    for c, v in pool.items():
        v["cap5"] = cap5[c]
    return pool


def e077_signals(pool: Mapping[str, dict], hist: frozenset[str]) -> list[str]:
    """Signal codes: top ``TOP`` by volume ratio (desc), ties by code."""
    sig = []
    for code, v in pool.items():
        b: Quote = v["q"]
        p30, p50 = v["pct30"], v["pct50"]
        if p30 is None or p50 is None or b.volume_ratio is None or b.turnover is None or b.float_cap is None:
            continue
        if (PCT_LO <= p30 <= PCT_HI and PCT_LO <= p50 <= PCT_HI and b.volume_ratio >= VR_MIN and TO_LO <= b.turnover <= TO_HI
                and b.float_cap <= CAP_MAX and code in hist):
            sig.append((-b.volume_ratio, code))
    return [c for _, c in sorted(sig)[:TOP]]


def e077_row(day: date, exit_day: date, q1430: Mapping[str, Quote], q1450: Mapping[str, Quote], qexit: Mapping[str, Quote],
             hist: frozenset[str]) -> dict:
    """One decision-day row: pool size, signals, and (when any signal has an exit price) ret / cell / ex / net in percent, 3 places."""
    pool = e077_pool(q1430, q1450)
    signals = e077_signals(pool, hist)
    ret: dict[str, Decimal] = {}
    for code, v in pool.items():
        ex = qexit.get(code)
        px = v["q"].price
        if ex is not None and ex.price is not None and px is not None and px != ZERO:
            ret[code] = ex.price / px * HUNDRED - HUNDRED
    by_cell: dict[int | None, list[Decimal]] = {}
    for code, r in ret.items():
        by_cell.setdefault(pool[code]["cap5"], []).append(r)
    cell = {k: mean(v) for k, v in by_cell.items()}
    held = [c for c in signals if c in ret]
    row: dict = {"date": day.isoformat(), "exit_day": exit_day.isoformat(), "pool": len(pool), "signals": signals, "n": len(held),
                 "missing_exit": len(signals) - len(held)}
    if held:
        r_mean = mean([ret[c] for c in held])
        c_mean = mean([cell[pool[c]["cap5"]] for c in held])
        ex_mean = mean([ret[c] - cell[pool[c]["cap5"]] for c in held])
        row.update({"ret": q(r_mean, 3), "cell": q(c_mean, 3), "ex": q(ex_mean, 3), "net": q(r_mean - COST_PCT, 3)})
    return row


def halfhour(p1000: Mapping[str, Quote], p1430: Mapping[str, Quote], pclose: Mapping[str, Quote]) -> dict | None:
    """x, x2, y in percent (4 places) as means over the codes present at all three time points; None when nothing joins."""
    codes = [c for c in p1000 if c in p1430 and c in pclose]
    codes = [c for c in codes if p1000[c].prev_close is not None and p1000[c].open is not None
             and p1000[c].prev_close > ZERO and p1000[c].open > ZERO]
    if not codes:
        return None
    x = [p1000[c].price / p1000[c].prev_close - 1 for c in codes if p1000[c].price is not None]
    x2 = [p1000[c].price / p1000[c].open - 1 for c in codes if p1000[c].price is not None]
    y = [pclose[c].price / p1430[c].price - 1 for c in codes
         if pclose[c].price is not None and p1430[c].price is not None and p1430[c].price != ZERO]
    mx, mx2, my = mean(x), mean(x2), mean(y)
    return {"n": len(codes), "x": None if mx is None else q(mx * HUNDRED, 4), "x2": None if mx2 is None else q(mx2 * HUNDRED, 4),
            "y": None if my is None else q(my * HUNDRED, 4)}


def e078_row(day: date, ew: dict | None, index: dict | None, *, index_recorded: bool) -> dict:
    """One row: the main-board equal-weight series and, when the index recording exists for the day, the index series (None if it has no data)."""
    row = {"date": day.isoformat(), "ew": ew}
    if index_recorded:
        row["index"] = index
    return row
