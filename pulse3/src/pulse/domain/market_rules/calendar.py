"""Trading calendar port used for T+1 eligibility and session advance.

Why: sellability (``eligible_on``) must come from validated trading days, never a
client date (execution_standard.md section 2, advance_session). The authoritative
CalendarService belongs to another work package; this module defines the port and
two small implementations.
Invariants: ``next_trading_day`` is strictly after its argument.
Not covered: special sessions - ``WeekdayOnlyCalendar`` ignores holidays and must only be used for
synthetic data; production passes an ``ExplicitCalendar`` built from CalendarService output.
Future sessions: ``forward_days`` counts days after the last observed session with the official closure
schedule of ``holidays.py`` (2026/2027); a year without a complete citation keeps the weekday rule and is
reported as ``assumed``.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable, Protocol

from .holidays import weekday_tail


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


def forward_days(last_observed: date, calendar_days: int = 10) -> tuple[list[date], bool]:
    """Sessions within ``calendar_days`` calendar days after the last observed one, and whether holidays may be missing (``assumed``)."""
    return weekday_tail(last_observed, calendar_days)
