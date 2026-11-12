"""Decimal account semantics, not an exchange simulator or real broker adapter.

Calls must be serialized by a single writer. Quantities are integral Decimal
shares; board lots, auction rules and price bands belong to MarketRules outside
this small model. Fees are explicit fill inputs, never an assumed zero.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Mapping

ZERO = Decimal("0")


def _decimal(value: Decimal, name: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal, never float")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    return value


def _amount(value: Decimal, name: str, *, positive: bool = False) -> Decimal:
    _decimal(value, name)
    if value < ZERO or (positive and value == ZERO):
        raise ValueError(f"{name} must be {'positive' if positive else 'nonnegative'}")
    return value


def _quantity(value: Decimal, name: str, *, positive: bool = False,
              signed: bool = False) -> Decimal:
    _decimal(value, name)
    if not signed:
        _amount(value, name, positive=positive)
    if value != value.to_integral_value():
        raise ValueError(f"{name} must be an integral share quantity")
    return value


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


class CashPolicy(str, Enum):
    # Available for another trade; this does not imply bank-withdrawable cash.
    DOMESTIC_SALE_REUSABLE = "domestic_sale_reusable"
    UNSETTLED_UNTIL_RELEASE = "unsettled_until_release"


class ReservationStatus(str, Enum):
    OPEN = "open"
    FILLED = "filled"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Position:
    quantity: Decimal
    sellable_quantity: Decimal
    reserved_sell_quantity: Decimal = ZERO

    def __post_init__(self) -> None:
        _quantity(self.quantity, "quantity")
        _quantity(self.sellable_quantity, "sellable_quantity")
        _quantity(self.reserved_sell_quantity, "reserved_sell_quantity")
        if not ZERO <= self.reserved_sell_quantity <= self.sellable_quantity <= self.quantity:
            raise ValueError("require 0 <= reserved <= sellable <= total quantity")

    @property
    def available_to_sell(self) -> Decimal:
        return self.sellable_quantity - self.reserved_sell_quantity


@dataclass(frozen=True)
class SellReservation:
    order_id: str
    instrument_id: str
    original_quantity: Decimal
    remaining_quantity: Decimal
    status: ReservationStatus


@dataclass(frozen=True)
class Fill:
    fill_id: str
    side: str
    instrument_id: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    order_id: str | None


class Account:
    def __init__(self, *, trading_day: date, cash_available: Decimal,
                 positions: Mapping[str, Position] | None = None,
                 cash_unsettled: Decimal = ZERO,
                 cash_policy: CashPolicy = CashPolicy.DOMESTIC_SALE_REUSABLE,
                 currency: str = "CNY") -> None:
        if type(trading_day) is not date:
            raise TypeError("trading_day must be datetime.date")
        if not isinstance(cash_policy, CashPolicy):
            raise TypeError("cash_policy must be CashPolicy")
        self.trading_day = trading_day
        self.currency = _identifier(currency, "currency")
        self.cash_policy = cash_policy
        self._cash_available = _amount(cash_available, "cash_available")
        self._cash_unsettled = _amount(cash_unsettled, "cash_unsettled")
        self._positions = dict(positions or {})
        for symbol, position in self._positions.items():
            _identifier(symbol, "instrument_id")
            if not isinstance(position, Position):
                raise TypeError("positions must contain Position objects")
            if position.reserved_sell_quantity != ZERO:
                raise ValueError("opening positions cannot have untracked reservations")
        self._reservations: dict[str, SellReservation] = {}
        self._fills: dict[str, Fill] = {}
        self._fees_paid = ZERO
        # An immutable economic origin allows meaningful T-vs-hold comparisons.
        self._opening_state = (self.currency, trading_day, cash_available,
                               cash_unsettled, tuple(sorted(self._positions.items())))

    @property
    def cash_available(self) -> Decimal:
        return self._cash_available

    @property
    def cash_unsettled(self) -> Decimal:
        return self._cash_unsettled

    @property
    def fees_paid(self) -> Decimal:
        return self._fees_paid

    def position(self, instrument_id: str) -> Position:
        _identifier(instrument_id, "instrument_id")
        return self._positions.get(instrument_id, Position(ZERO, ZERO))

    def fork(self) -> Account:
        """Clone the same initial economic state for a passive comparison branch."""
        return deepcopy(self)

    def reserve_sell(self, order_id: str, instrument_id: str,
                     quantity: Decimal) -> SellReservation:
        """Reserve eligible inventory once; identical order retries are idempotent."""
        _identifier(order_id, "order_id")
        _identifier(instrument_id, "instrument_id")
        _quantity(quantity, "quantity", positive=True)
        old = self._reservations.get(order_id)
        if old is not None:
            if (old.instrument_id, old.original_quantity) != (instrument_id, quantity):
                raise ValueError("order_id was already used for another intent")
            return old  # A filled/cancelled intent is never reopened by a retry.
        position = self.position(instrument_id)
        if quantity > position.available_to_sell:
            raise ValueError("insufficient unreserved T+1-eligible inventory")
        reservation = SellReservation(order_id, instrument_id, quantity, quantity,
                                      ReservationStatus.OPEN)
        self._positions[instrument_id] = replace(
            position, reserved_sell_quantity=position.reserved_sell_quantity + quantity)
        self._reservations[order_id] = reservation
        return reservation

    @staticmethod
    def _make_fill(fill_id: str, side: str, instrument_id: str,
                   quantity: Decimal, price: Decimal, fee: Decimal,
                   order_id: str | None) -> Fill:
        _identifier(fill_id, "fill_id")
        _identifier(instrument_id, "instrument_id")
        _quantity(quantity, "quantity", positive=True)
        _amount(price, "price", positive=True)
        _amount(fee, "fee")
        return Fill(fill_id, side, instrument_id, quantity, price, fee, order_id)

    def _is_repeat(self, fill: Fill) -> bool:
        old = self._fills.get(fill.fill_id)
        if old is not None and old != fill:
            raise ValueError("fill_id was already used for a different fill")
        return old is not None

    def apply_buy(self, *, fill_id: str, instrument_id: str,
                  quantity: Decimal, price: Decimal, fee: Decimal) -> Fill:
        """Apply a buy fill atomically; today's shares do not increase sellable."""
        fill = self._make_fill(fill_id, "buy", instrument_id, quantity, price, fee, None)
        if self._is_repeat(fill):
            return self._fills[fill_id]
        debit = quantity * price + fee
        if debit > self._cash_available:
            raise ValueError("insufficient reusable cash")
        position = self.position(instrument_id)
        updated = replace(position, quantity=position.quantity + quantity)
        self._cash_available -= debit
        self._positions[instrument_id] = updated
        self._fees_paid += fee
        self._fills[fill_id] = fill
        return fill

    def apply_sell(self, *, fill_id: str, order_id: str,
                   quantity: Decimal, price: Decimal, fee: Decimal) -> Fill:
        """Consume an existing reservation; cash reusability is a configured policy."""
        reservation = self._reservations[order_id]
        fill = self._make_fill(fill_id, "sell", reservation.instrument_id,
                               quantity, price, fee, order_id)
        if self._is_repeat(fill):
            return self._fills[fill_id]
        if reservation.status != ReservationStatus.OPEN:
            raise ValueError("sell intent is no longer open")
        if quantity > reservation.remaining_quantity:
            raise ValueError("fill exceeds remaining reservation")
        credit = quantity * price - fee
        if credit < ZERO and -credit > self._cash_available:
            raise ValueError("insufficient cash for fee exceeding sale proceeds")
        position = self.position(reservation.instrument_id)
        updated = replace(position, quantity=position.quantity - quantity,
                          sellable_quantity=position.sellable_quantity - quantity,
                          reserved_sell_quantity=position.reserved_sell_quantity - quantity)
        remaining = reservation.remaining_quantity - quantity
        next_reservation = replace(reservation, remaining_quantity=remaining,
                                   status=(ReservationStatus.FILLED if remaining == ZERO
                                           else ReservationStatus.OPEN))
        # A negative net credit is a payable fee and cannot be deferred as an asset.
        if self.cash_policy == CashPolicy.DOMESTIC_SALE_REUSABLE or credit < ZERO:
            self._cash_available += credit
        else:
            self._cash_unsettled += credit
        self._positions[reservation.instrument_id] = updated
        self._reservations[order_id] = next_reservation
        self._fees_paid += fee
        self._fills[fill_id] = fill
        return fill

    def cancel_sell(self, order_id: str, *,
                    confirmed_cumulative_filled: Decimal) -> SellReservation:
        """Apply a reconciled cancellation confirmation, never a cancel request.

        The authoritative confirmation supplies cumulative executed quantity.
        Missing fill reports must be applied first; a mismatch preserves the
        reservation so another strategy cannot reuse still-committed inventory.
        """
        reservation = self._reservations[order_id]
        _quantity(confirmed_cumulative_filled, "confirmed_cumulative_filled")
        local_filled = sum((fill.quantity for fill in self._fills.values()
                            if fill.side == "sell" and fill.order_id == order_id), ZERO)
        if confirmed_cumulative_filled != local_filled:
            raise ValueError("reconcile cumulative fills before confirming cancellation")
        if reservation.status != ReservationStatus.OPEN:
            return reservation
        position = self.position(reservation.instrument_id)
        self._positions[reservation.instrument_id] = replace(
            position, reserved_sell_quantity=(position.reserved_sell_quantity
                                              - reservation.remaining_quantity))
        updated = replace(reservation, remaining_quantity=ZERO,
                          status=ReservationStatus.CANCELLED)
        self._reservations[order_id] = updated
        return updated

    def advance_trading_day(self, next_trading_day: date) -> None:
        """Unlock T+1 stock inventory; caller supplies an actual next trading date.

        This does not release service-specific unsettled cash. It also does not
        pretend to know whether a real broker retains orders across sessions.
        """
        if type(next_trading_day) is not date or next_trading_day <= self.trading_day:
            raise ValueError("next_trading_day must be a later trading date")
        self._positions = {symbol: replace(p, sellable_quantity=p.quantity)
                           for symbol, p in self._positions.items()}
        self.trading_day = next_trading_day

    def release_unsettled_cash(self, amount: Decimal | None = None) -> Decimal:
        """Move confirmed settled proceeds into reusable cash, without changing equity."""
        release = self._cash_unsettled if amount is None else _amount(amount, "amount", positive=True)
        if release > self._cash_unsettled:
            raise ValueError("release exceeds unsettled cash")
        self._cash_unsettled -= release
        self._cash_available += release
        return release

    def total_equity(self, marks: Mapping[str, Decimal]) -> Decimal:
        """Value all inventory and both cash buckets; never substitute closed profit."""
        equity = self._cash_available + self._cash_unsettled
        for instrument_id, position in self._positions.items():
            if position.quantity:
                mark = _amount(marks[instrument_id], "mark")
                equity += position.quantity * mark
        return equity


def net_target_deltas(*, strategy_targets: Mapping[str, Mapping[str, Decimal]],
                      account_quantities: Mapping[str, Decimal],
                      pending_deltas: Mapping[str, Decimal] | None = None) -> dict[str, Decimal]:
    """Net absolute sleeve targets at account level before emitting new orders.

    Input must include all active sleeves. Pending deltas are signed remaining
    orders, excluding already filled quantities. Output is a planning delta;
    negative deltas still need inventory reservation and MarketRules checks.
    """
    total_targets: dict[str, Decimal] = {}
    for strategy_id, targets in strategy_targets.items():
        _identifier(strategy_id, "strategy_id")
        for symbol, quantity in targets.items():
            _identifier(symbol, "instrument_id")
            _quantity(quantity, "target quantity")
            total_targets[symbol] = total_targets.get(symbol, ZERO) + quantity
    for symbol, quantity in account_quantities.items():
        _identifier(symbol, "instrument_id")
        _quantity(quantity, "account quantity")
    pending = dict(pending_deltas or {})
    for symbol, quantity in pending.items():
        _identifier(symbol, "instrument_id")
        _quantity(quantity, "pending delta", signed=True)
    result = {}
    for symbol in sorted(total_targets.keys() | account_quantities.keys() | pending.keys()):
        projected = account_quantities.get(symbol, ZERO) + pending.get(symbol, ZERO)
        if projected < ZERO:
            raise ValueError("pending orders imply a negative long-only position")
        result[symbol] = total_targets.get(symbol, ZERO) - projected
    return result


@dataclass(frozen=True)
class TEquityComparison:
    active_equity: Decimal
    passive_equity: Decimal
    incremental_equity: Decimal
    cash_delta: Decimal
    inventory_mark_value_delta: Decimal
    incremental_fees: Decimal


def compare_t_equity(active: Account, passive: Account,
                     marks: Mapping[str, Decimal]) -> TEquityComparison:
    """Measure T's incremental total equity against the same-origin hold branch.

    Both branches use the exact same terminal marks. Fees from both buy and sell
    are already in cash. Unclosed/partially repurchased inventory is marked too.
    This model has no deposits, withdrawals, corporate actions or FX cash flows.
    """
    if active._opening_state != passive._opening_state:
        raise ValueError("comparison requires the same initial economic state")
    if active.trading_day != passive.trading_day:
        raise ValueError("comparison requires the same valuation trading day")
    active_equity, passive_equity = active.total_equity(marks), passive.total_equity(marks)
    cash_delta = (active.cash_available + active.cash_unsettled
                  - passive.cash_available - passive.cash_unsettled)
    incremental = active_equity - passive_equity
    return TEquityComparison(active_equity, passive_equity, incremental, cash_delta,
                             incremental - cash_delta, active.fees_paid - passive.fees_paid)
