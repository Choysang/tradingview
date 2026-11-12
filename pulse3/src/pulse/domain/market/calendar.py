"""Trading calendar for period aggregation (spec FINAL §4.1, RECON §5, M1-E05).

Why: week/month/quarter/year bars are aggregated over *trading* sessions and an
unfinished period must be marked ``is_final=False``. The calendar is a versioned
set of observed session dates plus ``known_through``: the last date for which the
set is declared complete. Sessions after ``known_through`` are unknown, so a
period whose end lies beyond it can never be declared complete.

Invariants: ``calendar_version`` is a content hash of the sessions + known_through;
all dates are Asia/Shanghai session dates.
Not covered: half-day/auction session boundaries, exchange holiday publication
(sessions come from observed data or an injected official list).
"""
from __future__ import annotations

import bisect
import hashlib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

CST = ZoneInfo("Asia/Shanghai")
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(15, 0)
PERIODS = ("1d", "1w", "1mo", "1q", "1y")


def session_open_utc(d: date) -> datetime:
    return datetime.combine(d, SESSION_OPEN, tzinfo=CST).astimezone(timezone.utc)


def session_close_utc(d: date) -> datetime:
    return datetime.combine(d, SESSION_CLOSE, tzinfo=CST).astimezone(timezone.utc)


@dataclass(frozen=True)
class TradingCalendar:
    sessions: tuple[date, ...]
    known_through: date
    source: str = "observed"

    def __post_init__(self) -> None:
        s = tuple(sorted(set(self.sessions)))
        object.__setattr__(self, "sessions", s)
        if not s:
            raise ValueError("calendar needs at least one session")
        if s[-1] > self.known_through:
            raise ValueError("session after known_through")

    @property
    def version(self) -> str:
        h = hashlib.sha256(("|".join(d.isoformat() for d in self.sessions) + f"|{self.known_through}|{self.source}").encode())
        return f"{self.source}-{h.hexdigest()[:16]}"

    def is_session(self, d: date) -> bool:
        i = bisect.bisect_left(self.sessions, d)
        return i < len(self.sessions) and self.sessions[i] == d

    def between(self, start: date, end: date) -> tuple[date, ...]:
        lo = bisect.bisect_left(self.sessions, start)
        hi = bisect.bisect_right(self.sessions, end)
        return self.sessions[lo:hi]

    def period_key(self, d: date, timeframe: str) -> tuple[date, date]:
        """Calendar span (first_day, last_day) of the period containing ``d``."""
        if timeframe == "1w":
            start = d - timedelta(days=d.weekday())
            return start, start + timedelta(days=6)
        if timeframe == "1mo":
            start = d.replace(day=1)
            nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
            return start, nxt - timedelta(days=1)
        if timeframe == "1q":
            q0 = 3 * ((d.month - 1) // 3) + 1
            start = date(d.year, q0, 1)
            nxt = date(d.year + (q0 + 3 > 12), (q0 + 3 - 1) % 12 + 1, 1)
            return start, nxt - timedelta(days=1)
        if timeframe == "1y":
            return date(d.year, 1, 1), date(d.year, 12, 31)
        if timeframe == "1d":
            return d, d
        raise ValueError(f"unsupported period {timeframe!r}")

    def period_sessions(self, d: date, timeframe: str) -> tuple[date, ...]:
        first, last = self.period_key(d, timeframe)
        return self.between(first, last)

    def period_is_complete(self, d: date, timeframe: str) -> bool:
        """True only when the calendar is known through the end of the period's span."""
        _, last = self.period_key(d, timeframe)
        day = self.known_through + timedelta(days=1)
        while day <= last:  # unknown days left after known_through must all be weekends
            if day.weekday() < 5:
                return False
            day += timedelta(days=1)
        return True
