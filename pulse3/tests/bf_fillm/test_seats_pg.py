"""attach_seats on a real PostgreSQL: which seat reports are read (single-day, newest revision, usable quality, inside the panel window),
unknown days stay unknown, and an empty window is a reason, not an evaluation on nothing. SYNTHETIC reports: proves wiring and invariants."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg
import pytest

from pulse.application import bf_playbook_seats as BPS
from pulse.domain.playbooks.builder import RawSeries, build_views

DAYS = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30", "2026-11-02", "2026-11-03"]
NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)


@dataclass(frozen=True)
class FakePanel:
    views: list
    sessions: list


def fake_panel(iid: str) -> FakePanel:
    raw = RawSeries(iid, "main", tuple(DAYS), (10.0,) * 7, (11.0,) * 7, (9.0,) * 7, (10.5,) * 7, (1e7,) * 7)
    return FakePanel(build_views([raw], DAYS), list(DAYS))


@pytest.fixture()
def conn(world):
    c = psycopg.connect(world["dsn"], autocommit=False, row_factory=psycopg.rows.tuple_row)
    c.execute("SET session_replication_role = replica")             # the seat tables reference raw objects and sources this test does not need
    yield c
    c.rollback()
    c.close()


def put(conn, iid: str, start: str, end: str, rows, *, report: str, revision: int = 1, quality: str = "valid") -> None:
    rid = uuid.uuid4()
    conn.execute(
        "INSERT INTO intelligence.seat_report(report_revision_id, source_id, raw_object_id, source_report_id, revision, exchange_code, instrument_id, window_start, window_end,"
        " disclosure_reason, rule_version, trade_scope, published_at, available_at, recorded_at, quality_status)"
        " VALUES (%s,'test_src',%s,%s,%s,'SSE',%s,%s,%s,'r','v1','unknown',NULL,%s,%s,%s)",
        (rid, uuid.uuid4(), report, revision, iid, start, end, NOW, NOW, quality))
    for k, (side, name, amt) in enumerate(rows):
        conn.execute(
            "INSERT INTO intelligence.seat_report_row(report_revision_id, side, rank, raw_seat_name, raw_amount, raw_unit, canonical_amount_cny, resolution_status, resolution_evidence)"
            " VALUES (%s,%s,%s,%s,%s,'CNY',%s,'report_local_label',%s)", (rid, side, k + 1, name, amt, amt, json.dumps({})))


def test_reads_single_day_newest_revision_usable_quality_inside_the_window(conn):
    iid = str(uuid.uuid4())
    put(conn, iid, "2026-10-28", "2026-10-28", [("buy", "机构专用", 1e7)], report="a", revision=1)
    put(conn, iid, "2026-10-28", "2026-10-28", [("buy", "机构专用", 5e7), ("sell", "机构专用", 1e7)], report="a", revision=2)        # replaces revision 1
    put(conn, iid, "2026-10-27", "2026-10-29", [("buy", "机构专用", 9e9)], report="multi")                                      # three-day window: not attributable to a day
    put(conn, iid, "2026-10-29", "2026-10-29", [("buy", "机构专用", 9e9)], report="bad", quality="invalid")
    put(conn, iid, "2026-11-20", "2026-11-20", [("buy", "机构专用", 9e9)], report="future")                                     # after the last panel session
    panel, cov = BPS.attach_seats(conn, fake_panel(iid))
    assert cov.reason is None and cov.reports == 1 and cov.known_days == 1 and cov.first_day == cov.last_day == "2026-10-28"
    col = panel.views[0].seats
    assert col[2][0] is True and col[2][4] == pytest.approx(4e7), "newest revision: buy 5e7 minus sell 1e7"
    assert col[1] is None and col[3] is None, "days without an imported list stay unknown"


def test_a_window_without_any_usable_report_is_a_reason_and_leaves_the_panel_unchanged(conn):
    iid = str(uuid.uuid4())
    panel0 = fake_panel(iid)
    panel, cov = BPS.attach_seats(conn, panel0)
    assert cov.reason == "no_seat_report_in_the_panel_window" and panel is panel0
    assert all(v.seats is None for v in panel0.views)
