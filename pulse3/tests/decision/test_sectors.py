"""W05 sectors: opportunity ranking, in-sector ranking, overlap, surge pool (M3-E02)."""
from datetime import timedelta
from types import SimpleNamespace
from decimal import Decimal as D

import pytest

from pulse import contracts
from pulse.domain.ranking.run import build_ranking
from pulse.domain.ranking.universe import UniversePolicy, apply_universe
from pulse.domain.sectors.pool import (
    PoolPolicy,
    PoolState,
    SurgeComponents,
    assess_surge,
    candidate_pool_wire,
    decide_next_state,
    make_event,
)
from pulse.domain.sectors.ranking import (
    SectorNode,
    SectorPolicy,
    SectorSnapshot,
    assemble_sector_overview,
    overlap_report,
    restrict_universe,
    score_sectors_by_opportunity,
)

from .helpers import T0, facts, matrix, policy, uid
from .test_ranking import _meta


def _snaps():
    ids = [uid(f"inst{i}") for i in range(12)]
    a = SectorSnapshot(SectorNode("prov.concept", 3, "ai"), uid("msAB"), tuple(ids[:6]))
    b = SectorSnapshot(SectorNode("prov.concept", 3, "chip"), uid("msAB"), tuple(ids[4:10]))  # overlaps a on 2 stocks
    c = SectorSnapshot(SectorNode("prov.concept", 3, "ghost"), uid("msAB"), (uid("nobody1"), uid("nobody2")))
    return ids, a, b, c


def test_sector_opportunity_overview_contract_and_partial_quality():
    ids, a, b, c = _snaps()
    scores = {i: D(k) for k, i in enumerate(ids)}
    scores.pop(ids[0]); scores.pop(ids[1])  # a: 4 of 6 observed -> partial
    scored, excluded = score_sectors_by_opportunity(SectorPolicy("sec-v1", top_k=3), [a, b, c], scores)
    assert [n.node_id for n, _ in excluded] == ["ghost"]  # zero observed members: unknown, not 0
    by = {s.snapshot.node.node_id: s for s in scored}
    assert by["ai"].quality_status == "partial" and by["ai"].observed_member_count == 4
    assert by["chip"].quality_status == "valid"
    payload, expl = assemble_sector_overview(
        ranking_id=uid("ov"), release_id=uid("rel"), taxonomy_id="prov.concept", taxonomy_version=3,
        horizon_id="5td", forecast_target="sector_rel_opportunity", ranking_policy_version="sec-v1", scores=scored)
    contracts.check("SectorOverview", payload)
    assert [e["rank"] for e in payload["entries"]] == [1, 2]
    assert payload["entries"][0]["taxonomy_node"]["node_id"] == "chip"  # higher mean of top 3
    assert all(e["explanation_ref"] in expl for e in payload["entries"])


def test_taxonomies_are_not_merged():
    ids, a, b, _ = _snaps()
    other = SectorSnapshot(SectorNode("other.industry", 1, "semi"), uid("msD"), tuple(ids[:3]))
    scored, _ = score_sectors_by_opportunity(SectorPolicy("v"), [a, other], {i: D(1) for i in ids})
    with pytest.raises(ValueError, match="never merged"):
        assemble_sector_overview(ranking_id=uid("ov"), release_id=uid("rel"), taxonomy_id="prov.concept",
                                 taxonomy_version=3, horizon_id="5td", forecast_target="t",
                                 ranking_policy_version="v", scores=scored)


def test_overlap_kept_and_reported():
    ids, a, b, _ = _snaps()
    rep = overlap_report([a, b])
    assert set(rep.multi_member_instruments) == {ids[4], ids[5]}
    assert rep.multiplicity[ids[4]] == 2
    assert rep.jaccard[("prov.concept/ai", "prov.concept/chip")] == D("0.2")  # 2 / 10


def test_in_sector_stock_ranking_and_overlap_in_two_sector_rankings():
    ids, a, b, _ = _snaps()
    u = apply_universe(UniversePolicy(), facts(12))
    m = matrix([f.instrument_id for f in u.included])
    runs = []
    for s in (a, b):
        sub = restrict_universe(u, s)
        run = build_ranking(
            ranking_id=uid(f"rk-{s.node.node_id}"), release_id=uid("rel"), ranking_scope="sector",
            scope_key=f"{s.node.taxonomy_id}/{s.node.node_id}", policy=policy(), universe=sub, matrix=m,
            taxonomy_node=s.node.wire(), membership_snapshot_id=s.membership_snapshot_id)
        contracts.check("Ranking", run.to_wire())
        runs.append(run)
    in_a = {e.instrument_id for e in runs[0].entries}
    in_b = {e.instrument_id for e in runs[1].entries}
    assert in_a & in_b == {ids[4], ids[5]}  # overlapping members appear in both sector rankings
    ghost = restrict_universe(u, SectorSnapshot(SectorNode("x", 1, "g"), uid("m"), (uid("nobody1"),)))
    assert ghost.exclusions[0].reason_codes == ("not_in_universe",)


# ------------------------------------------------------------------ surge + pool


def _policy():
    w = {"relative_strength_change": D("0.3"), "turnover_acceleration": D("0.2"), "breadth": D("0.2"),
         "leader_confirmation": D("0.15"), "news_novelty": D("0.05"), "community_growth": D("0.05"), "fund_proxy": D("0.05")}
    return PoolPolicy("heat-v1", "p1", w, enter_threshold=D("1.0"), exit_threshold=D("0.5"), min_dwell_evaluations=2,
                      cooling_max_evaluations=2, required_components=frozenset({"relative_strength_change", "breadth"}))


def test_surge_components_missing_is_shown_not_zero():
    p = _policy()
    full = SurgeComponents(*[D("2")] * 7)
    a = assess_surge(p, full)
    assert a.complete and a.missing == () and a.score == D("2")
    part = assess_surge(p, SurgeComponents(relative_strength_change=D("2"), breadth=D("2"), fund_proxy=None))
    assert not part.complete
    assert "news_novelty" in part.missing and "fund_proxy" in part.missing
    assert part.score == D("1.0")  # 0.3*2 + 0.2*2: missing parts contribute nothing and are listed
    none = assess_surge(p, SurgeComponents())
    assert none.score is None and len(none.missing) == 7


def test_pool_hysteresis_dwell_hard_invalidation_and_history():
    p = _policy()
    inst, pool = uid("inst1"), uid("pool")
    good = assess_surge(p, SurgeComponents(*[D("2")] * 7))
    mid = assess_surge(p, SurgeComponents(*[D("0.7")] * 7))  # between exit and enter: hold
    low = assess_surge(p, SurgeComponents(*[D("0.1")] * 7))
    unknown = assess_surge(p, SurgeComponents())
    events, state = [], None

    def step(a, **kw):
        nonlocal state
        new_state, change = decide_next_state(p, state, a, **kw)
        if change:
            ev = make_event(pool_id=pool, instrument_id=inst, policy=p, prev=state, new=change,
                            taxonomy=("prov.concept", 3, "ai"), membership_snapshot_id=uid("ms"),
                            release_id=uid("rel"), plan_ref=kw.get("plan_ref"),
                            decision_as_of=T0, available_at=T0 + timedelta(seconds=1))
            events.append(ev)
        state = new_state
        return change

    assert step(good)[1] == "entered_pool" and state == PoolState("observing", 1, 1)
    assert step(good)[1] == "score_above_enter_threshold" and state.state == "qualified"
    assert step(mid) is None  # hysteresis band: no flapping
    assert step(unknown) is None and state.state == "qualified"  # unknown score never demotes
    assert step(low)[1] == "score_below_exit_threshold" and state.state == "cooling"
    assert step(low)[1] == "cooling_expired" and state.state == "removed"
    assert step(low) is None
    ch = step(good, hard_invalidation="suspended")
    assert ch is None  # already removed
    # re-entry is a new observing event with previous_state removed
    assert step(good)[1] == "re_entered_pool" and events[-1].previous_state == "removed"
    seqs = [e.event_sequence for e in events]
    assert seqs == list(range(1, len(events) + 1))  # monotone per (pool, instrument)
    assert len({e.pool_event_id for e in events}) == len(events)


def test_hard_invalidation_overrides_dwell_and_actionable_needs_plan():
    p = _policy()
    state = PoolState("observing", 1, 1)
    good = assess_surge(p, SurgeComponents(*[D("2")] * 7))
    state, ch = decide_next_state(p, state, good)
    assert state.state == "qualified"
    plan = (uid("plan"), 1)
    state, ch = decide_next_state(p, state, good, plan_ref=plan, plan_validated=False)
    assert ch is None and state.state == "qualified"  # unvalidated plan cannot make it actionable
    state, ch = decide_next_state(p, state, good, plan_ref=plan, plan_validated=True)
    assert state.state == "actionable"
    ev = make_event(pool_id=uid("pool"), instrument_id=uid("i"), policy=p, prev=PoolState("qualified", 2, 1), new=ch,
                    taxonomy=("t", 1, "n"), membership_snapshot_id=uid("ms"), release_id=uid("rel"),
                    plan_ref=plan, decision_as_of=T0, available_at=T0)
    wire = candidate_pool_wire(pool_id=uid("pool"), release_id=uid("rel"), policy_id=p.policy_id,
                               policy_version=p.policy_version, current=[ev])
    contracts.check("CandidatePool", wire)
    contracts.check("getCandidatePoolResponse", {"meta": _meta(SimpleNamespace(release_id=uid("rel"))), "data": wire})
    state, ch = decide_next_state(p, state, good, hard_invalidation="delisting_notice")
    assert state.state == "removed" and ch[1] == "hard_invalidation"  # no dwell wait
    with pytest.raises(ValueError):
        make_event(pool_id=uid("pool"), instrument_id=uid("i"), policy=p, prev=None,
                   new=(PoolState("qualified", 1, 1), "x", {}), taxonomy=("t", 1, "n"), membership_snapshot_id=uid("ms"),
                   release_id=uid("rel"), plan_ref=None, decision_as_of=T0, available_at=T0)  # first must be observing


def test_pool_policy_requires_hysteresis():
    with pytest.raises(ValueError):
        PoolPolicy("x", "1", {"breadth": D(1)}, enter_threshold=D("1"), exit_threshold=D("1"),
                   min_dwell_evaluations=1, cooling_max_evaluations=1)
