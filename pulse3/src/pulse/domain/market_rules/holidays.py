"""Exchange holiday schedule: frozen official closures for the years pulse3 must count FORWARD (2026, 2027).

Why: past sessions are observed from bars, but a forward book or a replay tail has to count sessions that have not happened.
Monday-Friday is wrong around exchange holidays, so future counting must know the official closure dates (legacy row #121).
Invariants:
* a year is ``COMPLETE`` only with a citation: the notice's title, its issuer, its publication date and the exact closed dates;
  a year without one is ``MISSING`` and says why, it never falls back to remembered dates;
* ``is_open`` answers ``None`` (unknown) for a weekday of a MISSING year; callers that must continue (T+1 tails) use
  ``weekday_tail`` which keeps the Monday-Friday assumption and reports ``assumed`` so the result can say so;
* weekends are never listed (they are closed anyway); only weekday closures are data.
Status 2026-11-20: 2026 is COMPLETE from the SSE notice 上证公告〔2025〕45号 (published 2025-12-22); its paragraph is stored verbatim in
``data/sse_2026_holiday_notice.txt`` and cross-checked against the State Council General Office notice on the 2026 public holidays
(2025-11-04, read as the Xinhua authorised release; the gov.cn page itself was not fetched). 2027 stays MISSING until the exchanges
publish it (December 2026). To fill a year: add a ``YearSchedule`` with ``status="COMPLETE"`` and the four citation fields;
``tests/bf_fillm/test_calendar.py`` locks the rules and checks the 2026 dates against the stored notice text.
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


SSE_2026_NOTICE = YearSchedule(
    2026, COMPLETE,
    # the weekday days of every closure range in the notice (weekends inside a range, and the notice's "周末休市" make-up Saturdays/Sundays, are not data)
    closed=frozenset(date(2026, m, d) for m, d in (
        (1, 1), (1, 2),                                              # 元旦: 1/1-1/3
        (2, 16), (2, 17), (2, 18), (2, 19), (2, 20), (2, 23),        # 春节: 2/15-2/23
        (4, 6),                                                      # 清明节: 4/4-4/6
        (5, 1), (5, 4), (5, 5),                                      # 劳动节: 5/1-5/5
        (6, 19),                                                     # 端午节: 6/19-6/21
        (9, 25),                                                     # 中秋节: 9/25-9/27
        (10, 1), (10, 2), (10, 5), (10, 6), (10, 7))),               # 国庆节: 10/1-10/7
    notice_title="关于上海证券交易所2026年部分节假日休市安排的通知",
    issuer="上海证券交易所 (上证公告〔2025〕45号)",
    published_on=date(2025, 12, 22),
    source_ref="https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml",
)
OFFICIAL_SCHEDULES: dict[int, YearSchedule] = {
    2026: SSE_2026_NOTICE,
    2027: YearSchedule(2027, MISSING, missing_reason="not published as of 2026-11-20 (the exchanges announce the next year in December)"),
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
