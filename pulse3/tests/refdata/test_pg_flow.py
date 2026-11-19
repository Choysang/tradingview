"""Native reference-data producers end to end on a private PostgreSQL + lake, fake HTTP world: no network, no legacy dataset touched."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import polars as pl
import pytest

from pulse.adapters.sources import ref_listing as L
from pulse.adapters.sources import ref_market as M
from pulse.adapters.sources.http import SourceBlocked
from pulse.adapters.sources.tencent import TencentAdapter
from pulse.application import bindings as B
from pulse.application import refdata_core as C
from pulse.application.data_service import DataService
from pulse.application.refdata_service import RefDataError, RefDataService, instrument_id_of
from pulse.domain.instruments import identity as ident

from .support import BARS_FROM, START, World, factor_body, fixture

pytestmark = pytest.mark.pg
TODAY = date(2026, 11, 19)
UTC = timezone.utc


def build(conn, lake, world: World) -> tuple[DataService, RefDataService]:
    svc = DataService(conn, lake, clock=world.now)
    svc.bootstrap()
    return svc, RefDataService(svc)


def manifests(conn, dataset: str) -> list[dict]:
    return conn.execute("SELECT * FROM market.dataset_manifest WHERE dataset_id=%s AND manifest_state='READY' ORDER BY manifest_version", (dataset,)).fetchall()


def cursor_of(conn, partition: str) -> dict | None:
    row = conn.execute("SELECT committed_cursor FROM ingest.source_cursor WHERE partition_key=%s", (partition,)).fetchone()
    return row["committed_cursor"] if row else None


# ---- adjusted bars -------------------------------------------------------------------------------------------------------
def test_full_run_publishes_the_legacy_schema_under_the_native_dataset_and_lineage(conn, lake):
    world = World()
    svc, rs = build(conn, lake, world)
    client = world.client()
    out = rs.adjusted_bars(M.SinaRefAdapter(client), ["000001", "000002", "600000"], start=BARS_FROM, today=TODAY, chunk=2)
    assert client.requests_made == 6                                        # 2 requests per code, nothing else
    assert world.mono >= client.requests_made - 1                           # the rate limiter slept >= 1 s between the requests
    frame = rs.read_frame(C.ADJ_DATASET)
    assert dict(frame.schema) == C.ADJ_SCHEMA                               # same columns and types as bars_legacy_fwd_adjusted_1d
    assert frame.height == 400 + 30 + 30 and set(frame["revision"].to_list()) == {1}
    assert set(frame["adjustment_version"].to_list()) == {"sina-ratio-qfq-v1"} and set(frame["adjustment_mode"].to_list()) == {"forward_adjusted"}
    assert frame["is_final"].all() and frame["turnover_amount_cny"].null_count() == frame.height          # NULL, never 0
    assert START <= frame["available_at"].min() and frame["available_at"].max() <= START + timedelta(seconds=10)    # each code carries its own fetch time
    row = frame.filter((pl.col("instrument_id") == instrument_id_of("000001")) & (pl.col("trade_date") == date(2025, 9, 10))).row(0, named=True)
    assert abs(float(row["close_price"]) - 10.922984695675343) < 1e-11 and row["volume_shares"] == 79266839   # the legacy warehouse value, 12 decimals
    assert out.counts["codes_fetched"] == 3 and out.counts["bars_new_keys"] == frame.height and out.counts["bars_fetched"] == frame.height
    # lineage: the native source and endpoints, never the legacy warehouse; two chunks = two cumulative snapshots
    ms = manifests(conn, C.ADJ_DATASET)
    assert [m["manifest_version"] for m in ms] == [1, 2] and ms[-1]["row_count"] == frame.height and ms[0]["row_count"] == 400 + 30
    batch = conn.execute("SELECT source_id, endpoint_id FROM ingest.batch WHERE batch_id=%s", (ms[-1]["batch_id"],)).fetchone()
    assert (batch["source_id"], batch["endpoint_id"]) == ("sina_ref", "adjusted_bars")
    raw = {(r["source_id"], r["endpoint_id"]) for r in conn.execute("SELECT DISTINCT source_id, endpoint_id FROM ingest.raw_object")}
    assert raw == {("sina_ref", "adj_kline"), ("sina_ref", "adj_factor")}
    assert conn.execute("SELECT count(*) AS n FROM market.dataset_manifest WHERE dataset_id LIKE '%%legacy%%'").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM ingest.observation").fetchone()["n"] == frame.height
    assert cursor_of(conn, "adjusted:universe")["state"] == "complete" and cursor_of(conn, "adjusted:universe")["last_code"] == "600000"
    assert ms[-1]["coverage_document"]["schema"] == "identical to bars_legacy_fwd_adjusted_1d"


def test_a_new_ex_right_event_is_a_new_revision_and_the_old_value_stays_visible_at_the_old_cutoff(conn, lake):
    world = World()
    svc, rs = build(conn, lake, world)
    rs.adjusted_bars(M.SinaRefAdapter(world.client()), ["000001", "000002"], start=BARS_FROM, today=TODAY, chunk=10)
    before = rs.read_frame(C.ADJ_DATASET)
    cutoff = world.now()
    # an identical re-fetch publishes nothing
    world.advance(hours=1)
    again = rs.adjusted_bars(M.SinaRefAdapter(world.client()), ["000001"], start=BARS_FROM, today=TODAY, scope=None)
    assert again.counts["bars_unchanged"] == 400 and len(manifests(conn, C.ADJ_DATASET)) == 1
    # a new ex-right event: every older factor is multiplied by 1.05 and the latest one opens a new segment
    old = M.parse_factors(fixture("sina_adj_factor_sz000001.js"))
    world.factor_override["sz000001"] = factor_body("sz000001", [(d.isoformat(), f"{f * 1.05:.16f}") for d, f in old] + [("2026-11-20", "1.0000000000000000")])
    world.advance(days=1)
    out = rs.adjusted_bars(M.SinaRefAdapter(world.client()), ["000001"], start=BARS_FROM, today=date(2026, 11, 20), scope=None)
    assert out.counts["bars_new_revisions"] == 400 and out.counts["codes_with_revised_history"] == 1
    after = rs.read_frame(C.ADJ_DATASET)
    iid = instrument_id_of("000001")
    mine = after.filter(pl.col("instrument_id") == iid)
    assert sorted(set(mine["revision"].to_list())) == [1, 2] and after.height == before.height + 400
    assert after.filter(pl.col("instrument_id") == instrument_id_of("000002"))["revision"].max() == 1          # other codes untouched
    day = date(2025, 9, 10)
    r1 = mine.filter((pl.col("trade_date") == day) & (pl.col("revision") == 1)).row(0, named=True)
    r2 = mine.filter((pl.col("trade_date") == day) & (pl.col("revision") == 2)).row(0, named=True)
    assert float(r2["close_price"]) == pytest.approx(float(r1["close_price"]) / 1.05, rel=1e-9) and r2["available_at"] > r1["available_at"]
    # point in time: at the old cutoff only revision 1 is visible and it is the old value; nothing was overwritten
    seen = after.filter(pl.col("available_at") <= cutoff).sort("revision").unique(C.BAR_KEY, keep="last")
    assert seen.filter((pl.col("instrument_id") == iid) & (pl.col("trade_date") == day))["close_price"].item() == r1["close_price"]
    assert [m["manifest_version"] for m in manifests(conn, C.ADJ_DATASET)] == [1, 2]
    n_obs = conn.execute("SELECT count(*) AS n, max(observation_revision) AS r FROM ingest.observation").fetchone()
    assert (n_obs["n"], n_obs["r"]) == (before.height + 400, 2)


def test_block_stops_at_once_publishes_the_finished_codes_and_resumes_without_refetching(conn, lake):
    world = World()
    svc, rs = build(conn, lake, world)
    codes = ["000001", "000002", "000003", "000004", "000005"]
    world.block_after = 5                                                   # code 1, 2 complete (4 requests), code 3's kline ok, its factor file is refused
    client = world.client()
    with pytest.raises(SourceBlocked) as err:
        rs.adjusted_bars(M.SinaRefAdapter(client), codes, start=BARS_FROM, today=TODAY, chunk=2)
    assert len(world.requests) == 6 and client.circuit_open               # the 403 was the last request; nothing was retried
    assert err.value.summary.counts["codes_fetched"] == 2                  # type: ignore[attr-defined]
    frame = rs.read_frame(C.ADJ_DATASET)
    assert {r for r in frame["instrument_id"].unique().to_list()} == {instrument_id_of("000001"), instrument_id_of("000002")}
    cur = cursor_of(conn, "adjusted:universe")
    assert cur["state"] == "running" and cur["last_code"] == "000002" and cur["failed"] == []
    # a rerun inside the cooldown does not send a single request
    n = len(world.requests)
    with pytest.raises(SourceBlocked, match="circuit_open"):
        rs.adjusted_bars(M.SinaRefAdapter(world.client()), codes, start=BARS_FROM, today=TODAY, chunk=2)
    assert len(world.requests) == n
    # after the cooldown the run continues with code 3: 3 codes x 2 requests, the finished codes are not fetched again
    world.advance(hours=2)
    world.block_after = None
    out = rs.adjusted_bars(M.SinaRefAdapter(world.client()), codes, start=BARS_FROM, today=TODAY, chunk=2)
    assert len(world.requests) == n + 6 and "resumed after 000002" in out.notes[0]
    assert {i for i in rs.read_frame(C.ADJ_DATASET)["instrument_id"].unique().to_list()} == {instrument_id_of(c) for c in codes}
    assert cursor_of(conn, "adjusted:universe")["state"] == "complete"


def test_a_failed_code_does_not_move_the_cursor_past_it_and_is_retried_first(conn, lake):
    world = World()
    svc, rs = build(conn, lake, world)
    codes = ["000001", "000002", "000003"]
    world.fail_symbols = {"sz000002"}
    out = rs.adjusted_bars(M.SinaRefAdapter(world.client()), codes, start=BARS_FROM, today=TODAY, chunk=10, max_codes=3)
    assert out.counts["codes_failed"] == 1 and out.counts["codes_fetched"] == 2
    cur = cursor_of(conn, "adjusted:universe")
    assert cur["failed"] == ["000002"] and cur["state"] == "complete"
    world.fail_symbols = set()
    world.advance(hours=1)
    n = len(world.requests)
    rs.adjusted_bars(M.SinaRefAdapter(world.client()), codes[:1] + ["000002"], start=BARS_FROM, today=TODAY, chunk=10, restart=False)
    # the previous pass completed, so this is a new pass over its codes: both codes are fetched, the failed one is no longer failed
    assert cursor_of(conn, "adjusted:universe")["failed"] == [] and len(world.requests) == n + 4
    assert {i for i in rs.read_frame(C.ADJ_DATASET)["instrument_id"].unique().to_list()} >= {instrument_id_of("000002")}


def test_max_codes_splits_a_universe_over_runs(conn, lake):
    world = World()
    svc, rs = build(conn, lake, world)
    codes = ["000001", "000002", "000003", "000004"]
    rs.adjusted_bars(M.SinaRefAdapter(world.client()), codes, start=BARS_FROM, today=TODAY, chunk=10, max_codes=3)
    assert cursor_of(conn, "adjusted:universe") == {"state": "running", "last_code": "000003", "failed": [], "started_at": cursor_of(conn, "adjusted:universe")["started_at"]}
    n = len(world.requests)
    rs.adjusted_bars(M.SinaRefAdapter(world.client()), codes, start=BARS_FROM, today=TODAY, chunk=10, max_codes=3)
    assert len(world.requests) == n + 2 and cursor_of(conn, "adjusted:universe")["state"] == "complete"


# ---- daily refresh from the closing quote ----------------------------------------------------------------------------------
def test_refresh_appends_unchanged_codes_from_the_quote_and_refetches_an_ex_right_code(conn, lake):
    world = World()
    svc, rs = build(conn, lake, world)
    rs.adjusted_bars(M.SinaRefAdapter(world.client()), ["000001", "000002"], start=BARS_FROM, today=TODAY, chunk=10)
    stored2 = float(rs.read_frame(C.ADJ_DATASET).filter((pl.col("instrument_id") == instrument_id_of("000002")) & (pl.col("trade_date") == date(2026, 11, 18)))["close_price"].item())
    world.advance(hours=1)
    world.quotes = {"000001": {"last": 12.5, "prev": 12.2, "open": 12.3, "lots": 100000, "time": "20261119150000", "high": 12.6, "low": 12.2},
                    "000002": {"last": 11.0, "prev": round(stored2 / 1.1, 2), "open": 11.0, "lots": 5000, "time": "20261119150000", "high": 11.1, "low": 10.9}}
    world.factor_override["sz000002"] = factor_body("sz000002", [("1990-01-01", "1.1"), ("2026-11-19", "1.0")])    # the ex-right day is in the factor file
    n = len(world.requests)
    out = rs.adjusted_refresh(M.SinaRefAdapter(world.client()), TencentAdapter(world.client()), ["000001", "000002", "000003"], start=BARS_FROM, today=TODAY)
    assert out.counts["to_append"] == 1 and out.counts["refetch_readjusted"] == 1 and out.counts["refetch_new"] == 1
    assert len(world.requests) - n == 1 + 2 + 2                           # one quote request, two kline+factor pairs (000002 readjusted, 000003 new)
    frame = rs.read_frame(C.ADJ_DATASET)
    one = frame.filter((pl.col("instrument_id") == instrument_id_of("000001")) & (pl.col("trade_date") == TODAY)).row(0, named=True)
    assert (float(one["close_price"]), one["volume_shares"], one["revision"]) == (12.5, 10_000_000, 1)
    two = frame.filter(pl.col("instrument_id") == instrument_id_of("000002"))
    assert two["revision"].max() == 2 and two.filter(pl.col("revision") == 2).height == 30
    assert instrument_id_of("000003") in frame["instrument_id"].to_list()
    obs = conn.execute("SELECT r.source_id, r.endpoint_id FROM ingest.observation o JOIN ingest.raw_object r USING (raw_object_id) WHERE o.entity_key=%s AND o.event_time > %s",
                       (instrument_id_of("000001"), datetime(2026, 11, 18, 12, tzinfo=UTC))).fetchall()
    assert [(r["source_id"], r["endpoint_id"]) for r in obs] == [("tencent", "qt_quote")]       # provenance of an appended bar is the quote capture


def test_refresh_needs_an_existing_dataset(conn, lake):
    world = World()
    svc, rs = build(conn, lake, world)
    with pytest.raises(RefDataError, match="--mode full"):
        rs.adjusted_refresh(M.SinaRefAdapter(world.client()), TencentAdapter(world.client()), ["000001"], start=BARS_FROM, today=TODAY)


# ---- index bars -----------------------------------------------------------------------------------------------------------
def test_index_bars_six_requests_legacy_schema_and_appended_versions(conn, lake):
    world = World()
    svc, rs = build(conn, lake, world)
    out = rs.index_bars(M.SinaRefAdapter(world.client()), M.TencentRefAdapter(world.client()), start=date(2026, 1, 1), today=TODAY)
    assert len(world.requests) == 6 and out.counts["indices_fetched"] == 6
    frame = rs.read_frame(C.INDEX_DATASET)
    assert dict(frame.schema) == C.INDEX_SCHEMA and sorted(frame["symbol"].unique().to_list()) == sorted(M.INDEX_TICKERS)
    tc = frame.filter((pl.col("symbol") == "000985") & (pl.col("date") == date(2026, 10, 8))).row(0, named=True)
    assert tc["close"] == 5560.01 and tc["volume"] == 974518436.0 * 100 and START <= tc["ingested_at"] <= START + timedelta(seconds=10)
    assert frame.filter(pl.col("symbol") == "000985")["date"].max() == date(2026, 11, 19)              # the session is complete at 16:30 CST
    sh = frame.filter((pl.col("symbol") == "000001") & (pl.col("date") == date(2026, 11, 18))).row(0, named=True)
    assert sh["instrument_id"] == str(ident.index_uuid(ident.XSHG, "000001")) and sh["price_basis"] == "index_points_unadjusted"
    # unchanged re-fetch: nothing new; a changed value: one appended version per index series, the old row stays
    world.advance(hours=1)
    assert rs.index_bars(M.SinaRefAdapter(world.client()), M.TencentRefAdapter(world.client()), start=date(2026, 1, 1), today=TODAY).counts["index_new_versions"] == 0
    world.index_close_bump = 1.5
    world.advance(hours=1)
    rs.index_bars(M.SinaRefAdapter(world.client()), M.TencentRefAdapter(world.client()), start=date(2026, 1, 1), today=TODAY)
    after = rs.read_frame(C.INDEX_DATASET)
    assert after.height == frame.height + 5
    two = after.filter((pl.col("symbol") == "000001") & (pl.col("date") == date(2026, 11, 18))).sort("ingested_at")
    assert two["close"].to_list() == [3587.832, 3589.332] and two["ingested_at"][1] > two["ingested_at"][0]


def test_an_unfinished_session_is_not_stored(conn, lake):
    world = World(datetime(2026, 11, 19, 2, 30, tzinfo=UTC))                # 10:30 CST: today's bar is not finished
    svc, rs = build(conn, lake, world)
    rs.index_bars(M.SinaRefAdapter(world.client()), M.TencentRefAdapter(world.client()), start=date(2026, 1, 1), today=TODAY)
    frame = rs.read_frame(C.INDEX_DATASET)
    assert frame["date"].max() == date(2026, 11, 18)


# ---- boards ---------------------------------------------------------------------------------------------------------------
def test_boards_publish_industry_and_concept_snapshots_and_an_identical_day_publishes_nothing(conn, lake):
    world = World()
    svc, rs = build(conn, lake, world)
    out = rs.boards(M.SinaRefAdapter(world.client()), chunk_boards=100)
    assert len(world.requests) == 1 + 261 and out.counts["published"] == 2
    ind, con = rs.read_frame(C.MEMBERSHIP_DATASETS["industry"]), rs.read_frame(C.MEMBERSHIP_DATASETS["concept"])
    assert dict(ind.schema) == C.MEMBERSHIP_SCHEMA and ind["node_id"].n_unique() <= 48 and con["node_id"].n_unique() <= 213
    assert ind["node_id"].str.starts_with("new_").all() and con["node_id"].str.starts_with("gn_").all()
    assert len(set(ind["available_at"].to_list())) == 1 and START <= ind["available_at"][0] <= START + timedelta(seconds=300) and set(ind["effective_from"].to_list()) == {datetime(2026, 11, 18, 16, tzinfo=UTC)}   # 2026-11-19 00:00 CST
    man = manifests(conn, C.MEMBERSHIP_DATASETS["industry"])[0]
    assert man["contract_id"] == "pulse.sector_membership" and len(man["coverage_document"]["nodes"]) == 48 and "look-ahead" in man["coverage_document"]["note"]
    assert cursor_of(conn, "boards:snapshot")["state"] == "complete"
    world.advance(hours=2)
    again = rs.boards(M.SinaRefAdapter(world.client()), chunk_boards=100)
    assert again.counts["snapshots_unchanged"] == 2 and len(manifests(conn, C.MEMBERSHIP_DATASETS["industry"])) == 1


def test_boards_block_resumes_the_same_day_and_starts_over_on_another_day(conn, lake):
    world = World()
    svc, rs = build(conn, lake, world)
    world.block_after = 101                                                 # nodes + 100 boards, then 403
    with pytest.raises(SourceBlocked):
        rs.boards(M.SinaRefAdapter(world.client()), chunk_boards=25)
    cur = cursor_of(conn, "boards:snapshot")
    assert cur["state"] == "running" and len(cur["done"]) == 100 and len(cur["nodes"]) == 261
    assert market_ready(conn, C.MEMBERSHIP_DATASETS["industry"]) is None    # a partial snapshot is never published
    world.advance(hours=2)
    world.block_after = None
    n = len(world.requests)
    out = rs.boards(M.SinaRefAdapter(world.client()), chunk_boards=25)
    assert len(world.requests) - n == 261 - 100 and out.counts["published"] == 2 and "resumed: 100/261" in out.notes[0]


def test_boards_cursor_of_another_day_is_not_resumed(conn, lake):
    world = World()
    svc, rs = build(conn, lake, world)
    world.block_after = 11
    with pytest.raises(SourceBlocked):
        rs.boards(M.SinaRefAdapter(world.client()), chunk_boards=25)
    world.advance(days=1, hours=2)
    world.block_after = None
    n = len(world.requests)
    rs.boards(M.SinaRefAdapter(world.client()), chunk_boards=25)
    assert len(world.requests) - n == 1 + 261                               # a snapshot never mixes days: nodes fetched again, every board again


def test_a_failed_board_blocks_publication_and_is_the_only_one_retried(conn, lake):
    world = World()
    svc, rs = build(conn, lake, world)
    world.member_status = {"new_blhy": 500}
    out = rs.boards(M.SinaRefAdapter(world.client()), chunk_boards=100)
    assert out.counts["boards_failed"] == 1 and out.counts.get("published", 0) == 0 and "incomplete" in out.notes[-1]
    assert market_ready(conn, C.MEMBERSHIP_DATASETS["industry"]) is None
    world.member_status = {}
    world.advance(hours=1)
    n = len(world.requests)
    out = rs.boards(M.SinaRefAdapter(world.client()), chunk_boards=100)
    assert len(world.requests) - n == 1 and out.counts["published"] == 2


def test_boards_limit_is_a_smoke_run_that_never_publishes(conn, lake):
    world = World()
    svc, rs = build(conn, lake, world)
    out = rs.boards(M.SinaRefAdapter(world.client()), limit=2)
    assert len(world.requests) == 3 and out.counts["published"] == 0 and market_ready(conn, C.MEMBERSHIP_DATASETS["concept"]) is None


def market_ready(conn, dataset):
    from pulse.adapters.postgres import market_repo

    return market_repo.latest_ready(conn, dataset)


# ---- listings ----------------------------------------------------------------------------------------------------------------
def test_listings_two_requests_and_the_old_minimum_guard(conn, lake):
    world = World()
    svc, rs = build(conn, lake, world)
    with pytest.raises(RefDataError, match="not published"):
        rs.listings(L.ListingAdapter(world.client()))                       # the fixtures hold 50 rows: below the 1000-row guard
    assert market_ready(conn, C.LISTING_DATASET) is None
    out = rs.listings(L.ListingAdapter(world.client()), min_rows=10)
    assert out.counts["listings"] == 50 and len(world.requests) == 2 + 2
    frame = rs.read_frame(C.LISTING_DATASET)
    assert dict(frame.schema) == C.LISTING_SCHEMA and frame.filter(pl.col("code") == "000001")["listed_on"].item() == date(1991, 4, 3)
    assert set(frame["exchange"].to_list()) == {"XSHG", "XSHE"}
    again = rs.listings(L.ListingAdapter(world.client()), min_rows=10)
    assert again.counts["snapshots_unchanged"] == 1 and len(manifests(conn, C.LISTING_DATASET)) == 1


def test_a_blocked_listing_source_stops_the_run(conn, lake):
    world = World()
    svc, rs = build(conn, lake, world)
    world.block_after = 1                                                   # the SSE answer is fine, the SZSE request is refused
    with pytest.raises(SourceBlocked):
        rs.listings(L.ListingAdapter(world.client()), min_rows=10)
    assert len(world.requests) == 2 and market_ready(conn, C.LISTING_DATASET) is None


# ---- reconcile (read only) -------------------------------------------------------------------------------------------------------
def test_reconcile_compares_with_the_legacy_datasets_and_writes_nothing(conn, lake):
    from pulse.adapters.postgres import instrument_repo
    from pulse.domain.instruments.identity import InstrumentRecord

    world = World()
    svc, rs = build(conn, lake, world)
    rs.adjusted_bars(M.SinaRefAdapter(world.client()), ["000001", "000002"], start=BARS_FROM, today=TODAY, chunk=10)
    rs.listings(L.ListingAdapter(world.client()), min_rows=10)
    native = rs.read_frame(C.ADJ_DATASET)
    legacy = (native.filter(~((pl.col("instrument_id") == instrument_id_of("000002")) & (pl.col("trade_date") == date(2026, 11, 18))))
              .with_columns(pl.when((pl.col("instrument_id") == instrument_id_of("000001")) & (pl.col("trade_date") == date(2026, 11, 17)))
                            .then((pl.col("close_price").cast(pl.Float64) * 1.01).cast(pl.Decimal(38, 12))).otherwise(pl.col("close_price")).alias("close_price")))
    B.publish_derived(svc, C.LEGACY_ADJ_DATASET, legacy, {"dataset": C.LEGACY_ADJ_DATASET}, contract_id="pulse.analysis_artifact")
    with conn.transaction():
        instrument_repo.upsert_instruments(conn, [InstrumentRecord.build("000001", "平安银行", date(1991, 4, 3)),
                                                  InstrumentRecord.build("000002", "万科A", date(1991, 1, 29))], world.now(), ("legacy_warehouse",), "test")
    snapshot = lambda: (conn.execute("SELECT count(*) AS n FROM market.dataset_manifest").fetchone()["n"],
                        conn.execute("SELECT count(*) AS n FROM ingest.raw_object").fetchone()["n"],
                        conn.execute("SELECT count(*) AS n FROM ingest.batch").fetchone()["n"])
    before = snapshot()
    text = rs.reconcile(["adjusted", "index", "boards", "listings"])
    assert snapshot() == before                                              # read only
    assert "== reconcile adjusted bars ==" in text and "native_only_rows: 1" in text and "instruments_with_close_diff: 1" in text
    assert "== reconcile index bars ==" in text and "native dataset missing" in text
    assert "== reconcile listings ==" in text and "date_differs: 0" in text and "overlap: 2" in text
