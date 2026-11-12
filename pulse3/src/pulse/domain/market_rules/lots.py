"""Order quantity (lot size) rules and T+1 sellability dates.

Why: lot sizes and the sell-after-buy restriction are market rules, not strategy
constants (execution_standard.md sections 3-4). The reducer enforces the resulting
``eligible_on``; this module computes it from the trading calendar.
Rules coded (conventional exchange lot sizes; NOT verified against the 2026 rule
bodies - EXS06/EXS07 were not exhaustively read): main/ChiNext buy in multiples of
100; STAR buy at least 200 then any whole share; BSE buy at least 100 then any
whole share; sell in lot multiples, or the whole remaining odd lot / whole
position. Ordinary stocks are T+1: a share bought on a session day becomes
sellable on the next trading day. T+0 instruments raise ``UnsupportedInstrument``.
Not covered: auction odd-lot specifics, cash-in-lieu, revolving-trade instruments.
"""
from __future__ import annotations

from datetime import date

from .calendar import TradingCalendar
from .instruments import Board, InstrumentSpec

LOT_RULES_VERSION = "pulse-lot-rules-conventional-v1-unverified"


def validate_quantity(spec: InstrumentSpec, side: str, quantity: int, position_quantity: int = 0) -> str | None:
    """Return a rejection reason, or None when the quantity is tradable."""
    spec.require_ordinary_stock()
    if type(quantity) is not int or quantity <= 0:
        return "quantity_not_positive_integer"
    if side == "buy":
        if spec.board is Board.STAR:
            return None if quantity >= 200 else "star_buy_below_200"
        if spec.board is Board.BSE:
            return None if quantity >= 100 else "bse_buy_below_100"
        return None if quantity % 100 == 0 else "buy_not_lot_multiple"
    if side == "sell":
        if spec.board in (Board.STAR, Board.BSE):
            floor = 200 if spec.board is Board.STAR else 100
            return None if quantity >= floor or quantity == position_quantity else "sell_below_minimum_not_whole_position"
        if quantity % 100 == 0 or quantity == position_quantity or quantity == position_quantity % 100:
            return None
        return "sell_not_lot_multiple_or_whole_odd_lot"
    return "unsupported_side"


def lot_step(spec: InstrumentSpec) -> int:
    """Granularity the matcher rounds partial fills down to."""
    return 100 if spec.board in (Board.MAIN, Board.CHINEXT) else 1


def eligible_on_for_buy(spec: InstrumentSpec, session_day: date, calendar: TradingCalendar) -> date:
    """T+1: next trading day after the session day on which the shares were bought."""
    spec.require_ordinary_stock()
    return calendar.next_trading_day(session_day)
