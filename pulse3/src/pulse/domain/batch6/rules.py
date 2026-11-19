"""Shared rules of batch 6 (pre-registration section 1): scope, cost, segments, fills, Holm family.

Why: E093-E096 share one set of fill and cost rules; they are written once here and unit-tested by hand-computed cases. Nothing in this module may be tuned
after the pre-registration is frozen (sha256 in ``SPEC_SHA256``); a change is a new experiment.
Invariants: prices are integer cents (no float rounding at the limit price); a limit price is the previous close times (1 +- ratio) rounded half-up to the cent;
a buy at an open that is at or above the limit-up price is NOT filled; a sell at a price at or below the limit-down price is NOT possible and moves to the next
sellable time point (open, then close, of each following session; a session without a bar offers no time point).
Not covered: ST 5 percent rules (ST names are out of scope), IPO no-limit days (out of scope: listed 60 sessions), T+1 (the spec's same-day legs are measurement legs).
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

SPEC_PATH = "docs/research/2026-11-18-batch6-short-term-preregistration.md"
SPEC_SHA256 = "93a4b8136a219bff2f36b633abac1a7845a9695a1991097257f81a0289b7320d"
COST = Decimal("0.00252")                    # round trip, section 1
SEED = 20261118
N_BOOT = 10000
PRE_END = date(2026, 6, 30)                  # PRE: <= this day, DEV: after
FAMILY_SIZE = 7                              # H1..H7, Holm
ALPHA = 0.05
MAIN_PREFIXES = ("000", "001", "002", "003", "600", "601", "603", "605")   # 00 / 60 prefixes (the repository's main-board list)
MIN_LISTED_SESSIONS = 60
RATIO_BP = 1000                              # main board, non-ST: 10 percent (basis points)
GUARD_BP = 1210                              # a move beyond this is ex-rights / bad data: no limit verdict
MAX_DEFER_SESSIONS = 8                       # sell deferral horizon (sessions after the planned exit)

#: hypothesis -> (experiment, expected sign of the matched excess, holding sessions D+1 open .. D+h close)
HYPOTHESES: dict[str, tuple[str, int, int]] = {
    "H1": ("E093", -1, 3), "H2": ("E093", +1, 3), "H3": ("E094", -1, 5), "H4": ("E094", +1, 5),
    "H5": ("E095", +1, 1), "H6": ("E095", -1, 1), "H7": ("E096", +1, 1),
}


def segment_of(day: date) -> str:
    return "PRE" if day <= PRE_END else "DEV"


def limit_cents(prev_c: int, ratio_bp: int = RATIO_BP) -> tuple[int, int]:
    """(limit-up, limit-down) in cents: ``prev * (1 +- ratio)`` rounded half-up to one cent, integer arithmetic only."""
    up = (prev_c * (10000 + ratio_bp) + 5000) // 10000
    down = (prev_c * (10000 - ratio_bp) + 5000) // 10000
    return up, down


def buy_filled(open_c: int | None, up_c: int | None) -> bool:
    """D+1 open buy: not filled when the open is missing or equal to (or above) the limit-up price."""
    return open_c is not None and open_c > 0 and (up_c is None or open_c < up_c)


def sell_possible(px_c: int | None, down_c: int | None) -> bool:
    """A time point can sell when a price exists and it is above the limit-down price (equal means no buyer)."""
    return px_c is not None and px_c > 0 and (down_c is None or px_c > down_c)


def exit_with_deferral(points: Sequence[tuple[int | None, int | None]]) -> tuple[int, int] | None:
    """First sellable (price_cents, index) among time points ordered by time; ``(price, limit-down)`` per point. None = none sellable in the list."""
    for i, (px, down) in enumerate(points):
        if sell_possible(px, down):
            return px, i
    return None


def gross_return(entry_c: int, exit_c: int) -> float:
    return exit_c / entry_c - 1.0


def main_board(code: str) -> bool:
    return code[:3] in MAIN_PREFIXES


_ST = re.compile(r"^(S\*|\*|S)?ST(?![A-Z])")


def is_st(name: str | None, risk_warning: bool) -> bool:
    return bool(risk_warning) or _ST.match((name or "").strip().upper()) is not None


def listed_ok_from(listed_on: date | None, sessions: Sequence[date], need: int = MIN_LISTED_SESSIONS) -> date | None:
    """First session on which ``need`` sessions have passed since listing (listing day counts as the first). None = unknown listing date (out of scope).

    Before the first observed session the calendar is approximated by weekdays; a listing at least ``need`` weekdays before the first session is ok from
    the start (``date.min``)."""
    if listed_on is None or not sessions:
        return None
    first = sessions[0]
    if listed_on < first:
        import numpy as np
        before = int(np.busday_count(listed_on.isoformat(), first.isoformat()))
        if before >= need:
            return date.min
        rest = need - before                                   # still to count inside the observed sessions
        return sessions[rest - 1] if rest <= len(sessions) else None
    idx = next((i for i, s in enumerate(sessions) if s >= listed_on), None)
    if idx is None or idx + need - 1 >= len(sessions):
        return None
    return sessions[idx + need - 1]
