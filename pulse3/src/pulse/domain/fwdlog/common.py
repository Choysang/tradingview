"""Shared constants and number helpers of the fwdlog rules.

Why: every rule here is a pre-registered threshold; they live in one place so a change is visible as a change of
``RULES_VERSION`` (and a new experiment), never an edit in passing.
Invariants: money / price / ratio are ``Decimal`` (context precision 50, ROUND_HALF_EVEN); a missing number is ``None``, never 0;
instants are tz-aware; the session clock is Asia/Shanghai.
Not covered: IO, persistence.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

CST = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc
D = Decimal
ZERO = D(0)
HUNDRED = D(100)
CTX = Context(prec=50, rounding=ROUND_HALF_EVEN)
RULES_VERSION = "fwdlog-rules-v1"
#: main-board code prefixes (the experiments' universe); the old scripts used the same list
MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")


def dec(v) -> Decimal | None:
    """Decimal of a number or None (never 0 for a missing value); floats go through repr so 0.1 stays 0.1."""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    return D(repr(v)) if isinstance(v, float) else D(v)


def is_main(code: str) -> bool:
    return code[:3] in MAIN_PREFIX


def q(value: Decimal | None, places: int) -> Decimal | None:
    """Round half-even to ``places`` fractional digits; None stays None."""
    if value is None:
        return None
    with localcontext(CTX):
        return value.quantize(D(1).scaleb(-places))


def mean(values: Sequence[Decimal]) -> Decimal | None:
    vals = list(values)
    if not vals:
        return None
    with localcontext(CTX):
        return sum(vals, ZERO) / D(len(vals))


def median(values: Iterable[Decimal]) -> Decimal | None:
    s = sorted(values)
    if not s:
        return None
    n = len(s)
    with localcontext(CTX):
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / D(2)


def pct(price: Decimal | None, base: Decimal | None) -> Decimal | None:
    """price / base * 100 - 100, None when either is missing or base is not positive."""
    if price is None or base is None or base <= ZERO:
        return None
    with localcontext(CTX):
        return price / base * HUNDRED - HUNDRED


def at_clock(day: date, hhmm: time) -> datetime:
    return datetime.combine(day, hhmm, tzinfo=CST)
