"""Industry diffusion on seeded bars (real PostgreSQL + lake): a pack of limit-ups with a leader, followers and a per-session spread.
The synthetic world has no limit-ups, so this test builds its own six-stock industry. SYNTHETIC rows: they prove the rules, not the market."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest

from bf_fillm.support import cutoff_after, seed_closes, seed_equity, seed_frame, seed_manifest, raw_coverage, weekdays
from pulse.adapters.lake import LakeStore
from pulse.application import bf_fillm_crowd as CROWD

pytestmark = pytest.mark.pg
UTC = timezone.utc
N = 12
QUIET = "10.00"


def _series(last_moves: list[str]) -> list[str]:
    """N closes: quiet 10.00 until the last ``len(last_moves)`` days, which are the given closes."""
    return [QUIET] * (N - len(last_moves)) + last_moves


def test_leader_followers_and_spread_follow_the_rule(fdb, tmp_path):
    days = weekdays(date(2026, 10, 1), N)
    snap = seed_manifest(fdb, "bars", raw_coverage(days[0], days[-1]))
    plan = {  # code: closes of the last days (a 10% limit-up from 10.00 is 11.00, then 12.10)
        "600301": ["11.00", "12.10"],        # limit-up two days running: the leader (streak 2)
        "600302": [QUIET, "11.00"],          # limit-up today
        "600303": [QUIET, "10.40"],          # +4%: a follower
        "600304": [QUIET, "9.90"],           # down
        "600305": [QUIET, QUIET],            # flat
        "600306": [QUIET, "10.10"],          # +1%: up but not a follower
        "600401": [QUIET, "11.00"],          # industry B: a single limit-up -> not listed
        "600402": [QUIET, QUIET],
        "600403": [QUIET, QUIET],
    }
    ids = {}
    for code, moves in plan.items():
        iid = seed_equity(fdb, code, f"股{code}")
        ids[code] = iid
        seed_closes(fdb, iid, snap, days, _series(moves))
    cutoff = cutoff_after(days[-1])
    rows = [{"node_id": "bk_a" if c.startswith("6003") else "bk_b", "instrument_id": str(i)} for c, i in ids.items()]
    lake_root = tmp_path / "lake"
    msnap = seed_frame(fdb, lake_root, "sector_membership_test", pl.DataFrame(rows), ready_at=cutoff - timedelta(days=1))
    release = {"decision_as_of": cutoff, "availability_policy_version": "observed-actual-available-at-v1"}
    run = {"membership_snapshot_id": msnap, "taxonomy_id": "legacy_boards_industry", "taxonomy_version": 1}

    inds, spread, notes = CROWD.diffusion(fdb, LakeStore(lake_root), release, run)
    assert notes == [] and spread == days[-5:]
    assert [i["node_id"] for i in inds] == ["bk_a"], "industry B has one limit-up: not listed"
    a = inds[0]
    assert a["members"] == 6 and a["observed"] == 6 and a["limit_ups"] == 2
    assert a["leader"]["code"] == "600301" and a["leader"]["limit_streak"] == 2 and a["leader"]["is_limit_up"] is True
    assert [f["code"] for f in a["followers"]] == ["600303"] and a["followers"][0]["pct_change"] == Decimal("4.0000")
    # per session: yesterday only 600301 closed at the limit; today two
    by_day = {s["session"]: s for s in a["spread"]}
    assert [s["session"] for s in a["spread"]] == days[-5:]
    assert by_day[days[-2]]["limit_ups"] == 1 and by_day[days[-1]]["limit_ups"] == 2
    assert by_day[days[-1]]["up_share"] == Decimal("66.6667")                # 600301, 600302, 600303, 600306 up of 6
    assert by_day[days[-1]]["strong_followers"] == 1
    assert by_day[days[-3]]["limit_ups"] == 0 and by_day[days[-3]]["up_share"] == Decimal("0.0000")      # nobody moved: 0 of 6 up, a real zero (bars exist)
    assert a["avg_pct"] == Decimal("4.0000")                                   # (10 + 10 + 4 - 1 + 0 + 1) / 6


def test_a_stock_without_bars_is_not_counted_as_flat(fdb, tmp_path):
    days = weekdays(date(2026, 10, 1), N)
    snap = seed_manifest(fdb, "bars", raw_coverage(days[0], days[-1]))
    ids = []
    for code, moves in {"600501": [QUIET, "11.00"], "600502": [QUIET, "11.00"], "600503": None}.items():
        iid = seed_equity(fdb, code, f"股{code}")
        ids.append(iid)
        if moves:
            seed_closes(fdb, iid, snap, days, _series(moves))
    cutoff = cutoff_after(days[-1])
    lake_root = tmp_path / "lake"
    msnap = seed_frame(fdb, lake_root, "sector_membership_test", pl.DataFrame([{"node_id": "bk_x", "instrument_id": str(i)} for i in ids]), ready_at=cutoff - timedelta(days=1))
    inds, _, _ = CROWD.diffusion(fdb, LakeStore(lake_root), {"decision_as_of": cutoff, "availability_policy_version": "observed-actual-available-at-v1"},
                                 {"membership_snapshot_id": msnap, "taxonomy_id": "t", "taxonomy_version": 1})
    x = inds[0]
    assert x["members"] == 3 and x["observed"] == 2, "the member without bars is neither up nor flat"
    assert x["up_share"] == Decimal("100.0000")
