"""Optional limit pools on mock transports: own dataset and lineage, off by default, block / HTML / holiday / transport failure, and the
read-only comparison against the seal facts of a native day."""
from __future__ import annotations

from datetime import datetime, time

import httpx
import polars as pl
import pytest

from pulse.adapters.sources.http import RateLimitedClient
from pulse.adapters.sources.legacy_recordings import LegacyRecordings
from pulse.application import bf_live_service as B
from pulse.application import intraday_pools as P
from pulse.application.intraday_recorder import IntradayRecorder

from . import support as T

pytestmark = pytest.mark.pg


def at(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 11, 12, h, m, s, tzinfo=T.CST).astimezone(T.UTC)


def make(conn, lake, legacy_root, world, *, pools=True, **kw):
    svc = B.LiveService(conn, lake, LegacyRecordings(legacy_root), clock=world.now)
    svc.bootstrap()

    def client():
        return RateLimitedClient(min_interval_seconds=1.0, transport=httpx.MockTransport(world.handler), clock=world.monotonic, sleep=world.sleep,
                                 now=world.now)

    recorder = P.LimitPoolRecorder(svc, client(), sleep=world.sleep) if pools else None
    return svc, IntradayRecorder(svc, client(), sleep=world.sleep, limit_pools=recorder, **kw)


def pools_of(svc, lake):
    man = svc.latest_ready(B.DATASET_POOLS, T.DAY)
    return man, (None if man is None else lake.read_snapshot_frames(man["content_hash"]))


def test_pools_go_into_their_own_dataset_with_native_lineage(conn, lake, legacy_root):
    world = T.World(at(15, 24, 30), [T.plain()])
    svc, rec = make(conn, lake, legacy_root, world)
    s = rec.run(["600000"], T.DAY, time(15, 26))
    man, df = pools_of(svc, lake)
    assert df.schema == pl.Schema(P.POOL_SCHEMA) and man["row_count"] == df.height == 125 + 28 + 7
    assert man["coverage_document"]["recorder"] == "pulse_intraday_recorder" and man["coverage_document"]["comparison_only"]
    assert {p["pool"] for p in man["coverage_document"]["parts"].values()} == {"zt", "zb", "dt"} and s.counts["pool_parts_published"] == 3
    # point in time: every pool carries the response time of its own request, one request per second apart
    fetched = {r["pool"]: r["fetched_at"] for r in df.select("pool", "fetched_at").unique().iter_rows(named=True)}
    assert at(15, 25) <= fetched["zt"] < fetched["zb"] < fetched["dt"] and (fetched["zb"] - fetched["zt"]).total_seconds() >= 1.0 - 1e-9
    assert world.count(T.POOL_HOST) == 3 and min(world.gaps(T.POOL_HOST)) >= 1.0 - 1e-9 and (df["session_date"] == T.DAY).all()
    # lineage: the producer is the native source, the raw bodies are the vendor's; the quote dataset is untouched
    batches = conn.execute("SELECT source_id, endpoint_id FROM ingest.batch").fetchall()
    assert {(b["source_id"], b["endpoint_id"]) for b in batches} == {("pulse_intraday_recorder", "limit_pools")}
    assert conn.execute("SELECT count(*) n FROM ingest.raw_object WHERE source_id='eastmoney_limit_pools'").fetchone()["n"] == 3
    assert svc.latest_ready(B.DATASET_QUOTES, T.DAY) is None


def test_off_by_default(conn, lake, legacy_root):
    world = T.World(at(15, 24, 30), [T.plain()])
    svc, rec = make(conn, lake, legacy_root, world, pools=False)
    rec.run(["600000"], T.DAY, time(15, 26))
    assert world.count(T.POOL_HOST) == 0 and svc.latest_ready(B.DATASET_POOLS, T.DAY) is None


def test_a_block_stops_the_pool_host_without_retry_and_not_the_quotes(conn, lake, legacy_root):
    world = T.World(at(15, 24, 30), [T.plain()])
    world.block[T.POOL_HOST] = 2                              # the limit-up pool answers, the next request is refused
    svc, rec = make(conn, lake, legacy_root, world, static={"600000"})
    s = rec.run(["600000"], T.DAY, time(15, 27))
    assert world.count(T.POOL_HOST) == 2 and any("stopped for the session" in n for n in s.notes)
    man, df = pools_of(svc, lake)
    assert set(df["pool"].unique()) == {"zt"}
    quotes = svc.lake.read_snapshot_frames(svc.latest_ready(B.DATASET_QUOTES, T.DAY)["content_hash"])
    assert "after_hot" in set(quotes["kind"].unique())        # the quote recording went on
    h = conn.execute("SELECT runtime_state FROM ingest.source_health WHERE source_id='eastmoney_limit_pools'").fetchone()
    assert h["runtime_state"] == "unavailable"


def test_a_html_page_is_a_refusal(conn, lake, legacy_root):
    world = T.World(at(15, 24, 30), [T.plain()])
    world.pool_mode = "html"
    svc, rec = make(conn, lake, legacy_root, world)
    s = rec.run(["600000"], T.DAY, time(15, 27))
    assert world.count(T.POOL_HOST) == 1 and any("not JSON" in n for n in s.notes) and svc.latest_ready(B.DATASET_POOLS, T.DAY) is None


def test_a_pool_of_another_day_is_not_stored(conn, lake, legacy_root):
    world = T.World(at(15, 24, 30), [T.plain()])
    world.pool_mode = "stale"                                 # a holiday: the vendor answers the last session (qdate 20261102)
    svc, rec = make(conn, lake, legacy_root, world)
    s = rec.run(["600000"], T.DAY, time(15, 27))
    assert sum("not stored" in n for n in s.notes) == 3 and svc.latest_ready(B.DATASET_POOLS, T.DAY) is None


def test_one_retry_after_a_transport_failure_never_after_a_block(conn, lake, legacy_root):
    world = T.World(at(15, 24, 30), [T.plain()])
    world.pool_fail_first = 1
    svc, rec = make(conn, lake, legacy_root, world)
    s = rec.run(["600000"], T.DAY, time(15, 27))
    assert world.count(T.POOL_HOST) == 4 and s.counts["pool_transport_errors"] == 1 and s.counts["pool_parts_published"] == 3
    world2 = T.World(at(15, 24, 30), [T.plain()])
    world2.pool_fail_first = 2                                # the limit-up pool fails twice: not stored, the other two are
    svc2, rec2 = make(conn, lake, legacy_root, world2)
    s2 = rec2.run(["600000"], T.DAY, time(15, 27))
    assert s2.counts["pool_parts_published"] == 2 and any("transport failure twice" in n for n in s2.notes)


def test_comparison_against_the_seal_facts_of_a_native_day(conn, lake, legacy_root):
    sealed = [T.sealed("000525", "红太阳"), T.sealed("001230", "测试股")]               # both are in the vendor's limit-up pool fixture
    world = T.World(at(14, 55), sealed)
    svc, rec = make(conn, lake, legacy_root, world, pools=False)
    rec.run(["000525", "001230"], T.DAY, time(14, 58))
    world2 = T.World(at(15, 25), sealed)
    svc2, rec2 = make(conn, lake, legacy_root, world2)
    rec2.run(["000525", "001230"], T.DAY, time(15, 26))
    svc2.publish_seal_facts(T.DAY)
    r = P.compare_pools(svc2, T.DAY)
    s = r["sealed_at_close"]
    assert (s["vendor_pool"], s["both"], s["only_seal_facts"]) == (125, 2, []) and s["seal_facts"] == 2 and len(s["only_vendor"]) == 123
    assert r["first_seal_gap_seconds"]["n"] == 2 and r["breaks"]["vendor_total_zbc"] > 0 and r["limit_down_pool_names"] == 7
    assert r["seal_facts_basis"] and "sealed at close: vendor pool 125 | seal facts 2 | both 2" in P.render(r)


def test_comparison_reports_a_missing_dataset_instead_of_guessing(conn, lake, legacy_root):
    svc = B.LiveService(conn, lake, LegacyRecordings(legacy_root))
    svc.bootstrap()
    r = P.compare_pools(svc, T.DAY)
    assert len(r["notes"]) == 2 and "sealed_at_close" not in r
