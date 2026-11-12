"""Corporate calendar events from announcement titles already ingested by the I3/cninfo pipeline: lock-up expiries, suspensions, meetings.

Why: the old site's calendar had lock-up expiries and suspensions among its event classes. pulse3 already stores every cninfo
announcement as a claim (``announcement-claim-v1``); this module reads the TITLE and turns the ones that state a date into
calendar observations. Nothing is fetched here.
Invariants: an event is emitted only when the title itself contains an explicit date (``2026年11月20日`` or ``11月20日`` with the year
of the announcement); a title without a date (most lock-up notices state it only in the body) is counted as ``date_unresolved``
and NOT turned into an event dated at the announcement day (that would be a different fact); the announcement time is the
``available_at`` of the revision, never the event date; a title that says 取消 / 延期 / 更正 changes the status of the SAME event
key, it is not a new event.
Not covered: PDF bodies, share counts of the lock-up (not in titles).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from pulse.domain.calendar.events import Observation

SOURCE_ID = "cninfo_title_events"
_DATE_FULL = re.compile(r"(20\d{2})年(\d{1,2})月(\d{1,2})日")
_DATE_SHORT = re.compile(r"(?<!\d)(\d{1,2})月(\d{1,2})日")
_RULES: tuple[tuple[str, str, re.Pattern], ...] = (
    ("company_lockup", "限售股上市流通", re.compile(r"限售股.{0,24}(?:上市流通|解禁)|解除限售|解禁")),
    ("company_suspension", "停牌/复牌", re.compile(r"停牌|复牌")),
    ("company_meeting", "股东大会/说明会", re.compile(r"(?:股东大会|股东会|业绩说明会|投资者(?:说明会|关系活动))")),
)


@dataclass(frozen=True)
class Parsed:
    observations: list[Observation]
    date_unresolved: int
    ignored: int


def extract_date(title: str, announced: date) -> date | None:
    m = _DATE_FULL.search(title)
    try:
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = _DATE_SHORT.search(title)
        if m:
            d = date(announced.year, int(m.group(1)), int(m.group(2)))
            return d if (d - announced).days >= -30 else date(announced.year + 1, d.month, d.day)
    except ValueError:
        return None
    return None


def to_observations(rows: list[dict]) -> Parsed:
    """rows: {issuer_code, instrument_code, title, announced_at (datetime), source_record_id}."""
    obs: list[Observation] = []
    unresolved = ignored = 0
    for r in rows:
        title = r["title"] or ""
        for category, label, rx in _RULES:
            if not rx.search(title):
                continue
            when = extract_date(title, r["announced_at"].date())
            if when is None:
                unresolved += 1
                break
            key = f"{r['issuer_code']}:{category}:{when.isoformat()}"
            status = "cancelled" if re.search(r"取消|终止", title) else "scheduled"
            obs.append(Observation(SOURCE_ID, key, f"{r.get('display_name') or r['issuer_code']} {label}", category, "CN", None, when, "date", status,
                                   instrument_code=r["issuer_code"], notes={"announcement_title": title, "source_record_id": r["source_record_id"],
                                                                           "date_basis": "explicit_date_in_title"}))
            break
        else:
            ignored += 1
    return Parsed(obs, unresolved, ignored)
