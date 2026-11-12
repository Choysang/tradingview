"""Shared builders for the W07 execution tests (synthetic data only)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal as D

from pulse.domain.execution import Bar, ExecutionKernel, LedgerReducer
from pulse.domain.execution.numeric import MARKET_TIMEZONE
from pulse.domain.market_rules import (Board, CostProfileRegistry, Exchange, ExplicitCalendar,
                                       InstrumentSpec, synthetic_fixed_fee)

S = 'SAMPLE_MAIN_001'
SPEC = InstrumentSpec(S, Exchange.SSE, Board.MAIN)
DAYS = [date(2026, 11, 10), date(2026, 11, 11), date(2026, 11, 12), date(2026, 11, 13), date(2026, 11, 16)]
CAL = ExplicitCalendar(DAYS)
COSTS = CostProfileRegistry((synthetic_fixed_fee('5'),))


def make_kernel(*, cash='100000', qty=1000, cost='10000', day='2026-11-10', reusable=True, sink=None,
                specs=None, costs=COSTS, **kw) -> ExecutionKernel:
    positions = {S: {'quantity': qty, 'cost': cost}} if qty else {}
    ledger = LedgerReducer(trading_day=day, opening_cash=cash, positions=positions,
                           sale_proceeds_reusable=reusable)
    return ExecutionKernel(ledger, specs or {S: SPEC}, CAL, costs, sink=sink, **kw)


def at(day='2026-11-10', hhmm='09:30', sec=0) -> datetime:
    h, m = map(int, hhmm.split(':'))
    return datetime.combine(date.fromisoformat(day), time(h, m, sec), MARKET_TIMEZONE)


def daily_bar(day, o, h, l, c, vol=1_000_000, up=None, down=None, suspended=False, instrument=S) -> Bar:
    return Bar(instrument, at(day, '09:30'), at(day, '15:00'), D(o), D(h), D(l), D(c), vol,
               None if up is None else D(up), None if down is None else D(down), suspended)


def minute_bar(day, hhmm, o, h, l, c, vol=100_000, up=None, down=None) -> Bar:
    start = at(day, hhmm)
    return Bar(S, start, start + timedelta(minutes=1), D(o), D(h), D(l), D(c), vol,
               None if up is None else D(up), None if down is None else D(down))


def tz_day(bar: Bar) -> str:
    return bar.start.astimezone(MARKET_TIMEZONE).date().isoformat()


class Scripted:
    """BarStrategy that runs ``script(day, bar, kernel)`` once per session day, before matching."""

    def __init__(self, script):
        self.script, self.done = script, set()

    def before_bar(self, bar, kernel):
        day = tz_day(bar)
        if day not in self.done:
            self.done.add(day)
            self.script(day, bar, kernel)

    def on_fill(self, fill, bar, kernel):
        pass
