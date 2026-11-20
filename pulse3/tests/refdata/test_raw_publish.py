"""Raw bars + factor rows published from the SAME fetches as the adjusted bars; the raw-only mode with its own cursor."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import polars as pl
import pytest

from pulse.adapters.sources import ref_market as M
from pulse.adapters.sources.http import SourceBlocked
from pulse.application import refdata_core as C
from pulse.application.refdata_service import instrument_id_of

from .support import BARS_FROM, World, factor_body, fixture
from .test_pg_flow import build, cursor_of, manifests

pytestmark = pytest.mark.pg
TODAY = date(2026, 11, 19)
D = Decimal


def test_a_full_fetch_also_publishes_raw_bars_and_factor_rows_without_one_more_request(conn, lake):
    world = World()
    svc, rs = build(conn, lake, world)
    out = rs.adjusted_bars(M.SinaRefAdapter(world.client()), ["000001", "000002"], start=BARS_FROM, today=TODAY, chunk=10)
    assert len(world.requests) == 4                                          # 2 requests per code, exactly as before this feature
    raw, fac, adj = rs.read_frame(C.RAW_DATASET), rs.read_frame(C.FACTOR_DATASET), rs.read_frame(C.ADJ_DATASET)
    assert dict(raw.schema) == C.RAW_SCHEMA and dict(fac.schema) == C.FACTOR_SCHEMA
    assert raw.height == adj.height == 430 and set(raw["revision"].to_list()) == {1}
    first = raw.filter((pl.col("instrument_id") == instrument_id_of("000001")) & (pl.col("trade_date") == date(2025, 3, 27))).row(0, named=True)
    assert (first["open_price_raw"], first["close_price_raw"], first["volume_shares"]) == (D("11.37"), D("11.39"), 55334940)    # the vendor strings, exact
    assert first["turnover_amount_cny"] is None and first["is_final"] is True and first["quality_status"] == "partial" and first["timeframe"] == "1d"
    f1 = fac.filter(pl.col("instrument_id") == instrument_id_of("000001"))
    assert f1.height == 33 and f1.filter(pl.col("effective_from") == date(2025, 10, 15))["factor"].item() == D("1.0555650131575000")
    assert f1.filter(pl.col("effective_from") == date(2026, 9, 24))["factor"].item() == D("1")
    assert fac.filter(pl.col("instrument_id") == instrument_id_of("000002")).height == 1       # the synthetic code's one row, as served
    # adjusted == raw / factor in force: the three datasets describe the same fetch
    day, iid = date(2025, 9, 10), instrument_id_of("000001")
    r = raw.filter((pl.col("instrument_id") == iid) & (pl.col("trade_date") == day))["close_price_raw"].item()
    a = adj.filter((pl.col("instrument_id") == iid) & (pl.col("trade_date") == day))["close_price"].item()
    assert float(a) == pytest.approx(float(r) / 1.0775443093553, rel=1e-11)
    # lineage and coverage documents say what the datasets are
    for ds, contract in ((C.RAW_DATASET, "CanonicalBar"), (C.FACTOR_DATASET, "EvidenceDocument")):
        man = manifests(conn, ds)[0]
        batch = conn.execute("SELECT source_id, endpoint_id FROM ingest.batch WHERE batch_id=%s", (man["batch_id"],)).fetchone()
        assert (batch["source_id"], batch["endpoint_id"]) == ("sina_ref", "adjusted_bars") and man["contract_id"] == contract
    assert "exact limit-up" in manifests(conn, C.RAW_DATASET)[0]["coverage_document"]["purpose"]
    assert "raw price / factor" in manifests(conn, C.FACTOR_DATASET)[0]["coverage_document"]["semantics"]
    assert out.counts["raw_rows_new"] == 430 and out.counts["factor_rows_new"] == 34
    # the raw rows plus the factor rows give the exact flags: 000001's four ex-right days
    flags = C.limit_flags_raw(raw.filter(pl.col("instrument_id") == iid).with_columns(pl.lit("000001").alias("code"), pl.lit(False).alias("is_st")),
                              fac.filter(pl.col("instrument_id") == iid))
    assert flags.filter(pl.col("ref_method") == "factor_derived").height == 4


def test_a_new_ex_right_event_revises_the_factors_and_adjusted_bars_but_not_the_raw_bars(conn, lake):
    world = World()
    svc, rs = build(conn, lake, world)
    rs.adjusted_bars(M.SinaRefAdapter(world.client()), ["000001"], start=BARS_FROM, today=TODAY, chunk=10)
    cutoff_old = world.now()
    old = M.parse_factor_rows(fixture("sina_adj_factor_sz000001.js"))
    world.factor_override["sz000001"] = factor_body("sz000001", [(d.isoformat(), f"{D(f) * D('1.05'):.16f}") for d, f in old] + [("2026-11-20", "1.0000000000000000")])
    world.advance(days=1)
    out = rs.adjusted_bars(M.SinaRefAdapter(world.client()), ["000001"], start=BARS_FROM, today=date(2026, 11, 20), scope=None)
    assert out.counts["bars_new_revisions"] == 400 and out.counts["raw_rows_new"] == 0              # raw prices did not move: no new raw revision
    assert [m["manifest_version"] for m in manifests(conn, C.RAW_DATASET)] == [1]
    fac = rs.read_frame(C.FACTOR_DATASET)
    assert out.counts["factor_rows_new"] == 34 and out.counts["factor_rows_new_revisions"] == 33 and fac["revision"].max() == 2
    row = fac.filter((pl.col("effective_from") == date(2025, 10, 15)) & (pl.col("revision") == 2)).row(0, named=True)
    assert abs(row["factor"] - D("1.0555650131575") * D("1.05")) < D("1e-14")
    # point in time: at the old cutoff only revision 1 of the factors is visible
    seen = C.factor_in_force(fac.filter(pl.col("available_at") <= cutoff_old))
    assert seen[instrument_id_of("000001")][1][-1] == D("1")
    # a vendor correction of one raw price is a new raw revision and nothing else changes
    rows = json.loads(fixture("sina_adj_kline_sz000001.json"))
    rows[10]["close"] = "99.990"
    world.kline_override["sz000001"] = json.dumps(rows).encode()
    world.advance(days=1)
    out2 = rs.adjusted_bars(M.SinaRefAdapter(world.client()), ["000001"], start=BARS_FROM, today=date(2026, 11, 21), scope=None)
    assert out2.counts["raw_rows_new"] == 1 and out2.counts["raw_rows_new_revisions"] == 1
    raw = rs.read_frame(C.RAW_DATASET)
    fixed = raw.filter(pl.col("trade_date") == date.fromisoformat(rows[10]["day"]))
    assert sorted(fixed["revision"].to_list()) == [1, 2] and fixed.filter(pl.col("revision") == 2)["close_price_raw"].item() == D("99.99")
    assert C.latest_bars(raw).height == 400


def test_raw_only_is_one_request_per_code_has_its_own_cursor_and_publishes_only_raw(conn, lake):
    world = World()
    svc, rs = build(conn, lake, world)
    codes = ["000001", "000002", "000003"]
    world.block_after = 2                                                     # codes 1 and 2 complete (1 request each), code 3 is refused
    with pytest.raises(SourceBlocked):
        rs.adjusted_bars(M.SinaRefAdapter(world.client()), codes, start=BARS_FROM, today=TODAY, chunk=2, raw_only=True)
    assert len(world.requests) == 3 and not any("qfq.js" in u for u in world.requests)         # no factor file was ever requested
    assert {i for i in rs.read_frame(C.RAW_DATASET)["instrument_id"].unique().to_list()} == {instrument_id_of("000001"), instrument_id_of("000002")}
    assert rs.read_dataset(C.ADJ_DATASET)[0] is None and rs.read_dataset(C.FACTOR_DATASET)[0] is None
    cur = cursor_of(conn, "raw:universe")
    assert cur["state"] == "running" and cur["last_code"] == "000002" and cursor_of(conn, "adjusted:universe") is None
    world.advance(hours=2)
    world.block_after = None
    n = len(world.requests)
    out = rs.adjusted_bars(M.SinaRefAdapter(world.client()), codes, start=BARS_FROM, today=TODAY, chunk=2, raw_only=True)
    assert len(world.requests) - n == 1 and "resumed after 000002" in out.notes[0]          # only code 3
    assert cursor_of(conn, "raw:universe")["state"] == "complete"
    assert {i for i in rs.read_frame(C.RAW_DATASET)["instrument_id"].unique().to_list()} == {instrument_id_of(c) for c in codes}
    batch = conn.execute("SELECT endpoint_id FROM ingest.batch WHERE batch_id=%s", (manifests(conn, C.RAW_DATASET)[-1]["batch_id"],)).fetchone()
    assert batch["endpoint_id"] == "raw_bars"
    # the later full fetch finds the raw rows unchanged (the same vendor strings), publishes the adjusted bars and factors, keeps its own cursor
    world.advance(hours=1)
    full = rs.adjusted_bars(M.SinaRefAdapter(world.client()), codes, start=BARS_FROM, today=TODAY, chunk=10)
    assert full.counts["raw_rows_new"] == 0 and full.counts["raw_rows_fetched"] == 460
    assert rs.read_frame(C.ADJ_DATASET).height == 460 and rs.read_frame(C.FACTOR_DATASET).height == 33 + 1 + 1
    assert cursor_of(conn, "adjusted:universe")["state"] == "complete" and cursor_of(conn, "raw:universe")["state"] == "complete"


def test_the_daily_refresh_appends_the_raw_bar_of_the_quote_too(conn, lake):
    from pulse.adapters.sources.tencent import TencentAdapter

    world = World()
    svc, rs = build(conn, lake, world)
    rs.adjusted_bars(M.SinaRefAdapter(world.client()), ["000001"], start=BARS_FROM, today=TODAY, chunk=10)
    world.advance(hours=1)
    world.quotes = {"000001": {"last": 12.5, "prev": 12.2, "open": 12.3, "lots": 100000, "time": "20261119150000", "high": 12.6, "low": 12.2}}
    out = rs.adjusted_refresh(M.SinaRefAdapter(world.client()), TencentAdapter(world.client()), ["000001"], start=BARS_FROM, today=TODAY)
    assert out.counts["to_append"] == 1
    raw = rs.read_frame(C.RAW_DATASET).filter(pl.col("trade_date") == TODAY).row(0, named=True)
    assert (raw["close_price_raw"], raw["open_price_raw"], raw["volume_shares"], raw["revision"]) == (D("12.5"), D("12.3"), 10_000_000, 1)
