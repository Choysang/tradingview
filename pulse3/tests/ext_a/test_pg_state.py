"""Extension group A on real PostgreSQL: the optional market_state component, PEX01 / PEX09 / PEX03 reads.

Synthetic world (random walks plus exact limit-up / broken-board paths for five codes); proves wiring, persistence,
contract validity and the card acceptance items, not market behaviour.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import polars as pl
import pytest

from integration import world as W
from pulse import contracts
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db, market_repo
from pulse.adapters.postgres.release import PgAnalysisRelease
from pulse.application import account_service as A
from pulse.application import market_today_service as MT
from pulse.application.release_service import BuildConfig, build_release

pytestmark = pytest.mark.pg
SLOT = "main"
BODY = {"vitals", "thermometer", "ladder", "breadth", "growth_strip", "themes", "regime"}


@pytest.fixture(scope="module")
def release_id(built):
    """The release the slot points to now (other modules may have published later ones on the same data session)."""
    conn = built[1]
    return conn.execute("select current_release_id from decision.release_slot where slot_key=%s", (SLOT,)).fetchone()["current_release_id"]


def get_json(client, path, **params):
    r = client.get(path, params={k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in params.items() if v is not None})
    return r


def artifact(client, ref):
    r = client.get("/v1/x/a/artifact", params={"ref": ref})
    assert r.status_code == 200, r.text
    return r.json()["data"]


def panel(client, release_id, name):
    r = get_json(client, "/v1/x/query-market-today", release_id=str(release_id))
    assert r.status_code == 200, r.text
    f = next(x for x in r.json()["data"]["state"] if x["feature_id"] == f"market_state.{name}")
    return artifact(client, f["result"]["artifact"]["ref"])["body"]


# ---------------------------------------------------------------------------------------------- release component
def test_release_carries_the_optional_component_without_touching_required_ones(built):
    r, conn = built
    view = PgAnalysisRelease(conn).get_release(r.release_id)
    states = {c.component_key: c for c in view.components}
    assert view.release_state in ("READY", "SUPERSEDED")
    assert set(view.required_components) <= {k for k, c in states.items() if c.component_state == "READY"}
    assert "market_state" not in view.required_components and view.optional_components["market_state"] == "ALLOW_MISSING"
    assert states["market_state"].component_state == "READY" and states["market_state"].snapshot_id is not None
    assert {k for k, c in states.items() if c.component_state == "MISSING"} == {"heating.news_novelty", "heating.community_growth", "heating.fund_proxy"}
    assert "market_state" in r.counts and r.counts["market_state"]["panels"] == 7


def test_failed_market_state_degrades_to_missing_and_the_release_still_publishes(world, built):
    conn = db.connect(world["dsn"], autocommit=True)
    try:
        r = build_release(conn, LakeStore(world["lake_root"]), clock=W.clock(),
                          cfg=BuildConfig(slot_key="exta-failed", legacy_warehouse=str(world["warehouse"]), top_n_plans=3, workspace_page=3,
                                          fail_components=frozenset({"market_state"})))
        view = PgAnalysisRelease(conn).get_release(r.release_id)
        c = next(c for c in view.components if c.component_key == "market_state")
        assert view.release_state == "READY" and c.component_state == "MISSING" and "market_state" in r.degraded
        assert "forced_failure" in c.artifact_ref
        assert "market_state" in r.counts and "missing" in r.counts["market_state"]
    finally:
        conn.close()


def test_datasets_features_and_lineage_are_registered(built, lake):
    r, conn = built
    comp = MT.release_component(conn, r.release_id)
    today = market_repo.get_manifest(conn, comp["snapshot_id"])
    assert today["dataset_id"] == "market_today" and today["contract_id"] == "pulse.market_today" and today["manifest_state"] == "READY"
    sent_id = uuid.UUID(today["coverage_document"]["sentiment_snapshot_id"])
    sent = market_repo.get_manifest(conn, sent_id)
    assert sent["dataset_id"] == "market_sentiment" and sent["contract_id"] == "pulse.market_sentiment" and sent["manifest_state"] == "READY"
    deps = {(d["input_snapshot_id"], d["dependency_role"]) for d in conn.execute(
        "select input_snapshot_id, dependency_role from market.dataset_dependency where snapshot_id=%s", (today["snapshot_id"],))}
    assert (sent_id, "sentiment") in deps and any(role == "bars" for _, role in deps)
    df = lake.read_snapshot_frames(today["content_hash"])
    assert set(df["panel"].to_list()) == BODY
    for row in df.iter_rows(named=True):
        fr = conn.execute("select * from market.feature_result where feature_key_hash=%s", (row["feature_key_hash"],)).fetchone()
        assert fr["result_state"] == "READY" and fr["feature_id"] == f"market_state.{row['panel']}"
        contracts.check("FeatureKey", fr["full_key"])
        assert fr["full_key"]["adjustment_mode"] == "raw"                       # raw bars: never mixed with the adjusted research series
        assert len(fr["full_key"]["instrument_ids"]) == 1                       # scope uuid, not a 3,000-name list
        import io
        stored = pl.read_parquet(io.BytesIO(lake.read_object(fr["result_hash"])))
        assert stored["body"][0] == row["body"]


# ---------------------------------------------------------------------------------------------- PEX01
def test_market_today_is_contract_valid_and_carries_every_panel(client, release_id):
    r = get_json(client, "/v1/x/query-market-today", release_id=str(release_id))
    assert r.status_code == 200
    body = r.json()
    data = body["data"]
    assert {f["feature_id"].split(".", 1)[1] for f in data["state"]} == BODY
    assert data["release"]["release_id"] == str(release_id) and data["release"]["release_state"] == "READY"
    codes = {g["reason_code"] for g in data["capability_gaps"]}
    assert {"NO_INTRADAY_SEAL_TIMES", "TURNOVER_AMOUNT_ESTIMATED"} <= codes
    assert body["meta"]["release_id"] == str(release_id) and body["meta"]["input_snapshot_refs"][0]["dataset_id"] == "market_today"
    for f in data["state"]:
        contracts.check("FeatureResult", f["result"])
        assert f["result"]["artifact"]["ref"] == "feature:" + f["key_hash"]


def test_limit_facts_ladder_promotion_and_premium(client, release_id):
    v = panel(client, release_id, "vitals")
    assert (v["limit_up"], v["broken"], v["max_height"]) == (3, 1, 2)
    assert v["seal_rate_pct"] == "75"                                           # 3 / (3 + 1)
    assert Decimal(v["premium"]["close_pct"]) == Decimal("10") and v["premium"]["sample_count"] == 2
    assert v["volume"]["amount_is_estimate"] is True and v["session"] == "2026-11-10"
    assert v["coverage"]["expected_instruments"] >= v["coverage"]["observed"] > 0
    lad = panel(client, release_id, "ladder")
    rungs = {x["streak"]: x for x in lad["rungs"]}
    assert (rungs[2]["sealed_count"], rungs[1]["sealed_count"], rungs[1]["broken_count"]) == (2, 1, 1)
    assert rungs[2]["promotion"] == {"from_streak": 1, "frozen_count": 2, "promoted": 2, "missing": 0, "rate_pct": "100"}
    assert {m["code"] for m in rungs[2]["sealed"]} == {"600001", "600002"}
    assert [m["code"] for m in rungs[1]["broken"]] == ["600004"]


def test_thermometer_is_a_declared_uncalibrated_gauge(client, release_id):
    t = panel(client, release_id, "thermometer")
    assert t["calibrated"] is False and t["version"] == "mood-v1-old-site-weights-uncalibrated"
    assert t["weights"] == {"limit_ratio": "0.4", "breadth": "0.25", "height": "0.2", "seal_quality": "0.15"}
    c = {k: Decimal(v) for k, v in t["components"].items()}
    expect = (Decimal("0.4") * c["limit_ratio"] + Decimal("0.25") * c["breadth"] + Decimal("0.2") * c["height"] + Decimal("0.15") * c["seal_quality"]) * 100
    assert abs(Decimal(t["score"]) - expect) <= Decimal("0.2")                  # components are stored at 4 places
    assert t["band"]["word"] in {b["word"] for b in t["bands"]} and len(t["bands"]) == 6
    assert len(t["history"]) >= 60 and t["history"][-1]["date"] == "2026-11-10"
    assert Decimal(t["components"]["seal_quality"]) == Decimal("0.75")


def test_breadth_growth_and_themes_panels(client, release_id):
    b = panel(client, release_id, "breadth")
    assert len(b["bins"]) == 11 and sum(x["count"] for x in b["bins"]) == b["pct_valid"] == b["up"] + b["down"] + b["flat"]
    g = panel(client, release_id, "growth_strip")
    assert [x["streak"] for x in g["buckets"]] == [1, 2, 3, 4, 5, 6, 7]
    assert g["buckets"][-1]["label"] == "7连阳+"
    th = panel(client, release_id, "themes")
    assert th["calibrated"] is False and 0 < len(th["themes"]) <= 8
    strengths = [Decimal(t["strength"]) for t in th["themes"]]
    assert strengths == sorted(strengths, reverse=True)


def test_regime_timeline_has_four_dimensions_and_reports_the_fallback_index(client, release_id):
    r = panel(client, release_id, "regime")
    assert r["available"] and r["index"]["fallback"] is True and len(r["points"]) > 100
    cur = r["current"]
    assert set(r["labels"]) == {"trend", "liquidity", "sentiment", "style"} and cur["label"].count("·") == 3
    assert all(k in cur["basis"] for k in ("trend", "liquidity", "sentiment", "style"))
    assert cur["style"] == "unknown" and cur["basis"]["style"] == "missing_index"      # 000852 / 000300 pair incomplete in this world


def test_four_scenarios_of_the_baseline_have_deterministic_results():
    now = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
    fresh = now - timedelta(hours=2)
    assert MT.freshness_of("READY", fresh, now) == "current"                                         # baseline valid
    assert MT.freshness_of("SUPERSEDED", fresh, now) == "stale"                                      # replaced by a newer package
    assert MT.freshness_of("READY", fresh, now, expired_components=True) == "stale"                  # expired component
    assert MT.freshness_of("READY", now - timedelta(days=3), now) == "stale"                         # PC offline: nothing newer arrived


def test_old_package_is_labelled_stale_and_missing_component_is_a_gap_not_a_panel(client, world, release_id, monkeypatch):
    r = get_json(client, "/v1/x/query-market-today", release_id=str(release_id))
    m = r.json()["meta"]
    assert m["freshness_state"] == "current" and not any(w["code"] == "STALE_BASELINE" for w in m["warnings"])
    monkeypatch.setattr(MT, "STALE_AFTER", timedelta(seconds=0))                                         # PC offline: the baseline is now too old
    m = get_json(client, "/v1/x/query-market-today", release_id=str(release_id)).json()["meta"]
    assert m["freshness_state"] == "stale" and any(w["code"] == "STALE_BASELINE" for w in m["warnings"])
    monkeypatch.setattr(MT, "STALE_AFTER", timedelta(hours=36))
    conn = db.connect(world["dsn"], autocommit=True)
    try:
        res = build_release(conn, LakeStore(world["lake_root"]), clock=W.clock(),
                            cfg=BuildConfig(slot_key="exta-failed", legacy_warehouse=str(world["warehouse"]), top_n_plans=3, workspace_page=3,
                                            fail_components=frozenset({"market_state"})))
    finally:
        conn.close()
    r2 = get_json(client, "/v1/x/query-market-today", release_id=str(res.release_id))
    d2, m2 = r2.json()["data"], r2.json()["meta"]
    assert d2["state"] == [] and {g["reason_code"] for g in d2["capability_gaps"]} == {"COMPONENT_NOT_IN_RELEASE"}
    assert m2["quality_status"] == "partial" and any(w["code"] == "COMPONENT_NOT_IN_RELEASE" for w in m2["warnings"])
    assert d2["release"]["release_state"] == "READY"                                                  # required components untouched


def test_unknown_or_unpublished_release_and_bad_parameters(client):
    assert get_json(client, "/v1/x/query-market-today", release_id=str(uuid.uuid4())).status_code == 404
    bad = get_json(client, "/v1/x/query-market-today", release_id="not-a-uuid")
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "INVALID_CONTRACT"
    extra = client.get("/v1/x/query-market-today", params={"release_id": str(uuid.uuid4()), "bogus": "1"})
    assert extra.status_code == 400 and extra.json()["error"]["field_errors"][0]["code"] == "unknown_parameter"


def test_account_overlays_are_private_and_never_shared_between_accounts(client, world, release_id):
    conn = db.connect(world["dsn"], autocommit=True)
    try:
        a, b = uuid.uuid5(uuid.NAMESPACE_URL, "exta-acct-a"), uuid.uuid5(uuid.NAMESPACE_URL, "exta-acct-b")
        A.open_paper_account(conn, trading_day=date(2026, 11, 11), account_id=a)
        A.open_paper_account(conn, trading_day=date(2026, 11, 11), account_id=b, initial_cash="2000")
    finally:
        conn.close()
    pub = get_json(client, "/v1/x/query-market-today", release_id=str(release_id))
    assert "account" not in {o["kind"] for o in pub.json()["data"]["overlays"]} and "private" not in pub.headers.get("cache-control", "")
    ra = get_json(client, "/v1/x/query-market-today", release_id=str(release_id), account_id=str(a))
    rb = get_json(client, "/v1/x/query-market-today", release_id=str(release_id), account_id=str(b))
    oa = next(o for o in ra.json()["data"]["overlays"] if o["kind"] == "account")
    ob = next(o for o in rb.json()["data"]["overlays"] if o["kind"] == "account")
    assert oa["artifact"]["ref"] != ob["artifact"]["ref"] and oa["scope"]["scope_key"] == str(a)
    for r in (ra, rb):
        assert r.headers["cache-control"] == "private, no-store" and r.headers["vary"] == "Cookie"
    assert ra.json()["data"]["state"] == pub.json()["data"]["state"]            # the frozen content is identical; only overlays differ
    assert get_json(client, "/v1/x/query-market-today", release_id=str(release_id), account_id=str(uuid.uuid4())).status_code == 404


# ---------------------------------------------------------------------------------------------- PEX09
def test_sentiment_denominators_match_the_overview(client, release_id):
    v = panel(client, release_id, "vitals")
    r = get_json(client, "/v1/x/query-market-sentiment", release_id=str(release_id),
                 scope={"scope_kind": "market", "scope_key": "main-board", "membership_snapshot_id": None})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert [f["feature_id"] for f in d["features"]] == ["market_sentiment.limit_board", "market_sentiment.mood", "market_sentiment.promotion"]
    bodies = {f["feature_id"].split(".")[1]: artifact(client, f["result"]["artifact"]["ref"])["body"] for f in d["features"]}
    lb = bodies["limit_board"]
    assert (lb["limit_up"], lb["broken"], lb["max_height"], lb["seal_rate_pct"]) == (v["limit_up"], v["broken"], v["max_height"], v["seal_rate_pct"])
    promo = bodies["promotion"]
    assert promo["rungs"] == [{"from_streak": 1, "frozen_count": 2, "promoted": 2, "missing": 0, "rate_pct": "100"}] and promo["missing_countable"] is True
    assert d["coverage"]["observed_count"] == v["coverage"]["observed"] and d["coverage"]["expected_count"] == v["coverage"]["expected_instruments"]
    assert d["artifact"]["ref"].startswith("dataset:market_sentiment:") and len(d["artifact"]["content_hash"]) == 64
    assert bodies["mood"]["calibrated"] is False


def test_sector_scope_sentiment_and_unsupported_scopes(client, release_id):
    r = get_json(client, "/v1/x/query-market-sentiment", release_id=str(release_id),
                 scope={"scope_kind": "sector", "scope_key": "legacy_boards_industry/bk_a", "membership_snapshot_id": None})
    assert r.status_code == 200
    lb = artifact(client, next(f for f in r.json()["data"]["features"] if f["feature_id"].endswith("limit_board"))["result"]["artifact"]["ref"])["body"]
    assert lb["coverage"]["expected_instruments"] == 16 or lb["coverage"]["expected_instruments"] > 0
    promo = artifact(client, next(f for f in r.json()["data"]["features"] if f["feature_id"].endswith("promotion"))["result"]["artifact"]["ref"])["body"]
    assert promo["missing_countable"] is False                                    # a sector cannot count members without a bar as missing
    inst = get_json(client, "/v1/x/query-market-sentiment", release_id=str(release_id),
                    scope={"scope_kind": "instrument", "scope_key": str(uuid.uuid4()), "membership_snapshot_id": None})
    assert inst.status_code == 422 and inst.json()["error"]["code"] == "UNSUPPORTED_CONTEXT_SHAPE"
    gone = get_json(client, "/v1/x/query-market-sentiment", release_id=str(release_id),
                    scope={"scope_kind": "sector", "scope_key": "legacy_boards_industry/nope", "membership_snapshot_id": None})
    assert gone.status_code == 404


# ---------------------------------------------------------------------------------------------- PEX03
SECTOR = {"taxonomy_id": "legacy_boards_industry", "taxonomy_version": 1, "node_id": "bk_a"}
TARGET = "rel_return_20td_vs_universe_median_after_declared_costs_v1"


def test_sector_details_use_one_membership_for_members_ranking_and_pool(client, release_id, conn):
    r = get_json(client, "/v1/x/query-sector-details", sector=SECTOR, release_id=str(release_id), horizon_id="20td", forecast_target=TARGET)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    members = {m["instrument_id"] for m in d["members"]}
    assert d["sector"] == SECTOR and len(members) >= 5 and all(m["role_feature"] is None for m in d["members"])
    rk = d["ranking"]
    assert rk["ranking_scope"] == "sector" and rk["taxonomy_node"] == SECTOR and {e["instrument_id"] for e in rk["entries"]} <= members
    snap = conn.execute("select membership_snapshot_id from decision.ranking_run where ranking_id=%s", (rk["ranking_id"],)).fetchone()["membership_snapshot_id"]
    assert rk["membership_snapshot_id"] == str(snap)
    for pool in d["candidate_pools"]:
        assert {i["instrument_id"] for i in pool["items"]} <= members
        assert all(i["taxonomy_node"] == SECTOR and i["membership_snapshot_id"] == rk["membership_snapshot_id"] for i in pool["items"])
    cov = r.json()["meta"]["coverage"]
    assert cov["expected_count"] == len(members) and cov["observed_count"] == len(rk["entries"])


def test_sector_without_a_ranking_is_reported_with_coverage_not_widened(client, release_id):
    r = get_json(client, "/v1/x/query-sector-details", sector=SECTOR, release_id=str(release_id), horizon_id="20td",
                 forecast_target="some_other_target_v1")
    assert r.status_code == 200
    d, m = r.json()["data"], r.json()["meta"]
    assert d["ranking"]["entries"] == [] and d["ranking"]["ranking_policy_version"].startswith("not-ranked")
    assert m["coverage"]["observed_count"] == 0 and m["coverage"]["missing_count"] == len(d["members"])
    assert m["coverage"]["unverifiable_reasons"][0].startswith("SECTOR_RANKING_NOT_PRODUCED") and m["quality_status"] == "partial"
    assert len(d["members"]) >= 5                                                # the member list itself is not enlarged or shrunk


def test_other_taxonomy_versions_are_addressed_separately(client, release_id):
    r = get_json(client, "/v1/x/query-sector-details", sector={**SECTOR, "taxonomy_version": 2}, release_id=str(release_id),
                 horizon_id="20td", forecast_target=TARGET)
    assert r.status_code == 404
    r = get_json(client, "/v1/x/query-sector-details", sector={**SECTOR, "node_id": "nope"}, release_id=str(release_id),
                 horizon_id="20td", forecast_target=TARGET)
    assert r.status_code == 404


def test_extension_registry_has_every_served_path(conn):
    ops = {r["operation_id"] for r in conn.execute("select operation_id from core.api_binding")}
    assert {"queryMarketToday", "queryRankingHistory", "queryRankingTrackRecord", "querySectorDetails", "queryMarketSentiment",
            "streamRegisteredUpdates", "readArtifactA", "queryMarketTape", "streamMarketTape"} <= ops
    cv = {r["contract_id"] for r in conn.execute("select contract_id from core.contract_version")}
    assert {"pulse.market_today", "pulse.market_sentiment", "pulse.ranking_outcome", "pulse.market_tape"} <= cv
