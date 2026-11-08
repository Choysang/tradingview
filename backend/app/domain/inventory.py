"""The one T+1 cash and inventory ledger of an account (design 1.0 section 8.5), as pure functions.

Every function takes a PortfolioState and returns a new one with ledger_version + 1; nothing is mutated.
Buys move cash into reservation first and settle from it; sells must reserve sellable shares first, so a
share is never sold twice. Shares bought today stay in acquired_today until `roll_session` moves to the
next real trading session (not at midnight, not on a weekend). An invariant break raises LedgerError.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from contracts.core import Position, PortfolioState


class LedgerError(ValueError):
    pass


def _replace(state: PortfolioState, position: Position | None, instrument_id: str, now: datetime, **cash: Decimal) -> PortfolioState:
    others = tuple(p for p in state.positions if p.instrument_id != instrument_id)
    keep = position is not None and (position.total_qty or position.pending_buy_qty or position.reserved_sell_qty)
    positions = tuple(sorted(others + ((position,) if keep else ()), key=lambda p: p.instrument_id))
    return state.model_copy(update={"positions": positions, "ledger_version": state.ledger_version + 1, "as_of": now, **cash})


def _checked(state: PortfolioState) -> PortfolioState:
    """model_copy skips validation, so every new state is validated again here."""
    try:
        return PortfolioState.model_validate(state.model_dump())
    except ValueError as exc:
        raise LedgerError(str(exc)) from exc


def _position(state: PortfolioState, instrument_id: str) -> Position:
    return state.position(instrument_id) or Position(instrument_id=instrument_id, total_qty=0, sellable_qty=0, acquired_today=0)


def reserve_buy(state: PortfolioState, instrument_id: str, quantity: int, cash: Decimal, now: datetime) -> PortfolioState:
    """Freeze `cash` for a buy order of `quantity` shares."""
    if cash > state.cash_available:
        raise LedgerError(f"needs {cash}, only {state.cash_available} available")
    p = _position(state, instrument_id)
    p = p.model_copy(update={"pending_buy_qty": p.pending_buy_qty + quantity})
    return _checked(_replace(state, p, instrument_id, now, cash_available=state.cash_available - cash,
                             cash_reserved=state.cash_reserved + cash))


def buy_fill(state: PortfolioState, instrument_id: str, quantity: int, price: Decimal, fees: Decimal,
             released: Decimal, now: datetime) -> PortfolioState:
    """Book a buy fill paid from `released` reserved cash (any surplus returns to available cash)."""
    p = _position(state, instrument_id)
    if quantity > p.pending_buy_qty:
        raise LedgerError(f"fill {quantity} exceeds pending buy {p.pending_buy_qty}")
    if released > state.cash_reserved:
        raise LedgerError("releases more cash than reserved")
    cost = price * quantity + fees
    p = p.model_copy(update={"total_qty": p.total_qty + quantity, "acquired_today": p.acquired_today + quantity,
                             "pending_buy_qty": p.pending_buy_qty - quantity, "cost_basis": p.cost_basis + cost})
    return _checked(_replace(state, p, instrument_id, now, cash_available=state.cash_available + released - cost,
                             cash_reserved=state.cash_reserved - released))


def release_buy(state: PortfolioState, instrument_id: str, quantity: int, cash: Decimal, now: datetime) -> PortfolioState:
    """A buy order ended (canceled, expired, rejected) with `quantity` shares unfilled: unfreeze `cash`."""
    p = _position(state, instrument_id)
    if quantity > p.pending_buy_qty or cash > state.cash_reserved:
        raise LedgerError("releases more than reserved")
    p = p.model_copy(update={"pending_buy_qty": p.pending_buy_qty - quantity})
    return _checked(_replace(state, p, instrument_id, now, cash_available=state.cash_available + cash,
                             cash_reserved=state.cash_reserved - cash))


def reserve_sell(state: PortfolioState, instrument_id: str, quantity: int, now: datetime) -> PortfolioState:
    p = state.position(instrument_id)
    if p is None or quantity > p.free_to_sell:
        raise LedgerError(f"only {0 if p is None else p.free_to_sell} shares free to sell")
    p = p.model_copy(update={"reserved_sell_qty": p.reserved_sell_qty + quantity})
    return _checked(_replace(state, p, instrument_id, now))


def sell_fill(state: PortfolioState, instrument_id: str, quantity: int, price: Decimal, fees: Decimal,
              now: datetime) -> PortfolioState:
    p = state.position(instrument_id)
    if p is None or quantity > p.reserved_sell_qty:
        raise LedgerError("a sell fill must come from reserved shares")
    basis = p.cost_basis * quantity / p.total_qty if p.total_qty else Decimal(0)
    p = p.model_copy(update={"total_qty": p.total_qty - quantity, "sellable_qty": p.sellable_qty - quantity,
                             "reserved_sell_qty": p.reserved_sell_qty - quantity, "cost_basis": p.cost_basis - basis})
    return _checked(_replace(state, p, instrument_id, now, cash_available=state.cash_available + price * quantity - fees))


def release_sell(state: PortfolioState, instrument_id: str, quantity: int, now: datetime) -> PortfolioState:
    p = state.position(instrument_id)
    if p is None or quantity > p.reserved_sell_qty:
        raise LedgerError("releases more shares than reserved")
    p = p.model_copy(update={"reserved_sell_qty": p.reserved_sell_qty - quantity})
    return _checked(_replace(state, p, instrument_id, now))


def roll_session(state: PortfolioState, next_session: str, now: datetime) -> PortfolioState:
    """Move to the next real trading session: today's buys become sellable. Day orders must be closed first."""
    if next_session <= state.session_date:
        raise LedgerError(f"{next_session} is not after {state.session_date}")
    if state.open_order_ids or state.cash_reserved or any(p.pending_buy_qty or p.reserved_sell_qty for p in state.positions):
        raise LedgerError("close or release every open order before rolling the session")
    positions = tuple(p.model_copy(update={"sellable_qty": p.sellable_qty + p.acquired_today, "acquired_today": 0})
                      for p in state.positions)
    return _checked(state.model_copy(update={"positions": positions, "session_date": next_session,
                                             "ledger_version": state.ledger_version + 1, "as_of": now}))
