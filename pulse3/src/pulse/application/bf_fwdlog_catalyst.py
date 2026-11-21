"""M3 catalyst events from pulse3's own calendar (``calendar.event_revision``), the switch, and a comparison with the legacy dataset (#174).

Why: ``bf_fwdlog_service._events`` reads the imported ``legacy_calendar_events`` dataset. After the cutover the calendar is kept by
``alerts calendar-sync`` / ``calendar-derive``; this module reads that store and maps it to the same ``BD.CalEvent`` fields.
Switch: env ``PULSE_CATALYST_SOURCE`` = ``legacy`` (default, unchanged behaviour) | ``native``; any other value raises, and ``native`` never falls back to
legacy when the native calendar is empty (the run notes "absent: M3 catalyst unknown" instead).
Field mapping (native -> CalEvent): ``event_id``=source_event_key; ``event_date``=scheduled_date, else the Asia/Shanghai date of scheduled_at; ``event_at``=scheduled_at
(minute precision only); ``first_seen_at``=available_at of the newest visible revision (point in time: the cutoff hides later revisions); ``result_known_at``=available_at
of the first visible revision that carries a result (``actual`` or status occurred/corrected); ``instruments``=the event's own stock plus the stocks of its industry links
resolved at the cutoff. ``event_end_date`` is None: the native store keeps no end date, so a multi-day event counts as its first day (differs from the legacy file).
Not covered: writing to either store; matching by event id (the two stores use different keys, so the comparison matches on category + date + name).
"""
from __future__ import annotations

import hashlib
import os
from collections import Counter
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import psycopg

from pulse.domain.fwdlog import board as BD

CST = ZoneInfo("Asia/Shanghai")
ENV = "PULSE_CATALYST_SOURCE"
MODES = ("legacy", "native")
NOTE_EMPTY = "native calendar has no event visible at the cutoff (alerts calendar-sync / calendar-derive not run?)"

LATEST_SQL = """
WITH latest AS (
  SELECT DISTINCT ON (calendar_event_id) *
    FROM calendar.event_revision WHERE available_at <= %(cut)s ORDER BY calendar_event_id, revision DESC),
known AS (
  SELECT calendar_event_id, min(available_at) AS known_at
    FROM calendar.event_revision WHERE available_at <= %(cut)s AND (actual IS NOT NULL OR status IN ('occurred','corrected')) GROUP BY 1)
SELECT l.calendar_event_id::text AS cid, l.revision, l.source_event_key, l.name, l.category, l.status, l.scheduled_at, l.scheduled_date, l.available_at, k.known_at,
       (SELECT a.source_symbol FROM core.instrument_alias a WHERE a.instrument_id = l.instrument_id ORDER BY a.source_symbol DESC LIMIT 1) AS own_code
  FROM latest l LEFT JOIN known k USING (calendar_event_id)
"""
LINKS_SQL = """
WITH latest AS (
  SELECT DISTINCT ON (calendar_event_id) calendar_event_id, revision
    FROM calendar.event_revision WHERE available_at <= %(cut)s ORDER BY calendar_event_id, revision DESC)
SELECT DISTINCT l.calendar_event_id::text AS cid,
       (SELECT a.source_symbol FROM core.instrument_alias a WHERE a.instrument_id = t.instrument_id ORDER BY a.source_symbol DESC LIMIT 1) AS code
  FROM latest x
  JOIN calendar.event_link l ON l.calendar_event_id = x.calendar_event_id AND l.revision = x.revision
  JOIN core.instrument_tag t ON t.taxonomy_id = l.taxonomy_id AND t.taxonomy_version = l.taxonomy_version AND t.node_id = l.node_id
 WHERE t.effective_from <= %(cut)s AND (t.effective_to IS NULL OR t.effective_to > %(cut)s) AND t.available_at <= %(cut)s
"""


def source_mode(environ: dict | None = None) -> str:
    """``legacy`` (default) or ``native``; an unknown value raises instead of silently choosing one."""
    raw = (environ if environ is not None else os.environ).get(ENV)
    if raw is None or raw.strip() == "":
        return "legacy"
    mode = raw.strip().lower()
    if mode not in MODES:
        raise ValueError(f"{ENV}={raw!r}: expected one of {MODES}")
    return mode


def _event_date(scheduled_date: date | None, scheduled_at: datetime | None) -> date:
    if scheduled_date is not None:
        return scheduled_date
    return scheduled_at.astimezone(CST).date()


def read_native_events(conn: psycopg.Connection, cutoff: datetime | None = None) -> tuple[list[BD.CalEvent], dict]:
    """Newest revision of every native calendar event visible at ``cutoff`` (None = now), as ``BD.CalEvent``, and a note of what was read."""
    cut = cutoff or datetime.now(timezone.utc)
    rows = conn.execute(LATEST_SQL, {"cut": cut}).fetchall()
    linked: dict[str, set[str]] = {}
    for r in conn.execute(LINKS_SQL, {"cut": cut}).fetchall():
        if r["code"]:
            linked.setdefault(r["cid"], set()).add(r["code"])
    out: list[BD.CalEvent] = []
    for r in rows:
        codes = set(linked.get(r["cid"], ()))
        if r["own_code"]:
            codes.add(r["own_code"])
        at = r["scheduled_at"]
        out.append(BD.CalEvent(r["source_event_key"], r["name"], r["category"], r["status"], _event_date(r["scheduled_date"], at), r["available_at"],
                               None, at, "Asia/Shanghai" if at is not None else None, r["known_at"], frozenset(codes)))
    digest = hashlib.sha256("\n".join(sorted(f"{e.event_id}|{e.status}|{e.first_seen_at.isoformat()}" for e in out)).encode()).hexdigest()
    info = {"source": "pulse3_calendar_native", "path_present": bool(out), "events": len(out), "sha256": digest if out else None,
            "latest_first_seen_at": max((e.first_seen_at for e in out), default=None)}
    if not out:
        info["note"] = NOTE_EMPTY
    return out, info


# ---- comparison with the legacy dataset --------------------------------------------------------------------------------------------
def _key(e: BD.CalEvent) -> tuple[str, date, str]:
    return e.category, e.event_date, " ".join(e.name.split())


def compare(legacy: list[BD.CalEvent], native: list[BD.CalEvent], sample: int = 10) -> dict:
    """Counts by category on each side, the events only on one side (category + date + name) and how many matched ones link different stocks."""
    lk, nk = {_key(e): e for e in legacy}, {_key(e): e for e in native}
    both = lk.keys() & nk.keys()
    differ = sorted(k for k in both if lk[k].instruments != nk[k].instruments)

    def row(e: BD.CalEvent) -> dict:
        return {"category": e.category, "date": e.event_date.isoformat(), "name": e.name, "status": e.status, "stocks": len(e.instruments)}

    return {"legacy_events": len(legacy), "native_events": len(native), "matched": len(both),
            "by_category": {c: {"legacy": Counter(e.category for e in legacy)[c], "native": Counter(e.category for e in native)[c]}
                            for c in sorted({e.category for e in legacy} | {e.category for e in native})},
            "only_legacy": len(lk.keys() - nk.keys()), "only_native": len(nk.keys() - lk.keys()),
            "only_legacy_sample": [row(lk[k]) for k in sorted(lk.keys() - nk.keys())[:sample]],
            "only_native_sample": [row(nk[k]) for k in sorted(nk.keys() - lk.keys())[:sample]],
            "matched_with_different_stocks": len(differ),
            "different_stocks_sample": [{**row(lk[k]), "legacy_stocks": len(lk[k].instruments), "native_stocks": len(nk[k].instruments)} for k in differ[:sample]]}


def compare_stores(conn: psycopg.Connection, lake, cutoff: datetime | None = None) -> dict:
    """``compare`` of the READY ``legacy_calendar_events`` dataset against the native calendar, with what each side read."""
    from pulse.application import bf_fillm_calendar as CAL

    legacy, linfo = CAL.read_calendar_events(conn, lake, cutoff)
    native, ninfo = read_native_events(conn, cutoff)
    out = compare(legacy, native)
    out["legacy_present"], out["native_present"] = linfo["present"], ninfo["path_present"]
    notes = []
    if not linfo["present"]:
        notes.append("legacy_calendar_events dataset is absent: nothing to compare against")
    if not ninfo["path_present"]:
        notes.append(NOTE_EMPTY)
    out["notes"] = notes
    return out
