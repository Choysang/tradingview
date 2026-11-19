"""Exchange holiday schedule: frozen official closures for the years pulse3 must count FORWARD (2026, 2027).

Why: past sessions are observed from bars, but a forward book or a replay tail has to count sessions that have not happened.
Monday-Friday is wrong around exchange holidays, so future counting must know the official closure dates (legacy row #121).
Invariants:
* a year is ``COMPLETE`` only with a citation: the notice's title, its issuer, its publication date and the exact closed dates;
  a year without one is ``MISSING`` and says why, it never falls back to remembered dates;
* ``is_open`` answers ``None`` (unknown) for a weekday of a MISSING year; callers that must continue (T+1 tails) use
  ``weekday_tail`` which keeps the Monday-Friday assumption and reports ``assumed`` so the result can say so;
* weekends are never listed (they are closed anyway); only weekday closures are data.
Status 2026-11-18: NEITHER year has a citable notice in the old repository or its docs. The old seed
(``docs/research/seeds/calendar_seed_2026-11-02.json``) only links the SSE notice ``c_20251222_10802507`` (published 2025-12-22) as
a secondary source and its text is not stored anywhere in the repo, so no date is entered. To fill a year: add a ``YearSchedule``
with ``status="COMPLETE"`` and the four citation fields; ``tests/bf_fillm/test_calendar.py`` locks the rules.
Not covered: special weekend make-up sessions (the exchanges' 调休 Saturday/Sunday are closed for stocks) and half-days.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

COMPLETE, MISSING = "COMPLETE", "MISSING"


@dataclass(frozen=True)
class YearSchedule:
    year: int
    status: str
    closed: frozenset[date] = frozenset()
    notice_title: str | None = None
    issuer: str | None = None
    published_on: date | None = None
    source_ref: str | None = None
    missing_reason: str | None = None

    def __post_init__(self) -> None:
        if self.status == COMPLETE:
            if not (self.notice_title and self.issuer and self.published_on and self.source_ref and self.closed):
                raise ValueError(f"{self.year}: a COMPLETE schedule needs the notice title, issuer, publication date, source and closed dates")
            if any(d.year != self.year or d.weekday() >= 5 for d in self.closed):
                raise ValueError(f"{self.year}: closed dates must be weekdays of that year")
        elif self.status == MISSING:
            if self.closed or not self.missing_reason:
                raise ValueError(f"{self.year}: a MISSING schedule carries no dates and says why")
        else:
            raise ValueError(f"unknown status {self.status}")


_NOT_IN_REPO = ("no official SSE/SZSE notice text is stored in the old repository or docs; the old calendar seed only links SSE notice "
                "c_20251222_10802507 (2025-12-22) without its dates, so none is entered (no dates from memory)")
OFFICIAL_SCHEDULES: dict[int, YearSchedule] = {
    2026: YearSchedule(2026, MISSING, missing_reason=_NOT_IN_REPO),
    2027: YearSchedule(2027, MISSING, missing_reason="not published as of 2026-11-18 in anything the repository holds (exchanges announce the next year in December)"),
}


def schedule_for(year: int, schedules: dict[int, YearSchedule] | None = None) -> YearSchedule | None:
    return (schedules if schedules is not None else OFFICIAL_SCHEDULES).get(year)


def is_open(day: date, schedules: dict[int, YearSchedule] | None = None) -> bool | None:
    """True/False when the calendar knows; ``None`` for a weekday whose year has no complete official schedule."""
    if day.weekday() >= 5:
        return False
    sch = schedule_for(day.year, schedules)
    if sch is None or sch.status != COMPLETE:
        return None
    return day not in sch.closed


def weekday_tail(last: date, n_days: int, schedules: dict[int, YearSchedule] | None = None) -> tuple[list[date], bool]:
    """(open days within ``n_days`` calendar days after ``last``, assumed).

    A day is dropped only when a COMPLETE official schedule closes it; for an unknown year the weekday rule stays and ``assumed``
    is True (the caller should say that holidays may be missing)."""
    out: list[date] = []
    assumed = False
    for k in range(1, n_days + 1):
        d = last + timedelta(days=k)
        state = is_open(d, schedules)
        if state is None:
            assumed = True
            state = True
        if state:
            out.append(d)
    return out, assumed
