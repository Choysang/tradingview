"""Raw daily coverage gate and the full-universe raw refresh (server build of 2026-11-12 failed with an empty universe).

Why: the research (forward-adjusted) snapshot advanced to a session whose raw daily bars had not been ingested, so
every instrument lacked an execution-grade close and the build failed deep inside ranking with a misleading cascade.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import polars as pl
import pytest

from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db
from pulse.adapters.sources.legacy import LegacyWarehouse
from pulse.application.data_service import DataService
from pulse.application.release_service import RawCoverageMissing, require_raw_coverage

from .helpers import LEGACY_MINI

DAY = date(2026, 11, 12)


def _research(n: int) -> SimpleNamespace:
    ids = [str(uuid.uuid4()) for _ in range(n)]
    bars = pl.DataFrame({"instrument_id": ids + ids, "trade_date": [DAY] * n + [date(2026, 11, 11)] * n})
    return SimpleNamespace(bars=bars, session_date=DAY)


def test_session_without_raw_closes_is_refused_with_the_cause():
    with pytest.raises(RawCoverageMissing, match="raw daily bars missing for session 2026-11-12: 0 raw closes for 10"):
        require_raw_coverage(_research(10), {})


def test_session_with_enough_raw_closes_passes():
    require_raw_coverage(_research(10), {uuid.uuid4(): Decimal("1.00") for _ in range(5)})


def test_universe_codes_are_the_imported_legacy_codes(fresh_dsn, tmp_path):
    from pulse.workers.ingest import universe_codes

    conn = db.connect(fresh_dsn, autocommit=True)
    try:
        svc = DataService(conn, LakeStore(tmp_path / "lake"))
        svc.bootstrap()
        assert universe_codes(conn) == []
        svc.import_legacy(LegacyWarehouse(LEGACY_MINI), date(2026, 10, 1))
        codes = universe_codes(conn)
        assert codes and codes == sorted(set(codes)) and all(len(c) == 6 for c in codes)
    finally:
        conn.close()
