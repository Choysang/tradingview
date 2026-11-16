"""GET /v1/x/bf/stock/indicators against a real PostgreSQL: series come from the bars of the chart window, aligned to them,
with warm-up nulls, a point-in-time cutoff, the window hash of the bars endpoint, and the contract check on every payload."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from api_seed import FWD_CAP, lake_row, projection, raw_coverage, seed_daily_bars, seed_instrument, seed_manifest, weekdays, write_lake
from pulse import contracts
from pulse.application import bf_stock_service as S
from bf_stock.support import cutoff_after, publish_release, seed_closes

POLICY = "observed-actual-available-at-v1"
URL = "/v1/x/bf/stock/indicators"


def _world(db, n: int = 70, code: str = "600101"):
    iid = seed_instrument(db, code, "指标测试")
    days = weekdays(date(2026, 6, 1), n)
    snap = seed_manifest(db, "bars", raw_coverage(days[0], days[-1]))
    seed_daily_bars(db, iid, snap, days)
    cutoff = cutoff_after(days[-1])
    rid = publish_release(db, snapshot=snap, as_of=cutoff)
    return iid, days, snap, rid, cutoff


def _series(data):
    return {s["key"]: s for s in data["series"]}


@pytest.mark.pg
def test_series_are_aligned_to_the_bars_and_warm_up_is_null(client, db):
    iid, days, snap, rid, _ = _world(db)
    r = client.get(URL, params={"instrument_id": iid, "release_id": rid, "timeframe": "1d", "adjustment": "raw"})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert [b["trade_date"] for b in d["bars"]] == [x.isoformat() for x in days]
    s = _series(d)
    assert set(s) == {"ma5", "ma10", "ma20", "ma60", "ema12", "ema26", "boll_mid", "boll_upper", "boll_lower", "macd", "macd_signal",
                      "macd_hist", "kdj_k", "kdj_d", "kdj_j", "rsi6", "rsi14"}
    for ser in s.values():
        assert len(ser["values"]) == len(d["bars"]) == 70
        first = ser["first_valid_index"]
        assert first is not None and first >= ser["warmup_bars"]
        assert all(v is None for v in ser["values"][:first]), ser["key"]          # warm-up is missing, never 0
        assert all(v is not None for v in ser["values"][first:]), ser["key"]
        assert ser["valid_count"] == 70 - first
    assert s["ma5"]["values"][4] == "12.5" and s["ma5"]["values"][3] is None            # closes are 10.5, 11.5, ...
    assert s["ma60"]["first_valid_index"] == 59 and s["ma60"]["valid_count"] == 11
    assert s["rsi14"]["values"][-1] == "100"                                              # only up days
    assert (s["macd"]["first_valid_index"], s["macd_signal"]["first_valid_index"]) == (25, 33)
    assert s["kdj_k"]["first_valid_index"] == 8 and s["boll_upper"]["first_valid_index"] == 19
    assert s["ma20"]["pane"] == "price" and s["macd_hist"]["pane"] == "sub" and s["ma20"]["params"] == {"n": 20}
    # boll mid is the MA20 at the same bar; upper > mid > lower
    assert s["boll_mid"]["values"][-1] == s["ma20"]["values"][-1]
    assert Decimal(s["boll_upper"]["values"][-1]) > Decimal(s["boll_mid"]["values"][-1]) > Decimal(s["boll_lower"]["values"][-1])
    assert d["bars"][-1]["is_final"] is True


@pytest.mark.pg
def test_window_hash_equals_the_bars_endpoint_for_the_same_window(client, db):
    iid, days, snap, rid, cutoff = _world(db)
    body = client.get(URL, params={"instrument_id": iid, "release_id": rid}).json()
    d = body["data"]
    end = contracts.utc_z(cutoff)
    bars = client.get(f"/v1/stocks/{iid}/bars", params={
        "snapshot_id": snap, "decision_as_of": end, "availability_policy_version": POLICY, "timeframe": "1d",
        "session_policy_version": "regular_continuous_v1", "adjustment_mode": "raw", "start": contracts.utc_z(S.window_start(cutoff, "1d")), "end": end})
    assert bars.status_code == 200, bars.text
    assert d["window_hash"] == bars.json()["data"]["window_hash"]
    assert [b["bar_open_time"] for b in d["bars"]] == [b["bar_open_time"] for b in bars.json()["data"]["bars"]]
    assert d["snapshot_id"] == str(snap) and d["window_start"] and d["window_end"] == end
    meta = body["meta"]
    assert meta["release_id"] == str(rid) and meta["decision_as_of"] == end
    assert [x["snapshot_id"] for x in meta["input_snapshot_refs"]] == [str(snap)]
    # without release_id the current release of the main slot is used
    assert client.get(URL, params={"instrument_id": iid}).json()["data"]["release_id"] == str(rid)


@pytest.mark.pg
def test_a_bar_recorded_after_the_cutoff_is_not_used(client, db):
    iid = seed_instrument(db, "600102", "迟到K线")
    days = weekdays(date(2026, 6, 1), 30)
    snap = seed_manifest(db, "bars", raw_coverage(days[0], days[-1]))
    closes = [str(10 + i) for i in range(30)]
    seed_closes(db, iid, snap, days, closes, amounts=["1000"] * 30, late={25: timedelta(days=30)})   # day 25 is recorded a month late
    rid = publish_release(db, snapshot=snap, as_of=cutoff_after(days[-1]))
    d = client.get(URL, params={"instrument_id": iid, "release_id": rid, "set": "ma"}).json()["data"]
    dates = [b["trade_date"] for b in d["bars"]]
    assert days[25].isoformat() not in dates and len(dates) == 29
    ma5 = _series(d)["ma5"]["values"]
    assert len(ma5) == 29
    # the five newest bars are days 24, 26, 27, 28, 29 (day 25 is not visible yet): closes 10 + i
    assert Decimal(ma5[-1]) == Decimal(sum([24, 26, 27, 28, 29])) / 5 + 10


@pytest.mark.pg
def test_a_missing_price_inside_a_window_gives_null_not_a_bridge(client, db):
    iid = seed_instrument(db, "600103", "缺口")
    days = weekdays(date(2026, 6, 1), 12)
    snap = seed_manifest(db, "bars", raw_coverage(days[0], days[-1]))
    closes = [str(10 + i) for i in range(12)]
    closes[6] = None
    seed_closes(db, iid, snap, days, closes, amounts=["1"] * 12)
    rid = publish_release(db, snapshot=snap, as_of=cutoff_after(days[-1]))
    ma5 = _series(client.get(URL, params={"instrument_id": iid, "release_id": rid, "set": "ma"}).json()["data"])["ma5"]["values"]
    assert ma5[4] == "12" and ma5[5] == "13"                                        # windows before the gap are intact
    assert all(v is None for v in ma5[6:11])                                        # every window holding the gap is missing
    assert ma5[11] == "19"                                                                                         # closes 17..21 -> mean 19


@pytest.mark.pg
def test_set_selection_and_request_errors(client, db):
    iid, _, _, rid, _ = _world(db, n=30)
    only = client.get(URL, params={"instrument_id": iid, "release_id": rid, "set": "boll, macd"}).json()["data"]
    assert only["requested"] == ["boll", "macd"] and {x["indicator"] for x in only["series"]} == {"boll", "macd"}
    bad = client.get(URL, params={"instrument_id": iid, "release_id": rid, "set": "ma,zigzag"})
    assert bad.status_code == 400 and bad.json()["error"]["field_errors"][0]["path"] == "/set"
    assert client.get(URL, params={"instrument_id": iid, "release_id": rid, "timeframe": "3d"}).status_code == 400
    assert client.get(URL, params={"instrument_id": iid, "release_id": rid, "adjustment": "wild"}).status_code == 400
    assert client.get(URL, params={"instrument_id": iid, "release_id": uuid4()}).status_code == 404
    assert client.get(URL, params={"instrument_id": uuid4(), "release_id": rid}).status_code == 404
    # the snapshot has only raw prices: an adjusted chart is refused with the bars endpoint's structured 409, not guessed
    adj = client.get(URL, params={"instrument_id": iid, "release_id": rid, "adjustment": "forward_adjusted"})
    assert adj.status_code == 409 and adj.json()["error"]["code"] in {"CAPABILITY_UNAVAILABLE", "ADJUSTMENT_MODE_UNAVAILABLE"}


@pytest.mark.pg
def test_forward_adjusted_series_use_the_snapshot_the_workspace_names(client, db, tmp_path):
    iid = seed_instrument(db, "600104", "前复权")
    days = weekdays(date(2026, 6, 1), 25)
    raw_snap = seed_manifest(db, "bars", raw_coverage(days[0], days[-1]))
    seed_daily_bars(db, iid, raw_snap, days)
    digest = write_lake(tmp_path / "lake", "bars_legacy_fwd_adjusted_1d", [lake_row(iid, d, str(20 + i)) for i, d in enumerate(days)])
    cov = {"adjustment_mode": "forward_adjusted", "adjustment_version": "legacy-qfq-unversioned", "capability": FWD_CAP,
           "first_date": days[0].isoformat(), "last_date": days[-1].isoformat(), "adjustment_note": "unknown factors"}
    adj_snap = seed_manifest(db, "bars_adj", cov, content_hash=digest)
    rid = publish_release(db, snapshot=raw_snap, as_of=cutoff_after(days[-1]))
    projection(db, iid, rid, payload={"features": [], "plan_refs": [], "chart_window_refs": [
        {"ref": f"bars:{adj_snap}:{iid}:1d:forward_adjusted", "content_hash": "0" * 64, "contract_id": "x", "schema_version": "1"}]})
    d = client.get(URL, params={"instrument_id": iid, "release_id": rid, "adjustment": "forward_adjusted", "set": "ma"}).json()["data"]
    assert d["adjustment_mode"] == "forward_adjusted" and d["adjustment_version"] == "legacy-qfq-unversioned" and d["snapshot_id"] == str(adj_snap)
    assert _series(d)["ma5"]["values"][4] == "22"                                        # mean(20..24)


def test_contract_refuses_malformed_payloads():
    ok = {"key": "ma5", "indicator": "ma", "output": "ma", "pane": "price", "label": "MA5", "params": {"n": 5}, "definition_version": "v1",
          "implementation_digest": "a" * 64, "warmup_bars": 4, "first_valid_index": 4, "valid_count": 1, "values": [None, None, None, None, "1.5"]}
    sid = contracts.backfill_id("stock")
    contracts.check("IndicatorSeries", ok, sid)
    for bad in ({**ok, "values": [None, 1.5]}, {**ok, "values": ["1e3"]}, {**ok, "pane": "left"}, {**ok, "implementation_digest": "xyz"}):
        with pytest.raises(contracts.ContractViolation):
            contracts.check("IndicatorSeries", bad, sid)
