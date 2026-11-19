"""Short-term market state at a session's close from main-board daily bars (E054), used by the sell policy v2.

Why: selling a held limit-up at the next open beat selling at the next close by 0.5-0.7 points in mid / hot states and was flat or worse in cold
ones (three years of daily bars). ``heat`` ranks the day's main-board limit-up count within the previous 250 sessions (only earlier days, so the
state is known at that close): "cold" below the lower third, "hot" from the upper third, else "mid"; the failed-board rate (touched the limit but
did not close there / all that touched) is "hi_fail" from its upper third.
Invariants: prices are compared as integer cents (A-share prices have two decimals), the limit-up price is previous close x 1.1 rounded half up to
the cent, so no float noise decides a limit-up; fewer than 60 earlier sessions -> "unknown" (never a guessed state).
Not covered: ST names (the old state used 10 percent for every main-board name; an ST name closing at +5 percent is simply not a limit-up here).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction
from typing import Mapping, Sequence

HISTORY, MIN_HISTORY = 250, 60
LOW, HIGH = Fraction(1, 3), Fraction(2, 3)


@dataclass(frozen=True)
class MarketState:
    day: date
    limit_ups: int
    failed_rate: Fraction | None
    heat: str          # cold | mid | hot | unknown
    failed: str        # hi_fail | lo_fail | unknown


def cents(v: Decimal) -> int:
    """A price in whole cents (half up)."""
    return int((v * 100).to_integral_value(rounding="ROUND_HALF_UP"))


def limit_up_cents(prev_close_cents: int) -> int:
    """previous close x 1.1 rounded half up to the cent, in integers."""
    return (prev_close_cents * 11 + 5) // 10


def daily_counts(bars: Mapping[str, Sequence[tuple[date, Decimal | None, Decimal | None]]]) -> list[tuple[date, int, int]]:
    """Per date: (date, main-board limit-up closes, touches that failed). ``bars``: code -> (date, high, close) rows; the previous close is the
    previous row of the same code."""
    return daily_counts_cents({c: [(d, None if h is None else cents(h), None if cl is None else cents(cl)) for d, h, cl in rows] for c, rows in bars.items()})


def daily_counts_cents(bars: Mapping[str, Sequence[tuple[date, int | None, int | None]]]) -> list[tuple[date, int, int]]:
    """As ``daily_counts`` on integer cents (the loader hands cents over so a year of bars is not a million Decimals)."""
    zt: dict[date, int] = {}
    fail: dict[date, int] = {}
    for rows in bars.values():
        prev: int | None = None
        seen = set()
        for d, high, close in sorted(rows, key=lambda r: r[0]):
            if d in seen or close is None:
                continue
            seen.add(d)
            if prev is not None:
                up = limit_up_cents(prev)
                is_zt = close >= up
                touched = high is not None and high >= up
                zt[d] = zt.get(d, 0) + (1 if is_zt else 0)
                fail[d] = fail.get(d, 0) + (1 if touched and not is_zt else 0)
                zt.setdefault(d, 0)
            prev = close
    return sorted((d, zt.get(d, 0), fail.get(d, 0)) for d in set(zt) | set(fail))


def _rank(history: Sequence, value) -> Fraction:
    return Fraction(sum(1 for h in history if h < value), len(history))


def state_at(counts: Sequence[tuple[date, int, int]], day: date) -> MarketState:
    """State at the close of the latest session on or before ``day``."""
    rows = sorted(r for r in counts if r[0] <= day)
    if not rows:
        return MarketState(day, 0, None, "unknown", "unknown")
    d, zt, failed = rows[-1]
    rate = Fraction(failed, zt + failed) if zt + failed else None
    past = rows[:-1][-HISTORY:]
    if len(past) < MIN_HISTORY:
        return MarketState(d, zt, rate, "unknown", "unknown")
    q = _rank([p[1] for p in past], zt)
    heat = "cold" if q < LOW else "hot" if q >= HIGH else "mid"
    rates = [Fraction(p[2], p[1] + p[2]) for p in past if p[1] + p[2]]
    fstate = "unknown" if rate is None or not rates else "hi_fail" if _rank(rates, rate) >= HIGH else "lo_fail"
    return MarketState(d, zt, rate, heat, fstate)
