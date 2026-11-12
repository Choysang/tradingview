"""Canonical account events and the normalized inputs that produce them.

Why: all three entry points (backtest replay, online paper, normalized broker
report) must reduce *the same canonical events* through one reducer
(FINAL_SYSTEM_SPEC section 8). Event dicts keep the reference field names
(``kind``/``sequence`` map to production ``event_type``/``account_sequence``).
Invariants: event ids derive deterministically from business identity
(``reserve:<order_id>``, ``fill:<source_fill_id>``, ...) so redelivery is an
exact no-op and a changed redelivery is a conflict; ``effective_at`` is an
Asia/Shanghai ISO instant produced from a timezone-aware datetime.
Not covered: raw broker message parsing (done by adapters before this point).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .numeric import MARKET_TIMEZONE, display


def instant(at: datetime) -> str:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("timezone-aware datetime required")
    return at.astimezone(MARKET_TIMEZONE).isoformat()


def make_event(sequence: int, kind: str, at: datetime, payload: dict, event_id: str) -> dict:
    return {"event_id": event_id, "sequence": sequence, "kind": kind,
            "effective_at": instant(at), "payload": payload}


@dataclass(frozen=True)
class OrderIntent:
    order_id: str
    instrument_id: str
    side: str  # 'buy' | 'sell'
    quantity: int
    limit_price: Decimal
    strategy_id: str | None = None  # accounting is per account, never per strategy


@dataclass(frozen=True)
class ExecutionReport:
    """Normalized, already deduplicated-by-identity fill fact (simulated or actual)."""
    source_fill_id: str
    order_id: str
    quantity: int
    price: Decimal
    at: datetime
    fee: Decimal | None = None  # actual fee evidence; None -> CostEngine resolves
    execution_model_id: str | None = None  # None for actual broker reports
    scenario_id: str | None = None


@dataclass(frozen=True)
class SubmitResult:
    accepted: bool
    reason: str | None
    event: dict | None = None


@dataclass(frozen=True)
class IngestResult:
    applied: bool  # False: exact redelivery
    fee_status: str | None  # 'actual' | 'complete' | 'incomplete'
    unknown_fee_components: tuple[str, ...] = ()
    event: dict | None = None


def reserve_payload(i: OrderIntent, fee_budget: Decimal) -> dict:
    return {"order_id": i.order_id, "instrument_id": i.instrument_id, "side": i.side,
            "quantity": i.quantity, "limit_price": display(i.limit_price), "fee_budget": display(fee_budget)}


def fill_payload(r: ExecutionReport, fee: Decimal, eligible_on: str | None) -> dict:
    return {"fill_id": r.source_fill_id, "order_id": r.order_id, "quantity": r.quantity,
            "price": display(r.price), "fee": display(fee), "eligible_on": eligible_on}
