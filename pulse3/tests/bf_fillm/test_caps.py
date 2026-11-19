"""Point-in-time circulating-cap series for the playbook panel: the pure rules, no look-ahead, and a real-PostgreSQL run of the cap playbooks.
SYNTHETIC data: proves the rules and the wiring, not the value of the strategies."""
from __future__ import annotations

from datetime import date, datetime, timezone

import polars as pl
import pytest

from pulse.application import bindings as B
from pulse.domain.playbooks import capseries as CS
from pulse.domain.playbooks.builder import RawSeries, build_views
from pulse.domain.playbooks.capseries import CapVersion

UTC = timezone.utc
DAYS = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30", "2026-11-02", "2026-11-03"]
CLOSES = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0]


def v(as_of: str, cap: float, avail: str) -> CapVersion:
    return CapVersion(date.fromisoformat(as_of), cap, datetime.fromisoformat(avail).replace(tzinfo=UTC))


def test_a_session_sees_only_versions_known_at_that_session_and_scales_by_the_close_ratio():
    versions = [v("2026-10-28", 1200.0, "2026-10-28T01:00:00"), v("2026-11-02", 3000.0, "2026-11-02T01:00:00")]
    out = CS.pit_float_cap_series(DAYS, CLOSES, versions, usable_at=B.policy_usable_at)
    assert out[:2] == [None, None], "before the first version there is no cap: never back-filled from a later snapshot"
    assert out[2] == pytest.approx(1200.0)                       # the version's own day: recorded before that session's usable time
    assert out[3] == pytest.approx(1200.0 * 13 / 12)
    assert out[4] == pytest.approx(1200.0 * 14 / 12)
    assert out[5] == pytest.approx(3000.0)                       # the newer version takes over on its own day
    assert out[6] == pytest.approx(3000.0 * 16 / 15)


def test_a_version_recorded_after_the_session_is_not_usable_that_day_under_the_observed_policy():
    # as-of date is the session itself but the record arrived two days later: not known at that session
    late = [v("2026-10-28", 1200.0, "2026-10-30T10:00:00")]
    out = CS.pit_float_cap_series(DAYS, CLOSES, late, usable_at=B.policy_usable_at)
    assert out[2:5] == [None, None, None], "recorded 10-30 10:00 UTC, after the usable time of sessions up to 10-30 (08:00 UTC)"
    assert out[5] == pytest.approx(1200.0 * 15 / 12)
    recon = CS.pit_float_cap_series(DAYS, CLOSES, late, usable_at=None)     # reconstructed policy: the as-of date alone decides
    assert recon[2] == pytest.approx(1200.0)


def test_a_stale_version_and_a_missing_reference_close_give_no_cap():
    old = [v("2025-01-02", 1000.0, "2025-01-02T10:00:00")]
    assert CS.pit_float_cap_series(DAYS, CLOSES, old, usable_at=None) == [None] * len(DAYS)       # older than MAX_VERSION_AGE_DAYS
    ok = [v("2026-10-26", 1000.0, "2026-10-26T01:00:00")]
    gap = CS.pit_float_cap_series(DAYS, [None, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0], ok, usable_at=None)
    assert gap[0] is None and gap[1] is None, "no close on or before the as-of date: no reference, no cap"
    assert CS.pit_float_cap_series(DAYS, [None] * 7, ok, usable_at=None) == [None] * 7
    assert CS.pit_float_cap_series(DAYS, CLOSES, [v("2026-10-26", 0.0, "2026-10-26T01:00:00")], usable_at=None) == [None] * 7


def test_the_series_at_a_session_does_not_depend_on_later_bars_or_versions():
    versions = [v("2026-10-28", 1200.0, "2026-10-28T01:00:00"), v("2026-11-02", 3000.0, "2026-11-02T01:00:00")]
    full = CS.pit_float_cap_series(DAYS, CLOSES, versions, usable_at=B.policy_usable_at)
    for k in range(1, len(DAYS) + 1):
        cut = CS.pit_float_cap_series(DAYS[:k], CLOSES[:k], versions, usable_at=B.policy_usable_at)
        assert cut == full[:k], f"cap at session {k - 1} changed when later data were removed"


def test_the_view_carries_the_series_and_truncation_keeps_it_aligned():
    series = RawSeries("i1", "main", tuple(DAYS), tuple(CLOSES), tuple(CLOSES), tuple(CLOSES), tuple(CLOSES), tuple([1000.0] * 7), "600001", None)
    views = build_views([series], DAYS)
    cap = CS.pit_float_cap_series(DAYS, CLOSES, [v("2026-10-28", 1200.0, "2026-10-28T01:00:00")], usable_at=None)
    from dataclasses import replace
    view = replace(views[0], float_cap=cap)
    assert view.truncate(4).float_cap == cap[:4]


# ---- real PostgreSQL: the cap playbooks run on cap versions, not on a scaled latest snapshot -----------------------------------------
pytestmark_pg = pytest.mark.pg


@pytest.mark.pg
def test_cap_playbooks_run_when_a_version_is_usable_and_fail_with_the_reason_when_none_is(world, built, conn, lake):
    from api_seed import seed_sources
    from bf_fillm.support import seed_frame
    from pulse.workers import research as W

    cap_ids = ["PB:small_cap", "PB:high_low_switch", "PB:microcap_timed", "PB:low_risk_combo", "PB:low_turnover_low_vol"]
    W.ext_b_register(conn)
    # 1. the world's only cap version is dated at its last session: no session of the evaluation slices can use it
    out = W.ext_b_run_catalog(conn, lake, cap_ids, key="caps-1")
    for r in out:
        assert r["result"]["state"] == "failed" and r["result"]["error"] == "data_not_available:float_cap:no_instrument_has_a_cap_version_usable_in_the_panel", r["catalog_id"]
    # 2. an early cap version for every instrument (as-of the first session, recorded then): the playbooks evaluate
    first = conn.execute("select min(trade_date) d from market.bar_projection where timeframe='1d'").fetchone()["d"]
    ids = [r["instrument_id"] for r in conn.execute("select distinct instrument_id from market.bar_projection where timeframe='1d'")]
    seed_sources(conn)
    rows = [{"instrument_id": str(i), "as_of": first.isoformat(), "float_cap_cny": 5_000_000_000, "total_cap_cny": None,
             "available_at": datetime(first.year, first.month, first.day, 1, tzinfo=UTC)} for i in ids]
    frame = pl.DataFrame(rows, schema={"instrument_id": pl.String, "as_of": pl.String, "float_cap_cny": pl.Decimal(38, 2), "total_cap_cny": pl.Decimal(38, 2),
                                       "available_at": pl.Datetime("us", "UTC")})
    seed_frame(conn, lake.root, B.CAP_DATASET, frame, ready_at=datetime(first.year, first.month, first.day, 2, tzinfo=UTC))
    out = W.ext_b_run_catalog(conn, lake, cap_ids, key="caps-2")
    for r in out:
        assert r["result"]["state"] != "failed", (r["catalog_id"], r["result"].get("error"))
