"""Trading calendar port used for T+1 eligibility and session advance.

Why: sellability (``eligible_on``) must come from validated trading days, never a
client date (execution_standard.md section 2, advance_session). The authoritative
CalendarService belongs to another work package; this module defines the port and
two small implementations.
Invariants: ``next_trading_day`` is strictly after its argument.
Not covered: holidays, special sessions - ``WeekdayOnlyCalendar`` ignores them and
must only be used for synthetic data; production passes an ``ExplicitCalendar``
built from CalendarService output.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable, Protocol


class TradingCalendar(Protocol):
    def is_trading_day(self, day: date) -> bool: ...

    def next_trading_day(self, day: date) -> date: ...


class ExplicitCalendar:
    """Calendar from an explicit set of trading days (CalendarService output)."""

    def __init__(self, days: Iterable[date]):
        self._days = sorted(set(days))
        if not self._days:
            raise ValueError("calendar needs at least one trading day")

    def is_trading_day(self, day: date) -> bool:
        return day in self._days

    def next_trading_day(self, day: date) -> date:
        for d in self._days:
            if d > day:
                return d
        raise LookupError(f"calendar does not cover a trading day after {day}")


class WeekdayOnlyCalendar:
    """Monday-Friday only. SYNTHETIC USE ONLY: it knows no exchange holidays."""

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5

    def next_trading_day(self, day: date) -> date:
        d = day + timedelta(days=1)
        while not self.is_trading_day(d):
            d += timedelta(days=1)
        return d
