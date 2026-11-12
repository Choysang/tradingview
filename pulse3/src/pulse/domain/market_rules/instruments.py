"""Instrument facts the trading rules depend on.

Why: price limits, lot sizes and sellability differ by exchange, board and
risk-warning status (execution_standard.md section 3). The rule modules read
only this value object, so no strategy carries its own constants.
Invariants: immutable; identity is the stable instrument id (a UUID string in
production, a readable id in synthetic fixtures). Not covered: ETFs, bonds,
convertibles and other T+0 or non-0.01-tick instruments (``UnsupportedInstrument``).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class Exchange(str, Enum):
    SSE = "SSE"
    SZSE = "SZSE"
    BSE = "BSE"


class Board(str, Enum):
    MAIN = "main"
    CHINEXT = "chinext"
    STAR = "star"
    BSE = "bse"


class InstrumentKind(str, Enum):
    ORDINARY_STOCK = "ordinary_stock"
    OTHER = "other"  # ETF/bond/...: rules differ, not modelled here


class UnsupportedInstrument(ValueError):
    """The instrument kind has trading rules this package does not model."""


@dataclass(frozen=True)
class InstrumentSpec:
    instrument_id: str
    exchange: Exchange
    board: Board
    is_risk_warning: bool = False  # ST / *ST
    kind: InstrumentKind = InstrumentKind.ORDINARY_STOCK
    # 0.01 CNY is the conventional A-share ordinary-stock price unit. The 2026
    # rule bodies (EXS06/EXS07) were not exhaustively read; override per instrument.
    tick_size: Decimal = Decimal("0.01")

    def require_ordinary_stock(self) -> None:
        if self.kind is not InstrumentKind.ORDINARY_STOCK:
            raise UnsupportedInstrument(
                f"{self.instrument_id}: only ordinary stocks are modelled (T+1, 0.01 tick)")
