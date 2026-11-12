"""Incremental re-reads of older legacy files must not close a newer instrument state (server crash 2026-11-13)."""
from datetime import date, datetime, timezone

import psycopg
import pytest
from psycopg.rows import dict_row

from pulse.adapters.postgres import instrument_repo as R
from pulse.domain.instruments.identity import InstrumentRecord

pytestmark = pytest.mark.pg


def test_older_observation_after_newer_state_is_ignored(pg_dsn):
    now = datetime(2026, 11, 13, tzinfo=timezone.utc)
    with psycopg.connect(pg_dsn, row_factory=dict_row) as c:
        normal = InstrumentRecord.build("000972", "中基健康")
        st = InstrumentRecord.build("000972", "*ST中基")
        R.upsert_instruments(c, [normal], now, [], "test")
        assert R.append_state_if_changed(c, normal, date(2026, 10, 1), now, "f1") == 1
        assert R.append_state_if_changed(c, st, date(2026, 11, 4), now, "f2") == 2
        # an older file re-read during an incremental import
        assert R.append_state_if_changed(c, normal, date(2026, 10, 30), now, "f0") is None
        rows = c.execute("SELECT state_version, effective_from, effective_to FROM core.instrument_state"
                         " WHERE instrument_id=%s ORDER BY state_version", (normal.instrument_id,)).fetchall()
        assert [r["state_version"] for r in rows] == [1, 2] and rows[-1]["effective_to"] is None
        c.rollback()
