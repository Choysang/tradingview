"""ExecutionKernel: the one place that turns intents and execution facts into ledger events.

Why: FINAL_SYSTEM_SPEC section 8 / execution_standard.md section 2 - backtest
replay, online paper and normalized broker reports share one kernel and one
LedgerReducer; no entry point owns its own holding period, matching or booking.
Mapping of the standard's parts: RuleEngine = ``pulse.domain.market_rules``;
OrderStateMachine + ReservationBook = the reducer's order/holding state;
CostEngine = ``market_rules.costs``; MatcherPort = ``matcher`` (outside the
kernel; a matcher only proposes fills, the kernel books them).
Invariants: every accepted action is exactly one canonical event applied
atomically (a failure, including a persistence ``sink`` failure, leaves the
reducer unchanged); ids derive from business identity so redelivery is a no-op;
a cancel *request* releases nothing - only ``confirm_cancel`` with the cumulative
fills does; ``submit`` is a compare-and-swap on ``expected_account_version``.
Not covered: raw broker parsing, trade busts, late-event restatement, T+0
instruments (``UnsupportedInstrument``), margin, multi-currency.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Callable, Mapping

from pulse.domain.market_rules import (CostEngine, CostProfileRegistry, InstrumentSpec, PriceLimitRegistry,
                                       RuleNotVerified, TradingCalendar, eligible_on_for_buy, lot_step,
                                       validate_quantity)

from .events import (ExecutionReport, IngestResult, OrderIntent, SubmitResult, fill_payload, instant,
                     make_event, reserve_payload)
from .ledger import LedgerReducer
from .matcher import MatchOrder
from .numeric import ZERO, display

KERNEL_VERSION = "pulse-execution-kernel-v1"
_REJECTABLE = (ValueError, KeyError, TypeError)
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


class ExecutionKernel:
    def __init__(self, ledger: LedgerReducer, instruments: Mapping[str, InstrumentSpec],
                 calendar: TradingCalendar, costs: CostProfileRegistry,
                 price_limits: PriceLimitRegistry | None = None,
                 sink: Callable[[dict], None] | None = None, require_price_band: bool = True):
        self.ledger = ledger
        self.instruments = instruments
        self.calendar = calendar
        self.costs = costs
        self.price_limits = price_limits or PriceLimitRegistry()
        self.sink = sink
        self.require_price_band = require_price_band
        self._active_from: dict[str, datetime] = {}
        self._floors: dict[str, tuple[datetime, Decimal]] = {}
        self._pending_cancel: set[str] = set()

    # -- applying -----------------------------------------------------------------
    def apply_event(self, event: dict) -> bool:
        """Apply any canonical event (dividends, splits, deposits...). False = exact redelivery."""
        prior = self.ledger.__dict__  # apply() swaps in a new dict; the old one stays intact
        applied = self.ledger.apply(event)
        if applied and self.sink is not None:
            try:
                self.sink(event)
            except BaseException:
                self.ledger.__dict__ = prior
                raise
        return applied

    def _next(self, kind: str, at: datetime, payload: dict, event_id: str) -> dict:
        return make_event(self.ledger.sequence + 1, kind, at, payload, event_id)

    # -- submit -------------------------------------------------------------------
    def submit(self, intent: OrderIntent, *, expected_account_version: int, at: datetime,
               prev_close: Decimal | None = None, suspended: bool = False,
               path_floor: tuple[datetime, Decimal] | None = None) -> SubmitResult:
        if expected_account_version != self.ledger.account_version:
            return SubmitResult(False, "account_version_conflict")
        spec = self.instruments.get(intent.instrument_id)
        if spec is None:
            return SubmitResult(False, "unknown_instrument")
        spec.require_ordinary_stock()
        if suspended:
            return SubmitResult(False, "suspended")
        position = self.ledger.holdings.get(intent.instrument_id)
        reason = validate_quantity(spec, intent.side, intent.quantity, position.quantity if position else 0)
        if reason is None and (intent.limit_price % spec.tick_size) != 0:
            reason = "price_not_on_tick"
        if reason is None:
            reason = self._band_reason(spec, intent, prev_close)
        if reason is not None:
            return SubmitResult(False, reason)
        profile = self.costs.resolve(self.ledger.day)
        budget = CostEngine.cumulative_fee(profile, intent.side, intent.quantity * intent.limit_price)[0]
        event = self._next("order.reserve", at, reserve_payload(intent, budget), f"reserve:{intent.order_id}")
        try:
            self.apply_event(event)
        except _REJECTABLE as exc:
            return SubmitResult(False, str(exc.args[0]) if exc.args else type(exc).__name__)
        self._active_from[intent.order_id] = at
        if path_floor is not None:
            self._floors[intent.order_id] = path_floor
        return SubmitResult(True, None, event)

    def _band_reason(self, spec: InstrumentSpec, intent: OrderIntent, prev_close: Decimal | None) -> str | None:
        if prev_close is None:
            return "reference_price_missing" if self.require_price_band else None
        try:
            band = self.price_limits.band(spec, self.ledger.day, prev_close)
        except RuleNotVerified as exc:
            return f"price_rule_not_verified: {exc}"
        if not band.limit_down <= intent.limit_price <= band.limit_up:
            return "price_outside_limits"
        return None

    # -- cancel -------------------------------------------------------------------
    def request_cancel(self, order_id: str) -> None:
        """A request alone releases nothing (fills may still arrive while pending)."""
        if order_id not in self.ledger.orders:
            raise KeyError(order_id)
        self._pending_cancel.add(order_id)

    def confirm_cancel(self, order_id: str, cumulative_filled: int, at: datetime) -> bool:
        event = self._next("order.cancel_confirmed", at,
                           {"order_id": order_id, "cumulative_filled": cumulative_filled},
                           f"cancel:{order_id}")
        applied = self.apply_event(event)
        self._pending_cancel.discard(order_id)
        return applied

    # -- executions ---------------------------------------------------------------
    def ingest_execution(self, report: ExecutionReport) -> IngestResult:
        event_id = f"fill:{report.source_fill_id}"
        existing = self.ledger.recorded_event(event_id)
        if existing is not None:
            p = existing["payload"]
            same = (p["order_id"], p["quantity"], p["price"], datetime.fromisoformat(existing["effective_at"])) == (
                report.order_id, report.quantity, display(report.price), report.at)
            if not same:
                raise ValueError("event_id content conflict")
            return IngestResult(False, None, event=existing)
        order = self.ledger.orders[report.order_id]
        spec = self.instruments[order.instrument_id]
        fee, status, unknown = self._fee(report, order, spec)
        eligible = None
        if order.side == "buy":
            eligible = eligible_on_for_buy(spec, self.ledger.day, self.calendar).isoformat()
        event = self._next("fill.booked", report.at, fill_payload(report, fee, eligible), event_id)
        self.apply_event(event)
        return IngestResult(True, status, unknown, event)

    def _fee(self, report: ExecutionReport, order, spec) -> tuple[Decimal, str, tuple[str, ...]]:
        if report.fee is not None:
            return report.fee, "actual", ()
        prior = sum((f["quantity"] * Decimal(f["price"]) for f in self.ledger.fills.values()
                     if f["order_id"] == report.order_id), ZERO)
        cum = prior + report.quantity * report.price
        inc = CostEngine.increment(self.costs.resolve(self.ledger.day), order.side, cum, order.fees_paid)
        return inc.fee, inc.status, inc.unknown_components

    # -- session ------------------------------------------------------------------
    def advance_session(self, trading_day: date, at: datetime) -> bool:
        if not self.calendar.is_trading_day(trading_day):
            raise ValueError(f"{trading_day} is not a trading day in the calendar")
        event = self._next("session.open", at, {"trading_day": trading_day.isoformat()},
                           f"session:{trading_day.isoformat()}")
        return self.apply_event(event)

    # -- views for matchers ---------------------------------------------------------
    def open_orders(self, instrument_id: str | None = None, *, include_pending_cancel: bool = False) -> list[MatchOrder]:
        out = []
        for oid, o in self.ledger.orders.items():
            if o.remaining <= 0 or (instrument_id and o.instrument_id != instrument_id):
                continue
            if oid in self._pending_cancel and not include_pending_cancel:
                continue
            floor = self._floors.get(oid)
            out.append(MatchOrder(oid, o.instrument_id, o.side, o.remaining, o.limit_price,
                                  self._active_from.get(oid, _EPOCH),
                                  lot_step(self.instruments[o.instrument_id]),
                                  floor[0] if floor else None, floor[1] if floor else ZERO))
        return out

