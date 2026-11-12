"""W09 strategy registry + research league (M3-E05, M0-E04)."""
import json
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from pulse import contracts
from pulse.domain.research.context import ContextError, EvaluationContextSpec, InputSnapshot
from pulse.domain.research.experiments import (
    BlindBudgetExceeded,
    ExperimentError,
    TrialLedger,
    complete,
    evaluation_wire,
    fail,
    new_run,
)
from pulse.domain.research.league import (
    Candidate,
    ComparisonPolicy,
    LeagueError,
    LeagueHistory,
    PairingPolicy,
    SideEvidence,
    classify_pairing,
    group_comparable,
    incomparable,
    league_wire,
    pair_round,
    round_seed,
    to_db_outcome,
)
from pulse.domain.strategy import promotion as P
from pulse.domain.strategy.canonical import canonical_hash
from pulse.domain.strategy.lifecycle import LifecycleError, LifecycleTransition, allowed_targets
from pulse.domain.strategy.seed import seed_factors, seed_strategies
from pulse.domain.strategy.wire import strategy_list_wire

from .helpers import T0, uid
from .test_ranking import _meta

CAT = Path(__file__).resolve().parents[2] / "spec_package" / "catalogs"


def load(name):
    return json.loads((CAT / name).read_text(encoding="utf-8"))


# ------------------------------------------------------------------ seed


def test_seed_catalogs_are_candidates_only():
    strat, fac = load("strategy_catalog_v3.json"), load("factor_catalog_v3.json")
    s = seed_strategies(strat)
    f = seed_factors(fac, strat)
    assert len(s.definitions) == 61 and len(f.definitions) == 105
    assert len({d.strategy_id for d in s.definitions + f.definitions}) == 166
    for i in s.instances + f.instances:
        assert (i.lifecycle, i.implementation_status, i.reproduction_status) == ("discovered", "not_implemented", "not_started")
    assert {d.role for d in s.definitions} <= {"selection", "allocation", "entry", "holding", "execution", "exit"}
    assert all(d.method_family == "factor_candidate" and d.role == "selection" for d in f.definitions)
    # the spec of a candidate is the catalog entry verbatim; the hash is stable
    d0 = s.definitions[0]
    assert d0.specification["catalog_id"] == "S001" and d0.definition_hash == canonical_hash(d0.specification)
    assert seed_strategies(strat).definitions == s.definitions
    tag_axes = {t.taxonomy_id for t in s.tags}
    assert {"strategy.role", "strategy.method", "strategy.horizon", "strategy.family", "strategy.lifecycle"} <= tag_axes
    # tampered catalog (a candidate that claims to be implemented) is refused
    bad = json.loads(json.dumps(strat))
    bad["entries"][0]["implementation_status"] = "implemented"
    with pytest.raises(ValueError, match="discovered/not_implemented"):
        seed_strategies(bad)


def test_strategy_wire_passes_contract():
    s = seed_strategies(load("strategy_catalog_v3.json"))
    wire = strategy_list_wire(s.definitions[:5], s.instances, s.tags)
    contracts.check("StrategyList", wire)
    contracts.check("listStrategiesResponse", {"meta": _meta(type("R", (), {"release_id": uid("rel")})), "data": wire})
    assert wire["items"][0]["instances"][0]["lifecycle"] == "discovered"


# ------------------------------------------------------------------ lifecycle and promotion


def test_lifecycle_chain_pause_retire_revive():
    assert allowed_targets("discovered") == {"specified", "retired"}
    assert "active" in allowed_targets("shadow") and "active" not in allowed_targets("validated")
    assert allowed_targets("paused") == {"screened", "retired"}
    assert allowed_targets("retired") == {"specified"}
    with pytest.raises(LifecycleError):  # no skipping
        LifecycleTransition("i", "screened", "shadow", "x", T0, "p").validate()
    with pytest.raises(LifecycleError, match="revival"):
        LifecycleTransition("i", "retired", "specified", "new data", T0, "p").validate()
    LifecycleTransition("i", "retired", "specified", "new data", T0, "p", revival_ref="exp:9").validate()


POLICY = P.PromotionPolicy("promo-v1", min_independent_samples=30, min_forward_samples=20,
                           min_holdout_increment_lower=D("0"), max_forward_shadow_gap=D("0.01"),
                           min_marginal_contribution=D("0"))


def good_validated():
    return P.PromotionEvidence(
        kernel_benchmark_passed=True, training_validation_registered=True, trials_registered=True,
        holdout_increment_lower=D("0.002"), holdout_accessed_once=True, cost_sensitivity_explained=True,
        independent_samples=40)


def test_promotion_requires_all_gates_unknown_blocks():
    dec = P.decide_promotion(POLICY, instance_id="i", evaluation_experiment_id="e", from_state="screened",
                             target_state="validated", evidence=good_validated(), decided_at=T0)
    assert dec.outcome == "approved" and dec.to_state == "validated" and dec.from_state == "screened"
    assert len(dec.gate_results) == 7 and all(g.status == "pass" for g in dec.gate_results)
    partial = P.PromotionEvidence(kernel_benchmark_passed=True)
    rej = P.decide_promotion(POLICY, instance_id="i", evaluation_experiment_id="e", from_state="screened",
                             target_state="validated", evidence=partial, decided_at=T0)
    assert rej.outcome == "rejected" and rej.to_state == "screened"
    assert any(g.status == "unknown" for g in rej.gate_results)  # unknown evidence is never a pass
    weak = good_validated().__class__(**{**good_validated().__dict__, "holdout_increment_lower": D("-0.001")})
    assert P.decide_promotion(POLICY, instance_id="i", evaluation_experiment_id="e", from_state="screened",
                              target_state="validated", evidence=weak, decided_at=T0).outcome == "rejected"
    with pytest.raises(LifecycleError):  # cannot jump to active from validated
        P.decide_promotion(POLICY, instance_id="i", evaluation_experiment_id="e", from_state="validated",
                           target_state="active", evidence=good_validated(), decided_at=T0)


def test_league_rank_never_enters_promotion():
    # independence is structural: no evidence field mentions the league, rank, score or Elo
    names = " ".join(P.evidence_field_names())
    assert not any(w in names for w in ("league", "rank", "elo", "points", "score"))
    with pytest.raises(TypeError):
        P.PromotionEvidence(league_rank=1)  # type: ignore[call-arg]
    ev = P.PromotionEvidence(kernel_benchmark_passed=True, forward_shadow_gap=D("0.02"), marginal_contribution=D("0.1"),
                             independent_forward_samples=25)
    r = P.decide_promotion(POLICY, instance_id="i", evaluation_experiment_id="e", from_state="shadow",
                           target_state="active", evidence=ev, decided_at=T0)
    assert r.outcome == "rejected"  # forward gap 0.02 > 0.01 regardless of any imaginary league win
    ok = P.PromotionEvidence(kernel_benchmark_passed=True, forward_shadow_gap=D("0.005"), marginal_contribution=D("0.1"),
                             independent_forward_samples=25)
    assert P.decide_promotion(POLICY, instance_id="i", evaluation_experiment_id="e", from_state="shadow",
                              target_state="active", evidence=ok, decided_at=T0).to_state == "active"


def test_demotion_needs_ruling_out_data_and_cost_causes():
    kw = dict(instance_id="i", evaluation_experiment_id="e", from_state="active", target_state="paused",
              reason="drift", decided_at=T0)
    assert P.decide_demotion(POLICY, data_outage_ruled_out=False, cost_profile_change_ruled_out=True, **kw).outcome == "rejected"
    d = P.decide_demotion(POLICY, data_outage_ruled_out=True, cost_profile_change_ruled_out=True, **kw)
    assert d.outcome == "approved" and d.to_state == "paused"


# ------------------------------------------------------------------ context and experiments


def spec(**kw):
    base = dict(
        forecast_target="rel_opportunity_5td", horizon_id="5td", decision_as_of=T0, availability_policy_version="avail-v1",
        availability_mode="observed",
        availability_rule_ref={"ref": "policy://avail-v1", "content_hash": "a" * 64, "contract_id": "fixture.policy", "schema_version": "1.0.0"},
        assumed_ingestion_delay_ms=None, assumption_note=None, universe_policy_version="cn-mainboard-rules-v1+default-pref-v1",
        inputs=(InputSnapshot(uid("snap-bars"), "bars_raw_1d", 3, "b" * 64, "bars"),),
        market_rules_version="mr-1", execution_policy_version="ex-1", cost_policy_version="cost-1",
        ledger_policy_version="led-1", performance_policy_version="perf-1", numeric_policy_version="num-1",
        train_start=T0 - timedelta(days=400), train_end=T0 - timedelta(days=200),
        validation_start=T0 - timedelta(days=200), validation_end=T0 - timedelta(days=100),
        blind_start=T0 - timedelta(days=100), blind_end=T0 - timedelta(days=10), embargo_seconds=86400 * 5,
        benchmark_specification={"benchmarks": ["cash", "equal_weight_universe", "frozen_champion"], "version": "bm-1"},
        resource_budget={"max_trials": 200, "blind_access_budget": 1})
    base.update(kw)
    return EvaluationContextSpec(**base)


def test_context_hash_fixes_everything_and_wire_is_valid():
    a, b = spec(), spec()
    assert a.context_hash() == b.context_hash() and a.context_id() == b.context_id()
    for change in (dict(cost_policy_version="cost-2"), dict(resource_budget={"max_trials": 201}),
                   dict(benchmark_specification={"benchmarks": ["cash"], "version": "bm-1"}),
                   dict(universe_policy_version="other"),
                   dict(inputs=(InputSnapshot(uid("snap-bars"), "bars_raw_1d", 4, "b" * 64, "bars"),))):
        assert spec(**change).context_hash() != a.context_hash(), change
    wire = a.to_wire(sealed_at=T0)
    contracts.check("EvaluationContext", wire)
    with pytest.raises(ContextError):
        spec(availability_mode="reconstructed")  # constructed OK, invalid on use
        spec(availability_mode="reconstructed").validate()
    with pytest.raises(ContextError):
        spec(validation_start=T0 - timedelta(days=300)).validate()
    with pytest.raises(ContextError):
        spec(resource_budget={}).validate()


def test_all_trials_recorded_including_failures_and_blind_budget():
    ledger = TrialLedger(blind_access_budget=1)
    ctx_id = spec().context_id()
    mk = lambda seed, purpose="screening", fam="trend_price_transform": new_run(  # noqa: E731
        instance_id=uid("inst"), hypothesis_id=uid("hyp"), hypothesis_version=1, evaluation_context_id=ctx_id,
        run_purpose=purpose, implementation_digest="impl-1", random_seed=seed, run_manifest={}, family_key=fam)
    r1 = ledger.register(mk(1))
    r2 = ledger.register(mk(2))
    assert ledger.register(mk(1)).experiment_id == r1.experiment_id  # idempotent re-register
    ledger.update(complete(r1, started_at=T0, completed_at=T0 + timedelta(minutes=1), result_ref="res://1", result_hash="c" * 64))
    failed2 = fail(r2, started_at=T0, completed_at=T0, error_code="DATA_GAP")
    ledger.update(failed2)
    assert ledger.trial_count("trend_price_transform") == 2  # failed trial still counts for multiple testing
    assert ledger.counts_by_state()["failed"] == 1
    with pytest.raises(ExperimentError):
        fail(failed2, started_at=None, completed_at=T0, error_code="x")  # already closed
    with pytest.raises(ExperimentError):
        ledger.update(r1)
    ledger.register(mk(3, "blind_final"))
    with pytest.raises(BlindBudgetExceeded):
        ledger.register(mk(4, "blind_final"))


def test_evaluation_wire_passes_contract():
    ctx = spec().to_wire(sealed_at=T0)
    rec = new_run(instance_id=uid("inst"), hypothesis_id=uid("hyp"), hypothesis_version=1,
                  evaluation_context_id=spec().context_id(), run_purpose="screening", implementation_digest="i",
                  random_seed=7, run_manifest={}, family_key="f")
    rec = complete(rec, started_at=T0, completed_at=T0 + timedelta(minutes=2), result_ref="res://x", result_hash="d" * 64)
    metric = {"metric_name": "excess_return_twr", "metric_definition_version": "m1", "slice_key": "overall", "value": D("0.012"),
              "unit": "ratio", "lower_bound": D("0.001"), "upper_bound": D("0.02"), "confidence_level": D("0.95"),
              "sample_count": 120, "quality_status": "valid", "missing_reason": None}
    pv = {"market_rules_version": "mr-1", "execution_policy_version": "ex-1", "cost_policy_version": "cost-1",
          "ledger_policy_version": "led-1", "performance_policy_version": "perf-1", "numeric_policy_version": "num-1"}
    art = {"ref": "res://x", "content_hash": "d" * 64, "contract_id": "fixture.result", "schema_version": "1.0.0"}
    wire = evaluation_wire(rec, ctx, pv, (uid("hyp"), 1), [contracts.to_wire(metric)], art)
    contracts.check("Evaluation", wire)
    contracts.check("getEvaluationResponse", {"meta": _meta(type("R", (), {"release_id": uid("rel")})), "data": wire})


# ------------------------------------------------------------------ league


def cands(n, **kw):
    out = []
    for i in range(n):
        d = dict(instance_id=f"inst{i:02d}", role="selection", horizon_id="5td", universe="u1", forecast_target="t1",
                 context_hash="h" * 64, budget_id="b1", family=f"fam{i % 3}", identity=f"id{i}")
        d.update(kw)
        out.append(Candidate(**d))
    return out


def test_pairing_only_inside_comparable_groups_and_seed_saved():
    mixed = cands(2) + cands(2, role="entry")[:1]
    groups = group_comparable(mixed)
    assert len(groups) == 2
    with pytest.raises(LeagueError, match="not comparable"):
        pair_round(mixed, LeagueHistory("seed-1"), 1)
    inc = incomparable(mixed[0], mixed[2], 1, "seed-1")
    assert inc.outcome == "incomparable" and "role" in inc.reason
    h = LeagueHistory("seed-1")
    r1 = pair_round(cands(6), h, 1)
    assert r1.seed == round_seed("seed-1", 1)
    again = pair_round(cands(6), LeagueHistory("seed-1"), 1)
    assert [(p.left, p.right) for p in r1.pairings] == [(p.left, p.right) for p in again.pairings]  # replayable
    other = pair_round(cands(6), LeagueHistory("seed-2"), 1)
    assert [(p.left, p.right) for p in other.pairings] != [(p.left, p.right) for p in r1.pairings]
    seen = [x for p in r1.pairings for x in (p.left, p.right) if x]
    assert sorted(seen) == [c.instance_id for c in cands(6)]  # each candidate once per round
    # round 1 pairs across families when possible
    fam = {c.instance_id: c.family for c in cands(6)}
    assert all(fam[p.left] != fam[p.right] for p in r1.pairings if p.right)


def test_bye_handling_no_points_and_rotation():
    h = LeagueHistory("s")
    r1 = pair_round(cands(5), h, 1)
    byes = [p for p in r1.pairings if p.outcome == "bye"]
    assert len(byes) == 1 and byes[0].right is None
    h.byes[byes[0].left] += 1
    for p in r1.pairings:
        if p.right:
            h.record(p.left, p.right, "win", False)
    assert byes[0].left not in h.points  # bye earns nothing
    r2 = pair_round(cands(5), h, 2)
    bye2 = [p.left for p in r2.pairings if p.outcome == "bye"]
    assert bye2 and bye2[0] != byes[0].left  # fewest-byes candidate gets the next bye


def test_opponent_repeat_cap_and_duplicate_identities_merged():
    h = LeagueHistory("s")
    c = cands(4)
    r1 = pair_round(c, h, 1)
    for p in r1.pairings:
        h.record(p.left, p.right, "draw", False)
    r2 = pair_round(c, h, 2, PairingPolicy(max_repeat=1, max_same_family_pairings=99))
    prev = {frozenset((p.left, p.right)) for p in r1.pairings}
    assert all(frozenset((p.left, p.right)) not in prev for p in r2.pairings if p.right)
    dup = cands(4)
    dup[3] = Candidate(**{**dup[3].__dict__, "identity": dup[0].identity})
    r = pair_round(dup, LeagueHistory("s"), 1)
    assert r.merged_duplicates == (("inst03", "inst00"),)
    assert "inst03" not in [x for p in r.pairings for x in (p.left, p.right)]


def test_result_classification_never_inflates_small_differences():
    pol = ComparisonPolicy("cmp-v1", min_sample_count=30, tie_margin=D("0.002"))
    ev = lambda lo, hi, n=100, **k: SideEvidence(**{**dict(both_completed=True, execution_failed=False, labels_matured=True,  # noqa: E731
                                                            delta_lower=lo, delta_upper=hi, sample_count=n), **k})
    assert classify_pairing(ev(D("0.001"), D("0.01")), pol)[0] == "win"
    assert classify_pairing(ev(D("-0.01"), D("-0.001")), pol)[0] == "loss"
    assert classify_pairing(ev(D("-0.001"), D("0.001")), pol)[0] == "draw"
    assert classify_pairing(ev(D("-0.01"), D("0.02")), pol)[0] == "insufficient_evidence"
    assert classify_pairing(ev(D("0.001"), D("0.01"), n=5), pol)[0] == "insufficient_evidence"
    assert classify_pairing(ev(None, None), pol)[0] == "insufficient_evidence"
    assert classify_pairing(ev(D("0.001"), D("0.01"), labels_matured=False), pol)[0] == "pending"
    assert classify_pairing(ev(D("0.001"), D("0.01"), execution_failed=True), pol)[0] == "execution_failed"  # not a zero return
    assert classify_pairing(ev(D("0.001"), D("0.01"), both_completed=False), pol)[0] == "pending"
    assert [to_db_outcome(x) for x in ("win", "loss", "draw", "pending", "insufficient_evidence", "execution_failed", "incomparable")] == \
        ["left", "right", "tie", "pending", "insufficient_evidence", "execution_failed", "incomparable"]


def test_league_wire_contract():
    r1 = pair_round(cands(4), LeagueHistory("s"), 1)
    pairings = [{"pairing_id": p.pairing_id, "left_instance_id": uid(p.left), "right_instance_id": uid(p.right) if p.right else None,
                 "left_experiment_id": None, "right_experiment_id": None, "outcome": p.outcome, "reason_ref": p.reason}
                for p in r1.pairings]
    wire = league_wire(league_id=uid("lg"), league_version=1, evaluation_context_id=spec().context_id(),
                       display_name="selection/5td", pairing_policy_version="swiss-v1", promotion_policy_version="promo-v1",
                       rounds=[{"round_no": 1, "round_state": "scheduled", "pairings": pairings}])
    contracts.check("League", wire)
    contracts.check("getLeagueResponse", {"meta": _meta(type("R", (), {"release_id": uid("rel")})), "data": wire})
