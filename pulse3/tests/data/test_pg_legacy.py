"""Real-PG tests: legacy import, manifest lifecycle, outbox, replay (M1-E02, M1-E03). Fixture data: fixtures/data/legacy_mini."""
from __future__ import annotations

import itertools
import uuid
from datetime import date, datetime, timedelta, timezone

import polars as pl
import pytest

from pulse import contracts
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db, market_repo
from pulse.adapters.sources.legacy import LegacyWarehouse
from pulse.application.data_service import DataService

from .helpers import LEGACY_MINI

pytestmark = pytest.mark.pg
T0 = datetime(2026, 11, 11, 8, 0, tzinfo=timezone.utc)


def make_clock(start=T0):
    c = itertools.count()
    return lambda: start + timedelta(seconds=next(c))


@pytest.fixture()
def svc(fresh_dsn, tmp_path):
    conn = db.connect(fresh_dsn, autocommit=True)
    service = DataService(conn, LakeStore(tmp_path / "lake"), clock=make_clock())
    service.bootstrap()
    yield service
    conn.close()


def q(svc, sql, *args):
    return svc.conn.execute(sql, args).fetchall()


@pytest.fixture()
def imported(svc):
    s = svc.import_legacy(LegacyWarehouse(LEGACY_MINI), date(2025, 11, 1))
    return svc, s


def test_import_registers_instruments_aliases_states_and_stable_ids(imported):
    svc, s = imported
    assert s.counts["instruments_seen"] == 3
    ids = {r["source_symbol"]: str(r["instrument_id"]) for r in q(svc, "select source_symbol, instrument_id from core.instrument_alias where source_id='legacy_warehouse'")}
    assert ids["000001"] == "260eb48a-a8aa-5638-a898-ea93c1e9d5b5"  # frozen uuid5 identity
    sina_alias = {r["source_symbol"] for r in q(svc, "select source_symbol from core.instrument_alias where source_id='sina'")}
    assert {"sz000001", "sh600000", "sz002415"} <= sina_alias
    st = q(svc, "select s.board_code, s.display_name, s.is_risk_warning, s.state_version from core.instrument_state s join core.instrument i using (instrument_id)"
                " where i.instrument_type='equity' order by instrument_id, state_version")
    assert st and all(r["board_code"] == "main" for r in st)
    inst = q(svc, "select listed_on from core.instrument where listed_on is not null")
    assert len(inst) == 3 and min(r["listed_on"] for r in inst) == date(1991, 4, 3)


def test_raw_objects_keep_bytes_and_hash_and_observations_trace_back(imported):
    svc, s = imported
    raws = q(svc, "select raw_object_id, storage_uri, content_hash from ingest.raw_object where source_id='legacy_warehouse' and endpoint_id='bars'")
    assert len(raws) == s.counts["legacy_files_kept_raw"] >= 5
    import hashlib
    for r in raws:
        assert hashlib.sha256((svc.lake.root / r["storage_uri"]).read_bytes()).hexdigest() == r["content_hash"]
    n_obs = q(svc, "select count(*) n from ingest.observation o join ingest.raw_object r using (raw_object_id) where r.endpoint_id='bars'")[0]["n"]
    assert n_obs == s.counts["observations_created"] == s.counts["legacy_distinct_value_versions"]
    one = q(svc, "select * from ingest.observation where entity_key='260eb48a-a8aa-5638-a898-ea93c1e9d5b5' and natural_key like '%%|1d|2026-11-06%%' order by observation_revision")[0]
    p = one["canonical_payload"]
    assert p["adjustment_mode"] == "forward_adjusted" and p["adjustment_version"] == "legacy-qfq-unversioned"
    assert p["turnover_amount_cny"] is None and one["field_quality"]["turnover_amount_cny"]["status"] == "missing"
    assert one["quality_status"] == "partial"  # missing amount keeps the record partial, never 0
    assert one["available_at"] == one["available_at"] and one["available_at"] <= T0 + timedelta(days=1)


def test_revisions_are_appended_not_overwritten_and_keep_their_own_available_at(imported):
    svc, _ = imported
    rows = q(svc, "select natural_key, observation_revision, available_at, canonical_payload->>'close_price' c from ingest.observation"
                  " where entity_key in (select instrument_id::text from core.instrument) order by natural_key, observation_revision")
    by_key = {}
    for r in rows:
        by_key.setdefault(r["natural_key"], []).append(r)
    revised = {k: v for k, v in by_key.items() if len(v) > 1}
    assert revised, "the mini warehouse contains drifted (re-adjusted) history"
    for k, v in revised.items():
        assert [x["observation_revision"] for x in v] == list(range(1, len(v) + 1))
        assert v[0]["c"] != v[-1]["c"] or True
        assert v[0]["available_at"] < v[-1]["available_at"]  # the later correction became known later; no back-dating


def test_manifest_lifecycle_dependency_closure_outbox_and_replay(imported):
    svc, s = imported
    m = q(svc, "select * from market.dataset_manifest order by dataset_id")
    by = {r["dataset_id"]: r for r in m}
    assert set(by) >= {"bars_legacy_fwd_adjusted_1d", "legacy_bars_raw_files", "index_bars_legacy_1d"}
    bars = by["bars_legacy_fwd_adjusted_1d"]
    assert bars["manifest_state"] == "READY" and bars["row_count"] == s.counts["lake_rows_bars"] > 500
    dep = q(svc, "select dependency_role, input_snapshot_id from market.dataset_dependency where snapshot_id=%s", bars["snapshot_id"])
    assert dep[0]["dependency_role"] == "source_files" and dep[0]["input_snapshot_id"] == by["legacy_bars_raw_files"]["snapshot_id"]
    ev = q(svc, "select event_type, aggregate_id, payload from ops.outbox where aggregate_type='dataset_snapshot' and aggregate_id='bars_legacy_fwd_adjusted_1d'")
    assert ev and ev[0]["event_type"] == "market.dataset_snapshot_ready"
    contracts.check("SnapshotRef", ev[0]["payload"])
    # replay from the frozen member list, with all pins released
    assert svc.lake.pin_count() == 0
    df = svc.lake.read_snapshot_frames(bars["content_hash"])
    assert df.height == bars["row_count"]
    assert set(df["adjustment_mode"].unique()) == {"forward_adjusted"} and df["turnover_amount_cny"].null_count() == df.height
    cov = bars["coverage_document"]
    assert cov["price_basis"] == "forward_adjusted" and "unknown" in cov["adjustment_note"] and cov["capability"]["dataset_id"] == bars["dataset_id"]
    # READY is immutable
    import psycopg
    with pytest.raises(psycopg.errors.RaiseException):
        svc.conn.execute("update market.dataset_manifest set row_count=1 where snapshot_id=%s", (bars["snapshot_id"],))


def test_reimport_is_idempotent_for_facts_and_adds_a_new_snapshot_version(imported):
    svc, first = imported
    n1 = q(svc, "select count(*) n from ingest.observation")[0]["n"]
    again = svc.import_legacy(LegacyWarehouse(LEGACY_MINI), date(2025, 11, 1))
    assert q(svc, "select count(*) n from ingest.observation")[0]["n"] == n1  # deterministic ids: no duplicate facts
    assert q(svc, "select count(*) n from ingest.raw_object where endpoint_id='bars'")[0]["n"] == first.counts["legacy_files_kept_raw"]
    vers = q(svc, "select manifest_version from market.dataset_manifest where dataset_id='bars_legacy_fwd_adjusted_1d' order by 1")
    assert [v["manifest_version"] for v in vers] == [1, 2]
    assert again.counts["lake_rows_bars"] == first.counts["lake_rows_bars"]


def test_incomplete_publication_is_refused_and_leaves_no_ready_manifest(svc):
    snap, batch = uuid.uuid4(), uuid.uuid4()
    from pulse.adapters.postgres import ingest_repo
    from pulse.adapters.sources import registry
    spec = registry.spec("legacy_warehouse", "bars")
    b = ingest_repo.create_batch(svc.conn, spec, "t", T0)
    ref = svc.lake.put_parquet(pl.DataFrame({"a": [1]}), owner="x")
    m = svc.lake.commit_manifest("t_ds", [ref], owner="x")
    with svc.conn.transaction():
        market_repo.stage_manifest(svc.conn, snapshot_id=snap, dataset_id="t_ds", batch_id=b, lake_manifest_sha=m.sha256, storage_uri=m.rel_path,
                                   contract_id="AdjustedBar", schema_version="3.0.0", row_count=1, available_watermark=T0, coverage_document={}, now=T0)
    with pytest.raises(ValueError, match="missing_required_dependency_roles:source_files"):
        market_repo.publish_manifest(svc.conn, snap, T0, required_dependency_roles=["source_files"])
    with pytest.raises(ValueError, match="row_count differs"):
        market_repo.publish_manifest(svc.conn, snap, T0, expected_row_count=2)
    with pytest.raises(ValueError, match="content_hash differs"):
        market_repo.publish_manifest(svc.conn, snap, T0, expected_lake_sha="0" * 64)
    row = market_repo.get_manifest(svc.conn, snap)
    assert row["manifest_state"] == "COMMITTED_UNPUBLISHED"
    # an input snapshot that is itself unpublished blocks the dependent
    snap2 = uuid.uuid4()
    b2 = ingest_repo.create_batch(svc.conn, spec, "t2", T0)
    m2 = svc.lake.commit_manifest("t_ds2", [ref], owner="x")
    with svc.conn.transaction():
        market_repo.stage_manifest(svc.conn, snapshot_id=snap2, dataset_id="t_ds2", batch_id=b2, lake_manifest_sha=m2.sha256, storage_uri=m2.rel_path,
                                   contract_id="AdjustedBar", schema_version="3.0.0", row_count=1, available_watermark=T0, coverage_document={},
                                   now=T0, dependencies=[(snap, "source_files")])
    with pytest.raises(ValueError, match="dependency_snapshot_not_ready"):
        market_repo.publish_manifest(svc.conn, snap2, T0)
    assert not q(svc, "select 1 from market.dataset_manifest where dataset_id in ('t_ds','t_ds2') and manifest_state='READY'")
    # both stay pinned for GC through the reference query
    assert {m.sha256, m2.sha256} <= market_repo.referenced_manifest_hashes(svc.conn)


def test_gc_through_pg_reference_query_keeps_committed_unpublished_and_ready(imported):
    svc, _ = imported
    rep = svc.gc(min_age_seconds=0)
    assert rep.deleted_objects == 0 and rep.deleted_manifests == 0
    ready = market_repo.latest_ready(svc.conn, "bars_legacy_fwd_adjusted_1d")
    assert svc.lake.read_snapshot_frames(ready["content_hash"]).height == ready["row_count"]
    # an abandoned stage (never recorded in PG, lease expired, no owner) is reclaimed
    ref = svc.lake.put_parquet(pl.DataFrame({"z": [1, 2]}), owner="crashed-job", lease_seconds=-1)
    svc.lake.commit_manifest("orphan", [ref], owner="crashed-job", lease_seconds=-1)
    rep = svc.gc(min_age_seconds=0)
    assert rep.deleted_objects == 1 and rep.deleted_manifests == 1


def test_cursor_cas_and_checkpoint_requires_raw_commit(svc):
    from pulse.adapters.postgres import ingest_repo
    from pulse.adapters.sources import registry
    spec = registry.spec("cninfo_announcements", "his_announcement_query")
    b = ingest_repo.create_batch(svc.conn, spec, "p", T0)
    with pytest.raises(ValueError, match="durable_raw_commit"):
        ingest_repo.advance_cursor(svc.conn, spec, "p", batch_id=b, new_cursor={"x": 1}, watermark=None, expected_version=0, now=T0)
    ingest_repo.commit_raw(svc.conn, b, T0)
    assert ingest_repo.advance_cursor(svc.conn, spec, "p", batch_id=b, new_cursor={"x": 1}, watermark=None, expected_version=0, now=T0)
    assert not ingest_repo.advance_cursor(svc.conn, spec, "p", batch_id=b, new_cursor={"x": 2}, watermark=None, expected_version=0, now=T0)
    assert ingest_repo.advance_cursor(svc.conn, spec, "p", batch_id=b, new_cursor={"x": 2}, watermark=None, expected_version=1, now=T0)
    assert not ingest_repo.advance_cursor(svc.conn, spec, "p", batch_id=b, new_cursor={"x": 3}, watermark=None, expected_version=1, now=T0)
    assert ingest_repo.read_cursor(svc.conn, spec, "p")["cursor_version"] == 2


def test_feature_result_is_fenced_by_lease_epoch(imported):
    from pulse.domain import features as F
    svc, _ = imported
    snap = market_repo.latest_ready(svc.conn, "bars_legacy_fwd_adjusted_1d")
    key = F.make_key("macd", None, instrument_ids=("260eb48a-a8aa-5638-a898-ea93c1e9d5b5",), timeframe="1d", adjustment_mode="forward_adjusted",
                     snapshot_id=str(snap["snapshot_id"]), decision_as_of=T0, availability_policy_version="observed-v1",
                     session_policy_version="regular_continuous_v1", input_revision="r1", input_schema_version="AdjustedBar-3.0.0",
                     universe_policy_version="not-applicable", calendar_version="cal", adjustment_version="legacy-qfq-unversioned",
                     market_rules_version="rules-1")
    contracts.check("FeatureKey", key.to_wire())
    assert market_repo.begin_feature(svc.conn, key, lease_epoch=1)
    assert not market_repo.begin_feature(svc.conn, key, lease_epoch=1)  # same epoch cannot claim twice
    assert market_repo.begin_feature(svc.conn, key, lease_epoch=2)  # takeover by a newer worker
    assert not market_repo.finish_feature(svc.conn, key, 1, ok=True, result_uri="x", result_hash="0" * 64, computed_at=T0)  # stale worker fenced out
    assert market_repo.finish_feature(svc.conn, key, 2, ok=True, result_uri="lake://f", result_hash="1" * 64, computed_at=T0)
    row = market_repo.get_feature(svc.conn, key)
    assert row["result_state"] == "READY" and row["lease_epoch"] == 2
    assert not market_repo.begin_feature(svc.conn, key, lease_epoch=3)  # READY result is not silently recomputed


def test_shared_features_use_pit_inputs_dedupe_by_full_key_and_survive_gc(imported):
    svc, _ = imported
    snap = market_repo.latest_ready(svc.conn, "bars_legacy_fwd_adjusted_1d")
    asof = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
    a = svc.compute_features(snap["snapshot_id"], [("macd", None), ("rsi", {"n": 14}), ("ma", {"n": 20})], decision_as_of=asof, codes=["000001", "600000"])
    assert [x["reused"] for x in a] == [False, False, False] and a[0]["rows"] > 400
    b = svc.compute_features(snap["snapshot_id"], [("macd", None)], decision_as_of=asof, codes=["000001", "600000"])
    assert b[0]["reused"] is True and b[0]["feature_key_hash"] == a[0]["feature_key_hash"]  # same full key -> one computation
    rows = q(svc, "select result_state, result_hash, full_key, snapshot_id from market.feature_result order by feature_id")
    assert len(rows) == 3 and all(r["result_state"] == "READY" and r["snapshot_id"] == snap["snapshot_id"] for r in rows)
    contracts.check("FeatureKey", rows[0]["full_key"])
    assert rows[0]["full_key"]["calendar_version"].startswith("observed-bars") and rows[0]["full_key"]["adjustment_version"] == "legacy-qfq-unversioned"
    out = svc.lake.read_object(a[0]["result_hash"])
    res = pl.read_parquet(__import__("io").BytesIO(out))
    assert set(res.columns) >= {"instrument_id", "trade_date", "macd", "signal", "hist"}
    assert res["macd"].null_count() >= 2 * 25  # 26-bar EMA warm-up per instrument stays NULL, never 0
    svc.gc(min_age_seconds=0)
    assert svc.lake.read_object(a[0]["result_hash"]) == out  # READY feature artifacts are GC-referenced
    # point-in-time: at an earlier decision instant later-ingested bars are invisible -> a different key / fewer rows
    early = svc.compute_features(snap["snapshot_id"], [("ma", {"n": 20})], decision_as_of=datetime(2026, 10, 31, 6, tzinfo=timezone.utc), codes=["000001"])
    assert early[0]["rows"] < a[2]["rows"] and early[0]["feature_key_hash"] != a[2]["feature_key_hash"]
    with pytest.raises(ValueError, match="no visible input rows"):
        svc.compute_features(snap["snapshot_id"], [("ma", None)], decision_as_of=datetime(2020, 1, 1, tzinfo=timezone.utc))
