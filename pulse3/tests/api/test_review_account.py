"""Review fix M1: account projection is valued by the ledger and never marks with a bar later than the ledger day."""
from datetime import date

import pytest

from api_seed import raw_coverage, seed_daily_bars, seed_instrument, seed_manifest, weekdays
from test_account_routes import open_account  # noqa: F401  (account on 2026-10-09 with one position of 100 shares)


@pytest.mark.pg
def test_m1_mark_never_comes_from_after_the_ledger_day(client, db, pg_dsn):
    iid = seed_instrument(db, "600051")
    acct = open_account(pg_dsn, iid)
    days = weekdays(date(2026, 10, 5), 10)                       # 10-05 .. 10-16; the ledger day is 10-09
    snap = seed_manifest(db, "bars", raw_coverage(days[0], days[-1]))
    seed_daily_bars(db, iid, snap, days)
    d = client.get(f"/v1/accounts/{acct}", params={"account_version": 0, "valuation_snapshot_id": snap}).json()["data"]
    assert d["positions"][0]["mark_price_raw"] == "14.5"          # the 10-09 close, not the newest close in the snapshot
    assert d["nav_cny"] == "6450"


@pytest.mark.pg
def test_m1_only_future_bars_means_missing_mark_and_null_nav(client, db, pg_dsn):
    iid = seed_instrument(db, "600052")
    acct = open_account(pg_dsn, iid)
    days = weekdays(date(2026, 10, 12), 3)                       # all after the ledger day
    snap = seed_manifest(db, "bars", raw_coverage(days[0], days[-1]))
    seed_daily_bars(db, iid, snap, days)
    r = client.get(f"/v1/accounts/{acct}", params={"account_version": 0, "valuation_snapshot_id": snap})
    d = r.json()["data"]
    assert d["nav_cny"] is None and d["positions"][0]["mark_price_raw"] is None and d["positions"][0]["mark_quality_status"] == "invalid"
    assert r.json()["meta"]["warnings"][0]["code"] == "MARK_MISSING"


# ---- review fix M3: aggregated bars are deterministic and never "available" after the decision time
@pytest.mark.pg
def test_m3_aggregated_bars_have_input_derived_available_at_and_stable_hash(client, db):
    from test_market_routes import bars_params
    iid = seed_instrument(db, "600053")
    days = weekdays(date(2026, 10, 5), 7)
    snap = seed_manifest(db, "bars", raw_coverage(days[0], days[-1]))
    seed_daily_bars(db, iid, snap, days)
    asof = "2026-10-14T00:00:00Z"                                  # long before "now"
    params = bars_params(snap, tf="1w", start="2026-10-01T00:00:00Z", asof=asof)
    daily = client.get(f"/v1/stocks/{iid}/bars", params=bars_params(snap, start="2026-10-01T00:00:00Z", asof=asof)).json()["data"]["bars"]
    first = client.get(f"/v1/stocks/{iid}/bars", params=params).json()["data"]
    second = client.get(f"/v1/stocks/{iid}/bars", params=params).json()["data"]
    assert first["window_hash"] == second["window_hash"]            # no wall clock in the window
    assert all(b["available_at"] <= asof for b in first["bars"])
    assert first["bars"][0]["available_at"] == max(b["available_at"] for b in daily if b["trade_date"] <= "2026-10-09")


# ---- review fix M4: reconstructed policy ignores raw-bar versions recorded after as_of (first version: usable by policy time)
@pytest.mark.pg
def test_m4_reconstructed_raw_loaders_ignore_versions_recorded_after_as_of(db):
    from datetime import timedelta
    from decimal import Decimal
    from pulse.application import bindings as B
    iid = seed_instrument(db, "600054")
    day = date(2026, 9, 14)
    s1 = seed_manifest(db, "bars", raw_coverage(day, day))
    s2 = seed_manifest(db, "bars", raw_coverage(day, day))
    seed_daily_bars(db, iid, s1, [day], base=Decimal("10"))                                              # first version -> close 10.5
    seed_daily_bars(db, iid, s2, [day], base=Decimal("20"), available_after_close=timedelta(days=3))     # later restatement -> 20.5
    usable = B.policy_usable_at(day)
    pol = B.RECONSTRUCTED_POLICY
    early, late = usable + timedelta(hours=1), usable + timedelta(days=5)
    assert B.load_raw_series(db, [iid], day, early, availability_policy=pol)[iid]["c"] == [10.5]
    assert B.load_raw_series(db, [iid], day, late, availability_policy=pol)[iid]["c"] == [20.5]
    assert B.load_raw_close(db, day, early, pol)[iid] == Decimal("10.5") and B.load_raw_close(db, day, late, pol)[iid] == Decimal("20.5")
    # a bar whose policy usable time has not come yet stays invisible
    assert iid not in B.load_raw_series(db, [iid], day, usable - timedelta(minutes=1), availability_policy=pol)


# ---- review fix M6: the raw loader keeps exact Decimal closes next to the float kernel columns
@pytest.mark.pg
def test_m6_raw_series_carries_exact_decimal_closes(db):
    from datetime import datetime, timezone
    from decimal import Decimal
    from pulse.application import bindings as B
    iid = seed_instrument(db, "600055")
    days = weekdays(date(2026, 9, 14), 3)
    snap = seed_manifest(db, "bars", raw_coverage(days[0], days[-1]))
    seed_daily_bars(db, iid, snap, days)
    s = B.load_raw_series(db, [iid], days[-1], datetime(2026, 12, 1, tzinfo=timezone.utc))[iid]
    assert s["dc"] == [Decimal("10.5"), Decimal("11.5"), Decimal("12.5")] and all(isinstance(x, Decimal) for x in s["dc"])
    assert s["c"] == [10.5, 11.5, 12.5]
