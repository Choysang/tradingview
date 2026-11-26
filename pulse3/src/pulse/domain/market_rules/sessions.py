"""Intraday session phases of one A-share trading day (Asia/Shanghai), on top of the holiday calendar.

Why: a job that needs a live feed must tell "the feed is stale" from "the market is not trading": the lunch break 11:30-13:00 carries no quotes at
all, so a freshness check run at 11:35 fails by construction (pulse3 tape task, 2026-11-23: 17 runs of exit 5 between 11:35 and 12:55).
Invariants:
* continuous trading is 09:30-11:30 and 13:00-15:00; a boundary instant belongs to the phase that STARTS there (11:30:00 is lunch, 13:00:00 is open);
* the day is open unless the official schedule closes it (``holidays.is_open``): weekends and listed holidays are not trading; a weekday of a year
  without a complete official schedule is treated as OPEN, so an unknown calendar can only make a caller fail loudly, never skip silently;
* the call auctions (09:15-09:25, 14:57-15:00) are not separate phases: before 09:30 is ``before open``, 15:00 and later is ``after close``.
Not covered: half-days, special make-up sessions, and the exchange's own after-hours fixed-price session (15:05-15:30).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from .holidays import YearSchedule, is_open

CST = timezone(timedelta(hours=8))                       # Asia/Shanghai has no daylight saving: a fixed offset is exact
MORNING = (time(9, 30), time(11, 30))
AFTERNOON = (time(13, 0), time(15, 0))


@dataclass(frozen=True)
class MarketPhase:
    """``trading`` is True inside a continuous session (``session_start`` is then that session's opening instant, aware, Asia/Shanghai);
    otherwise ``label`` says why not: weekend / holiday / before open / lunch / after close."""
    trading: bool
    label: str
    session_start: datetime | None = None


def _at(day: date, t: time) -> datetime:
    return datetime.combine(day, t, tzinfo=CST)


def phase_at(now: datetime, schedules: dict[int, YearSchedule] | None = None) -> MarketPhase:
    """The phase of the instant ``now`` (must be timezone-aware)."""
    if now.tzinfo is None:
        raise ValueError("phase_at needs an aware datetime")
    local = now.astimezone(CST)
    day, t = local.date(), local.time()
    if is_open(day, schedules) is False:
        return MarketPhase(False, "weekend" if day.weekday() >= 5 else "holiday")
    if t < MORNING[0]:
        return MarketPhase(False, "before open")
    if t < MORNING[1]:
        return MarketPhase(True, "open", _at(day, MORNING[0]))
    if t < AFTERNOON[0]:
        return MarketPhase(False, "lunch")
    if t < AFTERNOON[1]:
        return MarketPhase(True, "open", _at(day, AFTERNOON[0]))
    return MarketPhase(False, "after close")
