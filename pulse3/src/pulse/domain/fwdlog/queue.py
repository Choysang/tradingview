"""Limit-up queue fill model for the shadow buys B1 / B3 (three scenarios, never one truth).

Why: a buy order at the limit-up price joins the queue behind everyone already bidding there. Level-1 snapshots about 10-60 s apart cannot show which
shares traded at the limit between two snapshots or whether a shrinking bid 1 was cancelled ahead of us or behind us, so the model brackets the answer
(OPTIMISTIC / BASE / PESSIMISTIC): queue ahead = bid 1 lots of the last snapshot before us / the larger of before and first after / the largest of the
three snapshots around us; volume consumes the queue when an interval has either end / the later end / both ends at the limit. A broken board (the price
leaves the limit and bid 1 is no longer at it) means sellers ate the whole queue: the order fills in full at the limit in every scenario, flagged ``broken``.
The model is for an order that must queue: when asks are still offered below the limit on arrival the caller books that fill at the ask instead.
Invariants: quantities are lots (100 shares, float64 in the recording, kept as Decimal); the outcome is a SHADOW record, never an account booking.
Not covered: cancellations behind us, the order's price improvement, partial fills against asks.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Sequence

ZERO = Decimal(0)
EPS = Decimal("0.000001")


class Scenario(str, Enum):
    OPTIMISTIC = "OPTIMISTIC"
    BASE = "BASE"
    PESSIMISTIC = "PESSIMISTIC"


@dataclass(frozen=True)
class QueueRow:
    """The columns the model reads from one snapshot."""
    quote_at: datetime
    price: Decimal | None
    limit_up: Decimal | None
    bid1_px: Decimal | None
    bid1_lots: Decimal | None
    volume_lots: Decimal | None


@dataclass(frozen=True)
class QueueFill:
    scenario: Scenario
    queue_ahead: Decimal
    filled_lots: Decimal
    fill_time: datetime | None
    broken: bool


def _at(price: Decimal | None, limit: Decimal | None) -> bool:
    return price is not None and limit is not None and abs(price - limit) < EPS


def simulate(snaps: Sequence[QueueRow], arrive: datetime, lots: Decimal, until: datetime) -> dict[Scenario, QueueFill]:
    """Fill of a ``lots`` buy at the limit-up price that reaches the exchange at ``arrive`` and lives to ``until``."""
    by_time: dict[datetime, QueueRow] = {}
    for r in sorted(snaps, key=lambda r: r.quote_at):
        by_time[r.quote_at] = r                          # one row per quote time, the last wins
    rows = list(by_time.values())
    k = next((i for i, r in enumerate(rows) if r.quote_at >= arrive), None)
    return {sc: _one(rows, k, arrive, lots, until, sc) for sc in Scenario}


def _queue_ahead(rows: list[QueueRow], k: int, sc: Scenario) -> Decimal:
    def bid(i: int) -> Decimal:
        if 0 <= i < len(rows) and _at(rows[i].bid1_px, rows[i].limit_up):
            return rows[i].bid1_lots or ZERO
        return ZERO
    if sc is Scenario.OPTIMISTIC:
        return bid(k - 1)
    if sc is Scenario.BASE:
        return max(bid(k - 1), bid(k))
    return max(bid(k - 1), bid(k), bid(k + 1))


def _one(rows: list[QueueRow], k: int | None, arrive: datetime, lots: Decimal, until: datetime, sc: Scenario) -> QueueFill:
    if k is None:
        return QueueFill(sc, ZERO, ZERO, None, False)
    ahead = _queue_ahead(rows, k, sc)
    consumed = ZERO
    for i in range(max(k - 1, 0), len(rows) - 1):
        a, b = rows[i], rows[i + 1]
        if b.quote_at > until:
            break
        if b.quote_at <= arrive:
            continue
        if not _at(b.bid1_px, b.limit_up) and not _at(b.price, b.limit_up) and _at(a.price, a.limit_up):
            return QueueFill(sc, ahead, lots, b.quote_at, True)           # the board broke: the queue was eaten
        dv = max((b.volume_lots or ZERO) - (a.volume_lots or ZERO), ZERO)
        at_a, at_b = _at(a.price, a.limit_up), _at(b.price, b.limit_up)
        counts = (at_a or at_b) if sc is Scenario.OPTIMISTIC else at_b if sc is Scenario.BASE else (at_a and at_b)
        if counts:
            consumed += dv
        if sc is Scenario.OPTIMISTIC and _at(a.bid1_px, a.limit_up) and _at(b.bid1_px, b.limit_up):
            cancelled = (a.bid1_lots or ZERO) - (b.bid1_lots or ZERO) - dv
            ahead = max(ahead - max(cancelled, ZERO), ZERO)             # unexplained shrink = cancels ahead of us
        if consumed >= ahead + lots:
            return QueueFill(sc, ahead, lots, b.quote_at, False)
    return QueueFill(sc, ahead, min(max(consumed - ahead, ZERO), lots), None, False)
