"""Trading calendar and session clock.

Holidays are configuration, not inference: the exchange publishes them, so an
operator maintains `backend/data/calendar.json`. Missing file means "weekdays
only", which is stated explicitly rather than silently assumed to be complete.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
import json
from pathlib import Path

from ..config import settings

CST_MORNING = (time(9, 30), time(11, 30))
CST_AFTERNOON = (time(13, 0), time(15, 0))
NO_LIMIT_LISTING_DAYS = 5


@dataclass(frozen=True)
class Calendar:
    holidays: frozenset[str] = frozenset()
    source: str = "weekday rule"

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5 and day.isoformat() not in self.holidays

    def session(self, moment: datetime) -> str:
        if not self.is_trading_day(moment.date()):
            return "closed"
        clock = moment.time()
        if clock < CST_MORNING[0]:
            return "pre_open"
        if CST_MORNING[0] <= clock <= CST_MORNING[1]:
            return "morning"
        if CST_MORNING[1] < clock < CST_AFTERNOON[0]:
            return "lunch_break"
        if CST_AFTERNOON[0] <= clock <= CST_AFTERNOON[1]:
            return "afternoon"
        return "closed"


def load_calendar(path: Path | None = None) -> Calendar:
    target = path or settings.data_dir / "calendar.json"
    if not target.exists():
        return Calendar()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        holidays = frozenset(str(item) for item in payload.get("holidays", []))
    except (OSError, ValueError):
        return Calendar()
    return Calendar(holidays=holidays, source=str(target))


def has_price_limit(days_since_listing: int | None) -> bool:
    """The first five sessions after listing have no price limit."""
    if days_since_listing is None:
        return True
    return days_since_listing >= NO_LIMIT_LISTING_DAYS
