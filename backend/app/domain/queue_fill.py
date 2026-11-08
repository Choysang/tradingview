"""Limit-up queue fill model v2 (patch C, corrected by design 1.0 section 10.3): three scenarios, never one truth.

A buy order at the limit-up price joins the queue (封单) behind everyone already bidding there. 10-second
level-1 snapshots cannot show which shares traded at the limit between two snapshots or whether a shrinking
bid1 was cancelled ahead of us or behind us, so the model brackets the answer:

| scenario    | queue ahead when we arrive                  | volume that consumes the queue         | cancels   |
|-------------|---------------------------------------------|----------------------------------------|-----------|
| OPTIMISTIC  | bid1 lots of the last snapshot before us    | an interval with either end at limit   | ahead of us |
| BASE        | max(bid1 before, bid1 first after) (patch C) | an interval ending at the limit       | ignored   |
| PESSIMISTIC | max bid1 over the 3 snapshots around us     | an interval with both ends at limit    | ignored   |

A broken board (the price leaves the limit and bid1 is no longer at it) means sellers ate the whole queue:
the order fills in full at the limit in every scenario, flagged `broken` (adverse selection, not luck).
The model is for an order that must queue: if asks are still offered below the limit when the order
arrives, it trades against them at once and the caller books that fill instead.
Snapshot columns as recorded by warehouse/intraday.py: quote_ts, price, limit_up, bid1_px, bid1_lots,
volume_lots (cumulative). Quantities are in lots (100 shares).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import polars as pl

from contracts.core import FillScenario

EPS = 1e-6
NEEDED = ("quote_ts", "price", "limit_up", "bid1_px", "bid1_lots", "volume_lots")


@dataclass(frozen=True)
class QueueFill:
    scenario: FillScenario
    queue_ahead: float  # lots
    filled_lots: float
    fill_time: datetime | None  # when the order was complete (or last partly filled)
    broken: bool  # filled because the board broke


def _at(price: float | None, limit: float | None) -> bool:
    return price is not None and limit is not None and abs(price - limit) < EPS


def simulate(snaps: pl.DataFrame, arrive: datetime, lots: float, until: datetime) -> dict[FillScenario, QueueFill]:
    """Fill of a `lots` buy at the limit-up price that reaches the exchange at `arrive` and lives to `until`."""
    missing = [c for c in NEEDED if c not in snaps.columns]
    if missing:
        raise ValueError(f"snapshots lack {missing}")
    s = snaps.select(NEEDED).drop_nulls("quote_ts").sort("quote_ts").unique("quote_ts", keep="last", maintain_order=True)
    rows = s.rows(named=True)
    k = next((i for i, r in enumerate(rows) if r["quote_ts"] >= arrive), None)
    out = {}
    for sc in FillScenario:
        out[sc] = _one(rows, k, arrive, lots, until, sc)
    return out


def _queue_ahead(rows: list[dict], k: int, sc: FillScenario) -> float:
    def bid(i: int) -> float:
        if 0 <= i < len(rows) and _at(rows[i]["bid1_px"], rows[i]["limit_up"]):
            return float(rows[i]["bid1_lots"] or 0)
        return 0.0
    if sc is FillScenario.OPTIMISTIC:
        return bid(k - 1)
    if sc is FillScenario.BASE:
        return max(bid(k - 1), bid(k))
    return max(bid(k - 1), bid(k), bid(k + 1))


def _one(rows: list[dict], k: int | None, arrive: datetime, lots: float, until: datetime, sc: FillScenario) -> QueueFill:
    if k is None:
        return QueueFill(sc, 0.0, 0.0, None, False)
    ahead = _queue_ahead(rows, k, sc)
    consumed = 0.0
    start = max(k - 1, 0)
    for i in range(start, len(rows) - 1):
        a, b = rows[i], rows[i + 1]
        if b["quote_ts"] > until:
            break
        if b["quote_ts"] <= arrive:
            continue
        if not _at(b["bid1_px"], b["limit_up"]) and not _at(b["price"], b["limit_up"]) and _at(a["price"], a["limit_up"]):
            return QueueFill(sc, ahead, lots, b["quote_ts"], True)  # the board broke: the queue was eaten
        dv = max(float((b["volume_lots"] or 0) - (a["volume_lots"] or 0)), 0.0)
        at_a, at_b = _at(a["price"], a["limit_up"]), _at(b["price"], b["limit_up"])
        counts = (at_a or at_b) if sc is FillScenario.OPTIMISTIC else at_b if sc is FillScenario.BASE else (at_a and at_b)
        if counts:
            consumed += dv
        if sc is FillScenario.OPTIMISTIC and _at(a["bid1_px"], a["limit_up"]) and _at(b["bid1_px"], b["limit_up"]):
            cancelled = float(a["bid1_lots"] or 0) - float(b["bid1_lots"] or 0) - dv
            ahead = max(ahead - max(cancelled, 0.0), 0.0)  # treat unexplained shrink as cancels ahead of us
        if consumed >= ahead + lots:
            return QueueFill(sc, ahead, lots, b["quote_ts"], False)
    filled = min(max(consumed - ahead, 0.0), lots)
    return QueueFill(sc, ahead, filled, None, False)
