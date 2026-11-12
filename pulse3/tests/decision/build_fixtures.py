"""Generate the synthetic, contract-valid decision fixtures in fixtures/decision/.

Run: ``uv run python -m tests.decision.build_fixtures`` from pulse3/ (deterministic: same bytes
every run). File names are ``<ContractDef>__<case>.json``; ``test_fixtures.py`` re-validates
each against its ``$defs`` entry and re-generates to prove determinism. All data is SYNTHETIC
(fixture instruments, fixture scores): it demonstrates shapes and invariants, not alpha.
"""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

from pulse.contracts import to_wire
from pulse.domain.plans.conditions import Tri
from pulse.domain.portfolio.actionability import ActionabilityInput, PlanLeg, QuoteView, evaluate_actionability
from pulse.domain.portfolio.coordinator import AccountSnapshot, PositionView
from pulse.domain.ranking.run import ActionAssessment, build_ranking
from pulse.domain.ranking.universe import UniversePolicy, apply_universe
from pulse.domain.sectors.pool import PoolPolicy, SurgeComponents, assess_surge, candidate_pool_wire, decide_next_state, make_event
from pulse.domain.sectors.ranking import SectorNode, SectorPolicy, SectorSnapshot, assemble_sector_overview, score_sectors_by_opportunity
from pulse.domain.strategy.seed import seed_strategies
from pulse.domain.strategy.wire import strategy_list_wire

from .helpers import T0, facts, matrix, policy, uid
from .test_plans import compiled

OUT = Path(__file__).resolve().parents[2] / "fixtures" / "decision"
CAT = Path(__file__).resolve().parents[2] / "spec_package" / "catalogs"


def build() -> dict[str, dict]:
    out: dict[str, dict] = {}
    rel = uid("fx-release")
    u = apply_universe(UniversePolicy(), facts(12))
    ids = [f.instrument_id for f in u.included]
    m = matrix(ids)
    plain = build_ranking(ranking_id=uid("fx-rk"), release_id=rel, ranking_scope="market", scope_key="cn.mainboard",
                          policy=policy(), universe=u, matrix=m)
    top = plain.entries[0].instrument_id
    plan = compiled().plan
    run = build_ranking(ranking_id=uid("fx-rk"), release_id=rel, ranking_scope="market", scope_key="cn.mainboard", policy=policy(),
                        universe=u, matrix=m, actions={plain.entries[1].instrument_id: ActionAssessment(
                            plain.entries[1].instrument_id, "actionable", (), (uid("fx-plan"), 1), "actionable"),
                            top: ActionAssessment(top, "blocked", ("NO_COUNTERPARTY_LIMIT_UP",))})
    out["Ranking__market_synthetic"] = run.to_wire()
    out["TradePlan__watching_synthetic"] = {**plan, "plan_id": str(uid("fx-plan"))}
    ms = uid("fx-membership")
    snaps = [SectorSnapshot(SectorNode("fixture.concept", 1, "ai"), ms, tuple(ids[:6])),
             SectorSnapshot(SectorNode("fixture.concept", 1, "chip"), ms, tuple(ids[4:10]))]
    scored, _ = score_sectors_by_opportunity(SectorPolicy("sec-fixture-v1", top_k=3), snaps, {i: D(k) for k, i in enumerate(ids)})
    overview, _ = assemble_sector_overview(ranking_id=uid("fx-ov"), release_id=rel, taxonomy_id="fixture.concept", taxonomy_version=1,
                                           horizon_id="5td", forecast_target="synthetic_sector_opportunity",
                                           ranking_policy_version="sec-fixture-v1", scores=scored)
    out["SectorOverview__synthetic"] = overview
    p = PoolPolicy("heat-fixture", "v1", {"relative_strength_change": D("0.5"), "breadth": D("0.5")}, D("1"), D("0.5"), 1, 2, frozenset({"breadth"}))
    good = assess_surge(p, SurgeComponents(relative_strength_change=D("2"), breadth=D("2")))
    state, events = None, []
    for _ in range(2):
        state, change = decide_next_state(p, state, good)
        if change:
            events.append(make_event(pool_id=uid("fx-pool"), instrument_id=ids[2], policy=p, prev=None if not events else events[-1].state,
                                     new=change, taxonomy=("fixture.concept", 1, "ai"), membership_snapshot_id=ms, release_id=rel,
                                     plan_ref=None, decision_as_of=T0, available_at=T0 + timedelta(seconds=1)))
    out["CandidatePool__synthetic"] = candidate_pool_wire(pool_id=uid("fx-pool"), release_id=rel, policy_id=p.policy_id,
                                                          policy_version=p.policy_version, current=events)
    inp = ActionabilityInput(
        release_id=rel, ranking_id=run.ranking_id, quote_snapshot_id=uid("fx-quote"), policy_version="act-fixture-v1",
        checked_at=T0 + timedelta(seconds=5), valid_until=T0 + timedelta(seconds=65),
        ranking_order=[(e.instrument_id, e.action_rank) for e in run.entries],
        plans={run.entries[1].instrument_id: [PlanLeg((uid("fx-plan"), 1), "buy", 300, Tri.TRUE)]},
        quotes={run.entries[1].instrument_id: QuoteView(run.entries[1].instrument_id, D("10"), True, True)},
        account=AccountSnapshot(uid("fx-acct"), 3, D("50000"), {ids[0]: PositionView(0, 0)}))
    out["ActionabilitySnapshot__synthetic"] = evaluate_actionability(inp)
    strat = json.loads((CAT / "strategy_catalog_v3.json").read_text(encoding="utf-8"))
    s = seed_strategies(strat)
    out["StrategyList__catalog_candidates_first5"] = strategy_list_wire(s.definitions[:5], s.instances, s.tags)
    return out


def write() -> list[Path]:
    OUT.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, payload in sorted(build().items()):
        path = OUT / f"{name}.json"
        path.write_text(json.dumps(to_wire(payload), ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        paths.append(path)
    return paths


if __name__ == "__main__":
    for p in write():
        print(p)
