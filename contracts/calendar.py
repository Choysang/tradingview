"""Event calendar contracts (design supplement 2026-11-02: key dates, expectations, overseas mapping).

Three times per event, never merged: when the schedule became known (`schedule_known_at`), when the event
happens (`event_at` / `event_date`), when its result became known (`result_known_at`). Plus when our system
first saw the record (`first_seen_at`, set on import, never back-filled) and when a trading hypothesis about it
was first registered (`hypothesis_registered_at`). A reschedule, cancellation or agenda change is a new
version (`revision`, `supersedes`). Unknown precision stays unknown: no invented minutes.
"""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from contracts.core import Core, _aware


class EventCategory(str, Enum):
    MARKET_RULE = "market_rule"  # derivative expiries, rule changes, sessions
    POLICY_MEETING = "policy_meeting"
    MACRO_RELEASE = "macro_release"
    CONFERENCE = "conference"  # industry conferences and exhibitions
    COMPANY = "company"  # earnings, launches, shareholder meetings, approvals
    SHARE_INDEX = "share_index"  # unlocks, ex-dividend, buyback deadlines, index changes
    HOLIDAY = "holiday"
    FUNDING = "funding"  # month / quarter / year end
    ANNIVERSARY = "anniversary"
    NARRATIVE = "narrative"  # zodiac, homophones, number play
    OVERSEAS_POLICY = "overseas_policy"  # elections, tariffs, central banks
    OVERSEAS_MARKET = "overseas_market"  # foreign earnings, sessions, holidays


class Precision(str, Enum):
    MINUTE = "minute"
    APPROX_MINUTE = "approximate_minute"
    DATE = "date"
    DATE_RANGE = "date_range"
    UNKNOWN = "unknown"


class ScheduleStatus(str, Enum):
    OFFICIAL = "official_scheduled"
    RULE_COMPUTED = "rule_computed_pending_monthly_notice"
    EXPECTED_WINDOW = "expected_window"  # e.g. an earnings season window, not a set date
    RESCHEDULED = "rescheduled"
    CANCELLED = "cancelled"
    HAPPENED = "happened"


class Milestone(Core):
    name: str
    event_at: datetime | None = None
    event_date: date | None = None
    precision: Precision

    _tz = field_validator("event_at")(_aware)


class CalendarEvent(Core):
    event_id: str
    revision: int = Field(ge=0)
    supersedes: int | None = None
    name: str
    category: EventCategory
    region: Literal["CN", "HK", "US", "JP", "KR", "EU", "GLOBAL"]
    status: ScheduleStatus
    event_date: date
    event_end_date: date | None = None
    event_at: datetime | None = None  # only when the minute is published
    event_timezone: str | None = None
    precision: Precision
    milestones: tuple[Milestone, ...] = ()
    source_url: str
    source_announcement_date: date | None = None
    schedule_known_at: datetime | None = None  # earliest proven public time; None = not proven
    first_seen_at: datetime  # our import or capture time
    result_known_at: datetime | None = None
    hypothesis_registered_at: datetime | None = None
    themes: tuple[str, ...] = ()
    instruments: tuple[str, ...] = ()  # linked stocks with the relation kept elsewhere (contracts.news.Impact)
    notes: str = ""

    _tz = field_validator("event_at", "schedule_known_at", "first_seen_at", "result_known_at", "hypothesis_registered_at")(_aware)

    @model_validator(mode="after")
    def _consistent(self) -> CalendarEvent:
        if self.precision in (Precision.MINUTE, Precision.APPROX_MINUTE) and self.event_at is None:
            raise ValueError("a minute-precision event needs event_at")
        if self.precision not in (Precision.MINUTE, Precision.APPROX_MINUTE) and self.event_at is not None:
            raise ValueError("event_at is only for minute-precision events")
        if self.event_end_date and self.event_end_date < self.event_date:
            raise ValueError("event_end_date before event_date")
        if (self.revision == 0) != (self.supersedes is None):
            raise ValueError("revision 0 has no predecessor; later revisions name one")
        return self

    def usable_at(self, decision: datetime) -> bool:
        """A schedule may be used by a decision only if we had it by then (first_seen_at), whatever the source says."""
        return self.first_seen_at <= decision
