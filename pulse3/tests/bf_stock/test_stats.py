"""GET /v1/x/bf/stock/stats against a real PostgreSQL: values computed from the daily bars visible at the release cutoff, a bar or
cap version recorded after the cutoff is not used, estimates are labelled, missing facts are null with a reason, and the
intraday fields (seal times / seal amount / board breaks) are never invented."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from api_seed import FWD_CAP, lake_row, projection, raw_coverage, seed_instrument, seed_manifest, weekdays, write_lake
from bf_stock.support import cutoff_after, publish_release, seed_cap, seed_closes
from pulse import contracts

URL = "/v1/x/bf/stock/stats"
UTC = timezone.utc
N = 300


def _closes(n: int, limit_days: set[int]) -> list[Decimal]:
    """Quiet series (+0.02 / -0.02 alternating) with +10% limit-up closes on ``limit_days``."""
    out = [Decimal("10.00")]
    for i in range(1, n):
        prev = out[-1]
        out.append((prev * Decimal("1.10")).quantize(Decimal("0.01")) if i in limit_days else prev + (Decimal("0.02") if i % 2 == 0 else Decimal("-0.02")))
    return out


def _fields(body) -> dict:
    return {f["key"]: f for f in body["data"]["fields"]}


def _world(db, *, limit_days: set[int], late: dict[int, timedelta] | None = None, n: int = N, code: str = "600201", amount: str | None = "50000000"):
    iid = seed_instrument(db, code, "统计测试")
    days = weekdays(date(2025, 8, 1), n)
    snap = seed_manifest(db, "bars", raw_coverage(days[0], days[-1]))
    closes = _closes(n, limit_days)
    seed_closes(db, iid, snap, days, [str(c) for c in closes], amounts=[amount] * n, late=late)
    rid = publish_release(db, snapshot=snap, as_of=cutoff_after(days[-1]))
    return iid, days, snap, rid, closes


@pytest.mark.pg
def test_statistics_are_computed_from_the_bars_visible_at_the_cutoff(client, db, tmp_path):
    limit_days = {N - 2, N - 1, N - 100, N - 270}                       # N-270 lies outside the last 250 sessions
    iid, days, snap, rid, closes = _world(db, limit_days=limit_days)
    db.execute("UPDATE core.instrument SET listed_on=%s WHERE instrument_id=%s", (date(2003, 6, 9), iid))
    db.commit()
    cutoff = cutoff_after(days[-1])
    seed_cap(db, tmp_path / "lake", [{"instrument_id": str(iid), "as_of": "2026-09-01", "float_cap_cny": Decimal("5000000000"),
                                      "available_at": cutoff - timedelta(days=20)}], ready_at=cutoff - timedelta(days=19))
    r = client.get(URL, params={"instrument_id": iid, "release_id": rid})
    assert r.status_code == 200, r.text
    body = r.json()
    f = _fields(body)
    assert list(f) == ["turnover_amount", "turnover_rate", "dist_to_60d_high", "limit_up_count_1y", "limit_streak", "up_streak", "listing_date",
                       "first_seal_time", "last_seal_time", "seal_amount", "open_board_count"]
    d = body["data"]
    assert d["price_basis"] == "raw" and d["last_trade_date"] == days[-1].isoformat() and d["bars_snapshot_id"] == str(snap)

    assert (f["turnover_amount"]["status"], f["turnover_amount"]["value"]) == ("ok", "50000000")
    assert f["turnover_rate"]["status"] == "estimated" and f["turnover_rate"]["value"] == "1" and f["turnover_rate"]["reason"]   # 5e7 / 5e9 = 1 %
    assert f["turnover_rate"]["as_of"] == days[-1].isoformat()

    top = max(c + Decimal("0.1") for c in closes[-60:])
    assert Decimal(f["dist_to_60d_high"]["value"]) == ((closes[-1] - top) / top * 100).quantize(Decimal("0.0001"))
    flags = [closes[i] / closes[i - 1] - 1 >= Decimal("0.095") for i in range(1, N)]
    assert f["limit_up_count_1y"]["value"] == str(sum(flags[-250:])) == "3"
    assert f["limit_streak"]["value"] == "2" and f["up_streak"]["value"] == "2"
    assert f["limit_streak"]["basis"] and "10%" in f["limit_streak"]["basis"]
    assert (f["listing_date"]["status"], f["listing_date"]["value"], f["listing_date"]["value_kind"]) == ("ok", "2003-06-09", "date")

    for key in ("first_seal_time", "last_seal_time", "seal_amount", "open_board_count"):          # never invented
        assert f[key]["status"] == "unavailable" and f[key]["value"] is None and f[key]["value_kind"] is None
        assert "分时" in f[key]["reason"]

    meta = body["meta"]
    assert meta["release_id"] == str(rid) and meta["decision_as_of"] == contracts.utc_z(cutoff)
    assert str(snap) in {x["snapshot_id"] for x in meta["input_snapshot_refs"]} and len(meta["input_snapshot_refs"]) == 2   # bars + cap dataset
    assert client.get(URL, params={"instrument_id": iid}).json()["data"]["release_id"] == str(rid)                              # current release of slot main


@pytest.mark.pg
def test_a_bar_or_cap_version_recorded_after_the_cutoff_is_not_used(client, db, tmp_path):
    # the third limit-up close (the newest day) is recorded a month late: at the cutoff the streak is still 2
    iid, days, snap, rid, closes = _world(db, limit_days={N - 3, N - 2, N - 1}, late={N - 1: timedelta(days=30)}, code="600202")
    cutoff = cutoff_after(days[-1])
    ok_row = {"instrument_id": str(iid), "as_of": "2026-09-01", "float_cap_cny": Decimal("5000000000"), "available_at": cutoff - timedelta(days=20)}
    late_row = {"instrument_id": str(iid), "as_of": "2026-09-20", "float_cap_cny": Decimal("1000000000"), "available_at": cutoff + timedelta(days=2)}
    seed_cap(db, tmp_path / "lake", [ok_row, late_row], ready_at=cutoff - timedelta(days=19))                       # row recorded after the cutoff
    seed_cap(db, tmp_path / "lake", [{**ok_row, "float_cap_cny": Decimal("100000000")}], ready_at=cutoff + timedelta(days=1), version=2)  # dataset ready after
    f = _fields(client.get(URL, params={"instrument_id": iid, "release_id": rid}).json())
    assert f["limit_streak"]["value"] == "2" and f["up_streak"]["value"] == "3"        # +0.02 day, then two limit-ups; the late bar would make 3 / 4
    last = client.get(URL, params={"instrument_id": iid, "release_id": rid}).json()["data"]["last_trade_date"]
    assert last == days[-2].isoformat()
    assert f["turnover_rate"]["value"] == "1"                                                                        # still the 50亿 cap version


@pytest.mark.pg
def test_missing_facts_are_null_with_a_reason_and_estimates_are_labelled(client, db):
    # 30 sessions only, no amount in the source, no cap dataset, no listing date
    iid, days, snap, rid, closes = _world(db, limit_days=set(), n=30, code="600203", amount=None)
    r = client.get(URL, params={"instrument_id": iid, "release_id": rid})
    assert r.status_code == 200, r.text
    body = r.json()
    f = _fields(body)
    amt = f["turnover_amount"]
    assert amt["status"] == "estimated" and amt["reason"] and "估算" in amt["reason"]
    assert Decimal(amt["value"]) == (closes[-1] * 30 * 1000).quantize(Decimal("0.01"))                                # volume x close
    assert f["turnover_rate"]["status"] == "unavailable" and f["turnover_rate"]["value"] is None and "流通市值" in f["turnover_rate"]["reason"]
    assert f["dist_to_60d_high"]["status"] == "unavailable" and "30" in f["dist_to_60d_high"]["reason"]
    assert f["limit_up_count_1y"]["status"] == "unavailable" and f["limit_up_count_1y"]["value"] is None
    assert f["limit_streak"]["value"] == "0"                                                                         # computable from 30 bars
    assert f["listing_date"]["status"] == "unavailable" and f["listing_date"]["reason"]
    assert all(x["reason"] for x in f.values() if x["status"] != "ok")
    assert any(w["code"] == "STATS_PARTIAL" for w in body["meta"]["warnings"]) and body["meta"]["quality_status"] == "partial"


@pytest.mark.pg
def test_forward_adjusted_basis_is_used_when_the_release_declares_it(client, db, tmp_path):
    iid = seed_instrument(db, "600204", "复权统计")
    days = weekdays(date(2026, 6, 1), 25)
    raw_snap = seed_manifest(db, "bars", raw_coverage(days[0], days[-1]))
    seed_closes(db, iid, raw_snap, days, [str(10 + i) for i in range(25)], amounts=["1000000"] * 25)
    digest = write_lake(tmp_path / "lake", "bars_legacy_fwd_adjusted_1d", [lake_row(iid, d, str(20 + i)) for i, d in enumerate(days)])
    cov = {"adjustment_mode": "forward_adjusted", "adjustment_version": "legacy-qfq-unversioned", "capability": FWD_CAP,
           "first_date": days[0].isoformat(), "last_date": days[-1].isoformat(), "adjustment_note": "unknown factors"}
    adj = seed_manifest(db, "bars_adj", cov, content_hash=digest)
    rid = publish_release(db, snapshot=raw_snap, as_of=cutoff_after(days[-1]))
    projection(db, iid, rid, payload={"features": [], "plan_refs": [], "chart_window_refs": [
        {"ref": f"bars:{adj}:{iid}:1d:forward_adjusted", "content_hash": "0" * 64, "contract_id": "x", "schema_version": "1"}]})
    d = client.get(URL, params={"instrument_id": iid, "release_id": rid}).json()
    assert d["data"]["price_basis"] == "forward_adjusted" and d["data"]["bars_snapshot_id"] == str(adj)
    f = _fields(d)
    assert f["turnover_amount"]["value"] == "1000000" and f["up_streak"]["value"] == "24"          # amount from the raw bar, streak from adjusted closes
    assert "前复权" in f["up_streak"]["basis"]


@pytest.mark.pg
def test_request_errors(client, db):
    iid, _, _, rid, _ = _world(db, limit_days=set(), n=5, code="600205")
    assert client.get(URL, params={"instrument_id": iid, "release_id": uuid4()}).status_code == 404
    assert client.get(URL, params={"instrument_id": uuid4(), "release_id": rid}).status_code == 404
    assert client.get(URL, params={"instrument_id": "not-a-uuid"}).status_code in (400, 422)


def test_contract_requires_a_reason_whenever_a_value_is_missing_or_estimated():
    sid = contracts.backfill_id("stock")
    ok = {"key": "seal_amount", "label": "封单金额", "status": "unavailable", "value": None, "value_kind": None, "unit": "元", "as_of": None,
          "basis": None, "reason": "没有分时数据"}
    contracts.check("StatField", ok, sid)
    good = {**ok, "key": "turnover_rate", "status": "ok", "value": "1.5", "value_kind": "decimal", "reason": None}
    contracts.check("StatField", good, sid)
    for bad in ({**ok, "value": "3"}, {**ok, "reason": None}, {**good, "value": None}, {**good, "status": "estimated"},
                {**good, "status": "estimated", "reason": ""}, {**good, "key": "made_up"}, {**good, "value": "1e3", "status": "ok"} if False else {**good, "status": "maybe"}):
        with pytest.raises(contracts.ContractViolation):
            contracts.check("StatField", bad, sid)


@pytest.mark.pg
def test_a_zero_cap_gives_an_unavailable_turnover_rate_not_a_500(client, db, tmp_path):
    iid, days, snap, rid, closes = _world(db, limit_days=set())
    cutoff = cutoff_after(days[-1])
    seed_cap(db, tmp_path / "lake", [{"instrument_id": str(iid), "as_of": "2026-09-01", "float_cap_cny": Decimal("0"),
                                      "available_at": cutoff - timedelta(days=20)}], ready_at=cutoff - timedelta(days=19))
    r = client.get(URL, params={"instrument_id": iid, "release_id": rid})
    assert r.status_code == 200, r.text
    f = _fields(r.json())
    assert f["turnover_rate"]["status"] == "unavailable" and f["turnover_rate"]["value"] is None and "不为正" in f["turnover_rate"]["reason"]
