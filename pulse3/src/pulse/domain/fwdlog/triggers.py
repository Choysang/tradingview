"""Intraday triggers B1 / B3 (buy) and S1 / S3 (sell) and the sell policy, version ``triggers-v2.1`` (E053-E057), as pure state machines.

Why: the old paper replay runs these rules on every recorded session; the rules move unchanged. Each trigger reads one snapshot row at a time in known-time
order and keeps its own state, so the same code runs on a recorded day and on a live stream. All thresholds are candidate parameters; a change is a new
``TRIGGERS_VERSION`` and a new ledger entry, never an edit.
* B1 auction weak-to-strong: sealed the previous session, the 09:25 final auction opens 2 percent (inclusive) to 7 percent (exclusive) above the previous
  close, with an auction amount of at least 3 percent of the previous session's amount. SHADOW only (E054: negative in every state on 3 years of daily bars).
* B3 divergence re-seal: a name that was sealed, opened for at least 30 s and seals again before 14:30. SHADOW only since v2.1 (E056).
* S1 seal decay: a sealed holding whose bid at the limit falls below 30 percent of its peak since it sealed (peak at least 2000 lots).
* S3 under VWAP: price below the day's average price for 90 s in a row.
* sell policy v2: a previous-session limit-up is sold at the open unless the market state at that close was cold; otherwise the first S1 / M3 exit sells,
  else the close. v3 (shadow comparison): sealed at the open -> keep until the first S1 / M3 exit (else the close), else sell at the open.
Invariants: B1 / B3 never touch an account (``ACTIVE`` is False for both); an unknown value never passes a buy check.
Not covered: M3 (``board``), the queue model (``queue``), booking.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from typing import NamedTuple

from .common import CST, D, ZERO

TRIGGERS_VERSION = "triggers-v2.1"
ACTIVE = {"B1": False, "B3": False}          # buy rules whose fills are booked: none (E054, E056)
EPS = Decimal("0.000001")
B1_GAP = (D(2), D(7))                        # percent above the previous close, half-open
B1_AUCTION_SHARE = D("0.03")
B3_MIN_BREAK_S = 30.0
B3_LATEST = time(14, 30)
S1_DECAY = D("0.3")
S1_MIN_PEAK_LOTS = D(2000)
VWAP_GRACE_S = 90.0


class Snap(NamedTuple):
    """One recorded snapshot, ordered by ``known_at`` (the poll time plus the poll's normal duration)."""
    code: str
    kind: str
    known_at: datetime
    quote_at: datetime | None
    price: Decimal | None
    high: Decimal | None
    prev_close: Decimal | None
    open: Decimal | None
    limit_up: Decimal | None
    bid1_px: Decimal | None
    bid1_lots: Decimal | None
    ask1_px: Decimal | None
    vwap: Decimal | None
    volume_lots: Decimal | None
    amount_yuan: Decimal | None


def sealed(r: Snap) -> bool:
    """At the limit with bids there and nothing offered (ask 1 price missing or 0)."""
    up = r.limit_up
    return bool(up and r.price is not None and r.price >= up - EPS and (r.bid1_px or ZERO) >= up - EPS and not (r.ask1_px or ZERO))


def auction_amount(final: Snap) -> Decimal | None:
    """Matched auction amount: the recorded amount, else matched lots x 100 x price."""
    if final.amount_yuan:
        return final.amount_yuan
    price = final.open or final.price
    return final.volume_lots * 100 * price if final.volume_lots and price else None


def b1(final: Snap, prev_amount_yuan: Decimal | None) -> bool:
    prev, open_, amount = final.prev_close, final.open or final.price, auction_amount(final)
    if not (prev and open_ and prev_amount_yuan and amount):
        return False
    gap = (open_ / prev - 1) * 100
    return B1_GAP[0] <= gap < B1_GAP[1] and amount >= B1_AUCTION_SHARE * prev_amount_yuan


def _secs(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds()


@dataclass
class B3:
    was_sealed: bool = False
    broke_at: datetime | None = None
    fired: bool = False

    def feed(self, r: Snap) -> bool:
        if self.fired:
            return False
        t, s = r.known_at, sealed(r)
        if s and self.broke_at is not None and _secs(self.broke_at, t) >= B3_MIN_BREAK_S and t.astimezone(CST).time() < B3_LATEST:
            self.fired = True
            return True
        if s:
            self.was_sealed, self.broke_at = True, None
        elif self.was_sealed and self.broke_at is None:
            self.broke_at = t
        return False


@dataclass
class S1:
    peak: Decimal = ZERO
    fired: bool = False

    def feed(self, r: Snap) -> bool:
        if self.fired:
            return False
        if not sealed(r):
            self.peak = ZERO
            return False
        lots = r.bid1_lots or ZERO
        self.peak = max(self.peak, lots)
        if self.peak >= S1_MIN_PEAK_LOTS and lots < S1_DECAY * self.peak:
            self.fired = True
        return self.fired


@dataclass
class S3:
    below_since: datetime | None = None

    def below_s(self, r: Snap) -> float:
        """Seconds the price has been under VWAP, as of this row (0 when above or unknown)."""
        if r.price is None or not r.vwap or r.price >= r.vwap:
            self.below_since = None
            return 0.0
        self.below_since = self.below_since or r.known_at
        return _secs(self.below_since, r.known_at)


Exit = tuple[datetime, Decimal, str]


def sell_policy(prev_limit_up: bool, heat: str, s1_or_m3: Exit | None, open_fill: tuple[datetime, Decimal], close_fill: tuple[datetime, Decimal]) -> Exit:
    """When and at what price a holding is sold today under v2 (one decision per holding and day)."""
    if prev_limit_up and heat != "cold":
        return (*open_fill, f"open: previous-session limit-up, market {heat}")
    if s1_or_m3 is not None:
        return s1_or_m3
    return (*close_fill, "close: no intraday exit")


def sell_policy_v3(sealed_at_open: bool, s1_or_m3: Exit | None, open_fill: tuple[datetime, Decimal], close_fill: tuple[datetime, Decimal]) -> Exit:
    """Shadow comparison: sealed at the open -> leave on the first S1 / M3 exit (else the close); otherwise sell at the open."""
    if not sealed_at_open:
        return (*open_fill, "open: not sealed at the open")
    if s1_or_m3 is not None:
        return s1_or_m3
    return (*close_fill, "close: sealed at the open, no intraday exit")
