"""Held limit-up rules of the intraday paper replay: the M3 break state machine and the calendar catalyst (triggers v2.1, E053-E057).

Why: a holding that sealed and then opened is either a benign divergence or the start of an exit. M3 decides it from four checks: the price is
above the day's average price (VWAP), at least two peers of the same industry are sealed, it is not late in the day, and the catalyst is still ahead.
Two versions run side by side: M3-Tape (an unknown catalyst does not veto; only a catalyst known to have landed does) and M3-Strict (all four,
the catalyst must be known to be pending). EXIT wins over benign: 90 s under VWAP or a sector dump exits, and it does not depend on the version.
The catalyst comes from calendar events linked to stocks by industry (``event-links-v1``, frozen rules in ``fwdlog_data``); an event counts from
the moment it was first seen; macro releases never count.
Invariants: an unknown input is ``None`` and never passes a check; the same inputs give the same state; events are point in time (``first_seen_at``).
Not covered: reading the calendar store (``application.bf_fwdlog_inputs``), the author-consensus modules M2/M4 of the old design (not part of this experiment).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

VWAP_GRACE_S = 90.0
LATE_BREAK = time(14, 20)
PEERS_MIN = 2
SECTOR_DUMP_PCT = -1.0
EVENT_LINKS_VERSION = "event-links-v1"
NOT_CATALYST = frozenset({"macro_release"})        # routine releases never count as a stock's catalyst even when linked


class BreakState(str, Enum):
    SEALED = "sealed"
    BROKEN_WATCH = "broken_watch"
    BENIGN = "benign"
    RESEALED = "resealed"
    EXIT = "exit"


@dataclass(frozen=True)
class BreakCheck:
    state: BreakState
    reason: str = ""


def break_step(prev: BreakState, at: datetime, sealed: bool, above_vwap: bool | None, below_vwap_s: float, peers_sealed: int | None,
               catalyst_pending: bool | None, sector_dump: bool | None, variant: str = "strict") -> BreakCheck:
    """One step for a held limit-up. ``catalyst_pending``: True = the event has not landed, False = it landed, None = unknown."""
    if prev is BreakState.EXIT:
        return BreakCheck(BreakState.EXIT, "exit already signalled")
    if sealed:
        return BreakCheck(BreakState.RESEALED if prev in (BreakState.BROKEN_WATCH, BreakState.BENIGN) else BreakState.SEALED)
    if sector_dump or below_vwap_s >= VWAP_GRACE_S:
        return BreakCheck(BreakState.EXIT, "sector dump" if sector_dump else f"below VWAP {below_vwap_s:.0f} s")
    normal = at.astimezone(ZoneInfo("Asia/Shanghai")).time() < LATE_BREAK
    tape = above_vwap is True and (peers_sealed or 0) >= PEERS_MIN and normal
    benign = tape and (catalyst_pending is not False if variant == "tape" else catalyst_pending is True)
    return BreakCheck(BreakState.BENIGN if benign else BreakState.BROKEN_WATCH, "" if benign else "checks not all passed")


# ---- calendar catalyst ----------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class CalEvent:
    """One calendar event revision as the old store holds it (read only)."""
    event_id: str
    name: str
    category: str
    status: str
    event_date: date
    first_seen_at: datetime
    event_end_date: date | None = None
    event_at: datetime | None = None
    event_timezone: str | None = None
    result_known_at: datetime | None = None
    instruments: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class LinkRule:
    pattern: str
    industries: tuple[str, ...]
    theme: str


def link_rule_hits(name: str, category: str, rules: Sequence[LinkRule], skip_categories: Iterable[str]) -> tuple[str, ...]:
    """Industries of every rule whose pattern the event name matches (none for a skipped category)."""
    if category in set(skip_categories):
        return ()
    out: list[str] = []
    for r in rules:
        if re.search(r.pattern, name):
            out.extend(i for i in r.industries if i not in out)
    return tuple(out)


def link_events(events: Iterable[CalEvent], members: Mapping[str, frozenset[str]], rules: Sequence[LinkRule],
                skip_categories: Iterable[str]) -> list[CalEvent]:
    """Copies of the events with ``instruments`` filled from the rules and the industry members known on the decision date."""
    skip = tuple(skip_categories)
    out = []
    for ev in events:
        industries = link_rule_hits(ev.name, ev.category, rules, skip)
        if not industries:
            out.append(ev)
            continue
        codes = set(ev.instruments)
        for i in industries:
            codes |= members.get(i, frozenset())
        out.append(CalEvent(ev.event_id, ev.name, ev.category, ev.status, ev.event_date, ev.first_seen_at, ev.event_end_date, ev.event_at,
                            ev.event_timezone, ev.result_known_at, frozenset(codes)))
    return out


def _bounds(ev: CalEvent) -> tuple[datetime, datetime]:
    """When the event starts and when its window is over, as aware datetimes."""
    tz = ZoneInfo(ev.event_timezone or "Asia/Shanghai")
    start = ev.event_at or datetime.combine(ev.event_date, time(0, 0), tz)
    last = ev.event_end_date or ev.event_date
    end = max(start, datetime.combine(last + timedelta(days=1), time(0, 0), tz))
    return start, end


def catalyst_pending(events: Iterable[CalEvent], code: str, now: datetime) -> bool | None:
    """True = a linked event is still ahead, False = every linked event has landed (result known or window over), None = no linked event known by ``now``
    or an event is under way. One pending linked event outweighs landed ones: something is still ahead."""
    seen = [e for e in events if code in e.instruments and e.category not in NOT_CATALYST and e.first_seen_at <= now and e.status != "cancelled"]
    if not seen:
        return None
    pending = False
    for e in seen:
        start, end = _bounds(e)
        landed = (e.result_known_at is not None and e.result_known_at <= now) or now >= end
        if not landed and now < start:
            pending = True
    if pending:
        return True
    return False if all((e.result_known_at is not None and e.result_known_at <= now) or now >= _bounds(e)[1] for e in seen) else None
