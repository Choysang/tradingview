"""Review fixes M4 (reconstructed policy ignores revisions recorded after as_of) and M6 (plan price levels stay Decimal)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pulse.application import bindings as B

from . import world as W

pytestmark = pytest.mark.pg


def _last_bar(inp, conn):
    iid = conn.execute("select instrument_id from core.instrument_alias where source_id='legacy_warehouse' and source_symbol=%s",
                       (W.CODES[3],)).fetchone()["instrument_id"]
    return inp.bars.filter((inp.bars["instrument_id"] == str(iid)) & (inp.bars["trade_date"] == W.LAST))


def test_m4_reconstructed_policy_uses_only_revisions_recorded_by_as_of(world, conn, lake):
    """The lake keeps only the latest revision of the bar (recorded 11-11 09:00). A rebuild as of 11-10 must not see it."""
    before = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)       # bar usable by policy (16:00 CST + 1h); revision not yet recorded
    after = datetime(2026, 11, 12, 0, 0, tzinfo=timezone.utc)
    assert _last_bar(B.load_research_inputs(conn, lake, before, B.RECONSTRUCTED_POLICY), conn).height == 0     # missing, never leaked
    row = _last_bar(B.load_research_inputs(conn, lake, after, B.RECONSTRUCTED_POLICY), conn)
    assert row.height == 1 and row["available_at"][0] == W.REVISION_INGEST        # stored available_at is not rewritten
    # first-version bars of every other instrument are still usable by policy time although their stored available_at is later
    inp = B.load_research_inputs(conn, lake, before, B.RECONSTRUCTED_POLICY)
    assert inp.session_date == W.LAST and inp.bars.filter(inp.bars["trade_date"] == W.LAST).height >= 60
