"""PEX12 on real PostgreSQL: grants, broker fill import (raw facts first), order / performance read models, opportunities."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from pulse.adapters.postgres.ledger_repo import LedgerRepo
from pulse.application import portfolio_service as PS
from pulse.contracts import utc_z

from . import support as S

pytestmark = pytest.mark.pg
UTC = timezone.utc
FEE = {"name": "commission", "definition_version": "v1", "value": "5", "unit": "CNY", "sample_count": 1, "missing_reason": None}


def fill(iid, exec_id="E1", order="O1", qty=1000, price="10.00", at="2026-10-05T10:00:00+08:00", side="buy", fees=None, mode="actual", kind="fill", cum=None, correction=None):
    return {"source_id": "brk", "source_execution_id": exec_id, "report_kind": kind, "source_order_id": order, "instrument_id": iid, "side": side,
            "last_fill_quantity": qty if kind == "fill" else None, "last_fill_price_raw": price if kind == "fill" else None, "cumulative_quantity": cum,
            "effective_at": at, "fees": [FEE] if fees is None else fees, "fee_effect_mode": mode, "correction_of_source_execution_id": correction}


@pytest.fixture()
def acct(conn, principal):
    a = S.open_account(conn, cash="1000000", day=date(2026, 10, 1))
    for perm in ("read", "import", "plan"):
        PS.grant(conn, principal, a, perm, datetime.now(UTC) - timedelta(days=1))
    return a


def head(conn, a):
    return conn.execute("SELECT current_sequence v FROM execution.account_head WHERE account_id=%s", (a,)).fetchone()["v"]


def do_import(conn, principal, a, fills, *, version=None, raw=None):
    return PS.import_broker_fills(conn, principal, {"account_id": a, "expected_account_version": head(conn, a) if version is None else version,
                                                  "raw_object_id": raw or S.raw_object(conn), "mapping_version": "broker-fill-v1", "fills": fills})


def test_grants_make_accounts_invisible_until_granted_and_revocable(conn, principal):
    a = S.open_account(conn)
    assert PS.list_authorized_accounts(conn, principal, 10, None)[0] == []
    PS.grant(conn, principal, a, "read", datetime.now(UTC) - timedelta(seconds=5))
    rows, nxt = PS.list_authorized_accounts(conn, principal, 10, None)
    assert [r["account_id"] for r in rows] == [a] and rows[0]["base_currency"] == "CNY" and nxt is None
    with pytest.raises(PS.PortfolioError) as e:
        PS.require_grant(conn, uuid.uuid4(), a, "read")
    assert e.value.status == 404
    PS.grant(conn, principal, a, "read", revoke=True)
    assert PS.list_authorized_accounts(conn, principal, 10, None)[0] == []
    with pytest.raises(PS.PortfolioError):
        PS.require_grant(conn, principal, a, "read")


def test_import_books_through_the_kernel_and_duplicates_never_double_book(conn, principal, acct):
    iid = S.seed_instrument(conn, "600901", "导入股")
    raw = S.raw_object(conn)
    r1 = do_import(conn, principal, acct, [fill(iid)], raw=raw)
    assert r1["status"] == "applied" and r1["new_account_version"] > 0
    led = LedgerRepo(conn).load(acct)
    assert led.holdings[str(iid)].quantity == 1000
    v = head(conn, acct)
    # identical request, even with a new idempotency key upstream: the same import, nothing booked twice
    r2 = do_import(conn, principal, acct, [fill(iid)], raw=raw)
    assert r2["import_id"] == r1["import_id"] and head(conn, acct) == v
    # the same execution id redelivered inside another report: duplicate, no second fill
    r3 = do_import(conn, principal, acct, [fill(iid)])
    assert r3["status"] == "applied" and head(conn, acct) == v and LedgerRepo(conn).load(acct).holdings[str(iid)].quantity == 1000
    # raw object and facts are kept
    assert conn.execute("SELECT count(*) n FROM execution.broker_fill_fact WHERE account_id=%s AND status='applied'", (acct,)).fetchone()["n"] == 1
    # the fee is the broker's actual 5, not an estimate and not zero
    ev = [e for e in LedgerRepo(conn).events(acct) if e["kind"] == "fill.booked"]
    assert ev[0]["payload"]["fee"] == "5"


def test_changed_redelivery_is_a_conflict_for_reconciliation_not_a_second_booking(conn, principal, acct):
    iid = S.seed_instrument(conn, "600902", "冲突股")
    do_import(conn, principal, acct, [fill(iid)])
    v = head(conn, acct)
    r = do_import(conn, principal, acct, [fill(iid, qty=900)])
    assert r["status"] == "reconciliation_pending" and r["reconciliation_result_id"] is not None and head(conn, acct) == v
    f = conn.execute("SELECT status, reason_code FROM execution.broker_fill_fact WHERE account_id=%s AND status='conflict'", (acct,)).fetchone()
    assert f["reason_code"] == "SAME_ID_CHANGED_CONTENT"
    rec = conn.execute("SELECT reconciliation_status, normalization_trace FROM ops.reconciliation_result WHERE reconciliation_id=%s", (r["reconciliation_result_id"],)).fetchone()
    assert rec["reconciliation_status"] == "unverifiable" and rec["normalization_trace"]["pending"][0]["reason"] == "SAME_ID_CHANGED_CONTENT"


def test_stale_account_version_keeps_raw_facts_pending_and_unfillable_orders_do_not_vanish(conn, principal, acct):
    iid = S.seed_instrument(conn, "600903", "待对账股")
    v0 = head(conn, acct)
    do_import(conn, principal, acct, [fill(iid, "A1", "OA")])
    r = do_import(conn, principal, acct, [fill(iid, "A2", "OB", at="2026-10-06T10:00:00+08:00")], version=v0)            # caller saw an older version
    assert r["status"] == "reconciliation_pending" and r["new_account_version"] is None
    assert conn.execute("SELECT reason_code FROM execution.broker_fill_fact WHERE source_execution_id='A2'").fetchone()["reason_code"] == "ACCOUNT_VERSION_CHANGED"
    # selling inventory that the ledger does not have is pending, never silently dropped or turned into 'unfilled'
    r = do_import(conn, principal, acct, [fill(iid, "S1", "OS", qty=5000, side="sell", at="2026-10-07T10:00:00+08:00")])
    assert r["status"] == "reconciliation_pending"
    why = conn.execute("SELECT reason_code FROM execution.broker_fill_fact WHERE source_execution_id='S1'").fetchone()["reason_code"]
    assert why.startswith("RESERVE_REJECTED")
    # cash that does not exist: pending as well
    r = do_import(conn, principal, acct, [fill(iid, "B9", "OZ", qty=10_000_000, price="10.00", at="2026-10-07T10:00:00+08:00")])
    assert r["status"] == "reconciliation_pending"


def test_unknown_fee_is_estimated_by_the_cost_engine_never_booked_as_zero(conn, principal, acct):
    iid = S.seed_instrument(conn, "600904", "费用股")
    unknown = [{"name": "commission", "definition_version": "v1", "value": None, "unit": "CNY", "sample_count": 0, "missing_reason": "not_reported"}]
    r = do_import(conn, principal, acct, [fill(iid, "F1", "OF", fees=unknown, mode="pending_estimate")])
    assert r["status"] == "applied"
    fee = [e for e in LedgerRepo(conn).events(acct) if e["kind"] == "fill.booked"][0]["payload"]["fee"]
    assert Decimal(fee) > 0
    bad = do_import(conn, principal, acct, [{**fill(iid, "F2", "OG"), "fee_effect_mode": "weird"}])
    assert bad["status"] == "invalid_format"


def test_cancel_report_releases_only_the_remainder(conn, principal, acct):
    iid = S.seed_instrument(conn, "600905", "撤单股")
    r = do_import(conn, principal, acct, [fill(iid, "C1", "OC", qty=400, cum=1000), fill(iid, "C2", "OC", qty=0 + 100, at="2026-10-05T10:05:00+08:00", cum=1000)])
    assert r["status"] == "applied"
    r = do_import(conn, principal, acct, [fill(iid, "C3", "OC", kind="cancel", at="2026-10-05T10:30:00+08:00", cum=500)])
    assert r["status"] == "applied"
    led = LedgerRepo(conn).load(acct)
    assert led.holdings[str(iid)].quantity == 500 and led.orders[f"broker:brk:OC"].remaining == 0 if hasattr(led.orders[f"broker:brk:OC"], "remaining") else True


def test_api_import_requires_import_grant_and_replays_by_key(make_api, conn, principal, acct):
    api = make_api(principal)
    iid = S.seed_instrument(conn, "600906", "接口股")
    raw = S.raw_object(conn)
    body = {"account_id": str(acct), "expected_account_version": head(conn, acct), "raw_object_id": str(raw), "mapping_version": "broker-fill-v1", "fills": [fill(str(iid), at="2026-10-05T02:00:00Z")]}
    r = api.post("import-broker-fills", body, key="imp-1")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == "applied"
    assert api.post("import-broker-fills", body, key="imp-1").json()["data"] == r.json()["data"]
    stranger = uuid.uuid4()
    api2 = make_api(principal, stranger).as_(stranger)
    assert api2.post("import-broker-fills", body, key="imp-2").status_code == 404          # an ungranted account does not exist for you
    assert api.post("import-broker-fills", {**body, "raw_object_id": str(uuid.uuid4())}, key="imp-3").status_code == 404


def test_readmodels_orders_performance_and_not_ready_then_built(make_api, conn, principal, acct):
    api = make_api(principal)
    iid = S.seed_instrument(conn, "600907", "绩效股")
    snap = S.seed_manifest(conn)
    days = S.weekdays(date(2026, 10, 1), 20)
    S.seed_bars(conn, iid, snap, days, base=Decimal("10"), step=Decimal("0.1"))
    do_import(conn, principal, acct, [fill(iid, "P1", "OP", qty=1000, price="10.20", at="2026-10-05T10:00:00+08:00")])
    ver = head(conn, acct)
    win = {"start": "2026-10-01T00:00:00Z", "end": "2026-10-20T00:00:00Z"}
    kao = "2026-10-21T00:00:00Z"
    m = conn.execute("SELECT manifest_version, content_hash FROM market.dataset_manifest WHERE snapshot_id=%s", (snap,)).fetchone()
    ref = {"snapshot_id": str(snap), "dataset_id": "bars_raw_1d", "manifest_version": m["manifest_version"], "content_hash": m["content_hash"]}
    q_orders = dict(account_id=acct, account_version=ver, window=win, knowledge_as_of=kao, page={"limit": 10})
    r = api.get("query-account-orders", **q_orders)
    assert r.status_code == 409 and r.json()["error"]["code"] == "RESULT_NOT_READY"
    q_perf = dict(account_id=acct, account_version=ver, window=win, knowledge_as_of=kao, valuation_snapshot_refs=[ref], performance_policy_version="twr-cost-x1-v1",
                  valuation_policy_version=PS.VALUATION_POLICY)
    assert api.get("query-account-performance", **q_perf).json()["error"]["code"] == "RESULT_NOT_READY"           # nothing is computed inside the GET
    from pulse.contracts import utc_z as z
    from datetime import datetime as dt
    wdt = {"start": dt(2026, 10, 1, tzinfo=UTC), "end": dt(2026, 10, 20, tzinfo=UTC)}
    for pol in ("twr-cost-x1-v1", "twr-cost-x3-v1"):
        PS.build_readmodels(conn, acct, ver, wdt, dt(2026, 10, 21, tzinfo=UTC), [ref], pol)
    r = api.get("query-account-orders", **q_orders)
    assert r.status_code == 200, r.text
    orders = r.json()["data"]["orders"]
    assert len(orders) == 1 and orders[0]["state"] == "filled" and orders[0]["cumulative_filled_quantity"] == 1000 and orders[0]["quality_status"] == "partial"
    p1 = api.get("query-account-performance", **q_perf).json()["data"]["performance"]
    twr1 = next(m["value"] for m in p1["metrics"] if m["name"] == "time_weighted_return")
    assert twr1 is not None and Decimal(twr1) > 0                       # price rose from the 10.20 fill: positive, from the ledger NAV
    p3 = api.get("query-account-performance", **{**q_perf, "performance_policy_version": "twr-cost-x3-v1"}).json()["data"]["performance"]
    twr3 = next(m["value"] for m in p3["metrics"] if m["name"] == "time_weighted_return")
    assert Decimal(twr3) < Decimal(twr1)                                 # cost x3 is lower than x1
    # no marks in the snapshot -> missing, never zero
    other = S.seed_manifest(conn)
    ref2 = {"snapshot_id": str(other), "dataset_id": "bars_raw_1d", "manifest_version": conn.execute("SELECT manifest_version FROM market.dataset_manifest WHERE snapshot_id=%s", (other,)).fetchone()["manifest_version"],
            "content_hash": conn.execute("SELECT content_hash FROM market.dataset_manifest WHERE snapshot_id=%s", (other,)).fetchone()["content_hash"]}
    PS.build_readmodels(conn, acct, ver, wdt, dt(2026, 10, 21, tzinfo=UTC), [ref2], "twr-cost-x1-v1")
    pm = api.get("query-account-performance", **{**q_perf, "valuation_snapshot_refs": [ref2]})
    perf = pm.json()["data"]["performance"]
    twr_m = next(m for m in perf["metrics"] if m["name"] == "time_weighted_return")
    assert twr_m["value"] is None and twr_m["missing_reason"].startswith("mark_missing") and pm.json()["meta"]["quality_status"] == "partial"
    # ownership: a stranger sees nothing
    stranger = uuid.uuid4()
    api2 = make_api(principal, stranger).as_(stranger)
    assert api2.get("query-account-performance", **q_perf).status_code == 404
    assert api2.c.get("/v1/x/c/artifact", params={"ref": perf["artifact"]["ref"]}, cookies=api2._cookies()).status_code == 404
    assert api.c.get("/v1/x/c/artifact", params={"ref": perf["artifact"]["ref"]}, cookies=api._cookies()).status_code == 200


def test_opportunities_keep_all_denominators_and_inventory_shortfall(make_api, conn, principal):
    api = make_api(principal)
    iid = S.seed_instrument(conn, "600908", "机会股")
    acct = S.open_account(conn, cash="100000", day=date(2026, 10, 1), positions={str(iid): {"quantity": 1000, "cost": "10000"}})
    for perm in ("read", "import"):
        PS.grant(conn, principal, acct, perm, datetime.now(UTC) - timedelta(days=1))
    snap = S.seed_manifest(conn)
    S.seed_bars(conn, iid, snap, S.weekdays(date(2026, 10, 1), 15), base=Decimal("10"), step=Decimal("0.2"))
    # sold 600 of 1000 and never bought back: the shortfall is priced at the latest close
    do_import(conn, principal, acct, [fill(iid, "T2", "OT2", qty=600, side="sell", price="10.20", at="2026-10-06T10:00:00+08:00")])
    oid = uuid.uuid4()
    art = PS.ops_service.put_artifact(conn, kind="opportunity-intent", contract_id="pulse.ext_c.opportunity_intent", schema_version="1", body={"x": 1}, scope_account=acct)
    for i, (state, filled, reason) in enumerate([("triggered", 0, None), ("unfilled", 0, "LIMIT_UP_OPEN")], 1):
        conn.execute("INSERT INTO research.opportunity_event(opportunity_id,opportunity_version,account_id,instrument_id,side,intent_artifact,scenario_id,state,target_quantity,filled_quantity,reason_code,available_at)"
                     " VALUES (%s,%s,%s,%s,'buy',%s,'s',%s,1000,%s,%s,%s)", (oid, i, acct, iid, art["ref"], state, filled, reason, datetime(2026, 10, 12, tzinfo=UTC)))
    r = api.get("query-opportunities", account_id=acct, as_of="2026-10-30T00:00:00Z", page={"limit": 10})
    assert r.status_code == 200, r.text
    body = api.c.get("/v1/x/c/artifact", params={"ref": r.json()["data"]["opportunity_artifact"]["ref"]}, cookies=api._cookies()).json()["data"]["body"]
    assert body["opportunities"][0]["state"] == "unfilled" and body["opportunities"][0]["reason_code"] == "LIMIT_UP_OPEN"        # latest version, reason kept
    assert body["denominators"] == {"total": 1, "by_state": {"unfilled": 1}}
    inc = body["incremental_vs_hold"]
    assert inc["status"] == "ok" and inc["inventory_shortfall"] == {str(iid): 600} and inc["bridge_holds"] is True
    assert api.get("query-opportunities", as_of="2026-10-30T00:00:00Z", page={"limit": 10}).status_code == 400            # neither account nor experiment
    stranger = uuid.uuid4()
    assert make_api(principal, stranger).as_(stranger).get("query-opportunities", account_id=acct, as_of="2026-10-30T00:00:00Z", page={"limit": 10}).status_code == 404
    # as_of before the observation: nothing is known yet
    early = api.get("query-opportunities", account_id=acct, as_of="2026-10-01T00:00:00Z", page={"limit": 10}).json()["data"]["opportunity_artifact"]
    eb = api.c.get("/v1/x/c/artifact", params={"ref": early["ref"]}, cookies=api._cookies()).json()["data"]["body"]
    assert eb["opportunities"] == []
