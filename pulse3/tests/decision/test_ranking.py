"""W05 ranking: universe, baseline, missing handling, trial fusion, ranks, paging (M3-E01)."""
from decimal import Decimal as D

import pytest

from pulse import contracts
from pulse.domain.ranking.baseline import FeatureSpec, RankingBlocked, deduplicate_features, score_baseline
from pulse.domain.ranking.run import (
    ActionAssessment,
    CursorMismatch,
    TrialCandidate,
    build_ranking,
    paginate,
)
from pulse.domain.ranking.universe import UniversePolicy, UserUniversePreference, apply_universe

from .helpers import facts, matrix, policy, uid


def _universe(n=12, **over):
    return apply_universe(UniversePolicy(), facts(n, **over))


def test_default_universe_excludes_with_reasons():
    f_ok = facts(1)[0]
    f_price = facts(2)[1].__class__(**{**facts(2)[1].__dict__, "instrument_id": uid("p"), "price_raw": D("100.01")})
    f_cap = f_ok.__class__(**{**f_ok.__dict__, "instrument_id": uid("c"), "circulating_market_cap_cny": D("10000000001")})
    f_new = f_ok.__class__(**{**f_ok.__dict__, "instrument_id": uid("n"), "listed_sessions": 10})
    f_chinext = f_ok.__class__(**{**f_ok.__dict__, "instrument_id": uid("x"), "board_code": "chinext"})
    f_unknown = f_ok.__class__(**{**f_ok.__dict__, "instrument_id": uid("u"), "price_raw": None})
    f_edge = f_ok.__class__(**{**f_ok.__dict__, "instrument_id": uid("e"), "price_raw": D("100")})
    res = apply_universe(UniversePolicy(), [f_ok, f_price, f_cap, f_new, f_chinext, f_unknown, f_edge])
    reasons = {e.instrument_id: e.reason_codes for e in res.exclusions}
    assert reasons[uid("p")] == ("price_above_cap",)
    assert reasons[uid("c")] == ("circulating_market_cap_above_cap",)
    assert reasons[uid("n")] == ("new_listing",)
    assert reasons[uid("x")] == ("board_not_in_universe:chinext",)
    assert reasons[uid("u")] == ("unknown_price",)  # unknown never passes, never 0
    assert {f.instrument_id for f in res.included} == {f_ok.instrument_id, uid("e")}  # <= 100 is included


def test_preference_is_versioned_separately_from_rules():
    pol = UniversePolicy(preference=UserUniversePreference("pref-2", D("30"), None))
    assert "pref-2" in pol.policy_version and pol.rules.rules_version in pol.policy_version
    res = apply_universe(pol, facts(12))
    assert all(f.price_raw <= 30 for f in res.included) and res.exclusions


def test_dedup_same_source_and_correlated():
    u = _universe()
    ids = [f.instrument_id for f in u.included]
    m = matrix(ids)
    kept, dropped = deduplicate_features(policy(), ids, m)
    assert [k.feature_id for k in kept] == ["mom20", "turnover_z", "sector_rs"]
    assert dropped[0].feature_id == "mom20_dup" and dropped[0].reason == "same_source_group"
    # a correlated pair from different source groups is also collapsed
    feats = (FeatureSpec("a", "1", 1, D("1"), "g1"), FeatureSpec("b", "1", 1, D("1"), "g2"))
    m2 = {i: {"a": D(k), "b": D(k) * 2 + 1} for k, i in enumerate(ids)}
    kept2, dropped2 = deduplicate_features(policy(features=feats), ids, m2)
    assert [k.feature_id for k in kept2] == ["a"] and dropped2[0].reason == "correlated"


def test_missing_feature_excludes_row_no_reweight():
    u = _universe()
    ids = [f.instrument_id for f in u.included]
    m = matrix(ids, {ids[3]: "turnover_z"})
    out = score_baseline(policy(), u, m)
    assert ids[3] not in out.scores
    ex = {e.instrument_id: e.reason_codes for e in out.exclusions}
    assert ex[ids[3]] == ("missing_feature:turnover_z",)
    # weights are one fixed vector for all rows, summing to 1 (dup feature dropped)
    assert sum(out.retained_weights.values()) == pytest.approx(1)
    assert set(out.retained_weights) == {"mom20", "turnover_z", "sector_rs"}
    # contributions of every scored row use exactly those weights
    for i in out.scores:
        assert set(out.contributions[i]) == set(out.retained_weights)


def test_group_normalization_and_direction():
    u = _universe(20)
    ids = [f.instrument_id for f in u.included]
    out = score_baseline(policy(), u, matrix(ids))
    for fid in out.retained_weights:
        zs = [out.normalized[i][fid] for i in out.scores]
        assert abs(sum(zs)) < D("0.001")  # cross-sectional mean ~ 0 inside the universe
    # turnover has direction -1: normalized (direction-adjusted) value is the negated z
    hi_turn = max(ids, key=lambda i: matrix(ids)[i]["turnover_z"])
    assert out.normalized[hi_turn]["turnover_z"] < 0


def test_coverage_below_policy_blocks():
    u = _universe(10)
    ids = [f.instrument_id for f in u.included]
    m = matrix(ids, {i: "sector_rs" for i in ids[:6]})
    with pytest.raises(RankingBlocked, match="coverage"):
        score_baseline(policy(min_coverage_ratio=D("0.5")), u, m)
    with pytest.raises(RankingBlocked, match="invalid"):
        score_baseline(policy(), u, matrix(ids), baseline_valid=False)


def _run(n=12, **kw):
    u = _universe(n)
    ids = [f.instrument_id for f in u.included]
    return build_ranking(
        ranking_id=uid("rk"), release_id=uid("rel"), ranking_scope="market", scope_key="cn.mainboard",
        policy=policy(), universe=u, matrix=matrix(ids), **kw,
    ), ids


def test_ranking_contract_and_rank_invariants():
    run, ids = _run()
    wire = run.to_wire()
    contracts.check("Ranking", wire)
    contracts.check("getMarketRankingResponse", {"meta": _meta(run), "data": wire})
    ranks = [e["alpha_rank"] for e in wire["entries"]]
    assert ranks == list(range(1, len(ids) + 1))
    scores = [D(e["relative_rank_score"]) for e in wire["entries"]]
    assert scores == sorted(scores, reverse=True)
    assert all(e["calibrated_probability"] is None for e in wire["entries"])  # no probability claimed
    assert all(r in run.explanations for r in (e["explanation_ref"] for e in wire["entries"]))
    # deterministic: same inputs -> byte-identical payload
    assert _run()[0].to_wire() == wire


def _meta(run):
    return {
        "request_id": str(uid("req")), "generated_at": "2026-11-10T02:00:00Z", "trace_id": None,
        "contract_version": "3.0.0", "release_id": str(run.release_id), "decision_as_of": "2026-11-10T02:00:00Z",
        "availability_policy_version": "observed-v1", "input_snapshot_refs": [], "quality_status": "valid",
        "freshness_state": "current",
        "coverage": {"policy_version": "c1", "expected_count": 12, "observed_count": 12, "missing_count": 0, "unverifiable_reasons": []},
        "warnings": [], "next_cursor": None, "actionability_id": None,
    }


def test_alpha_rank_vs_action_rank():
    u = _universe()
    ids = [f.instrument_id for f in u.included]
    plain, _ = _run()
    order = [e.instrument_id for e in plain.entries]
    # 2nd and 4th research ranks are actionable; the top one is blocked (limit-up, no counterparty)
    acts = {
        order[0]: ActionAssessment(order[0], "blocked", ("NO_COUNTERPARTY_LIMIT_UP",)),
        order[1]: ActionAssessment(order[1], "actionable", (), (uid("plan1"), 1), "actionable"),
        order[3]: ActionAssessment(order[3], "actionable", (), (uid("plan2"), 1), "actionable"),
    }
    run, _ = _run(actions=acts)
    by = {e.instrument_id: e for e in run.entries}
    assert by[order[0]].alpha_rank == 1 and by[order[0]].action_rank is None  # research rank kept
    assert by[order[1]].action_rank == 1 and by[order[3]].action_rank == 2
    assert by[order[2]].action_status == "watching"
    contracts.check("Ranking", run.to_wire())
    with pytest.raises(ValueError):
        ActionAssessment(ids[0], "actionable")  # actionable needs a plan


def test_trial_budget_fusion_and_ineligible_return_to_baseline():
    u = _universe()
    ids = [f.instrument_id for f in u.included]
    base, _ = _run()
    scores = {i: D(k) for k, i in enumerate(reversed(ids))}
    good = TrialCandidate("trial-a", D("0.04"), scores, "weighted_zscore_v1", uid("i-a"), uid("p-a"))
    gappy = TrialCandidate("trial-b", D("0.06"), {k: v for k, v in list(scores.items())[:-1]}, "weighted_zscore_v1", uid("i-b"), uid("p-b"))
    wrong_scale = TrialCandidate("trial-c", D("0"), scores, "other_scale", uid("i-c"), uid("p-c"))
    run, _ = _run(trial_candidates=[good, gappy, wrong_scale])
    w = dict(run.budget.candidate_weights)
    assert w == {"trial-a": D("0.04"), "trial-b": D("0"), "trial-c": D("0")}
    assert run.budget.baseline_weight == D("0.96")  # b's 0.06 returned to baseline, not given to a
    assert run.ineligible_candidates["trial-b"] == "incomplete_input_coverage"
    assert run.ineligible_candidates["trial-c"].startswith("score_scale_mismatch")
    first = run.entries[0]
    body = run.explanations[first.explanation_ref]
    assert set(body["component_contributions"]) == {"baseline", "trial-a"}
    assert base.entries[0].relative_rank_score != first.relative_rank_score
    contracts.check("Ranking", run.to_wire())
    # trial cap respected: 0.1 total
    with pytest.raises(Exception, match="exceeds"):
        _run(trial_candidates=[TrialCandidate("x", D("0.08"), scores, "weighted_zscore_v1", uid("i-x"), uid("p-x")),
                               TrialCandidate("y", D("0.08"), scores, "weighted_zscore_v1", uid("i-y"), uid("p-y"))])


def test_baseline_invalid_blocks_ready_even_with_candidate():
    u = _universe()
    ids = [f.instrument_id for f in u.included]
    cand = TrialCandidate("t", D("0.1"), {i: D(1) for i in ids}, "weighted_zscore_v1", uid("i-t"), uid("p-t"))
    with pytest.raises(RankingBlocked):
        build_ranking(ranking_id=uid("rk"), release_id=uid("rel"), ranking_scope="market", scope_key="s",
                      policy=policy(), universe=u, matrix=matrix(ids), baseline_valid=False, trial_candidates=[cand])


def test_pagination_stable_and_pinned():
    run, _ = _run(12)
    seen = []
    cursor = None
    while True:
        page = paginate(run, limit=5, cursor=cursor)
        seen += [e.alpha_rank for e in page.items]
        cursor = page.next_cursor
        if cursor is None:
            break
    assert seen == list(range(1, 13))
    # a cursor from another ranking/release is refused, never silently re-sorted
    other, _ = _run(12)
    other = type(other)(**{**other.__dict__, "release_id": uid("newer-release")})
    p1 = paginate(run, limit=5)
    with pytest.raises(CursorMismatch):
        paginate(other, limit=5, cursor=p1.next_cursor)
    with pytest.raises(CursorMismatch):
        paginate(run, limit=5, cursor=p1.next_cursor[:-2] + "zz")


def test_sector_ranking_requires_taxonomy_and_membership():
    u = _universe()
    ids = [f.instrument_id for f in u.included]
    with pytest.raises(ValueError):
        build_ranking(ranking_id=uid("rk2"), release_id=uid("rel"), ranking_scope="sector", scope_key="s",
                      policy=policy(), universe=u, matrix=matrix(ids))
