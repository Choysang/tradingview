"""Intraday triggers B1 / B3 (buy) and S1 / S3 (sell), first versions for paper replay (APEX design, P1).

They read one snapshot row at a time (a dict as `warehouse.intraday_view` gives it, in known_time order) and
keep their own state, so the same code runs on a recorded day and on a live stream. All thresholds are
candidate parameters; a change is a new TRIGGERS_VERSION and a new ledger entry.

* B1 竞价弱转强 (auction only; the author-consensus version of M4 waits for author data): a name that sealed the
  previous day, whose final auction (09:25) opens B1_GAP above the previous close with an auction amount of
  at least B1_AUCTION_SHARE of the previous day's amount. The previous day's own auction is not recorded yet,
  so the amount share stands in for "auction volume above yesterday's auction".
* B3 分歧换手回封: a name that was sealed, opened for at least B3_MIN_BREAK_S and seals again before B3_LATEST.
  The order joins the queue at the limit (fill from `queue_fill.simulate`).
* S1 封单衰减: a sealed holding whose bid at the limit falls below S1_DECAY of its peak since it sealed (peak at
  least S1_MIN_PEAK_LOTS, so a thin board does not fire at once). Sells into the bid at the limit.
* S3 跌破均价: price under VWAP for VWAP_GRACE_S in a row. The M3 state machine (`board_rules.break_step`) is the
  version for a limit-up holding that opened; plain S3 serves holdings that never sealed.

v2 (2026-11-07, E054): B1 is shadow only (B1_ACTIVE False: recorded, never booked), because buying yesterday's
limit-ups at the next open lost in every market state on three years of daily bars. Holdings follow
`sell_policy`: a previous-session limit-up is sold at the open unless the market state at that close was cold;
otherwise (and for every other holding) the first S1 seal decay or M3 exit sells, else the close. 开板即卖 is
no longer a policy rule; it stays in the replay as a comparison row.
v2.1 (2026-11-07, E056): B3 is shadow only too: 107 base-scenario fills in the first recorded week lost 1.77% to
the next open (38% won), and the live failed-board rate at the signal did not separate good from bad.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Any

from .board_rules import VWAP_GRACE_S

TRIGGERS_VERSION = "triggers-v2.1"
B1_ACTIVE = False  # E054: shadow only
B3_ACTIVE = False  # E056: shadow only (2026-11-07)
ACTIVE = {"B1": B1_ACTIVE, "B3": B3_ACTIVE}  # buy rules whose fills are booked
EPS = 1e-6
B1_GAP = (2.0, 7.0)  # % above the previous close, half-open
B1_AUCTION_SHARE = 0.03
B3_MIN_BREAK_S = 30.0
B3_LATEST = time(14, 30)
S1_DECAY = 0.3
S1_MIN_PEAK_LOTS = 2000.0

Row = dict[str, Any]


def sealed(r: Row) -> bool:
    """At the limit with bids there and nothing offered (ask1 price 0 or missing)."""
    up = r.get("limit_up")
    return bool(up and r.get("price") is not None and r["price"] >= up - EPS and (r.get("bid1_px") or 0) >= up - EPS
                and not (r.get("ask1_px") or 0))


def auction_amount(final: Row) -> float | None:
    """Matched auction amount: field 35's amount, else matched lots x 100 x price (some auction rows lack field 35)."""
    if final.get("amount_yuan"):
        return final["amount_yuan"]
    lots, price = final.get("volume_lots"), final.get("open") or final.get("price")
    return lots * 100 * price if lots and price else None


def b1(final: Row, prev_amount_yuan: float | None) -> bool:
    prev, open_, amount = final.get("prev_close"), final.get("open") or final.get("price"), auction_amount(final)
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

    def feed(self, r: Row) -> bool:
        if self.fired:
            return False
        t, s = r["known_time"], sealed(r)
        if s and self.broke_at is not None and _secs(self.broke_at, t) >= B3_MIN_BREAK_S and t.time() < B3_LATEST:
            self.fired = True
            return True
        if s:
            self.was_sealed, self.broke_at = True, None
        elif self.was_sealed and self.broke_at is None:
            self.broke_at = t
        return False


@dataclass
class S1:
    peak: float = 0.0
    fired: bool = False

    def feed(self, r: Row) -> bool:
        if self.fired:
            return False
        if not sealed(r):
            self.peak = 0.0
            return False
        lots = r.get("bid1_lots") or 0.0
        self.peak = max(self.peak, lots)
        if self.peak >= S1_MIN_PEAK_LOTS and lots < S1_DECAY * self.peak:
            self.fired = True
        return self.fired


@dataclass
class S3:
    below_since: datetime | None = None
    fired: bool = False

    def below_s(self, r: Row) -> float:
        """Seconds the price has been under VWAP, as of this row (0 when above or unknown)."""
        price, vwap, t = r.get("price"), r.get("vwap"), r["known_time"]
        if price is None or not vwap or price >= vwap:
            self.below_since = None
            return 0.0
        self.below_since = self.below_since or t
        return _secs(self.below_since, t)

    def feed(self, r: Row) -> bool:
        if not self.fired and self.below_s(r) >= VWAP_GRACE_S:
            self.fired = True
            return True
        return False


def sell_policy(prev_limit_up: bool, heat: str, s1_or_m3: tuple[datetime, float, str] | None,
                open_fill: tuple[datetime, float], close_fill: tuple[datetime, float]) -> tuple[datetime, float, str]:
    """When and at what price a holding is sold today under v2 (one decision per holding and day)."""
    if prev_limit_up and heat != "cold":
        return (*open_fill, f"open: previous-session limit-up, market {heat}")
    if s1_or_m3 is not None:
        return s1_or_m3
    return (*close_fill, "close: no intraday exit")


def sell_policy_v3(sealed_at_open: bool, s1_or_m3: tuple[datetime, float, str] | None,
                   open_fill: tuple[datetime, float], close_fill: tuple[datetime, float]) -> tuple[datetime, float, str]:
    """Shadow candidate (2026-11-07): a previous-session limit-up sealed at the open is kept and leaves on the first
    S1 seal decay or M3 exit (else the close); one that opens below the limit is sold at the open. On 2023-11 ~
    2026-10 daily bars, an at-limit open lost about 2 points by that close but gained by the next open, so only an
    intraday rule can tell the names that stay sealed from the rest."""
    if not sealed_at_open:
        return (*open_fill, "open: not sealed at the open")
    if s1_or_m3 is not None:
        return s1_or_m3
    return (*close_fill, "close: sealed at the open, no intraday exit")
