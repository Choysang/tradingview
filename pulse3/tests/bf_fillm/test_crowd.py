"""Crowd and linkage reads on real PostgreSQL (synthetic world): industry diffusion pinned to a release, institutional net buy from stored
Dragon-Tiger reports (point in time, single-session windows, no 0 for unknown), name clusters reported unavailable.
SYNTHETIC data: proves wiring, point-in-time rules and null policy, not market behaviour."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from pulse import contracts
from pulse.adapters.postgres import intelligence_repo as repo
from pulse.adapters.postgres.ingest_repo import insert_raw_object
from pulse.domain.intelligence import seats

pytestmark = pytest.mark.pg
UTC = timezone.utc
BASE = "/v1/x/bf/fillm"
SLOT = "main"


@pytest.fixture()
def release(built):
    conn = built[1]
    return conn.execute("select r.release_id, r.decision_as_of from decision.release_slot s join decision.release r on r.release_id=s.current_release_id"
                        " where s.slot_key=%s", (SLOT,)).fetchone()


def _session(client, release_id) -> date:
    return date.fromisoformat(client.get(f"{BASE}/crowd", params={"release_id": release_id}).json()["data"]["session_date"])


def test_crowd_is_contract_valid_and_pinned_to_the_release(client, release):
    r = client.get(f"{BASE}/crowd", params={"release_id": str(release["release_id"])})
    assert r.status_code == 200, r.text
    body = r.json()
    contracts.check("CrowdSnapshot", body["data"], contracts.backfill_id("fillm"))
    d = body["data"]
    assert body["meta"]["release_id"] == str(release["release_id"])
    assert d["taxonomy_id"] and d["membership_snapshot_id"] and d["sessions_in_spread"]
    assert d["sessions_in_spread"] == sorted(d["sessions_in_spread"]) and len(d["sessions_in_spread"]) <= 5
    assert d["session_date"] == d["sessions_in_spread"][-1]
    assert any("same membership" in n for n in d["notes"])
    assert d["name_clusters"]["status"] == "unavailable" and d["name_clusters"]["retire_recommended"] is True
    assert "name history" in d["name_clusters"]["reason"]


def test_diffusion_industries_follow_the_rule(client, release):
    d = client.get(f"{BASE}/crowd", params={"release_id": str(release["release_id"])}).json()["data"]
    for ind in d["industries"]:
        assert ind["limit_ups"] >= 2 and ind["observed"] <= ind["members"]
        assert ind["leader"] is not None and ind["leader"]["is_limit_up"] is True
        assert all(f["is_limit_up"] is not True and Decimal(f["pct_change"]) >= 3 for f in ind["followers"])
        assert [s["session"] for s in ind["spread"]] == d["sessions_in_spread"]
        assert ind["spread"][-1]["limit_ups"] == ind["limit_ups"]
        for s in ind["spread"]:
            assert s["up_share"] is None or 0 <= Decimal(s["up_share"]) <= 100
    ups = [i["limit_ups"] for i in d["industries"]]
    assert ups == sorted(ups, reverse=True)


def test_unknown_taxonomy_reports_no_diffusion_instead_of_inventing_one(client, release):
    r = client.get(f"{BASE}/crowd", params={"release_id": str(release["release_id"]), "taxonomy_id": "no_such_taxonomy"})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["industries"] == [] and d["membership_snapshot_id"] is None
    assert any("no sector overview" in n for n in d["notes"])


def test_crowd_refuses_an_unknown_release(client):
    assert client.get(f"{BASE}/crowd", params={"release_id": str(uuid.uuid4())}).status_code == 404


# ---- institutional net buy -----------------------------------------------------------------------------------------------------
INST = "机构专用"


def _codes(conn, n: int) -> list[tuple[str, uuid.UUID]]:
    rows = conn.execute("select a.source_symbol code, a.instrument_id from core.instrument_alias a join core.instrument i using (instrument_id)"
                        " where a.source_id='legacy_warehouse' and i.instrument_type='equity' order by a.source_symbol limit %s", (n,)).fetchall()
    return [(r["code"], r["instrument_id"]) for r in rows]


def _store(conn, rep: seats.SeatReportRec, iid: uuid.UUID, key: str) -> None:
    now = datetime(2026, 11, 20, tzinfo=UTC)
    rid, _ = insert_raw_object(conn, source_id="legacy_warehouse", endpoint_id="evidence_files", source_schema_version="legacy-warehouse-1",
                               source_record_key=key, content_hash=uuid.uuid4().hex + uuid.uuid4().hex, storage_uri="raw/x", mime_type="application/octet-stream",
                               published_at=None, source_time_precision="unknown", first_seen_at=now, ingested_at=now)
    repo.insert_seat_report(conn, rep, revision=1, raw_object_id=rid, instrument_id=iid, seat_versions={}, published_at=None, now=now)


def _legacy(code: str, day: date, reason: str, rows: list[tuple], ing: datetime, sessions: list[date]):
    """rows: (side, seat, buy 亿, sell 亿)."""
    stocks = [{"code": code, "date": day, "reason": reason, "ingested_at": ing}]
    srows = [{"code": code, "date": day, "reason": reason, "side": side, "rank": i, "seat": seat, "seat_code": None, "buy": buy, "sell": sell,
              "net": None if buy is None or sell is None else buy - sell, "ingested_at": ing} for i, (side, seat, buy, sell) in enumerate(rows, start=1)]
    (rep,) = seats.reports_from_legacy(stocks, srows, sessions)
    return rep


@pytest.fixture()
def seeded(client, built, release, world):
    from pulse.adapters.postgres import ingest_repo
    from pulse.adapters.sources import registry

    conn = built[1]
    ingest_repo.bootstrap(conn, registry.SPECS.values(), datetime(2026, 11, 1, tzinfo=UTC))
    day = _session(client, str(release["release_id"]))
    (c1, i1), (c2, i2), (c3, i3), (c4, i4) = _codes(conn, 4)
    sess = [date(2026, 11, 6), date(2026, 11, 9), day]
    ok = datetime.combine(day, datetime.min.time(), tzinfo=UTC).replace(hour=9)            # available before the release cutoff
    late = release["decision_as_of"] + timedelta(days=2)
    reason = "日涨幅偏离值达到7%的前5只证券"
    # stock 1: the institution pool on both lists with the same amounts -> counted once: net = 3.0 - 1.0 = 2.0 亿
    _store(conn, _legacy(c1, day, reason, [("buy", INST, 3.0, 1.0), ("buy", "拉萨天团", 1.0, 0.1), ("sell", INST, 3.0, 1.0)], ok, sess), i1, f"s1-{uuid.uuid4()}")
    # stock 1 again under another reason (listing twice): the same institution row must not double the amount
    _store(conn, _legacy(c1, day, "日换手率达到20%的前5只证券", [("buy", INST, 3.0, 1.0)], ok, sess), i1, f"s1b-{uuid.uuid4()}")
    # stock 2: an institution sold more than it bought -> negative net
    _store(conn, _legacy(c2, day, reason, [("sell", INST, 0.4, 1.5)], ok, sess), i2, f"s2-{uuid.uuid4()}")
    # stock 3: listed, no institution row -> not shown at all (unknown, not 0)
    _store(conn, _legacy(c3, day, reason, [("buy", "拉萨天团", 1.0, 0.2)], ok, sess), i3, f"s3-{uuid.uuid4()}")
    # stock 4: a three-session window report is never added to a single-session net; a report available after the cutoff is not visible
    _store(conn, _legacy(c4, day, "连续三个交易日内，涨幅偏离值累计达到20%的证券", [("buy", INST, 9.0, 0.0)], ok, sess), i4, f"s4-{uuid.uuid4()}")
    _store(conn, _legacy(c4, day, "日振幅达到15%的前5只证券", [("buy", INST, 8.0, 0.0)], late, sess), i4, f"s4b-{uuid.uuid4()}")
    return {"day": day, "ids": (i1, i2, i3, i4)}


def test_institution_net_is_point_in_time_single_session_and_never_zero_for_unknown(client, release, seeded):
    d = client.get(f"{BASE}/crowd", params={"release_id": str(release["release_id"])}).json()["data"]
    inst = d["institutions"]
    assert inst["status"] == "ok" and inst["session_date"] == seeded["day"].isoformat()
    rows = {r["instrument_id"]: r for r in inst["rows"]}
    i1, i2, i3, i4 = (str(x) for x in seeded["ids"])
    assert Decimal(rows[i1]["net_cny"]) == Decimal("200000000") and rows[i1]["institution_rows"] == 1      # (3.0 - 1.0) 亿, listed twice -> once
    assert Decimal(rows[i1]["buy_cny"]) == Decimal("300000000") and Decimal(rows[i1]["sell_cny"]) == Decimal("100000000")
    assert Decimal(rows[i2]["net_cny"]) == Decimal("-110000000")                                           # (0.4 - 1.5) 亿
    assert i3 not in rows, "a listed stock without an institution row is unknown, not net 0"
    assert i4 not in rows, "a multi-session window and a report after the cutoff are not used"
    nets = [Decimal(r["net_cny"]) for r in inst["rows"] if r["net_cny"] is not None]
    assert nets == sorted(nets, reverse=True)
    assert inst["reports_seen"] >= 3 and "top-5" in inst["basis"]


def test_institution_row_with_an_undisclosed_amount_has_no_net(client, built, release, world):
    from pulse.adapters.postgres import ingest_repo
    from pulse.adapters.sources import registry

    conn = built[1]
    ingest_repo.bootstrap(conn, registry.SPECS.values(), datetime(2026, 11, 1, tzinfo=UTC))
    day = _session(client, str(release["release_id"]))
    code, iid = _codes(conn, 5)[4]
    ok = datetime.combine(day, datetime.min.time(), tzinfo=UTC).replace(hour=9)
    # sell amount missing: the importer keeps the row (buy known) and the evidence has no sell amount
    _store(conn, _legacy(code, day, "日涨幅偏离值达到7%的前5只证券", [("buy", INST, 2.0, None)], ok, [day]), iid, f"u-{uuid.uuid4()}")
    rows = {r["instrument_id"]: r for r in client.get(f"{BASE}/crowd", params={"release_id": str(release["release_id"])}).json()["data"]["institutions"]["rows"]}
    row = rows[str(iid)]
    assert row["net_cny"] is None and row["sell_cny"] is None and row["net_reason"]
