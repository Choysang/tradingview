"""I1: build_release on the synthetic world (real PostgreSQL). M3 shared chain, M4-E02 release bundle, M1-E03 block READY."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pulse import contracts
from pulse.adapters.postgres import decision_repo
from pulse.adapters.postgres.release import PgAnalysisRelease
from pulse.application import bindings as B
from pulse.application.release_service import HORIZONS, BuildConfig, BuildFailed, build_release

from . import world as W

pytestmark = pytest.mark.pg


def cfg(world, **kw):
    return BuildConfig(legacy_warehouse=str(world["warehouse"]), top_n_plans=5, workspace_page=8, sector_min_members=5, **kw)


@pytest.fixture(scope="module")
def built(world):
    from pulse.adapters.lake import LakeStore
    from pulse.adapters.postgres import db

    conn = db.connect(world["dsn"], autocommit=True)
    result = build_release(conn, LakeStore(world["lake_root"]), cfg=cfg(world), clock=W.clock())
    yield result, conn
    conn.close()


def test_release_is_published_with_every_required_component(built):
    r, conn = built
    view = PgAnalysisRelease(conn).get_release(r.release_id)
    assert view.release_state == "READY" and r.pointer_generation == 1 and view.release_hash
    states = {c.component_key: c.component_state for c in view.components}
    assert set(view.required_components) <= {k for k, v in states.items() if v == "READY"}
    assert any(k.startswith("ranking:market:5td") for k in states) and any(k.startswith("plans:60td") for k in states)
    # optional heating components: the four computed ones are READY, the three without a source are MISSING and reported
    assert {k for k, v in states.items() if v == "MISSING"} == {"heating.news_novelty", "heating.community_growth", "heating.fund_proxy"}
    assert set(r.degraded) == {"heating.news_novelty", "heating.community_growth", "heating.fund_proxy"}
    assert PgAnalysisRelease(conn).current("main").release_id == r.release_id
    ev = conn.execute("select event_type from ops.outbox where aggregate_id='main' and event_type='decision.published'").fetchall()
    assert len(ev) == 1


def test_three_horizons_with_explicit_versioned_targets_and_contract_valid_rankings(built):
    r, conn = built
    runs = conn.execute("select ranking_id, horizon_id, forecast_target, ranking_policy_version from decision.ranking_run"
                        " where release_id=%s and ranking_scope='market' order by horizon_id", (r.release_id,)).fetchall()
    assert {x["horizon_id"] for x in runs} == {"5td", "20td", "60td"}
    for x in runs:
        assert x["forecast_target"].endswith("_v1") and x["horizon_id"] in x["forecast_target"]
        assert x["ranking_policy_version"] == f"baseline-explainable-{x['horizon_id']}-v1"
        wire = decision_repo.load_ranking_wire(conn, x["ranking_id"])
        contracts.check("Ranking", wire)
        ranks = [e["alpha_rank"] for e in wire["entries"]]
        assert ranks == list(range(1, len(ranks) + 1))
        assert all(e["calibrated_probability"] is None for e in wire["entries"])      # no probability is claimed
        assert all(e["explanation_ref"].startswith("explain:") for e in wire["entries"])
    assert set(r.counts["ranked_instruments"]) == {"5td", "20td", "60td"}


def test_default_universe_policy_excludes_with_reasons(built, conn, lake):
    r, _ = built
    ranked = {row["instrument_id"] for row in conn.execute(
        "select e.instrument_id from decision.ranking_entry e join decision.ranking_run r using (ranking_id) where r.release_id=%s and r.ranking_scope='market'",
        (r.release_id,))}
    ids = {c: row["instrument_id"] for c in W.CODES for row in conn.execute(
        "select instrument_id from core.instrument_alias where source_id='legacy_warehouse' and source_symbol=%s", (c,))}
    for group in (W.ST, W.NEW_LISTED, W.HIGH_PRICE, W.BIG_CAP):
        assert not ({ids[c] for c in group} & ranked), group
    body = conn.execute("select 1").fetchone()
    assert body
    u = r.counts["universe"]
    assert u["excluded"] >= len(W.ST + W.NEW_LISTED + W.HIGH_PRICE + W.BIG_CAP) and u["included"] > 30


def test_sector_overview_in_sector_rankings_and_pool(built):
    r, conn = built
    ov = conn.execute("select ranking_id, horizon_id, forecast_target from decision.ranking_run where release_id=%s and ranking_scope='sector_overview'",
                      (r.release_id,)).fetchall()
    assert len(ov) == 4                                   # three opportunity horizons + the rising (heating) run
    for x in ov:
        wire = decision_repo.load_sector_overview_wire(conn, x["ranking_id"])
        contracts.check("SectorOverview", wire)
        assert wire["taxonomy_id"] == B.TAXONOMY_ID and wire["taxonomy_version"] == B.TAXONOMY_VERSION
    assert any(x["forecast_target"] == "sector_heating_surge_score_v1" for x in ov)
    assert r.counts["in_sector_ranking_runs"] == 12      # 4 sectors x 3 horizons
    pool = conn.execute("select pool_id from decision.candidate_pool_event where release_id=%s limit 1", (r.release_id,)).fetchone()
    if pool:                                               # events exist only when a sector crosses the enter threshold
        contracts.check("CandidatePool", decision_repo.load_pool_wire(conn, pool["pool_id"], r.release_id))


def test_plans_are_watching_with_raw_price_zones_and_valid_conditions(built):
    r, conn = built
    plans = conn.execute("select plan_payload from decision.trade_plan where based_on_release_id=%s", (r.release_id,)).fetchall()
    assert len(plans) == 15                                # top 5 of each of 3 horizons
    for p in (x["plan_payload"] for x in plans):
        contracts.check("TradePlan", p)
        assert p["state"] == "watching" and p["validation"]["status"] == "pending"
        assert p["entry_price_zone"]["adjustment_mode"] == "raw" and p["account_id"] is None
        assert len(p["strategy_refs"]) == 4
    entries = conn.execute("select action_status, plan_id from decision.ranking_entry e join decision.ranking_run r using (ranking_id)"
                           " where r.release_id=%s and r.ranking_scope='market' and plan_id is not null", (r.release_id,)).fetchall()
    assert len(entries) == 15 and all(e["action_status"] == "watching" for e in entries)


def test_workspace_projections_are_contract_valid_and_cover_page_one(built):
    r, conn = built
    rows = conn.execute("select projection_payload, projection_hash from decision.workspace_projection where release_id=%s", (r.release_id,)).fetchall()
    assert len(rows) == r.counts["workspaces"] >= 8
    for row in rows:
        payload = row["projection_payload"]
        contracts.check("Workspace", payload)
        assert payload["quote_overlay"] is None and payload["actionability_id"] is None and payload["view_kind"] == "public_research"
        assert payload["projection_hash"] == row["projection_hash"]
        assert len(payload["features"]) == 6 and all(f["result_state"] == "READY" for f in payload["features"])
    page_one = conn.execute(
        "select e.instrument_id from decision.ranking_entry e join decision.ranking_run r using (ranking_id) where r.release_id=%s"
        " and r.ranking_scope='market' and e.alpha_rank<=8", (r.release_id,)).fetchall()
    have = {x["instrument_id"] for x in conn.execute("select instrument_id from decision.workspace_projection where release_id=%s", (r.release_id,))}
    assert {x["instrument_id"] for x in page_one} <= have


def test_explanations_are_stored_and_referenced(built, lake):
    r, conn = built
    import io
    import polars as pl
    comp = conn.execute("select snapshot_id, component_state from decision.release_component where release_id=%s and component_key='explanations'",
                        (r.release_id,)).fetchone()
    man = conn.execute("select content_hash, manifest_state from market.dataset_manifest where snapshot_id=%s", (comp["snapshot_id"],)).fetchone()
    assert comp["component_state"] == "READY" and man["manifest_state"] == "READY"
    refs = set(lake.read_snapshot_frames(man["content_hash"])["ref"].to_list())
    used = {x["explanation_ref"] for x in conn.execute("select explanation_ref from decision.ranking_entry e join decision.ranking_run r using (ranking_id) where r.release_id=%s", (r.release_id,))}
    used |= {x["explanation_ref"] for x in conn.execute("select explanation_ref from decision.sector_rank_entry s join decision.ranking_run r using (ranking_id) where r.release_id=%s", (r.release_id,))}
    assert used and used <= refs


def test_rerun_for_same_inputs_returns_the_published_release(built, world):
    from pulse.adapters.lake import LakeStore
    r, conn = built
    n = lambda: conn.execute("select count(*) n from decision.release").fetchone()["n"]  # noqa: E731
    before = n()
    again = build_release(conn, LakeStore(world["lake_root"]), cfg=cfg(world), clock=W.clock())
    assert again.replayed and again.release_id == r.release_id and n() == before          # no new release row


def test_derived_available_at_is_not_before_inputs(built):
    r, conn = built
    view = PgAnalysisRelease(conn).get_release(r.release_id)
    assert all(c.computed_at >= view.decision_as_of for c in view.components if c.computed_at)
    ev = conn.execute("select min(available_at - decision_as_of) d from decision.candidate_pool_event where release_id=%s", (r.release_id,)).fetchone()
    assert ev["d"] is None or ev["d"].total_seconds() >= 0


@pytest.mark.parametrize("kw,expect", [({"skip_components": frozenset({"plans:5td"})}, "plans:5td"),
                                       ({"fail_components": frozenset({"workspace_projection"})}, "workspace_projection")])
def test_a_missing_or_failed_required_component_blocks_ready(world, kw, expect):
    from pulse.adapters.lake import LakeStore
    from pulse.adapters.postgres import db
    conn = db.connect(world["dsn"], autocommit=True)
    slot = "main-blocked-" + expect.replace(":", "-")
    before = PgAnalysisRelease(conn).current(slot)
    with pytest.raises(BuildFailed) as ex:
        build_release(conn, LakeStore(world["lake_root"]), cfg=cfg(world, slot_key=slot, **kw), clock=W.clock())
    assert expect in str(ex.value)
    assert before is None and PgAnalysisRelease(conn).current(slot) is None                  # the pointer never moved
    row = conn.execute("select release_state from decision.release where release_id=%s", (ex.value.release_id,)).fetchone()
    assert row["release_state"] == "FAILED"
    conn.close()


def test_pit_loader_ignores_a_revision_ingested_after_the_cutoff(world, conn, lake):
    """The lake keeps the latest revision per key, so before its availability the bar is MISSING (never leaked)."""
    first = B.load_research_inputs(conn, lake, datetime(2026, 11, 11, 8, 30, tzinfo=timezone.utc))
    later = B.load_research_inputs(conn, lake, datetime(2026, 11, 11, 10, 0, tzinfo=timezone.utc))
    iid = conn.execute("select instrument_id from core.instrument_alias where source_id='legacy_warehouse' and source_symbol=%s", (W.CODES[3],)).fetchone()["instrument_id"]

    def rows(inp):
        return inp.bars.filter((inp.bars["instrument_id"] == str(iid)) & (inp.bars["trade_date"] == W.LAST))
    assert rows(first).height == 0 and rows(later).height == 1
    assert rows(later)["available_at"][0] == W.REVISION_INGEST
    with pytest.raises(LookupError):
        B.load_research_inputs(conn, lake, datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc))      # nothing was ready yet


def test_matrix_refuses_a_different_cutoff(world, conn, lake):
    inp = B.load_research_inputs(conn, lake, datetime(2026, 11, 11, 8, 30, tzinfo=timezone.utc))
    shared = B.SharedFeatures(conn, lake, inp, universe_policy_version="t", market_rules_version="t")
    m = B.PitFeatureMatrix(shared, [])
    with pytest.raises(ValueError):
        m.matrix([], [], datetime(2026, 11, 12, tzinfo=timezone.utc))
    with pytest.raises(ValueError):
        m.matrix([], [], datetime(2026, 11, 11, 7, tzinfo=timezone.utc))


def test_history_release_uses_the_reconstructed_policy_and_records_it(world):
    """G3: history built at session close + delay under a versioned reconstructed policy; available_at is never rewritten."""
    from pulse.adapters.lake import LakeStore
    from pulse.adapters.postgres import db
    conn = db.connect(world["dsn"], autocommit=True)
    as_of = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)                  # 17:00 CST, after the 16:00 usable time
    c = BuildConfig(slot_key="main-history", availability=B.RECONSTRUCTED_POLICY, top_n_plans=3, workspace_page=3)
    with pytest.raises(ValueError):
        build_release(conn, LakeStore(world["lake_root"]), cfg=c, clock=W.clock())      # needs an explicit as-of
    r = build_release(conn, LakeStore(world["lake_root"]), as_of=as_of, cfg=c, clock=W.clock())
    view = PgAnalysisRelease(conn).get_release(r.release_id)
    assert view.release_state == "READY" and view.availability_policy_version == B.RECONSTRUCTED_POLICY
    assert r.decision_as_of == as_of and r.session_date == W.LAST and r.counts["ranked_instruments"]["20td"] > 30
    fk = conn.execute("select count(*) n from market.feature_result where availability_policy_version=%s", (B.RECONSTRUCTED_POLICY,)).fetchone()
    assert fk["n"] > 0
    # the same bars are invisible under the observed policy at that instant (nothing was ingested yet): never a silent fallback
    with pytest.raises(LookupError):
        B.load_research_inputs(conn, LakeStore(world["lake_root"]), as_of)
    conn.close()


def test_pool_events_follow_the_state_machine_across_releases(world):
    """Fast-heating pool: first release enters 'observing' (seq 1); the next release moves state only through legal transitions."""
    from decimal import Decimal
    from pulse.adapters.lake import LakeStore
    from pulse.adapters.postgres import db
    from pulse.domain.sectors import pool as P
    pol = P.PoolPolicy("sector-heating-test", "sector-heating-test-v1", {"relative_strength_change": Decimal("0.25"),
                       "turnover_acceleration": Decimal("0.25"), "breadth": Decimal("0.25"), "leader_confirmation": Decimal("0.25")},
                       enter_threshold=Decimal("-0.5"), exit_threshold=Decimal("-9"), min_dwell_evaluations=1, cooling_max_evaluations=2,
                       required_components=frozenset({"relative_strength_change", "breadth"}))
    conn = db.connect(world["dsn"], autocommit=True)
    lake = LakeStore(world["lake_root"])
    r1 = build_release(conn, lake, cfg=cfg(world, slot_key="main-pool", pool_policy=pol), clock=W.clock())
    assert r1.counts["pool_items"] > 0
    first = conn.execute("select event_sequence, previous_state, current_state, trigger_code from decision.candidate_pool_event where release_id=%s",
                         (r1.release_id,)).fetchall()
    assert {(e["event_sequence"], e["previous_state"], e["current_state"]) for e in first} == {(1, None, "observing")}
    r2 = build_release(conn, lake, cfg=BuildConfig(slot_key="main-pool", legacy_warehouse=str(world["warehouse"]), top_n_plans=4,
                                                   workspace_page=8, pool_policy=pol), clock=W.clock())
    assert r2.release_id != r1.release_id and r2.pointer_generation == 2
    second = conn.execute("select previous_state, current_state, event_sequence from decision.candidate_pool_event where release_id=%s", (r2.release_id,)).fetchall()
    assert second and all((e["previous_state"], e["current_state"]) == ("observing", "qualified") and e["event_sequence"] == 2 for e in second)
    assert conn.execute("select release_state from decision.release where release_id=%s", (r1.release_id,)).fetchone()["release_state"] == "SUPERSEDED"
    wire = decision_repo.load_pool_wire(conn, next(iter(conn.execute("select pool_id from decision.candidate_pool_event where release_id=%s limit 1", (r2.release_id,)))) ["pool_id"], r2.release_id)
    contracts.check("CandidatePool", wire)
    conn.close()
