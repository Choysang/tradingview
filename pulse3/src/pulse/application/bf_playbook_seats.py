"""Attach Dragon-Tiger seat features to the playbook panel (legacy rows smart_seat_follow / institution_net_buy, #145).

Why: two seat playbooks were ``_none`` ("no seat history"), yet ``intelligence.seat_report`` holds three years of single-day lists.
``attach_seats`` reads them and gives every view a ``seats`` series built by ``domain.playbooks.seats.seat_series``.
Invariants:
* only single-day reports (window_start == window_end), quality ``valid`` or ``partial``, newest revision of each report; multi-day windows are not attributable to a day;
* availability is rule-based, not read from ``available_at`` (the legacy import stamps it with the import time): the list of session D is published the evening of D, so bar D may read it
  and the signal executes at the D+1 open; a list dated after the last panel session is never read;
* a day with no imported list is UNKNOWN (``None`` in the series), never "nobody was listed"; ``SeatCoverage.reason`` is set when no usable list falls inside the panel;
* the input panel is not mutated (new views are built).
Not covered: seat identity merging (raw names), ST screening, intraday paths.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import psycopg

from pulse.domain.playbooks import seats as SE


@dataclass(frozen=True)
class SeatCoverage:
    reports: int
    rows: int
    known_days: int
    first_day: str | None
    last_day: str | None
    instruments_listed: int
    reason: str | None


SQL = """
SELECT DISTINCT ON (r.source_id, r.source_report_id)
       r.report_revision_id, r.instrument_id::text, r.window_end::text
  FROM intelligence.seat_report r
 WHERE r.window_start = r.window_end AND r.quality_status IN ('valid','partial')
   AND r.window_end BETWEEN %(lo)s AND %(hi)s
 ORDER BY r.source_id, r.source_report_id, r.revision DESC
"""
ROWS_SQL = """
SELECT report_revision_id, side, raw_seat_name, canonical_amount_cny
  FROM intelligence.seat_report_row
 WHERE report_revision_id = ANY(%(ids)s)
"""


def load_rows(conn: psycopg.Connection, lo: str, hi: str) -> tuple[list[SE.SeatRow], set[tuple[str, str]], set[str], int]:
    """(rows keyed by instrument id, listed (instrument, day) pairs, known days, report count) with window days in [lo, hi]."""
    with conn.cursor() as cur:
        cur.execute(SQL, {"lo": lo, "hi": hi})
        reps = cur.fetchall()
        meta = {rid: (iid, day) for rid, iid, day in reps}
        rows: list[SE.SeatRow] = []
        ids = list(meta)
        for k in range(0, len(ids), 20000):
            cur.execute(ROWS_SQL, {"ids": ids[k:k + 20000]})
            for rid, side, name, amt in cur.fetchall():
                iid, day = meta[rid]
                rows.append(SE.SeatRow(iid, day, side, name, None if amt is None else float(amt)))
    return rows, {(iid, day) for iid, day in meta.values()}, {day for _, day in meta.values()}, len(reps)


def attach_seats(conn: psycopg.Connection, panel):
    """(new panel with ``seats`` series on every view, SeatCoverage). The input panel is not changed."""
    if not panel.sessions:
        return panel, SeatCoverage(0, 0, 0, None, None, 0, "no_sessions")
    lo, hi = panel.sessions[0], panel.sessions[-1]
    rows, listed, known, n_rep = load_rows(conn, lo, hi)
    if not known:
        return panel, SeatCoverage(0, 0, 0, None, None, 0, "no_seat_report_in_the_panel_window")
    bars = {v.instrument_id: (v.dates, v.open, v.close) for v in panel.views}
    series = SE.seat_series(rows, listed, known, bars, panel.sessions)
    views = [replace(v, seats=series.get(v.instrument_id), xs={}, _cache={}) for v in panel.views]
    cov = SeatCoverage(n_rep, len(rows), len(known), min(known), max(known), len({i for i, _ in listed}), None)
    return replace(panel, views=views), cov
