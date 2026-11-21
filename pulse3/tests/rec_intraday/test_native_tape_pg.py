"""Tape and actionability from the native recording (no request): two native parts, a watchlist name outside the top N, a stale recording.

The recorder runs on a mock transport (four 'minutes' of a synthetic day, 61 main-board names); the snapshot / tape / actionability code then reads only
the published dataset. Anything that would touch the network fails the test.
"""
from __future__ import annotations

import argparse
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest import mock

import httpx
import psycopg
import pytest

from integration import world as W
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db, migrate
from pulse.adapters.sources.http import RateLimitedClient
from pulse.adapters.sources.legacy_recordings import LegacyRecordings
from pulse.application import account_service as A
from pulse.application import bindings as B
from pulse.application import bf_live_service as BL
from pulse.application import market_today_service as MT
from pulse.application import native_quotes as NQ
from pulse.application.data_service import DataService
from pulse.application.intraday_recorder import IntradayRecorder
from pulse.application.release_service import BuildConfig, build_release
from pulse.domain.market_state import daily
from pulse.workers import analysis

from . import support as T

pytestmark = pytest.mark.pg
ADMIN = os.environ.get("PULSE_TEST_ADMIN_DSN", "postgresql://postgres@127.0.0.1:55432/postgres")
DAY = T.DAY                                                    # 2026-11-12: the bars of the synthetic world end on 2026-11-10
SEAL, SURGE, PLUNGE = "000001", "600000", "002415"             # the paths of the ext_a tape test
TOP_N = 5


def at(minute: int, second: int = 0) -> datetime:
    return datetime(2026, 11, 12, 10, minute, second, tzinfo=T.CST).astimezone(T.UTC)


@contextmanager
def no_http():
    def boom(*a, **k):
        raise AssertionError("an HTTP request was made")

    with mock.patch.object(RateLimitedClient, "request", boom), mock.patch.object(httpx.Client, "send", boom):
        yield


class Tick:
    """A settable clock for the snapshot / tape / actionability side."""

    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture(scope="module")
def nt(tmp_path_factory):
    try:
        with psycopg.connect(ADMIN, connect_timeout=3):
            pass
    except Exception:
        pytest.skip("PostgreSQL not reachable")
    name = f"pulse_ntape_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f'create database "{name}"')
    dsn = psycopg.conninfo.make_conninfo(ADMIN, dbname=name)
    migrate.apply(dsn)
    tmp = tmp_path_factory.mktemp("ntape")
    world = W.build_world(dsn, tmp)
    conn = db.connect(dsn, autocommit=True)
    lake = LakeStore(world["lake_root"])
    A.open_paper_account(conn, trading_day=DAY)
    res = build_release(conn, lake, cfg=BuildConfig(top_n_plans=8, workspace_page=3, legacy_warehouse=str(world["warehouse"])),
                        account_id=A.PAPER_ACCOUNT_ID, clock=W.clock())
    ranking = conn.execute("select ranking_id from decision.ranking_run where release_id=%s and ranking_scope='market' and horizon_id='20td'",
                           (res.release_id,)).fetchone()["ranking_id"]
    top = [r["instrument_id"] for r in conn.execute(
        "select instrument_id from decision.ranking_entry where ranking_id=%s and plan_id is not null order by alpha_rank limit %s", (ranking, TOP_N))]
    prev = {r["code"]: r["c"] for r in conn.execute(
        "select distinct on (b.instrument_id) a.source_symbol code, b.close_price_raw c from market.bar_projection b join core.instrument_alias a"
        " on a.instrument_id=b.instrument_id and a.source_id='legacy_warehouse' where b.timeframe='1d' and b.trade_date=%s"
        " order by b.instrument_id, b.available_at desc", (W.LAST,))}
    top_codes = set(r["source_symbol"] for r in conn.execute("select source_symbol from core.instrument_alias where instrument_id = any(%s)", (top,)))
    watch = next(c for c in W.CODES if c not in top_codes and c not in (SEAL, SURGE, PLUNGE) and c in prev)
    yield {"dsn": dsn, "conn": conn, "lake": lake, "tmp": tmp, "prev": prev, "top": top, "top_codes": top_codes, "watch": watch, "world": world}
    conn.close()
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f'drop database if exists "{name}" with (force)')


def stocks_for(nt, step: int) -> list[T.Stock]:
    """The 61 names: flat at yesterday's close except the four paths (000001 seal / break / reseal, 600000 and the watchlist name surge, 002415 plunge)."""
    fmt = lambda x: float(Decimal(x).quantize(Decimal("0.01")))                                 # noqa: E731
    out = []
    for code, pc in nt["prev"].items():
        up, down = daily.limit_prices(pc, Decimal("0.10"))
        price, ask = pc, pc + Decimal("0.01")
        if code == SEAL:
            price, ask = [(up - Decimal("0.2"), up - Decimal("0.1")), (up, None), (up - Decimal("0.1"), up), (up, None)][step]
        elif code in (SURGE, nt["watch"]):
            price, ask = pc * [Decimal("1.03"), Decimal("1.055"), Decimal("1.056"), Decimal("1.056")][step], Decimal("99")
        elif code == PLUNGE:
            price, ask = pc * [Decimal("0.97"), Decimal("0.945"), Decimal("0.944"), Decimal("0.944")][step], Decimal("99")
        bid = (fmt(price), 5000) if ask is None else (fmt(price - Decimal("0.01")), 100)
        out.append(T.Stock(code, f"股{code}", fmt(pc), fmt(price), bids=[bid, (0, 0), (0, 0), (0, 0), (0, 0)],
                           asks=[(0.0, 0)] * 5 if ask is None else [(fmt(ask), 100), (0, 0), (0, 0), (0, 0), (0, 0)],
                           limit_up=fmt(up), limit_down=fmt(down), high=fmt(max(price, pc)), low=fmt(min(price, pc))))
    return out


def record_step(nt, step: int) -> T.World:
    world = T.World(at(step), stocks_for(nt, step))
    svc = BL.LiveService(nt["conn"], nt["lake"], LegacyRecordings(nt["tmp"] / "legacy"), clock=world.now)
    svc.bootstrap()
    client = RateLimitedClient(min_interval_seconds=1.0, transport=httpx.MockTransport(world.handler), clock=world.monotonic, sleep=world.sleep, now=world.now)
    IntradayRecorder(svc, client, sleep=world.sleep).run(sorted(nt["prev"]), DAY, time(10, step, 8))
    return world


@pytest.fixture(scope="module")
def played(nt):
    """Four native parts, one quote snapshot cut from each, then the tape over the pairs."""
    conn, lake = nt["conn"], nt["lake"]
    snaps, clock = [], Tick(at(0))
    for step in range(4):
        world = record_step(nt, step)
        clock.t = world.t
        with no_http():
            snaps.append(NQ.publish_native_quote_snapshot(DataService(conn, lake, clock=clock)))
    with no_http():
        result = MT.derive_tape(DataService(conn, lake, clock=clock))
    return {"snaps": snaps, "tape": result, "clock": clock, "last": clock.t}


def events(conn) -> list[tuple[str, str]]:
    return [(r["payload"]["code"], r["payload"]["event_kind"]) for r in conn.execute(
        "select payload from ops.outbox where aggregate_type='market_tape' order by aggregate_version, event_ordinal").fetchall()]


def test_tape_over_native_parts_gives_every_event_kind(nt, played):
    conn = nt["conn"]
    assert played["tape"]["pairs"] == 3 and played["tape"]["events"] >= 6
    kinds = events(conn)
    assert (SEAL, "seal") in kinds and (SEAL, "break") in kinds and (SEAL, "reseal") in kinds
    assert kinds.index((SEAL, "seal")) < kinds.index((SEAL, "break")) < kinds.index((SEAL, "reseal"))
    assert (SURGE, "surge") in kinds and (PLUNGE, "plunge") in kinds


def test_a_watchlist_name_outside_the_top_n_gets_events(nt, played):
    watch = nt["watch"]
    assert watch not in nt["top_codes"] and len(nt["top"]) == TOP_N
    assert (watch, "surge") in events(nt["conn"])                          # the old fetch (top N + holdings) never quoted this name


def test_every_snapshot_is_the_whole_board_with_native_lineage(nt, played):
    conn = nt["conn"]
    for snap in played["snaps"]:
        assert snap.manifest["manifest_state"] == "READY" and len(snap.views) == len(nt["prev"]) and not snap.missing
        n = conn.execute("select count(*) n from market.quote_projection where quote_snapshot_id=%s", (snap.manifest["snapshot_id"],)).fetchone()["n"]
        assert n == len(nt["prev"])
    row = conn.execute("select q.source_id, q.endpoint_id, q.available_at, q.first_seen_at, o.available_at oa, o.event_time, o.contract_id, r.source_id rs,"
                       " r.storage_uri from market.quote_projection q join ingest.observation o using (observation_id)"
                       " join ingest.raw_object r on r.raw_object_id=o.raw_object_id where q.quote_snapshot_id=%s limit 1",
                       (played["snaps"][-1].manifest["snapshot_id"],)).fetchone()
    assert (row["source_id"], row["endpoint_id"], row["rs"], row["contract_id"]) == ("pulse_intraday_recorder", "intraday_quotes", "pulse_intraday_recorder", "CanonicalQuote")
    assert row["oa"] == row["available_at"] and row["first_seen_at"] <= row["available_at"]            # availability = the row's poll_at
    assert row["event_time"] <= row["oa"] and row["storage_uri"]
    cov = played["snaps"][-1].manifest["coverage_document"]
    assert cov["recording"]["http_requests"] == 0 and cov["recording"]["dataset"] == "quote_snapshots_60s" and cov["source"] == "pulse_intraday_recorder"


def test_a_second_snapshot_over_the_same_recording_adds_no_observation(nt, played):
    conn = nt["conn"]
    before = conn.execute("select count(*) n from ingest.observation").fetchone()["n"]
    clock = Tick(played["last"])
    with no_http():
        NQ.publish_native_quote_snapshot(DataService(conn, nt["lake"], clock=clock))
    assert conn.execute("select count(*) n from ingest.observation").fetchone()["n"] == before


def args(**kw) -> argparse.Namespace:
    return argparse.Namespace(slot=B.SLOT_KEY, horizon="20td", limit=TOP_N, scope="board", **kw)


def test_actionability_reads_the_recording_and_makes_no_request(nt, played, capsys):
    conn = nt["conn"]
    client = RateLimitedClient(min_interval_seconds=0)
    with no_http():
        rc = analysis._actionability(args(), conn, nt["lake"], client, clock=Tick(played["last"]))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["http_requests"] == 0 and client.requests_made == 0 and out["quote_missing"] == [] and out["entries"] >= TOP_N
    n = conn.execute("select count(*) n from market.quote_projection where quote_snapshot_id=%s", (out["quote_snapshot_id"],)).fetchone()["n"]
    assert n == len(nt["prev"])                                            # the whole board, not the top N


def test_focus_scope_is_the_top_n_holdings_and_watchlists_only(nt, played, capsys):
    conn = nt["conn"]
    wid = uuid.uuid4()
    conn.execute("insert into ui.watchlist(watchlist_id, principal_id, watchlist_version, display_name, description, created_at, updated_at)"
                 " values (%s,%s,1,'w',null,now(),now())", (wid, uuid.UUID(int=7)))
    iid = conn.execute("select instrument_id from core.instrument_alias where source_id='legacy_warehouse' and source_symbol=%s",
                       (nt["watch"],)).fetchone()["instrument_id"]
    conn.execute("insert into ui.watchlist_member(watchlist_id, instrument_id, sort_position, added_at) values (%s,%s,1,now())", (wid, iid))
    ns = args()
    ns.scope = "focus"
    with no_http():
        rc = analysis._actionability(ns, conn, nt["lake"], RateLimitedClient(min_interval_seconds=0), clock=Tick(played["last"]))
    out = json.loads(capsys.readouterr().out)
    n = conn.execute("select count(*) n from market.quote_projection where quote_snapshot_id=%s", (out["quote_snapshot_id"],)).fetchone()["n"]
    assert rc == 0 and n == TOP_N + 1                                      # top N plus the watchlist name


def test_a_stale_recording_publishes_nothing_and_makes_no_request(nt, played, capsys):
    conn = nt["conn"]
    snaps = conn.execute("select count(*) n from market.dataset_manifest where dataset_id='quotes_l1'").fetchone()["n"]
    obs = conn.execute("select count(*) n from ingest.observation").fetchone()["n"]
    late = Tick(played["last"] + timedelta(minutes=10))
    client = RateLimitedClient(min_interval_seconds=0)
    with no_http():
        rc = analysis._actionability(args(), conn, nt["lake"], client, clock=late)
        with pytest.raises(NQ.NoFreshRecording, match="no fresh recording"):
            NQ.publish_native_quote_snapshot(DataService(conn, nt["lake"], clock=late))
    err = capsys.readouterr().err
    assert rc == 5 and "no fresh recording" in err and "nothing published" in err and client.requests_made == 0
    assert conn.execute("select count(*) n from market.dataset_manifest where dataset_id='quotes_l1'").fetchone()["n"] == snaps
    assert conn.execute("select count(*) n from ingest.observation").fetchone()["n"] == obs


def test_a_day_without_a_recording_publishes_nothing(nt, played):
    next_day = Tick(played["last"] + timedelta(days=1))
    with no_http(), pytest.raises(NQ.NoFreshRecording, match="no recording of 2026-11-13"):
        NQ.publish_native_quote_snapshot(DataService(nt["conn"], nt["lake"], clock=next_day))
