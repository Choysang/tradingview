"""Listing dates: the Eastmoney fallback when an exchange list is unreachable (never after a block); per-row source (offline fixtures + fake HTTP)."""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from pulse.adapters.sources import ref_listing as L
from pulse.application import refdata_core as C
from pulse.application.refdata_service import RefDataError
from pulse.adapters.sources.http import SourceBlocked, TransportFailure
from pulse.domain.evidence.announcements import NotData, SchemaChanged

from .support import World, fixture
from .test_pg_flow import build, manifests


# ---- parsing (recorded fixtures) ---------------------------------------------------------------------------------------------------------
def test_eastmoney_pages_parse_codes_dates_and_the_declared_total():
    rows, total = L.parse_eastmoney(fixture("eastmoney_list_p1.json"))
    assert total == 4942 and len(rows) == L.EM_PAGE_SIZE
    assert rows[0] == {"code": "000001", "list_date": date(1991, 4, 3), "name": "平安银行"} and rows[1]["list_date"] == date(1991, 1, 29)
    assert all(r["list_date"] is not None for r in rows)
    last, total2 = L.parse_eastmoney(fixture("eastmoney_list_last.json"))
    assert total2 == total and len(last) == 4942 - 49 * L.EM_PAGE_SIZE and last[-1]["code"] == "605599"        # a short page ends the list
    assert [r["code"] for r in rows] == sorted(r["code"] for r in rows)                                          # po=0: ascending code, stable paging


def test_eastmoney_unknown_dates_are_null_and_bad_shapes_are_errors():
    body = b'{"data":{"total":3,"diff":[{"f12":"000001","f26":"-"},{"f12":"000002","f26":0},{"f12":"600000","f26":19991110}]}}'
    rows, total = L.parse_eastmoney(body)
    assert [(r["code"], r["list_date"], r["name"]) for r in rows] == [("000001", None, None), ("000002", None, None), ("600000", date(1999, 11, 10), None)] and total == 3
    assert L.parse_eastmoney(b'{"rc":0,"data":null}') == ([], None)                                              # past the last page
    with pytest.raises(SchemaChanged):
        L.parse_eastmoney(b'{"data":{"total":1,"diff":[{"f12":"000001"}]}}')
    with pytest.raises(SchemaChanged):
        L.parse_eastmoney(b'{"rc":0}')
    with pytest.raises(NotData):
        L.parse_eastmoney(b"<html>")


def test_capture_log_names_the_two_eastmoney_requests():
    import json

    log = {c["file"]: c for c in json.loads(fixture("CAPTURE.json"))}
    assert {"eastmoney_list_p1.json", "eastmoney_list_last.json"} <= set(log) and "pn=1&" in log["eastmoney_list_p1.json"]["url"]


# ---- the service -------------------------------------------------------------------------------------------------------------------------------
def srcs(rs) -> dict:
    return dict(rs.read_frame(C.LISTING_DATASET)["source"].value_counts().iter_rows())


@pytest.mark.pg
def test_exchanges_reachable_never_touch_eastmoney_and_label_their_rows(conn, lake):
    world = World()
    svc, rs = build(conn, lake, world)
    out = rs.listings(L.ListingAdapter(world.client()), min_rows=10)
    assert len(world.requests) == 2 and not any("eastmoney" in u for u in world.requests)
    assert srcs(rs) == {"sse": 25, "szse": 25} and out.counts.get("exchange_lists_unreachable", 0) == 0


@pytest.mark.pg
def test_both_exchange_lists_unreachable_use_the_eastmoney_list_and_say_so_per_row(conn, lake):
    world = World()
    world.unreachable_hosts = {"query.sse.com.cn", "www.szse.cn"}
    svc, rs = build(conn, lake, world)
    out = rs.listings(L.ListingAdapter(world.client()), min_rows=10)
    assert out.counts["exchange_lists_unreachable"] == 2 and out.counts["eastmoney_requests"] == 3                  # 250 rows = 3 pages of 100
    frame = rs.read_frame(C.LISTING_DATASET)
    assert dict(frame.schema) == C.LISTING_SCHEMA and frame.height == 250 and srcs(rs) == {"eastmoney": 250}
    assert set(frame["exchange"].to_list()) == {"XSHG", "XSHE"} and frame.filter(pl.col("code") == "000001")["listed_on"].item() == date(1992, 1, 2)
    man = manifests(conn, C.LISTING_DATASET)[0]
    assert man["coverage_document"]["rows_by_source"] == {"eastmoney": 250}
    raw = {(r["source_id"], r["endpoint_id"]) for r in conn.execute("SELECT DISTINCT source_id, endpoint_id FROM ingest.raw_object")}
    assert raw == {("eastmoney_ref", "stock_list")}                                                                 # the captures are kept as evidence
    assert any("unreachable" in n and "Eastmoney" in n for n in out.notes)
    assert world.mono >= len(world.requests) - 1                                                                    # 1 request per second, failed requests included


@pytest.mark.pg
def test_only_the_unreachable_exchange_is_replaced(conn, lake):
    world = World()
    world.unreachable_hosts = {"query.sse.com.cn"}
    svc, rs = build(conn, lake, world)
    rs.listings(L.ListingAdapter(world.client()), min_rows=10)
    frame = rs.read_frame(C.LISTING_DATASET)
    assert srcs(rs) == {"eastmoney": 100, "szse": 25}                                                              # Shanghai from Eastmoney (100 synthetic rows), Shenzhen from the SZSE list
    assert set(frame.filter(pl.col("source") == "eastmoney")["exchange"].to_list()) == {"XSHG"}
    assert frame.filter(pl.col("source") == "szse")["code"].str.starts_with("0").all()


@pytest.mark.pg
def test_a_block_stops_and_never_triggers_the_fallback(conn, lake):
    world = World()
    world.host_status = {"query.sse.com.cn": 403}
    svc, rs = build(conn, lake, world)
    with pytest.raises(SourceBlocked):
        rs.listings(L.ListingAdapter(world.client()), min_rows=10)
    assert len(world.requests) == 1 and rs.read_dataset(C.LISTING_DATASET)[0] is None          # one request, nothing published, no Eastmoney call


@pytest.mark.pg
def test_a_block_by_eastmoney_stops_the_run_and_publishes_nothing(conn, lake):
    world = World()
    world.unreachable_hosts = {"query.sse.com.cn", "www.szse.cn"}
    world.host_status = {"82.push2.eastmoney.com": 429}
    svc, rs = build(conn, lake, world)
    with pytest.raises(SourceBlocked):
        rs.listings(L.ListingAdapter(world.client()), min_rows=10)
    assert rs.read_dataset(C.LISTING_DATASET)[0] is None
    n = len(world.requests)
    with pytest.raises(SourceBlocked, match="circuit_open"):                                                     # the cooldown holds for the next run
        rs.listings(L.ListingAdapter(world.client()), min_rows=10)
    assert len(world.requests) > n                                                                               # the exchange hosts are tried again, Eastmoney's gate stops it
    assert not any("eastmoney" in u for u in world.requests[n:])


@pytest.mark.pg
def test_everything_unreachable_is_a_transport_failure(conn, lake):
    world = World()
    world.unreachable_hosts = {"query.sse.com.cn", "www.szse.cn", "82.push2.eastmoney.com"}
    svc, rs = build(conn, lake, world)
    with pytest.raises(TransportFailure):
        rs.listings(L.ListingAdapter(world.client()), min_rows=10)
    assert rs.read_dataset(C.LISTING_DATASET)[0] is None


@pytest.mark.pg
def test_a_list_that_ends_before_its_declared_total_is_refused(conn, lake):
    world = World()
    world.unreachable_hosts = {"query.sse.com.cn", "www.szse.cn"}
    orig = world.eastmoney_page
    world.eastmoney_page = lambda page, size: orig(page, size) if page == 1 else b'{"rc":0,"data":null}'      # the server cut the paging after page 1
    svc, rs = build(conn, lake, world)
    with pytest.raises(RefDataError, match="incomplete: 100 of 250"):
        rs.listings(L.ListingAdapter(world.client()), min_rows=10)
    assert rs.read_dataset(C.LISTING_DATASET)[0] is None


@pytest.mark.pg
def test_the_source_is_part_of_the_snapshot_and_reconcile_reports_it(conn, lake):
    world = World()
    svc, rs = build(conn, lake, world)
    rs.listings(L.ListingAdapter(world.client()), min_rows=10)
    world.advance(hours=1)
    again = rs.listings(L.ListingAdapter(world.client()), min_rows=10)
    assert again.counts["snapshots_unchanged"] == 1 and len(manifests(conn, C.LISTING_DATASET)) == 1
    world.unreachable_hosts = {"query.sse.com.cn", "www.szse.cn"}
    world.advance(hours=1)
    rs.listings(L.ListingAdapter(world.client()), min_rows=10)
    assert len(manifests(conn, C.LISTING_DATASET)) == 2                                                        # same kind of data, other provenance: a new snapshot
    text = rs.reconcile(["listings"])
    assert "native_sources:" in text and "eastmoney" in text
