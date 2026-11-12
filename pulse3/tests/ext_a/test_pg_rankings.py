"""PEX02 on real PostgreSQL: ranking history (stable paging, real filters) and the forward track record (no hindsight)."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from integration import world as W
from pulse import contracts
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db, decision_repo
from pulse.application import bindings as B
from pulse.application import market_today_service as MT
from pulse.application.data_service import DataService
from pulse.application.release_service import BuildConfig, build_release

pytestmark = pytest.mark.pg
TARGET = "rel_return_20td_vs_universe_median_after_declared_costs_v1"
NOW = lambda: datetime.now(timezone.utc)                                                # noqa: E731
OUT = datetime(2026, 11, 20, tzinfo=timezone.utc)                                                   # after the six synthetic later sessions
END = (NOW() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")                                 # one fixed window end: the cursor binds the query


def later_main_release(world, minutes: int):
    conn = db.connect(world["dsn"], autocommit=True)
    try:
        return build_release(conn, LakeStore(world["lake_root"]), as_of=NOW(), clock=W.clock(),
                             cfg=BuildConfig(legacy_warehouse=str(world["warehouse"]), top_n_plans=3, workspace_page=3))
    finally:
        conn.close()


@pytest.fixture(scope="module")
def second(world, built):
    return later_main_release(world, 1)


@pytest.fixture(scope="module")
def forward(world, second):
    """Raw bars of six later sessions, published AFTER the rankings above were decided (their availability is the publish time)."""
    from ext_a.support import publish_forward_bars

    return publish_forward_bars(world, 6)


def hist(client, **kw):
    q = {"scope": {"scope_kind": "market", "scope_key": "market", "membership_snapshot_id": None},
         "window": {"start": "2026-01-01T00:00:00Z", "end": END},
         "horizon_id": "20td", "forecast_target": TARGET, "page": {"limit": 10, "cursor": None}, **kw}
    return client.get("/v1/x/query-ranking-history", params={k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in q.items()})


def ranking_ids(conn, release_id):
    return {r["horizon_id"]: r["ranking_id"] for r in conn.execute(
        "select ranking_id, horizon_id from decision.ranking_run where release_id=%s and ranking_scope='market'", (release_id,))}


def test_history_lists_each_published_ranking_newest_first_with_real_filters(client, built, second, conn):
    r = hist(client)
    assert r.status_code == 200, r.text
    es = r.json()["data"]["entries"]
    assert [e["release_id"] for e in es][:2] == [str(second.release_id), str(built[0].release_id)] and len(es) == 2
    assert es[0]["decision_as_of"] > es[1]["decision_as_of"] and all(e["outcome_artifact"] is None for e in es)
    assert {e["ranking_id"] for e in es} == {str(ranking_ids(conn, second.release_id)["20td"]), str(ranking_ids(conn, built[0].release_id)["20td"])}
    # the horizon / target / scope filter is real: another horizon returns its own rankings, a wrong target nothing
    h5 = hist(client, horizon_id="5td", forecast_target=TARGET.replace("20td", "5td")).json()["data"]["entries"]
    assert {e["ranking_id"] for e in h5} == {str(ranking_ids(conn, second.release_id)["5td"]), str(ranking_ids(conn, built[0].release_id)["5td"])}
    assert hist(client, forecast_target="nope_v1").json()["data"]["entries"] == []
    assert hist(client, window={"start": "2020-01-01T00:00:00Z", "end": "2020-02-01T00:00:00Z"}).json()["data"]["entries"] == []


def test_display_labels_are_not_filters_and_bad_requests_are_typed(client):
    r = hist(client, horizon_id="SHORT")
    assert r.status_code == 400 and r.json()["error"]["field_errors"][0]["code"] == "display_label"
    assert hist(client, window={"start": "2026-02-01T00:00:00Z", "end": "2026-01-01T00:00:00Z"}).status_code == 400
    assert hist(client, page={"limit": 500, "cursor": None}).status_code == 400
    assert hist(client, scope={"scope_kind": "instrument", "scope_key": "x", "membership_snapshot_id": None}).status_code == 422
    empty = hist(client, scope={"scope_kind": "account", "scope_key": str(uuid.uuid4()), "membership_snapshot_id": None})
    assert empty.status_code == 200 and empty.json()["data"]["entries"] == []                  # private scope: authorised, nothing ranked


def test_paging_is_pinned_to_the_publication_watermark(client, world, built, second):
    p1 = hist(client, page={"limit": 1, "cursor": None}).json()
    nxt = p1["data"]["page"]["next_cursor"]
    assert nxt and len(p1["data"]["entries"]) == 1 and p1["meta"]["next_cursor"] == nxt
    third = later_main_release(world, 2)                                                       # published between page 1 and page 2
    p2 = hist(client, page={"limit": 1, "cursor": nxt}).json()
    assert [e["release_id"] for e in p2["data"]["entries"]] == [str(built[0].release_id)] and p2["data"]["page"]["next_cursor"] is None
    assert p2["data"]["page"]["snapshot_hash"] == p1["data"]["page"]["snapshot_hash"]
    fresh = hist(client).json()["data"]["entries"]
    assert [e["release_id"] for e in fresh][0] == str(third.release_id) and len(fresh) == 3     # a new query sees the new ranking
    other = hist(client, horizon_id="5td", forecast_target=TARGET.replace("20td", "5td"), page={"limit": 1, "cursor": nxt})
    assert other.status_code == 409 and other.json()["error"]["code"] == "CURSOR_EXPIRED"       # never silently re-based on another query
    assert hist(client, page={"limit": 1, "cursor": "garbage"}).status_code == 409


def track(client, ids, ctx=None, as_of=None):
    return client.get("/v1/x/query-ranking-track-record", params={
        "ranking_ids": json.dumps([str(i) for i in ids]), "evaluation_context_id": str(ctx or MT.OUTCOME_CONTEXT_ID),
        "outcome_as_of": (as_of or OUT).strftime("%Y-%m-%dT%H:%M:%SZ")})


def test_unmatured_rankings_are_pending_and_nothing_is_fetched_early(client, conn, built, world):
    ids = list(ranking_ids(conn, built[0].release_id).values())
    r = track(client, ids)
    assert r.status_code == 200, r.text
    assert r.json()["data"] == {"outcomes": [], "pending_count": 3}
    svc = DataService(conn, LakeStore(world["lake_root"]))
    res = MT.evaluate_outcomes(svc, outcome_as_of=OUT)
    assert res["published"] is False and res["resolved"] == 0
    assert track(client, ids).json()["data"]["pending_count"] == 3                             # last bar is the decision session: no future bar exists


def test_context_and_ids_are_validated(client, conn, built):
    ids = list(ranking_ids(conn, built[0].release_id).values())
    bad = track(client, ids, ctx=uuid.uuid4())
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "UNSUPPORTED_CONTEXT_SHAPE"
    assert track(client, [uuid.uuid4()]).status_code == 404
    assert track(client, []).status_code == 400


def test_resolved_outcome_is_a_separate_immutable_live_forward_artifact(client, conn, world, built, forward):
    ids = ranking_ids(conn, built[0].release_id)
    before = decision_repo.load_ranking_wire(conn, ids["5td"])
    svc = DataService(conn, LakeStore(world["lake_root"]))
    res = MT.evaluate_outcomes(svc, outcome_as_of=OUT)
    assert res["published"] is True and res["resolved"] >= 1
    d = track(client, list(ids.values())).json()["data"]
    assert len(d["outcomes"]) == 1 and d["pending_count"] == 2                      # 5td matured; 20td (20 sessions) and 60td are still pending
    contracts.check("ArtifactRef", d["outcomes"][0])
    art = client.get("/v1/x/a/artifact", params={"ref": d["outcomes"][0]["ref"]}).json()["data"]
    assert art["content_hash"] == d["outcomes"][0]["content_hash"]
    body = art["body"]
    assert body["status"] == "resolved" and body["horizon_id"] == "5td" and body["record_kind"] == "live_forward"
    assert body["ranking_id"] == str(ids["5td"]) and body["decision_session"] == "2026-11-10" and body["entry_session"] == "2026-11-11"
    assert body["exit_session"] == "2026-11-17" and body["observed_through"] >= body["exit_session"]
    m = {x["name"]: x for x in body["metrics"]}
    for x in body["metrics"]:
        contracts.check("Metric", x, contracts.EXT_ID)
    assert m["top_opportunity_count"]["value"] == "10"
    assert m["top_hit_rate_over_opportunities"]["sample_count"] == m["top_opportunity_count"]["sample_count"]
    assert m["top_mean_excess_return"]["value"] is not None and m["universe_median_gross_return"]["sample_count"] > 30
    assert not any("short" in n for n in m) and "avoid_descriptive_long_only_mean_excess_return" in m          # avoid group is never a short return
    assert body["cost_ratio"] and body["method_version"] == "ranking-forward-outcome-v1"
    assert [x["alpha_rank"] for x in body["members"]] == sorted(x["alpha_rank"] for x in body["members"])
    # the original ranking is untouched and a re-run adds nothing
    assert decision_repo.load_ranking_wire(conn, ids["5td"]) == before
    assert MT.evaluate_outcomes(svc, outcome_as_of=OUT + timedelta(hours=1))["published"] is False


def test_outcome_is_invisible_to_a_cutoff_before_it_existed(client, conn, built, forward):
    ids = list(ranking_ids(conn, built[0].release_id).values())
    r = track(client, ids, as_of=datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert r.json()["data"] == {"outcomes": [], "pending_count": 3}


def test_outcome_references_resolve_through_the_same_lookup(conn, lake, built, forward):
    refs = MT.latest_outcome_refs(conn, lake, list(ranking_ids(conn, built[0].release_id).values()))
    assert len(refs) == 1 and next(iter(refs.values()))["ref"].startswith("outcome:")
