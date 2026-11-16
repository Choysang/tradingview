"""Intraday import on real PostgreSQL: idempotent + incremental, PIT times, byte-exact raw, seal facts, the two reads (contract-validated)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import polars as pl
import pytest

from pulse import contracts
from pulse.adapters.sources.legacy_recordings import LegacyRecordings
from pulse.application import bf_live_service as B
from pulse.domain.instruments.identity import instrument_uuid, venue_for_code

from . import support as T

pytestmark = pytest.mark.pg
UTC = timezone.utc


def _svc(conn, lake, root, clock):
    s = B.LiveService(conn, lake, LegacyRecordings(root), clock=clock)
    s.bootstrap()
    return s


def _seed(root):
    q, hot = T.scenario_rows()
    T.write_quotes(root, "quotes", "part-0001.parquet", q)
    T.write_quotes(root, "hot", "part-0001.parquet", hot)


def _count(conn, sql, *a):
    return conn.execute(sql, a).fetchone()["n"]


def test_import_is_idempotent_incremental_and_pit(conn, lake, legacy_root, clock):
    _seed(legacy_root)
    before = T.tree_state(legacy_root)
    svc = _svc(conn, lake, legacy_root, clock)
    s1 = svc.import_intraday(T.DAY)
    assert s1.counts["parts_imported"] == 2 and s1.counts["session_rows_total"] == 15
    man = svc.latest_ready(B.DATASET_QUOTES, T.DAY)
    assert man["manifest_state"] == "READY" and man["row_count"] == 15 and man["available_watermark"] == clock.t
    cov = man["coverage_document"]
    assert cov["capability"] == "sampled_snapshots_estimated" and cov["not_tick_data"] and cov["session_date"] == "2026-11-12"
    df = lake.read_snapshot_frames(man["content_hash"])
    assert df.height == 15 and df["quote_at"].dtype == pl.Datetime("ms", "UTC")
    first = df.filter(pl.col("code") == "000001").sort("quote_at").row(0, named=True)
    assert first["quote_at"] == datetime(2026, 11, 12, 1, 20, tzinfo=UTC)               # 09:20 CST
    assert first["session_date"] == T.DAY and first["source_stream"] in ("quotes", "hot")
    # the old files are untouched and every part is also stored byte-exact as a raw object
    assert T.tree_state(legacy_root) == before
    raws = conn.execute("SELECT content_hash, storage_uri FROM ingest.raw_object WHERE source_id='legacy_recordings'").fetchall()
    assert len(raws) == 2
    for r in raws:
        assert lake.read_object(r["content_hash"]) in {p.read_bytes() for p in legacy_root.rglob("*.parquet")}
    batches, manifests = _count(conn, "SELECT count(*) n FROM ingest.batch"), _count(conn, "SELECT count(*) n FROM market.dataset_manifest WHERE dataset_id=%s", B.DATASET_QUOTES)
    # re-run: nothing new -> no batch, no manifest
    s2 = svc.import_intraday(T.DAY)
    assert s2.counts["parts_unchanged"] == 2 and "parts_imported" not in s2.counts
    assert _count(conn, "SELECT count(*) n FROM ingest.batch") == batches
    assert _count(conn, "SELECT count(*) n FROM market.dataset_manifest WHERE dataset_id=%s", B.DATASET_QUOTES) == manifests
    # a new part arrives later: a new cumulative version, available at the later import time, earlier members kept
    clock.advance(minutes=30)
    T.write_quotes(legacy_root, "quotes", "part-0002.parquet", T.quote_rows("000002", "万科A", 10.0, 11.0, [("14:58:00", 10.4, 10.39, 10.0, 10.41, 10.0)]))
    s3 = svc.import_intraday(T.DAY)
    assert s3.counts["parts_imported"] == 1 and s3.counts["session_rows_total"] == 16
    man2 = svc.latest_ready(B.DATASET_QUOTES, T.DAY)
    assert man2["manifest_version"] == man["manifest_version"] + 1 and man2["available_watermark"] == clock.t
    assert lake.read_snapshot_frames(man2["content_hash"]).height == 16
    assert lake.read_snapshot_frames(man["content_hash"]).height == 15                    # the earlier version is still replayable
    assert svc.import_intraday(T.DAY).counts.get("parts_imported") is None


def test_unreadable_and_mismatched_parts_are_reported_not_imported(conn, lake, legacy_root, clock):
    _seed(legacy_root)
    (legacy_root / "intraday" / T.DAY.isoformat() / "quotes" / "part-0002.parquet").write_bytes(b"half written")
    svc = _svc(conn, lake, legacy_root, clock)
    s = svc.import_intraday(T.DAY)
    assert s.counts["parts_unreadable"] == 1 and s.counts["parts_imported"] == 2
    (legacy_root / "intraday" / T.DAY.isoformat() / "quotes" / "part-0002.parquet").unlink()
    assert _count(conn, "SELECT count(*) n FROM ingest.raw_object WHERE source_id='legacy_recordings'") == 2


def test_seal_facts_dataset_values_dependency_and_idempotence(conn, lake, legacy_root, clock):
    _seed(legacy_root)
    svc = _svc(conn, lake, legacy_root, clock)
    assert "no READY" in svc.publish_seal_facts(T.DAY).notes[0]                         # nothing imported yet: says so, publishes nothing
    svc.import_intraday(T.DAY)
    clock.advance(minutes=1)
    s = svc.publish_seal_facts(T.DAY)
    assert s.counts["instruments_observed"] == 4 and s.counts["touched_limit_count"] == 3 and s.counts["sealed_ever_count"] == 2
    man = svc.latest_ready(B.DATASET_SEAL, T.DAY)
    src = svc.latest_ready(B.DATASET_QUOTES, T.DAY)
    assert man["coverage_document"]["input_snapshot_id"] == str(src["snapshot_id"]) and man["coverage_document"]["estimation"] == "estimated_from_60s_snapshots"
    dep = conn.execute("SELECT input_snapshot_id, dependency_role FROM market.dataset_dependency WHERE snapshot_id=%s", (man["snapshot_id"],)).fetchone()
    assert dep["input_snapshot_id"] == src["snapshot_id"] and dep["dependency_role"] == "quote_snapshots"
    facts = {r["code"]: r for r in lake.read_snapshot_frames(man["content_hash"]).iter_rows(named=True)}
    a = facts["000001"]
    assert a["first_seal_at"] == datetime(2026, 11, 12, 1, 35, tzinfo=UTC) and a["last_seal_at"] == datetime(2026, 11, 12, 2, 20, tzinfo=UTC)
    assert a["broken_count"] == 1 and a["sealed_at_close"] is True and str(a["close_seal_order_amount_cny"]) == "8800000.00"
    assert a["available_at"] == clock.t and a["available_at"] >= src["available_watermark"]            # derived availability = computed time
    b = facts["600000"]
    assert b["touched_limit"] and b["first_seal_at"] is None and b["sealed_at_close"] is False
    assert facts["000002"]["touched_limit"] is False and facts["000002"]["first_seal_at"] is None
    assert facts["600519"]["broken_count"] == 1 and facts["600519"]["sealed_at_close"] is False
    # unchanged input -> nothing published; a new input version -> a new facts version
    assert svc.publish_seal_facts(T.DAY).counts == {"facts_unchanged": 1}
    T.write_quotes(legacy_root, "quotes", "part-0002.parquet", T.quote_rows("000002", "万科A", 10.0, 11.0, [("14:58:00", 10.4, 10.39, 10.0, 10.41, 10.0)]))
    clock.advance(minutes=5)
    svc.import_intraday(T.DAY)
    assert svc.publish_seal_facts(T.DAY).counts["manifest_version"] == man["manifest_version"] + 1


def test_the_two_reads_are_contract_valid(conn, lake, legacy_root, clock):
    _seed(legacy_root)
    svc = _svc(conn, lake, legacy_root, clock)
    assert svc.seal_clock(None) is None                                                  # nothing published yet
    absent = svc.seal_fact(instrument_uuid(venue_for_code("000001"), "000001"), T.DAY)
    assert absent["status"] == "no_dataset"
    contracts.check("SealFacts", contracts.to_wire(absent), contracts.backfill_id("live"))
    svc.import_intraday(T.DAY)
    svc.publish_seal_facts(T.DAY)
    clk = svc.seal_clock(None)
    contracts.check("SealClock", contracts.to_wire(clk), contracts.backfill_id("live"))
    by = {b["clock"]: b for b in clk["buckets"]}
    assert clk["session_date"] == T.DAY and clk["sealed_ever_count"] == 2 and by["09:35"]["first_seal_count"] == 1 and by["09:40"]["first_seal_count"] == 1
    assert clk["estimation"] == "estimated_from_60s_snapshots" and clk["source"]["available_at"] == clock.t
    iid = instrument_uuid(venue_for_code("000001"), "000001")
    ok = svc.seal_fact(iid, T.DAY)
    contracts.check("SealFacts", contracts.to_wire(ok), contracts.backfill_id("live"))
    assert ok["status"] == "ok" and ok["fact"]["broken_count"] == 1 and ok["fact"]["code"] == "000001"
    missing = svc.seal_fact(uuid.uuid4(), T.DAY)
    assert missing["status"] == "not_observed" and missing["fact"] is None
    contracts.check("SealFacts", contracts.to_wire(missing), contracts.backfill_id("live"))
    wrong_day = svc.seal_fact(iid, T.DAY.replace(day=11))
    assert wrong_day["status"] == "no_dataset"
    # pinned to a release whose cutoff is before the facts were ready: nothing recorded later is visible (point in time)
    ready = conn.execute("SELECT max(ready_at) t FROM market.dataset_manifest WHERE dataset_id='intraday_seal_facts'").fetchone()["t"]
    from datetime import timedelta
    assert svc.seal_clock(T.DAY, cutoff=ready - timedelta(seconds=1)) is None
    assert svc.seal_fact(iid, T.DAY, cutoff=ready - timedelta(seconds=1))["status"] == "no_dataset"
    assert svc.seal_fact(iid, T.DAY, cutoff=ready)["status"] == "ok"


def test_extended_guba_and_alert_comparison_datasets(conn, lake, legacy_root, clock):
    T.write_ext(legacy_root, [T.ext_row("hf_CHA50CFD", "future", 12800.64), T.ext_row("fx_susdcnh", "fx", 6.6736)])
    gd = legacy_root / "news" / T.DAY.isoformat() / "guba"
    gd.mkdir(parents=True)
    pl.DataFrame({"code": ["000001"], "title": ["讨论"], "read_count": [10]}).write_parquet(gd / "sample.parquet")
    al = legacy_root / "news" / T.DAY.isoformat() / "alerts.jsonl"
    al.write_text('{"level": "P1", "title": "x"}\nnot json\n', encoding="utf-8")
    svc = _svc(conn, lake, legacy_root, clock)
    assert svc.import_ext(T.DAY).counts["rows_imported"] == 2
    assert svc.import_guba(T.DAY).counts["rows_imported"] == 1
    s = svc.import_alerts(T.DAY)
    assert s.counts["rows_imported"] == 2
    frame = lake.read_snapshot_frames(svc.latest_ready(B.DATASET_ALERTS, T.DAY)["content_hash"]).sort("line_no")
    assert frame["parsed_ok"].to_list() == [True, False]
    # the old alert log is a comparison dataset only: nothing reaches pulse3's own alert tables
    assert _count(conn, "SELECT count(*) n FROM notification.alert_intent") == 0
    assert svc.import_ext(T.DAY).counts.get("parts_unchanged") == 1


def test_cli_legacy_intraday_then_seal_facts(pg_dsn, fresh_dsn, tmp_path, legacy_root, capsys):
    from pulse.workers import ingest

    _seed(legacy_root)
    lake_dir = str(tmp_path / "cli-lake")
    base = ["--dsn", fresh_dsn, "--lake", lake_dir]
    assert ingest.run([*base, "legacy-intraday", "--date", "2026-11-12", "--legacy-data", str(legacy_root)]) == 0
    assert ingest.run([*base, "seal-facts", "--date", "2026-11-12", "--legacy-data", str(legacy_root)]) == 0
    out = capsys.readouterr().out
    assert "parts_imported: 2" in out and "sealed_ever_count: 2" in out
    assert ingest.run([*base, "legacy-intraday", "--date", "2026-11-12", "--legacy-data", str(legacy_root)]) == 0
    assert "parts_unchanged: 2" in capsys.readouterr().out


def test_compaction_of_loose_ext_parts_does_not_double_count(conn, lake, legacy_root, clock):
    """Loose parts imported first, then the recorder compacts them into quotes_ext.parquet: the dataset holds each row once."""
    rows = [T.ext_row("hf_CHA50CFD", "future", 12800.64), T.ext_row("fx_susdcnh", "fx", 6.6736)]
    T.write_ext(legacy_root, rows[:1], name="part-0001.parquet")
    T.write_ext(legacy_root, rows[1:], name="part-0002.parquet")
    svc = _svc(conn, lake, legacy_root, clock)
    svc.import_ext(T.DAY)
    first = svc.latest_ready(B.DATASET_EXT, T.DAY)
    assert first["coverage_document"]["parts"].keys() == {"part-0001.parquet", "part-0002.parquet"} or len(first["coverage_document"]["parts"]) == 2
    T.write_ext(legacy_root, rows, name="quotes_ext.parquet")              # compaction (loose files may stay on disk)
    svc.import_ext(T.DAY)
    man = svc.latest_ready(B.DATASET_EXT, T.DAY)
    assert list(man["coverage_document"]["parts"]) == ["quotes_ext.parquet"]
    assert lake.read_snapshot_frames(man["content_hash"]).height == 2


def test_compacted_quotes_after_the_close_replace_the_parts_once(conn, lake, legacy_root, clock):
    """After the close the old recorder writes intraday/<d>/quotes.parquet and hot.parquet and empties the part folders
    (seen on the server 2026-11-13). Parts imported during the session leave the dataset; every row is held once."""
    q, h = T.scenario_rows()
    qp = T.write_quotes(legacy_root, "quotes", "part-0001.parquet", q)
    hp = T.write_quotes(legacy_root, "hot", "part-0001.parquet", h)
    svc = _svc(conn, lake, legacy_root, clock)
    svc.import_intraday(T.DAY)
    assert svc.latest_ready(B.DATASET_QUOTES, T.DAY)["row_count"] == len(q) + len(h)
    day_dir = legacy_root / "intraday" / T.DAY.isoformat()
    qp.replace(day_dir / "quotes.parquet")
    hp.replace(day_dir / "hot.parquet")
    svc.import_intraday(T.DAY)
    man = svc.latest_ready(B.DATASET_QUOTES, T.DAY)
    assert sorted(man["coverage_document"]["parts"]) == ["hot.parquet", "quotes.parquet"]
    assert man["row_count"] == len(q) + len(h) and lake.read_snapshot_frames(man["content_hash"]).height == len(q) + len(h)


def test_a_new_seal_policy_recomputes_the_same_input(conn, lake, legacy_root, clock, monkeypatch):
    """Facts computed under an older policy are recomputed from the same snapshots when the policy version changes."""
    from pulse.domain.market_state import seal as S
    _seed(legacy_root)
    svc = _svc(conn, lake, legacy_root, clock)
    svc.import_intraday(T.DAY)
    monkeypatch.setattr(S, "POLICY_VERSION", "seal-facts-v0")
    svc.publish_seal_facts(T.DAY)
    monkeypatch.undo()
    assert "facts_unchanged" not in svc.publish_seal_facts(T.DAY).counts
    assert svc.latest_ready(B.DATASET_SEAL, T.DAY)["coverage_document"]["policy_version"] == S.POLICY_VERSION
    assert "facts_unchanged" in svc.publish_seal_facts(T.DAY).counts
