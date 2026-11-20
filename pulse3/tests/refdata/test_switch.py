"""The consumer switch to the native reference data (PULSE_REFDATA_SOURCE, default legacy), the native industry writer and fill-listed-on."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import UUID

import polars as pl
import pytest

from pulse.adapters.postgres import instrument_repo, market_repo
from pulse.adapters.sources import ref_listing as L
from pulse.adapters.sources import ref_market as M
from pulse.application import bindings as B
from pulse.application.refdata_service import RefDataError, instrument_id_of
from pulse.domain.instruments.identity import InstrumentRecord
from pulse.workers import refdata as W

from .support import BARS_FROM, World, factor_body, fixture
from .test_pg_flow import build

UTC = timezone.utc
TODAY = date(2026, 11, 19)
NATIVE_IDS = ("bars_native_fwd_adjusted_1d", "index_bars_native_1d", "sector_membership_native_boards_industry")
LEGACY_IDS = ("bars_legacy_fwd_adjusted_1d", "index_bars_legacy_1d", "sector_membership_legacy_boards_industry")


# ---- the switch itself (no database) --------------------------------------------------------------------------------------------
def test_default_is_legacy_and_nothing_changes_until_the_variable_is_set(monkeypatch):
    monkeypatch.delenv("PULSE_REFDATA_SOURCE", raising=False)
    assert (B.RESEARCH_DATASET, B.INDEX_DATASET, B.MEMBERSHIP_DATASET) == LEGACY_IDS
    assert (B.research_dataset(), B.index_dataset(), B.membership_dataset()) == LEGACY_IDS
    monkeypatch.setenv("PULSE_REFDATA_SOURCE", "")
    assert B.RESEARCH_DATASET == LEGACY_IDS[0]                                         # empty = unset


def test_native_resolves_every_consumer_that_reads_the_module_attribute(monkeypatch):
    monkeypatch.setenv("PULSE_REFDATA_SOURCE", "native")
    assert (B.RESEARCH_DATASET, B.INDEX_DATASET, B.MEMBERSHIP_DATASET) == NATIVE_IDS
    assert B.refdata_source() == "native"
    from pulse.application.bindings import RESEARCH_DATASET                             # a from-import evaluates at import time too
    assert RESEARCH_DATASET == NATIVE_IDS[0]
    monkeypatch.setenv("PULSE_REFDATA_SOURCE", "NATIVE ")
    assert B.refdata_source() == "native"


def test_an_unknown_value_is_an_error_never_a_silent_legacy(monkeypatch):
    monkeypatch.setenv("PULSE_REFDATA_SOURCE", "natve")
    with pytest.raises(ValueError, match="expected 'legacy' or 'native'"):
        B.research_dataset()
    with pytest.raises(ValueError):
        _ = B.INDEX_DATASET
    with pytest.raises(AttributeError):
        _ = B.NO_SUCH_NAME


def test_every_dataset_the_switch_names_has_a_capability_or_a_documented_schema():
    from pulse.domain.market.capability import ADJUSTED_PRICES, DAILY_BARS, REGISTRY

    cap = REGISTRY["bars_native_fwd_adjusted_1d"]
    assert cap.source_id == "sina_ref" and {DAILY_BARS, ADJUSTED_PRICES} <= set(cap.features) and not cap.estimated
    assert REGISTRY["bars_legacy_fwd_adjusted_1d"].features == cap.features                   # a bars snapshot of either source satisfies the same requirements


def test_latest_visible_revision_is_one_rule_for_both_frame_shapes():
    t = [datetime(2026, 11, d, 8, tzinfo=UTC) for d in (18, 19, 20)]

    def row(rev, close, at, day=date(2026, 11, 17)):
        return {"instrument_id": "i", "trade_date": day, "revision": rev, "close_price": close, "available_at": at}

    native = pl.DataFrame([row(1, 10.0, t[0]), row(2, 9.5, t[2]), row(1, 11.0, t[1], date(2026, 11, 18))])
    legacy = pl.DataFrame([row(1, 10.0, t[0])])
    pick = lambda df, at: {(r["trade_date"], r["close_price"]) for r in B.latest_visible_revision(df, at).iter_rows(named=True)}
    assert pick(native, t[0]) == {(date(2026, 11, 17), 10.0)}                              # before the re-publication: the old value
    assert pick(native, t[1]) == {(date(2026, 11, 17), 10.0), (date(2026, 11, 18), 11.0)}
    assert pick(native, t[2]) == {(date(2026, 11, 17), 9.5), (date(2026, 11, 18), 11.0)}   # after: revision 2, still one row per day
    assert pick(legacy, t[2]) == {(date(2026, 11, 17), 10.0)}                              # the legacy shape is unchanged by the rule
    assert pick(legacy, t[0] - timedelta(hours=1)) == set()
    # reconstructed policy: revision 1 is usable by policy time even if stored later; a later revision only once recorded
    late = pl.DataFrame([row(1, 10.0, t[2]), row(2, 9.5, t[2])])
    cutoff = B.policy_usable_at(date(2026, 11, 17)) + timedelta(minutes=1)
    got = B.latest_visible_revision(late, cutoff, recon=True)
    assert got["revision"].to_list() == [1] and got["close_price"].to_list() == [10.0]


def test_cli_has_fill_listed_on():
    a = W._parser().parse_args(["fill-listed-on", "--dry"])
    assert a.cmd == "fill-listed-on" and a.dry


# ---- research bars, index and the bars route ------------------------------------------------------------------------------------
@pytest.mark.pg
def test_native_switch_reads_the_native_datasets_with_the_latest_revision_visible_at_the_cutoff(conn, lake, monkeypatch):
    world = World()
    svc, rs = build(conn, lake, world)
    rs.adjusted_bars(M.SinaRefAdapter(world.client()), ["000001", "000002"], start=BARS_FROM, today=TODAY, chunk=10)
    rs.index_bars(M.SinaRefAdapter(world.client()), M.TencentRefAdapter(world.client()), start=date(2026, 1, 1), today=TODAY)
    cutoff_old = world.now()
    old = M.parse_factors(fixture("sina_adj_factor_sz000001.js"))
    world.factor_override["sz000001"] = factor_body("sz000001", [(d.isoformat(), f"{f * 1.05:.16f}") for d, f in old] + [("2026-11-20", "1.0000000000000000")])
    world.advance(days=1)
    rs.adjusted_bars(M.SinaRefAdapter(world.client()), ["000001"], start=BARS_FROM, today=date(2026, 11, 20), scope=None)
    cutoff_new = world.now()

    # default: the legacy datasets are the source, and they do not exist here: the switch being off changes nothing, nothing leaks in
    monkeypatch.delenv("PULSE_REFDATA_SOURCE", raising=False)
    with pytest.raises(LookupError, match="bars_legacy_fwd_adjusted_1d"):
        B.load_research_inputs(conn, lake, cutoff_new)

    monkeypatch.setenv("PULSE_REFDATA_SOURCE", "native")
    day, iid = date(2025, 9, 10), instrument_id_of("000001")
    before = B.load_research_inputs(conn, lake, cutoff_old)
    after = B.load_research_inputs(conn, lake, cutoff_new)
    assert before.manifest["dataset_id"] == "bars_native_fwd_adjusted_1d" and after.manifest["dataset_id"] == before.manifest["dataset_id"]
    assert before.benchmark_manifest is not None and before.benchmark_manifest["dataset_id"] == "index_bars_native_1d" and before.benchmark
    for ri in (before, after):
        assert ri.bars.columns == ["instrument_id", "trade_date", "available_at", "open", "high", "low", "close", "volume"]
        assert ri.bars.height == ri.bars.unique(["instrument_id", "trade_date"]).height            # one row per key although the frame has two revisions
    pick = lambda ri: ri.bars.filter((pl.col("instrument_id") == iid) & (pl.col("trade_date") == day))["close"].item()
    assert pick(after) == pytest.approx(pick(before) / 1.05, rel=1e-9)                              # the later cutoff sees revision 2, the earlier one revision 1
    assert abs(pick(before) - 10.922984695675343) < 1e-9
    assert after.bars.filter(pl.col("instrument_id") == instrument_id_of("000002")).height == before.bars.filter(pl.col("instrument_id") == instrument_id_of("000002")).height


@pytest.mark.pg
def test_the_bars_route_reader_returns_one_row_per_day_for_the_native_frame(conn, lake, monkeypatch):
    from pulse.api import routes_market as R

    world = World()
    svc, rs = build(conn, lake, world)
    rs.adjusted_bars(M.SinaRefAdapter(world.client()), ["000001"], start=BARS_FROM, today=TODAY, chunk=10)
    cutoff_old = world.now()
    old = M.parse_factors(fixture("sina_adj_factor_sz000001.js"))
    world.factor_override["sz000001"] = factor_body("sz000001", [(d.isoformat(), f"{f * 1.05:.16f}") for d, f in old] + [("2026-11-20", "1.0")])
    world.advance(days=1)
    rs.adjusted_bars(M.SinaRefAdapter(world.client()), ["000001"], start=BARS_FROM, today=date(2026, 11, 20), scope=None)
    man = market_repo.latest_ready(conn, "bars_native_fwd_adjusted_1d")
    monkeypatch.setattr(R, "_lake_manifest", lambda sha: [lake.root / m["path"] for m in lake.read_manifest(sha).members])
    d0, d1, iid = date(2025, 9, 1), date(2025, 9, 30), UUID(instrument_id_of("000001"))
    both = R._lake_daily_rows(man, iid, d0, d1)                                                     # no cutoff: every stored revision (raw reader)
    old_rows = R._lake_daily_rows(man, iid, d0, d1, cutoff_old)
    new_rows = R._lake_daily_rows(man, iid, d0, d1, world.now())
    assert len(both) == 2 * len(old_rows) and len(old_rows) == len(new_rows) > 10
    assert {r["revision"] for r in old_rows} == {1} and {r["revision"] for r in new_rows} == {2}
    bars, _ = R._read_lake_bars(man, iid, d0, d1, cutoff_old)
    assert len(bars) == len(old_rows) and len({b.trade_date for b in bars}) == len(bars)
    assert man["coverage_document"]["capability"]["dataset_id"] == "bars_native_fwd_adjusted_1d"          # the bars API requires it in the coverage


# ---- native industry membership ------------------------------------------------------------------------------------------------------
def register_board_stocks(conn, when: datetime) -> None:
    recs = [InstrumentRecord.build(f"600{n:03d}", "股票", None) for n in range(0, 753)]
    with conn.transaction():
        instrument_repo.upsert_instruments(conn, recs, when, ("legacy_warehouse",), "test")


def tag_counts(conn) -> tuple[int, int]:
    r = conn.execute("SELECT count(*) FILTER (WHERE effective_to IS NULL) AS open, count(*) FILTER (WHERE effective_to IS NOT NULL) AS closed"
                     " FROM core.instrument_tag WHERE taxonomy_id='legacy_boards_industry'").fetchone()
    return r["open"], r["closed"]


@pytest.mark.pg
def test_native_industry_writer_publishes_tags_nodes_and_follows_later_snapshots(conn, lake, monkeypatch):
    world = World()
    svc, rs = build(conn, lake, world)
    register_board_stocks(conn, world.now())
    rs.boards(M.SinaRefAdapter(world.client()), chunk_boards=100)
    t1 = world.now()
    frame1 = rs.read_frame("sector_membership_native_boards_industry")

    # switch OFF: the legacy writer finds no legacy evidence, the native snapshot is not touched
    monkeypatch.delenv("PULSE_REFDATA_SOURCE", raising=False)
    assert B.ensure_membership(svc, lake, t1) is None and tag_counts(conn) == (0, 0)

    monkeypatch.setenv("PULSE_REFDATA_SOURCE", "native")
    m = B.ensure_membership(svc, lake, t1)
    assert m is not None and (m.taxonomy_id, m.taxonomy_version) == ("legacy_boards_industry", 1)
    assert m.manifest["dataset_id"] == "sector_membership_native_boards_industry" == B.membership_dataset()
    pairs1 = set(zip(frame1["instrument_id"].to_list(), frame1["node_id"].to_list()))
    assert tag_counts(conn) == (len(pairs1), 0)
    assert sum(len(v) for v in m.members.values()) == len(pairs1) and set(m.nodes) == {n for _, n in pairs1}
    assert m.nodes["new_blhy"] == rs.read_dataset("sector_membership_native_boards_industry")[0]["coverage_document"]["nodes"]["new_blhy"]
    assert m.as_of == date(2026, 11, 19) and m.evidence.startswith("native_boards_industry:sector_membership_native_boards_industry@")
    assert conn.execute("SELECT count(*) AS n FROM core.taxonomy_node WHERE taxonomy_id='legacy_boards_industry'").fetchone()["n"] == len(m.nodes)
    assert any(len([1 for p in pairs1 if p[0] == i]) > 1 for i in {p[0] for p in pairs1})        # the fixture has stocks in two boards: both tags are written
    # idempotent
    again = B.ensure_membership(svc, lake, t1)
    assert tag_counts(conn) == (len(pairs1), 0) and again.members == m.members

    # a later snapshot where every board lost one member: the vanished pairs are closed, the kept ones stay open, nothing is duplicated
    world.member_count = 2
    world.advance(days=1, hours=2)
    rs.boards(M.SinaRefAdapter(world.client()), chunk_boards=100)
    t2 = world.now()
    frame2 = rs.read_frame("sector_membership_native_boards_industry")
    pairs2 = set(zip(frame2["instrument_id"].to_list(), frame2["node_id"].to_list()))
    assert pairs2 < pairs1
    m2 = B.ensure_membership(svc, lake, t2)
    assert tag_counts(conn) == (len(pairs2), len(pairs1 - pairs2)) and sum(len(v) for v in m2.members.values()) == len(pairs2)
    # point in time: a reader at the first snapshot's time still sees the first membership (the closing starts at the second effective_from)
    seen = conn.execute("SELECT count(*) AS n FROM core.instrument_tag WHERE taxonomy_id='legacy_boards_industry' AND effective_from <= %s"
                        " AND (effective_to IS NULL OR effective_to > %s) AND available_at <= %s", (t1, t1, t1)).fetchone()["n"]
    assert seen == len(pairs1)
    assert B.ensure_membership(svc, lake, t2).members == m2.members and tag_counts(conn) == (len(pairs2), len(pairs1 - pairs2))


@pytest.mark.pg
def test_native_industry_without_a_snapshot_is_missing_not_an_error(conn, lake, monkeypatch):
    monkeypatch.setenv("PULSE_REFDATA_SOURCE", "native")
    world = World()
    svc, rs = build(conn, lake, world)
    assert B.ensure_membership(svc, lake, world.now()) is None


# ---- fill-listed-on --------------------------------------------------------------------------------------------------------------------
@pytest.mark.pg
def test_fill_listed_on_fills_nulls_reports_conflicts_and_never_overwrites(conn, lake):
    world = World()
    svc, rs = build(conn, lake, world)
    with pytest.raises(RefDataError, match="listings"):
        rs.fill_listed_on()
    rs.listings(L.ListingAdapter(world.client()), min_rows=10)
    with conn.transaction():
        instrument_repo.upsert_instruments(conn, [
            InstrumentRecord.build("000001", "平安银行", None),                          # NULL: to be filled with 1991-04-03
            InstrumentRecord.build("000002", "万科A", date(1991, 1, 29)),                # equal
            InstrumentRecord.build("000006", "深振业A", date(2000, 1, 1)),               # conflict (native 1992-04-27): reported, kept
            InstrumentRecord.build("000011", "深物业A", None)], world.now(), ("legacy_warehouse",), "test")   # NULL: filled with 1992-03-30
    listed = lambda code: conn.execute("SELECT listed_on FROM core.instrument WHERE instrument_id=%s", (instrument_id_of(code),)).fetchone()["listed_on"]
    dry = rs.fill_listed_on(dry=True)
    assert dry.counts["to_fill"] == 2 and dry.counts["conflicts_not_overwritten"] == 1 and dry.counts["already_equal"] == 1 and "filled" not in dry.counts
    assert listed("000001") is None and listed("000011") is None                       # dry changes nothing
    out = rs.fill_listed_on()
    assert out.counts["filled"] == 2 and out.counts["conflicts_not_overwritten"] == 1
    assert listed("000001") == date(1991, 4, 3) and listed("000011") == date(1992, 3, 30)
    assert listed("000006") == date(2000, 1, 1) and listed("000002") == date(1991, 1, 29)       # a set value is never overwritten
    assert any("000006" in n and "2000-01-01 kept" in n and "1992-04-27" in n for n in out.notes)
    assert out.counts["native_codes_without_instrument"] == 50 - 4
    again = rs.fill_listed_on()
    assert again.counts["to_fill"] == 0 and again.counts["conflicts_not_overwritten"] == 1 and again.counts["already_equal"] == 3
