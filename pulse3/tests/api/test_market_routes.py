"""getStockWorkspace / getStockQuote / getStockBars against a real PostgreSQL 18 (every response is contract-checked
by ``respond``; the tests assert behaviour on top: PIT, capability refusal, freshness, structured errors)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from api_seed import (FWD_CAP, cst, lake_row, projection, publish, raw_coverage, seed_daily_bars, seed_instrument, seed_manifest,
                      seed_quote_observation, sha, weekdays, write_lake)

UTC = timezone.utc
POLICY = "observed-actual-available-at-v1"


# ---- workspace ------------------------------------------------------------------------------------------------
@pytest.mark.pg
def test_workspace_reads_the_frozen_projection_and_overlays_stay_separate(client, db):
    iid = seed_instrument(db, "600001", "测试一号")
    snap = seed_manifest(db, "bars", raw_coverage(date(2026, 10, 1), date(2026, 10, 9)))
    rid = publish(db, snapshot=snap)
    feat = {"feature_key_hash": sha("f"), "result_state": "RUNNING", "artifact": None, "computed_at": None, "quality_status": "partial",
            "key": {"feature_id": "ma", "definition_version": "1", "implementation_digest": sha("a"), "parameter_digest": sha("b"),
                    "instrument_ids": [str(iid)], "snapshot_id": str(snap), "decision_as_of": "2026-11-11T07:00:00Z",
                    "availability_policy_version": POLICY, "timeframe": "1d", "session_policy_version": "regular_continuous_v1",
                    "input_revision": "r", "input_schema_version": "1", "dependency_digest": sha("d"), "calendar_version": "c",
                    "adjustment_mode": "raw", "adjustment_version": "not-applicable", "universe_policy_version": "u",
                    "market_rules_version": "m", "numeric_policy_version": "n"}}
    projection(db, iid, rid, payload={"features": [feat], "plan_refs": [], "chart_window_refs": []})
    r = client.get(f"/v1/stocks/{iid}/workspace", params={"release_id": rid, "view_kind": "public_research"})
    assert r.status_code == 200, r.text
    body = r.json()
    d = body["data"]
    assert d["instrument"]["display_name"] == "测试一号" and d["scope_key"] == "public"
    assert d["quote_overlay"] is None and d["actionability_id"] is None
    assert d["projection_hash"] == sha(f"{iid}{rid}")
    assert body["meta"]["release_id"] == str(rid) and body["meta"]["quality_status"] == "partial"  # a RUNNING feature
    assert [s["snapshot_id"] for s in body["meta"]["input_snapshot_refs"]] == [str(snap)]
    assert {w["code"] for w in body["meta"]["warnings"]} == {"QUOTE_OVERLAY_MISSING"}
    # a quote observation becomes the overlay without touching the frozen hash; old quote reads stale
    seed_quote_observation(db, iid, event_time=datetime.now(UTC) - timedelta(days=2))
    r2 = client.get(f"/v1/stocks/{iid}/workspace", params={"release_id": rid, "view_kind": "public_research"}).json()
    assert r2["data"]["quote_overlay"]["freshness_state"] == "stale"
    assert r2["data"]["projection_hash"] == d["projection_hash"]
    assert "QUOTE_STALE" in {w["code"] for w in r2["meta"]["warnings"]}


@pytest.mark.pg
def test_workspace_structured_errors(client, db):
    iid = seed_instrument(db, "600002")
    rid = publish(db, missing=())
    ok = {"release_id": rid, "view_kind": "public_research"}
    assert client.get(f"/v1/stocks/{iid}/workspace", params={**ok, "view_kind": "nope"}).json()["error"]["code"] == "BAD_REQUEST"
    r = client.get(f"/v1/stocks/{iid}/workspace", params={**ok, "view_kind": "account_holding"})
    assert r.status_code == 400 and r.json()["error"]["field_errors"][0]["path"] == "/account_id"
    assert client.get(f"/v1/stocks/{iid}/workspace", params=ok).status_code == 404  # no projection row
    assert client.get(f"/v1/stocks/{uuid4()}/workspace", params=ok).status_code == 404  # unknown instrument
    assert client.get(f"/v1/stocks/{iid}/workspace", params={**ok, "release_id": uuid4()}).status_code == 404
    # a release whose workspace component is MISSING answers 409, not "not found"
    rid2 = publish(db, optional={"workspace": "ALLOW_MISSING"}, missing=("workspace",))
    r = client.get(f"/v1/stocks/{iid}/workspace", params={**ok, "release_id": rid2})
    assert r.status_code == 409 and r.json()["error"]["code"] == "RELEASE_COMPONENT_UNAVAILABLE"


@pytest.mark.pg
def test_account_holding_view_uses_the_account_scope(client, db):
    iid = seed_instrument(db, "600003")
    rid = publish(db)
    acct = uuid4()
    projection(db, iid, rid, view="account_holding", scope=str(acct))
    r = client.get(f"/v1/stocks/{iid}/workspace", params={"release_id": rid, "view_kind": "account_holding", "account_id": acct})
    assert r.status_code == 200 and r.json()["data"]["scope_key"] == str(acct)
    # another account's scope is not served
    other = client.get(f"/v1/stocks/{iid}/workspace", params={"release_id": rid, "view_kind": "account_holding", "account_id": uuid4()})
    assert other.status_code == 404


# ---- quote ----------------------------------------------------------------------------------------------------
@pytest.mark.pg
def test_quote_from_observation_freshness_and_no_daily_close_fallback(client, db):
    iid = seed_instrument(db, "600010")
    q = {"quote_policy_version": "quote-observation-latest-v1"}
    r = client.get(f"/v1/stocks/{iid}/quote", params=q)
    assert r.status_code == 404  # nothing observed: no invented quote, daily close is not a quote
    seed_manifest(db, "bars", raw_coverage(date(2026, 10, 1), date(2026, 10, 9)))
    seed_quote_observation(db, iid, event_time=datetime.now(UTC) - timedelta(seconds=30))
    r = client.get(f"/v1/stocks/{iid}/quote", params=q)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["freshness_state"] == "current" and d["quote"]["last_price_raw"] == "11.71"
    assert d["quote"]["ask_price_raw"] is None  # a zero ask is "no ask", not a price
    assert d["quote"]["bid_price_raw"] == "11.7" and d["quote"]["ask_volume_shares"] == 0
    assert "QUOTE_PROJECTION_ABSENT" in {w["code"] for w in r.json()["meta"]["warnings"]}
    # an old observation becomes stale at read time; the stored facts are unchanged
    iid2 = seed_instrument(db, "600011")
    seed_quote_observation(db, iid2, event_time=datetime.now(UTC) - timedelta(hours=3))
    old = client.get(f"/v1/stocks/{iid2}/quote", params=q).json()
    assert old["data"]["freshness_state"] == "stale" and old["meta"]["freshness_state"] == "stale"
    assert "QUOTE_STALE" in {w["code"] for w in old["meta"]["warnings"]}
    assert client.get(f"/v1/stocks/{iid}/quote", params={"quote_policy_version": "other"}).status_code == 404
    assert client.get(f"/v1/stocks/{iid}/quote", params={**q, "quote_snapshot_id": uuid4()}).status_code == 404


@pytest.mark.pg
def test_quote_projection_row_wins_and_unready_snapshot_is_refused(client, db):
    iid = seed_instrument(db, "600012")
    oid = seed_quote_observation(db, iid, event_time=datetime.now(UTC) - timedelta(seconds=10))
    for state, expect in (("READY", 200), ("COMMITTED_UNPUBLISHED", 409)):
        snap = seed_manifest(db, "quotes", {}, state=state)
        db.execute("INSERT INTO market.quote_projection(quote_snapshot_id,instrument_id,quote_policy_version,live_quote_version,source_id,"
                   "endpoint_id,source_schema_version,mapping_version,observation_id,trade_date,source_event_time,first_seen_at,available_at,"
                   "currency,last_price_raw,quality_status,field_quality) VALUES (%s,%s,%s,%s,'apisrc','quote','s1','m1',%s,current_date,"
                   "clock_timestamp()-interval '5 seconds',clock_timestamp()-interval '5 seconds',clock_timestamp(),'CNY',12.5,'valid','{}')",
                   (snap, iid, f"pol-{state}", 1, oid))
        db.commit()
        r = client.get(f"/v1/stocks/{iid}/quote", params={"quote_policy_version": f"pol-{state}"})
        assert r.status_code == expect, r.text
        if expect == 200:
            assert r.json()["data"]["quote_snapshot_id"] == str(snap) and r.json()["data"]["quote"]["last_price_raw"] == "12.5"
            assert r.json()["meta"]["input_snapshot_refs"][0]["snapshot_id"] == str(snap)
        else:
            assert r.json()["error"]["code"] == "QUOTE_SNAPSHOT_NOT_READY"


# ---- bars -----------------------------------------------------------------------------------------------------
def bars_params(snap, tf="1d", mode="raw", start="2026-09-01T00:00:00Z", end="2026-11-30T00:00:00Z", asof="2026-11-30T00:00:00Z", **extra):
    return {"snapshot_id": snap, "decision_as_of": asof, "availability_policy_version": POLICY, "timeframe": tf,
            "session_policy_version": "regular_continuous_v1", "adjustment_mode": mode, "start": start, "end": end, **extra}


@pytest.mark.pg
def test_daily_bars_pit_filter_finality_and_window_flags(client, db):
    iid = seed_instrument(db, "600020")
    days = weekdays(date(2026, 10, 5), 5)
    snap = seed_manifest(db, "bars", raw_coverage(days[0], days[-1]))
    seed_daily_bars(db, iid, snap, days)
    r = client.get(f"/v1/stocks/{iid}/bars", params=bars_params(snap, start="2026-10-05T00:00:00Z"))
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert [b["trade_date"] for b in d["bars"]] == [x.isoformat() for x in days]
    assert d["bars"][0]["open_price_raw"] == "10" and d["bars"][0]["is_final"] is True
    assert d["adjustment_mode"] == "raw" and d["adjustment_version"] is None and d["window_complete"] is True
    assert r.json()["meta"]["input_snapshot_refs"][0]["snapshot_id"] == str(snap)
    # PIT: a decision time before the 4th bar became available excludes it and everything later
    cutoff = cst(days[2], 18, 0).isoformat().replace("+00:00", "Z")
    early = client.get(f"/v1/stocks/{iid}/bars", params=bars_params(snap, start="2026-10-05T00:00:00Z", asof=cutoff)).json()["data"]["bars"]
    assert [b["trade_date"] for b in early] == [x.isoformat() for x in days[:3]]
    # a window that starts before the dataset's declared coverage is reported incomplete, not silently shortened
    wide = client.get(f"/v1/stocks/{iid}/bars", params=bars_params(snap)).json()
    assert wide["data"]["window_complete"] is False
    assert any(x.startswith("window_starts_before_dataset_coverage") for x in wide["meta"]["coverage"]["unverifiable_reasons"])
    assert wide["meta"]["quality_status"] == "partial"
    # an unregistered availability policy is refused (409); a registered reconstructed one is filtered by real available_at and says so
    odd = client.get(f"/v1/stocks/{iid}/bars", params={**bars_params(snap), "availability_policy_version": "custom-v9"})
    assert odd.status_code == 409 and odd.json()["error"]["code"] == "AVAILABILITY_POLICY_UNREGISTERED"
    recon = client.get(f"/v1/stocks/{iid}/bars", params={**bars_params(snap), "availability_policy_version": "reconstructed-session-close-plus-1h-v1"}).json()
    assert "AVAILABILITY_POLICY_NOT_APPLIED" in {w["code"] for w in recon["meta"]["warnings"]}
    # empty window is "no bars", flagged incomplete
    none = client.get(f"/v1/stocks/{iid}/bars", params=bars_params(snap, start="2026-12-01T00:00:00Z", end="2026-12-05T00:00:00Z"))
    assert none.json()["data"]["bars"] == [] and none.json()["data"]["window_complete"] is False


@pytest.mark.pg
def test_weekly_aggregation_is_final_only_for_a_completed_period(client, db):
    iid = seed_instrument(db, "600021")
    days = weekdays(date(2026, 10, 5), 7)  # Mon 10-05 .. Tue 10-13: week 1 complete, week 2 has two sessions
    snap = seed_manifest(db, "bars", raw_coverage(days[0], days[-1]))
    seed_daily_bars(db, iid, snap, days)
    r = client.get(f"/v1/stocks/{iid}/bars", params=bars_params(snap, tf="1w", start="2026-10-01T00:00:00Z"))
    assert r.status_code == 200, r.text
    wk = r.json()["data"]["bars"]
    assert len(wk) == 2 and wk[0]["timeframe"] == "1w"
    assert wk[0]["is_final"] is True and wk[1]["is_final"] is False
    assert wk[0]["open_price_raw"] == "10" and wk[0]["close_price_raw"] == "14.5" and wk[0]["volume_shares"] == 15000
    assert wk[0]["field_quality"]["*"]["status"] == "derived"
    mo = client.get(f"/v1/stocks/{iid}/bars", params=bars_params(snap, tf="1mo", start="2026-10-01T00:00:00Z")).json()["data"]["bars"]
    assert len(mo) == 1 and mo[0]["is_final"] is False  # the calendar cannot prove October ended


@pytest.mark.pg
def test_bars_refuse_what_the_snapshot_cannot_provide(client, db):
    iid = seed_instrument(db, "600022")
    days = weekdays(date(2026, 10, 5), 3)
    snap = seed_manifest(db, "bars", raw_coverage(days[0], days[-1]))
    seed_daily_bars(db, iid, snap, days)
    p = bars_params(snap, start="2026-10-01T00:00:00Z")
    r = client.get(f"/v1/stocks/{iid}/bars", params={**p, "adjustment_mode": "forward_adjusted"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "CAPABILITY_UNAVAILABLE"
    assert "adjusted_prices" in r.json()["error"]["field_errors"][0]["message"]
    r = client.get(f"/v1/stocks/{iid}/bars", params={**p, "timeframe": "1m"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "CAPABILITY_UNAVAILABLE"  # daily snapshot has no true minute OHLC
    assert client.get(f"/v1/stocks/{iid}/bars", params={**p, "adjustment_version": "adj-v1"}).status_code == 400
    assert client.get(f"/v1/stocks/{iid}/bars", params={**p, "timeframe": "banana"}).status_code == 400
    assert client.get(f"/v1/stocks/{iid}/bars", params={**p, "adjustment_mode": "weird"}).status_code == 400
    assert client.get(f"/v1/stocks/{iid}/bars", params={**p, "start": "2026-12-01T00:00:00Z"}).status_code == 400
    assert client.get(f"/v1/stocks/{iid}/bars", params={**p, "snapshot_id": uuid4()}).status_code == 404
    notready = seed_manifest(db, "bars", raw_coverage(days[0], days[-1]), state="COMMITTED_UNPUBLISHED")
    assert client.get(f"/v1/stocks/{iid}/bars", params={**p, "snapshot_id": notready}).json()["error"]["code"] == "SNAPSHOT_NOT_READY"
    not_bars = seed_manifest(db, "news", {})
    assert client.get(f"/v1/stocks/{iid}/bars", params={**p, "snapshot_id": not_bars}).json()["error"]["code"] == "SNAPSHOT_NOT_BARS"
    wrong_scope = client.get(f"/v1/stocks/{iid}/bars", params={**p, "session_policy_version": "cn-a-continuous-v1"})
    assert wrong_scope.status_code == 409 and wrong_scope.json()["error"]["code"] == "SESSION_POLICY_UNAVAILABLE"


@pytest.mark.pg
def test_forward_adjusted_bars_come_from_the_frozen_lake_manifest(client, db, tmp_path, monkeypatch):
    monkeypatch.setenv("PULSE_LAKE", str(tmp_path))
    iid = seed_instrument(db, "600023")
    days = weekdays(date(2026, 10, 5), 4)
    other = uuid4()
    digest = write_lake(tmp_path, "bars_legacy_fwd_adjusted_1d",
                        [lake_row(iid, d, str(20 + i)) for i, d in enumerate(days)] + [lake_row(other, days[0], "99")])
    cov = {"adjustment_mode": "forward_adjusted", "adjustment_version": "legacy-qfq-unversioned", "capability": FWD_CAP,
           "first_date": days[0].isoformat(), "last_date": days[-1].isoformat(), "adjustment_note": "unknown factors"}
    snap = seed_manifest(db, "bars_adj", cov, content_hash=digest)
    p = bars_params(snap, mode="forward_adjusted", start="2026-10-01T00:00:00Z")
    r = client.get(f"/v1/stocks/{iid}/bars", params=p)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["adjustment_mode"] == "forward_adjusted" and d["adjustment_version"] == "legacy-qfq-unversioned"
    assert [b["close_price"] for b in d["bars"]] == ["20", "21", "22", "23"]  # only this instrument; AdjustedBar names, no *_raw
    assert "close_price_raw" not in d["bars"][0] and d["bars"][0]["turnover_amount_cny"] is None
    assert d["bars"][0]["field_quality"]["turnover_amount_cny"]["status"] == "missing"
    explicit = client.get(f"/v1/stocks/{iid}/bars", params={**p, "adjustment_version": "legacy-qfq-unversioned"})
    assert explicit.status_code == 200
    bad = client.get(f"/v1/stocks/{iid}/bars", params={**p, "adjustment_version": "adj-v1"})
    assert bad.status_code == 409 and bad.json()["error"]["code"] == "ADJUSTMENT_VERSION_MISMATCH"
    back = client.get(f"/v1/stocks/{iid}/bars", params={**p, "adjustment_mode": "backward_adjusted"})
    assert back.status_code == 409 and back.json()["error"]["code"] == "ADJUSTMENT_MODE_UNAVAILABLE"
    raw = client.get(f"/v1/stocks/{iid}/bars", params={**p, "adjustment_mode": "raw"})
    assert raw.status_code == 409  # the adjusted snapshot has no raw prices
    wk = client.get(f"/v1/stocks/{iid}/bars", params={**p, "timeframe": "1w"})
    assert wk.status_code == 200 and wk.json()["data"]["bars"][0]["adjustment_mode"] == "forward_adjusted"
    assert client.get(f"/v1/stocks/{iid}/bars", params={**p, "timeframe": "5m"}).status_code == 409
    # tampering with the manifest bytes is refused, not read
    (tmp_path / "manifests" / f"{digest}.json").write_bytes(b'{"dataset_id":"x","members":[],"meta":{}}')
    assert client.get(f"/v1/stocks/{iid}/bars", params=p).status_code == 503


@pytest.mark.pg
def test_minute_bars_native_timeframe(client, db):
    iid = seed_instrument(db, "600024")
    day = date(2026, 10, 9)
    cap = {**{"dataset_id": "bars_raw_1m", "source_id": "apisrc", "endpoint_id": "m", "estimated": False, "notes": []},
           "features": ["interval_volume", "minute_bars_true_ohlc", "raw_prices"]}
    snap = seed_manifest(db, "bars_1m", {"adjustment_mode": "raw", "capability": cap, "first_date": day.isoformat(), "last_date": day.isoformat()})
    for i in range(3):
        o, c = cst(day, 9, 30 + i), cst(day, 9, 31 + i)
        db.execute("INSERT INTO market.bar_projection(trade_date,instrument_id,timeframe,session_scope,bar_open_time,bar_close_time,snapshot_id,"
                   "currency,open_price_raw,high_price_raw,low_price_raw,close_price_raw,volume_shares,is_final,available_at,quality_status)"
                   " VALUES (%s,%s,'1m','regular_continuous_v1',%s,%s,%s,'CNY',10,11,9,10.5,100,%s,%s,'partial')",
                   (day, iid, o, c, snap, i < 2, c + timedelta(seconds=90)))
    db.commit()
    p = bars_params(snap, tf="1m", start="2026-10-09T00:00:00Z", end="2026-10-09T08:00:00Z", asof="2026-10-09T08:00:00Z")
    r = client.get(f"/v1/stocks/{iid}/bars", params=p)
    assert r.status_code == 200, r.text
    bars = r.json()["data"]["bars"]
    assert len(bars) == 3 and bars[0]["timeframe"] == "1m" and bars[0]["turnover_amount_cny"] is None
    # the not-yet-available third bar disappears when decided earlier
    early = client.get(f"/v1/stocks/{iid}/bars", params={**p, "decision_as_of": "2026-10-09T01:33:00Z"}).json()["data"]["bars"]
    assert len(early) == 1
    assert client.get(f"/v1/stocks/{iid}/bars", params={**p, "timeframe": "1d"}).status_code == 409

