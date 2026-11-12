"""getAccountProjection on real PostgreSQL: replayed historical versions, marks from a READY snapshot, NAV identity."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from api_seed import raw_coverage, seed_daily_bars, seed_instrument, seed_manifest, weekdays
from pulse.adapters.postgres import db as pgdb
from pulse.adapters.postgres.ledger_repo import EventContext, LedgerRepo

CTX = EventContext("api-test", "rules", "cost", "exec", "none")
DAY = "2026-10-09"


def open_account(pg_dsn, iid, *, cash="5000", quantity=100, cost="1000") -> object:
    acct = uuid4()
    conn = pgdb.connect(pg_dsn, autocommit=True)
    try:
        repo = LedgerRepo(conn)
        repo.create_account(acct, {"trading_day": DAY, "opening_cash": cash, "positions": {str(iid): {"quantity": quantity, "cost": cost}},
                                   "sale_proceeds_reusable": True}, CTX)
        repo.append(acct, {"event_id": "dep1", "sequence": 1, "kind": "capital.deposit", "effective_at": f"{DAY}T10:00:00+08:00",
                           "payload": {"amount": "250"}}, CTX, expected_version=0)
    finally:
        conn.close()
    return acct


def valuation(db, iid, *, with_bars=True):
    days = weekdays(date(2026, 10, 5), 5)  # 10-09 is the last; closes are base+i+0.5 -> 14.5
    snap = seed_manifest(db, "bars", raw_coverage(days[0], days[-1]))
    if with_bars:
        seed_daily_bars(db, iid, snap, days)
    return snap


@pytest.mark.pg
def test_projection_is_replayed_per_version_and_nav_identity_holds(client, db, pg_dsn):
    iid = seed_instrument(db, "600040")
    acct = open_account(pg_dsn, iid)
    snap = valuation(db, iid)
    v0 = client.get(f"/v1/accounts/{acct}", params={"account_version": 0, "valuation_snapshot_id": snap})
    assert v0.status_code == 200, v0.text
    d0 = v0.json()["data"]
    pos = d0["positions"][0]
    assert pos["instrument_id"] == str(iid) and pos["quantity_shares"] == 100 and pos["mark_price_raw"] == "14.5"
    assert pos["market_value_cny"] == "1450" and pos["mark_quality_status"] == "valid"
    assert d0["cash_settled_cny"] == "5000" and d0["nav_cny"] == "6450" and d0["unrealized_cny"] == "450"
    assert d0["net_external_capital_cny"] == "6000" and d0["withdrawable_cash_cny"] is None
    assert d0["trading_date"] == DAY and d0["account_version"] == 0 and d0["valuation_policy_version"]
    nav, cap = Decimal(d0["nav_cny"]), Decimal(d0["net_external_capital_cny"])
    pnl = sum(Decimal(d0[k]) for k in ("realized_gross_cny", "unrealized_cny", "dividend_income_cny")) \
        - Decimal(d0["transaction_cost_cny"]) - Decimal(d0["withholding_tax_cny"])
    assert nav == cap + pnl and Decimal(d0["pnl_from_cost_origin_cny"]) == pnl
    # version 1 includes the deposit; version 0 stays as it was (history is replayed, not read from the current projection)
    d1 = client.get(f"/v1/accounts/{acct}", params={"account_version": 1, "valuation_snapshot_id": snap}).json()["data"]
    assert d1["cash_settled_cny"] == "5250" and d1["nav_cny"] == "6700" and d1["net_external_capital_cny"] == "6250"
    assert client.get(f"/v1/accounts/{acct}", params={"account_version": 0, "valuation_snapshot_id": snap}).json()["data"] == d0
    meta = v0.json()["meta"]
    assert meta["quality_status"] == "valid" and meta["input_snapshot_refs"][0]["snapshot_id"] == str(snap)


@pytest.mark.pg
def test_missing_mark_keeps_nav_null_and_flags_the_position(client, db, pg_dsn):
    iid = seed_instrument(db, "600041")
    acct = open_account(pg_dsn, iid)
    snap = valuation(db, iid, with_bars=False)
    r = client.get(f"/v1/accounts/{acct}", params={"account_version": 1, "valuation_snapshot_id": snap})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["nav_cny"] is None and d["unrealized_cny"] is None and d["pnl_from_cost_origin_cny"] is None
    assert d["positions"][0]["mark_price_raw"] is None and d["positions"][0]["market_value_cny"] is None
    assert d["positions"][0]["mark_quality_status"] == "invalid"
    assert d["cash_settled_cny"] == "5250"  # ledger facts that need no mark are still given
    assert r.json()["meta"]["quality_status"] == "partial" and r.json()["meta"]["warnings"][0]["code"] == "MARK_MISSING"
    assert r.json()["meta"]["coverage"]["missing_count"] == 1


@pytest.mark.pg
def test_account_projection_errors(client, db, pg_dsn, monkeypatch):
    iid = seed_instrument(db, "600042")
    acct = open_account(pg_dsn, iid)
    snap = valuation(db, iid)
    ok = {"account_version": 1, "valuation_snapshot_id": snap}
    ahead = client.get(f"/v1/accounts/{acct}", params={**ok, "account_version": 9})
    assert ahead.status_code == 409 and ahead.json()["error"]["code"] == "ACCOUNT_VERSION_UNKNOWN"
    assert client.get(f"/v1/accounts/{uuid4()}", params=ok).status_code == 404
    assert client.get(f"/v1/accounts/{acct}", params={**ok, "valuation_snapshot_id": uuid4()}).status_code == 404
    pending = seed_manifest(db, "bars", {}, state="COMMITTED_UNPUBLISHED")
    r = client.get(f"/v1/accounts/{acct}", params={**ok, "valuation_snapshot_id": pending})
    assert r.status_code == 409 and r.json()["error"]["code"] == "SNAPSHOT_NOT_READY"
    assert client.get(f"/v1/accounts/{acct}", params={**ok, "account_version": -1}).status_code == 400
    monkeypatch.delenv("PULSE_DEV_PRINCIPAL")
    denied = client.get(f"/v1/accounts/{acct}", params=ok)
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "AUTH_REQUIRED"
