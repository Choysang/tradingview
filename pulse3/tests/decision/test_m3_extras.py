"""Cross-cutting M3 rules: stale baseline, shadow-only policies, PIT membership, combinations."""
from datetime import timedelta
from decimal import Decimal as D

import pytest

from pulse.domain.plans.compiler import CompileError, compile_plan
from pulse.domain.portfolio.coordinator import AccountSnapshot, PositionView, Proposal, coordinate
from pulse.domain.sectors.ranking import (
    SectorNode,
    SectorPolicy,
    SectorSnapshot,
    assemble_sector_overview,
    score_sectors_by_opportunity,
)
from pulse.domain.strategy.composition import (
    CompositionError,
    CompositionSpec,
    ParentRef,
    build_composite_definition,
    identify_families,
    marginal_contribution,
)

from .helpers import T0, uid
from .test_plans import INST, outputs


def test_stale_ranking_cannot_seed_a_new_plan():
    kw = dict(plan_id=uid("p"), instrument_id=INST, release_id=uid("r"), outputs=outputs(), valid_from=T0,
              expires_at=T0 + timedelta(days=1), available_at=T0, validation_policy_version="v")
    assert compile_plan(**kw).plan["state"] == "watching"
    with pytest.raises(CompileError, match="stale"):
        compile_plan(basis_ranking_state="stale", **kw)


def test_unapproved_predictive_policy_is_shadow_only():
    pos = {INST: PositionView(1000, 1000)}
    acct = AccountSnapshot(uid("acct"), 1, D("0"), pos)
    veto = Proposal("veto", "news_sleeve", "holding", "trim", INST, 1000, authority="shadow")
    real = Proposal("rule", "base", "exit", "exit", INST, 400)
    r = coordinate([veto, real], acct)
    got = {d.proposal_id: d for d in r.decisions}
    assert got["veto"].approved_shares == 0 and got["veto"].reason_codes == ("SHADOW_ONLY_UNAPPROVED_POLICY",)
    assert got["rule"].approved_shares == 400  # shadow did not consume the shared sellable inventory
    assert "news_sleeve" not in r.sleeve_attribution  # and left no attribution
    with pytest.raises(ValueError):
        Proposal("x", "s", "exit", "exit", INST, 1, authority="maybe")


def test_membership_known_only_after_decision_time_is_excluded():
    ids = [uid(f"inst{i}") for i in range(4)]
    ms = uid("ms")
    known = SectorSnapshot(SectorNode("prov.concept", 3, "ai"), ms, tuple(ids), available_at=T0 - timedelta(hours=1))
    later = SectorSnapshot(SectorNode("prov.concept", 3, "chip"), ms, tuple(ids), available_at=T0 + timedelta(hours=1))
    unknown = SectorSnapshot(SectorNode("prov.concept", 3, "x"), ms, tuple(ids), available_at=None)
    scores = {i: D(k) for k, i in enumerate(ids)}
    scored, excluded = score_sectors_by_opportunity(SectorPolicy("v"), [known, later, unknown], scores, decision_as_of=T0)
    assert [s.snapshot.node.node_id for s in scored] == ["ai"]
    assert {n.node_id: r for n, r in excluded} == {"chip": "membership_not_yet_available", "x": "membership_availability_unknown"}
    # without a decision time nothing is filtered (explicit current-view call)
    assert len(score_sectors_by_opportunity(SectorPolicy("v"), [known, later], scores)[0]) == 2


def test_overview_requires_one_membership_snapshot():
    ids = [uid(f"inst{i}") for i in range(3)]
    a = SectorSnapshot(SectorNode("t", 1, "a"), uid("m1"), tuple(ids))
    b = SectorSnapshot(SectorNode("t", 1, "b"), uid("m2"), tuple(ids))
    scored, _ = score_sectors_by_opportunity(SectorPolicy("v"), [a, b], {i: D(1) for i in ids})
    with pytest.raises(ValueError, match="one membership snapshot"):
        assemble_sector_overview(ranking_id=uid("o"), release_id=uid("r"), taxonomy_id="t", taxonomy_version=1, horizon_id="5td",
                                 forecast_target="x", ranking_policy_version="v", scores=scored)


def _spec(**kw):
    base = dict(name="trend+grid", role="selection", forecast_target="t1", horizon_id="5td",
                parents=(ParentRef(uid("sa"), 1, "selection", D("0.6"), "ablation://1"),
                         ParentRef(uid("sb"), 2, "blend", D("0.4"), "ablation://2")),
                routing_policy_ref="route-v1", interaction_constraints={"max_simultaneous": 3},
                fallback_baseline_ref="baseline-v1", composition_policy_version="comp-v1")
    base.update(kw)
    return CompositionSpec(**base)


def test_composite_is_a_new_version_with_lineage_and_rerun_only_increment():
    d, parents = build_composite_definition(_spec())
    assert d.method_family == "composite" and d.strategy_version == 1 and len(parents) == 2
    assert d.specification["parents"][0]["strategy_version"] == 1 and d.specification["fallback_baseline_ref"] == "baseline-v1"
    assert build_composite_definition(_spec())[0].strategy_id == d.strategy_id  # deterministic
    assert build_composite_definition(_spec(routing_policy_ref="route-v2"))[0].definition_hash != d.definition_hash
    for bad in (_spec(parents=()), _spec(fallback_baseline_ref=""), _spec(routing_policy_ref="")):
        with pytest.raises(CompositionError):
            build_composite_definition(bad)
    with pytest.raises(CompositionError):
        build_composite_definition(_spec(parents=(ParentRef(uid("sa"), 0, "selection", D("1"), "x"),)))
    m = marginal_contribution(with_run_ref="run://with", without_run_ref="run://without", with_twr=D("0.031"), without_twr=D("0.028"))
    assert m.increment == D("0.003")
    with pytest.raises(CompositionError, match="unified-account"):
        marginal_contribution(with_run_ref="a", without_run_ref="b", with_twr=D(1), without_twr=D(0), source="sum_of_curves")
    with pytest.raises(CompositionError):
        marginal_contribution(with_run_ref="a", without_run_ref="a", with_twr=D(1), without_twr=D(0))


def test_correlated_families_identified_deterministically():
    corr = {("s1", "s2"): D("0.97"), ("s2", "s3"): D("-0.96"), ("s4", "s5"): D("0.2"), ("s1", "s4"): D("0.5")}
    fams = identify_families(["s5", "s3", "s1", "s2", "s4"], corr, D("0.95"))
    assert fams == [["s1", "s2", "s3"], ["s4"], ["s5"]]
