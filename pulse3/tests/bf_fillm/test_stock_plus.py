"""GET /v1/x/bf/fillm/stock-stats-plus on a real PostgreSQL: volume ratio from the bars visible at the cutoff, the circulating cap fact (estimated,
with its as-of date), and the seal success rate computed only over the sessions the seal facts cover (the coverage block says how many).
SYNTHETIC rows: they prove wiring, point-in-time cutoffs, the coverage statement and the null policy."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import polars as pl
import pytest

from bf_fillm.support import cutoff_after, publish_release, raw_coverage, seed_closes, seed_equity, seed_frame, seed_manifest, weekdays
from bf_stock.support import seed_cap
from pulse import contracts

pytestmark = pytest.mark.pg
URL = "/v1/x/bf/fillm/stock-stats-plus"
N = 40


def _world(fdb, *, volumes, code="600601"):
    iid = seed_equity(fdb, code, "加项测试")
    days = weekdays(date(2026, 9, 1), N)
    snap = seed_manifest(fdb, "bars", raw_coverage(days[0], days[-1]))
    seed_closes(fdb, iid, snap, days, ["10.00"] * N, amounts=["50000000"] * N, volumes=volumes)
    rid = publish_release(fdb, snapshot=snap, as_of=cutoff_after(days[-1]))
    return iid, days, rid


def _fields(body) -> dict:
    return {f["key"]: f for f in body["data"]["fields"]}


def _seal_session(fdb, lake_root, day: date, iid, touched: bool, sealed: bool, *, ready_at, policy="seal-facts-v2", present=True):
    rows = [{"instrument_id": str(iid) if present else "00000000-0000-0000-0000-000000000001", "touched_limit": touched, "sealed_at_close": sealed}]
    seed_frame(fdb, lake_root, "intraday_seal_facts", pl.DataFrame(rows, schema={"instrument_id": pl.String, "touched_limit": pl.Boolean, "sealed_at_close": pl.Boolean}),
               ready_at=ready_at, coverage={"dataset": "intraday_seal_facts", "session_date": day.isoformat(), "policy_version": policy})


def test_volume_ratio_is_latest_volume_over_the_previous_five_day_mean(fclient, fdb):
    iid, days, rid = _world(fdb, volumes=[1000] * (N - 1) + [3000])
    r = fclient.get(URL, params={"instrument_id": iid, "release_id": rid})
    assert r.status_code == 200, r.text
    contracts.check("StockStatsPlus", r.json()["data"], contracts.backfill_id("fillm"))
    f = _fields(r.json())
    assert list(f) == ["volume_ratio", "float_cap", "seal_success_rate_1y"]
    vr = f["volume_ratio"]
    assert (vr["status"], Decimal(vr["value"]), vr["unit"], vr["as_of"]) == ("ok", Decimal(3), "倍", days[-1].isoformat())
    assert "5" in vr["basis"]


def test_volume_ratio_with_a_gap_in_the_last_six_bars_is_missing_not_guessed(fclient, fdb):
    vols = [1000] * N
    vols[-3] = None
    iid, days, rid = _world(fdb, volumes=vols, code="600602")
    vr = _fields(fclient.get(URL, params={"instrument_id": iid, "release_id": rid}).json())["volume_ratio"]
    assert vr["status"] == "unavailable" and vr["value"] is None and "缺失" in vr["reason"]


def test_circulating_cap_is_estimated_dated_and_not_a_late_version(fclient, fdb, tmp_path):
    iid, days, rid = _world(fdb, volumes=[1000] * N, code="600603")
    cutoff = cutoff_after(days[-1])
    ok = {"instrument_id": str(iid), "as_of": "2026-09-20", "float_cap_cny": Decimal("5000000000"), "available_at": cutoff - timedelta(days=5)}
    seed_cap(fdb, tmp_path / "lake", [ok], ready_at=cutoff - timedelta(days=4))
    cap = _fields(fclient.get(URL, params={"instrument_id": iid, "release_id": rid}).json())["float_cap"]
    assert (cap["status"], cap["value"], cap["unit"], cap["as_of"]) == ("estimated", "50.00", "亿元", "2026-09-20")
    assert "流通市值" in cap["reason"] and "自由流通" in cap["reason"] and "不是当日值" in cap["reason"]
    late = {**ok, "as_of": "2026-10-20", "float_cap_cny": Decimal("9000000000"), "available_at": cutoff + timedelta(days=1)}
    seed_cap(fdb, tmp_path / "lake", [late], ready_at=cutoff + timedelta(days=2), version=2)          # recorded after the release cutoff
    cap2 = _fields(fclient.get(URL, params={"instrument_id": iid, "release_id": rid}).json())["float_cap"]
    assert cap2["value"] == "50.00", "a cap version recorded after the cutoff must not be used"


def test_cap_missing_is_a_reason_not_zero(fclient, fdb):
    iid, days, rid = _world(fdb, volumes=[1000] * N, code="600604")
    cap = _fields(fclient.get(URL, params={"instrument_id": iid, "release_id": rid}).json())["float_cap"]
    assert cap["status"] == "unavailable" and cap["value"] is None and cap["reason"]


def test_seal_success_counts_only_covered_sessions_and_says_how_many(fclient, fdb, tmp_path):
    iid, days, rid = _world(fdb, volumes=[1000] * N, code="600605")
    cutoff = cutoff_after(days[-1])
    lake_root = tmp_path / "lake"
    ready = cutoff - timedelta(hours=1)
    a, b, c, d, e, g = days[-8], days[-7], days[-6], days[-5], days[-4], days[-3]
    _seal_session(fdb, lake_root, a, iid, True, True, ready_at=ready)                        # touched, sealed at the close
    _seal_session(fdb, lake_root, b, iid, True, False, ready_at=ready)                       # touched, broke
    _seal_session(fdb, lake_root, c, iid, False, False, ready_at=ready)                      # no touch: covered, no numerator or denominator
    _seal_session(fdb, lake_root, d, iid, True, True, ready_at=ready, policy="seal-facts-v1")   # another policy: excluded and counted
    _seal_session(fdb, lake_root, e, iid, True, True, ready_at=ready, present=False)         # the stock has no snapshot that session: not covered
    _seal_session(fdb, lake_root, g, iid, True, True, ready_at=cutoff + timedelta(hours=5))  # published after the cutoff: invisible
    _seal_session(fdb, lake_root, b, iid, True, True, ready_at=ready)             # a newer version of session b wins: sealed
    body = fclient.get(URL, params={"instrument_id": iid, "release_id": rid}).json()
    contracts.check("StockStatsPlus", body["data"], contracts.backfill_id("fillm"))
    s = _fields(body)["seal_success_rate_1y"]
    cov = s["coverage"]
    assert (s["status"], s["value"], s["unit"]) == ("estimated", "100.00", "%")          # b was replaced by version 2 -> 2 touched, 2 sealed
    assert cov["sessions_covered"] == 3 and cov["touched"] == 2 and cov["sealed_at_close"] == 2 and cov["window_sessions"] == 250
    assert cov["first_session"] == a.isoformat() and cov["last_session"] == c.isoformat() and cov["sessions_excluded_other_policy"] == 1
    assert cov["policy_version"] == "seal-facts-v2"
    assert "只覆盖 3 个交易日" in s["reason"] and "不是250个交易日" in s["reason"], "a partial window must never be called a 1-year value"
    assert s["as_of"] == c.isoformat()


def test_seal_success_without_a_touch_has_no_rate_and_without_a_dataset_says_so(fclient, fdb, tmp_path):
    iid, days, rid = _world(fdb, volumes=[1000] * N, code="600606")
    none = _fields(fclient.get(URL, params={"instrument_id": iid, "release_id": rid}).json())["seal_success_rate_1y"]
    assert none["status"] == "unavailable" and none["value"] is None and none["coverage"]["sessions_covered"] == 0 and "封板事实" in none["reason"]
    _seal_session(fdb, tmp_path / "lake", days[-3], iid, False, False, ready_at=cutoff_after(days[-1]) - timedelta(hours=1))
    s = _fields(fclient.get(URL, params={"instrument_id": iid, "release_id": rid}).json())["seal_success_rate_1y"]
    assert s["status"] == "unavailable" and s["value"] is None and "没有分母" in s["reason"] and s["coverage"]["sessions_covered"] == 1


def test_unknown_release_is_refused(fclient, fdb):
    import uuid

    iid, _, _ = _world(fdb, volumes=[1000] * N, code="600607")
    assert fclient.get(URL, params={"instrument_id": iid, "release_id": str(uuid.uuid4())}).status_code == 404
