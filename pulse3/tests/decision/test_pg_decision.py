"""W05/W06 persistence on real PostgreSQL 18: rankings, sectors, plans, pool, actionability."""
from datetime import timedelta
from decimal import Decimal as D

import psycopg
import pytest

from pulse import contracts
from pulse.adapters.postgres import decision_repo as repo
from pulse.application import plan_service
from pulse.domain.plans.compiler import attach_validation
from pulse.domain.plans.versions import LateResult, PlanVersionError, next_version
from pulse.domain.portfolio.actionability import ActionabilityInput, PlanLeg, QuoteView
from pulse.domain.plans.conditions import Tri
from pulse.domain.ranking.run import ActionAssessment, build_ranking
from pulse.domain.ranking.universe import UniversePolicy, apply_universe
from pulse.domain.sectors.pool import PoolPolicy, PoolState, SurgeComponents, assess_surge, decide_next_state, make_event
from pulse.domain.sectors.ranking import SectorNode, SectorPolicy, SectorSnapshot, assemble_sector_overview, restrict_universe, score_sectors_by_opportunity

from . import pg_seed as S
from .helpers import T0, facts, matrix, policy
from .test_plans import compiled

pytestmark = pytest.mark.pg


@pytest.fixture(scope="module")
def ids(pg_dsn):
    return S.seed_base(pg_dsn)


def _ranking(release_id, rid="rk", n=12, actions=None, **kw):
    u = apply_universe(UniversePolicy(), facts(n))
    m = matrix([f.instrument_id for f in u.included])
    return build_ranking(ranking_id=S.sid(rid), release_id=release_id, ranking_scope="market", scope_key="cn.mainboard",
                         policy=policy(), universe=u, matrix=m, actions=actions, **kw)


def test_ranking_roundtrip_pagination_and_published_immutability(pg_dsn, ids):
    with S.conn(pg_dsn) as c:
        with c.transaction():
            rel = S.make_release(c, "rk1", publish=False)
            run = _ranking(rel, "rk1")
            repo.insert_ranking_run(c, run, created_at=T0)
        wire = repo.load_ranking_wire(c, run.ranking_id)
        assert wire == run.to_wire()  # stored form is the contract object, byte for byte
        contracts.check("Ranking", wire)
        page1 = repo.load_ranking_wire(c, run.ranking_id, limit=5)
        page2 = repo.load_ranking_wire(c, run.ranking_id, after_alpha_rank=page1["entries"][-1]["alpha_rank"], limit=5)
        assert [e["alpha_rank"] for e in page1["entries"] + page2["entries"]] == list(range(1, 11))
        with c.transaction():
            S.publish_release(c, rel, T0 + timedelta(seconds=1))
        # a published ranking cannot change: rows are immutable and new rows are refused
        with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
            with c.transaction():
                c.execute("UPDATE decision.ranking_entry SET action_rank=1 WHERE ranking_id=%s AND alpha_rank=1", (run.ranking_id,))
        with pytest.raises(psycopg.errors.RaiseException):
            with c.transaction():
                repo.insert_ranking_run(c, _ranking(rel, "rk1b"), created_at=T0)


def test_ranking_entry_plan_must_match_instrument_and_release(pg_dsn, ids):
    with S.conn(pg_dsn) as c:
        with c.transaction():
            rel = S.make_release(c, "rk2", publish=False)
            other_rel = S.make_release(c, "rk2-other", publish=False)
            comp = compiled()
            plan = {**comp.plan, "plan_id": str(S.sid("plan-rk2")), "based_on_release_id": str(other_rel),
                    "instrument_id": str(ids[0]), "strategy_refs": comp.plan["strategy_refs"]}
            repo.append_plan_version(c, plan)
            first = uid_of(ids[0])
        run = _ranking(rel, "rk2", actions={ids[0]: ActionAssessment(ids[0], "actionable", (), (S.sid("plan-rk2"), 1), "actionable")})
        with pytest.raises(psycopg.errors.RaiseException, match="differ"):
            with c.transaction():
                repo.insert_ranking_run(c, run, created_at=T0)
        assert first


def uid_of(x):
    return x


def test_sector_overview_and_in_sector_rankings_overlap(pg_dsn, ids):
    with S.conn(pg_dsn) as c:
        with c.transaction():
            rel = S.make_release(c, "sec1", publish=False)
            ms = S.make_manifest(c, "sec1-membership", dataset="membership")
            a = SectorSnapshot(SectorNode("prov.concept", 3, "ai"), ms, tuple(ids[:6]))
            b = SectorSnapshot(SectorNode("prov.concept", 3, "chip"), ms, tuple(ids[4:10]))
            scored, _ = score_sectors_by_opportunity(SectorPolicy("sec-v1", top_k=3), [a, b], {i: D(k) for k, i in enumerate(ids)})
            payload, _expl = assemble_sector_overview(
                ranking_id=S.sid("ov1"), release_id=rel, taxonomy_id="prov.concept", taxonomy_version=3, horizon_id="5td",
                forecast_target="sector_rel_opportunity", ranking_policy_version="sec-v1", scores=scored)
            repo.insert_sector_overview(c, payload, created_at=T0)
            u = apply_universe(UniversePolicy(), facts(12))
            m = matrix([f.instrument_id for f in u.included])
            for s in (a, b):
                run = build_ranking(ranking_id=S.sid("sk-" + s.node.node_id), release_id=rel, ranking_scope="sector",
                                    scope_key=f"prov.concept/{s.node.node_id}", policy=policy(), universe=restrict_universe(u, s),
                                    matrix=m, taxonomy_node=s.node.wire(), membership_snapshot_id=s.membership_snapshot_id)
                repo.insert_ranking_run(c, run, created_at=T0)
        assert repo.load_sector_overview_wire(c, S.sid("ov1")) == payload
        contracts.check("SectorOverview", repo.load_sector_overview_wire(c, S.sid("ov1")))
        wa = repo.load_ranking_wire(c, S.sid("sk-ai"))
        wb = repo.load_ranking_wire(c, S.sid("sk-chip"))
        contracts.check("Ranking", wa)
        assert {e["instrument_id"] for e in wa["entries"]} & {e["instrument_id"] for e in wb["entries"]} == {str(ids[4]), str(ids[5])}
        # the overview run is pinned to one membership snapshot; a stray entry with another one is refused
        other = S.make_manifest(c, "sec1-other-membership", dataset="membership")
        c.commit()
        with pytest.raises(psycopg.errors.RaiseException, match="differs"):
            with c.transaction():
                c.execute("""INSERT INTO decision.sector_rank_entry(ranking_id,taxonomy_id,taxonomy_version,node_id,rank_kind,rank,score,
                    member_count,observed_member_count,membership_snapshot_id,explanation_ref,quality_status)
                    VALUES (%s,'prov.concept',3,'ai','sector_opportunity_rank',9,1,1,1,%s,'x','valid')""", (S.sid("ov1"), other))


def test_plan_versions_append_only_supersede_and_late_results(pg_dsn, ids):
    with S.conn(pg_dsn) as c:
        with c.transaction():
            rel = S.make_release(c, "plan1", publish=True)
        r = compiled()
        p1 = {**r.plan, "plan_id": str(S.sid("plan-1")), "based_on_release_id": str(rel), "instrument_id": str(ids[1])}
        with c.transaction():
            plan_service.append_plan(c, p1)
        lifecycles = {s["instance_id"]: "active" for s in p1["strategy_refs"]}
        hist = repo.load_plan_history(c, S.sid("plan-1"))
        p2 = attach_validation(hist, status="passed", result_ref="val://1", validated_at=T0, available_at=T0 + timedelta(seconds=2),
                               evaluation_id=None, strategy_lifecycles=lifecycles)
        with c.transaction():
            plan_service.append_plan(c, p2)
        hist = repo.load_plan_history(c, S.sid("plan-1"))
        assert [(h["plan_version"], h["state"]) for h in hist] == [(1, "watching"), (2, "actionable")]
        assert hist[1]["supersedes"] == {"plan_id": p1["plan_id"], "plan_version": 1}
        for h in hist:
            contracts.check("TradePlan", h)
        # versions are immutable in SQL
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            with c.transaction():
                c.execute("UPDATE decision.trade_plan SET state='expired' WHERE plan_id=%s", (S.sid("plan-1"),))
        with pytest.raises(psycopg.errors.RaiseException):
            with c.transaction():
                c.execute("DELETE FROM decision.trade_plan WHERE plan_id=%s", (S.sid("plan-1"),))
        # a duplicate/skipped version is refused before SQL
        with pytest.raises(PlanVersionError):
            plan_service.append_plan(c, p2)
        # slow model result computed against v1 cannot overwrite v2
        stale = next_version(hist[0], state="watching", available_at=T0 + timedelta(seconds=3))
        late = LateResult(based_on_plan_version=1, analysis_as_of=T0 + timedelta(seconds=1), input_revoked=False,
                          critical_data_valid_until=None)
        verdict = plan_service.accept_late_model_result(c, S.sid("plan-1"), late, stale, now=T0 + timedelta(minutes=1))
        assert not verdict.may_replace_formal_plan and "superseded_by_newer_plan_version" in verdict.reasons
        assert len(repo.load_plan_history(c, S.sid("plan-1"))) == 2
        with c.transaction():  # DDL: actionable plans must carry a passed validation (contract allOf) - checked pre-insert
            with pytest.raises(contracts.ContractViolation):
                repo.append_plan_version(c, {**p2, "plan_version": 3, "supersedes": {"plan_id": p2["plan_id"], "plan_version": 2},
                                              "validation": {**p2["validation"], "status": "failed"}})


def _pool_world(c, ids):
    rel = S.make_release(c, "pool1", publish=False)
    ms = S.make_manifest(c, "pool1-membership", dataset="membership")
    return rel, ms


def test_candidate_pool_history_idempotent_and_cas(pg_dsn, ids):
    p = PoolPolicy("heat-v1", "p1", {"relative_strength_change": D("0.5"), "breadth": D("0.5")}, D("1"), D("0.5"), 1, 2,
                   frozenset({"breadth"}))
    pool, inst = S.sid("pool-A"), ids[2]
    good = assess_surge(p, SurgeComponents(relative_strength_change=D("2"), breadth=D("2")))
    with S.conn(pg_dsn) as c:
        with c.transaction():
            rel, ms = _pool_world(c, ids)
        events, state = [], None

        def step(**kw):
            nonlocal state
            new, change = decide_next_state(p, state, good, **kw)
            if change:
                events.append(make_event(pool_id=pool, instrument_id=inst, policy=p, prev=state, new=change,
                                         taxonomy=("prov.concept", 3, "ai"), membership_snapshot_id=ms, release_id=rel,
                                         plan_ref=kw.get("plan_ref"), decision_as_of=T0, available_at=T0 + timedelta(seconds=1)))
            state = new

        step(); step()
        assert [e.current_state for e in events] == ["observing", "qualified"]
        with c.transaction():
            S.publish_release(c, rel, T0 + timedelta(seconds=2))
        for e in events:
            with c.transaction():
                assert repo.append_pool_event(c, e, recorded_at=T0 + timedelta(seconds=3)) is True
        with c.transaction():
            assert repo.append_pool_event(c, events[0], recorded_at=T0 + timedelta(seconds=4)) is False  # redelivery: idempotent
        assert repo.current_pool_states(c, pool)[inst]["current_state"] == "qualified"
        wire = repo.load_pool_wire(c, pool, rel)
        assert wire["items"][0]["event_sequence"] == 2 and wire["items"][0]["current_state"] == "qualified"
        # a rival event with the same sequence loses the CAS (unique key), it never overwrites
        rival = make_event(pool_id=pool, instrument_id=inst, policy=p, prev=PoolState("observing", 1, 1),
                           new=(PoolState("removed", 2, 1), "hard_invalidation", {}), taxonomy=("prov.concept", 3, "ai"),
                           membership_snapshot_id=ms, release_id=rel, plan_ref=None, decision_as_of=T0, available_at=T0)
        object.__setattr__(rival, "pool_event_id", S.sid("rival-event"))
        with pytest.raises(psycopg.errors.UniqueViolation):
            with c.transaction():
                repo.append_pool_event(c, rival, recorded_at=T0)
        # DDL: actionable needs a plan reference
        with pytest.raises(psycopg.errors.CheckViolation):
            with c.transaction():
                c.execute("""INSERT INTO decision.candidate_pool_event(pool_event_id,pool_id,instrument_id,event_sequence,policy_id,policy_version,
                    taxonomy_id,taxonomy_version,node_id,membership_snapshot_id,release_id,previous_state,current_state,trigger_code,trigger_payload,
                    decision_as_of,available_at,recorded_at) VALUES (%s,%s,%s,3,'x','1','prov.concept',3,'ai',%s,%s,'qualified','actionable','t','{}',%s,%s,%s)""",
                          (S.sid("noplan"), pool, inst, ms, rel, T0, T0, T0))


def test_actionability_snapshot_binding_and_immutability(pg_dsn, ids):
    with S.conn(pg_dsn) as c:
        with c.transaction():
            rel = S.make_release(c, "act1", publish=True)
            quote = S.make_manifest(c, "act1-quote", dataset="quotes", ready_at=T0 + timedelta(seconds=2))
        run = None
        with c.transaction():
            rel2 = S.make_release(c, "act1-rk", publish=False)
            run = _ranking(rel2, "act1-rk")
            repo.insert_ranking_run(c, run, created_at=T0)
            S.publish_release(c, rel2, T0 + timedelta(seconds=1))
        order = [(e.instrument_id, e.action_rank) for e in run.entries]
        top = run.entries[0].instrument_id
        plan = {**compiled().plan}
        inp = ActionabilityInput(
            release_id=rel2, ranking_id=run.ranking_id, quote_snapshot_id=quote, policy_version="act-v1",
            checked_at=T0 + timedelta(seconds=5), valid_until=T0 + timedelta(seconds=65), ranking_order=order,
            plans={top: [PlanLeg((S.sid("plan-1"), 2), "buy", 300, Tri.TRUE)]},
            quotes={top: QuoteView(top, D("10"), True, True)}, account=None)
        with c.transaction():
            payload = plan_service.store_actionability(c, inp)
        contracts.check("ActionabilitySnapshot", payload)
        hdr = repo.actionability_header(c, payload["actionability_id"] and __import__("uuid").UUID(payload["actionability_id"]))
        assert hdr["ranking_id"] == run.ranking_id and hdr["release_id"] == rel2 and hdr["quote_snapshot_id"] == quote
        assert hdr["result_hash"] and hdr["account_id"] is None
        with pytest.raises(psycopg.errors.RaiseException, match="immutable|append"):
            with c.transaction():
                c.execute("UPDATE decision.actionability_snapshot SET valid_until=valid_until+interval '1 day'")
        # inputs must exist before the check: a quote not yet READY at checked_at is refused
        late_quote = S.make_manifest(c, "act1-late", dataset="quotes", ready_at=T0 + timedelta(hours=1))
        c.commit()
        bad = ActionabilityInput(**{**inp.__dict__, "quote_snapshot_id": late_quote})
        with pytest.raises(psycopg.errors.RaiseException, match="already available"):
            with c.transaction():
                plan_service.store_actionability(c, bad)
        assert plan and rel


class _Features:
    def __init__(self, drop=()):
        self.drop = set(drop)

    def matrix(self, instrument_ids, feature_ids, decision_as_of):
        m = matrix(list(instrument_ids))
        return {i: {k: (None if i in self.drop else v) for k, v in row.items()} for i, row in m.items()}


class _Writer:
    def __init__(self):
        self.calls = []

    def add_component(self, release_id, key, ref, digest):
        self.calls.append((release_id, key, ref, digest))


def test_ranking_service_stores_registers_component_and_blocks_without_side_effects(pg_dsn, ids):
    from pulse.application import ranking_service as rs
    from pulse.domain.ranking.baseline import RankingBlocked

    f = facts(12)
    with S.conn(pg_dsn) as c:
        with c.transaction():
            rel = S.make_release(c, "svc1", publish=False)
        w = _Writer()
        with c.transaction():
            built = rs.build_and_store_ranking(
                c, ranking_id=S.sid("svc-rk"), release_id=rel, ranking_scope="market", scope_key="cn.mainboard",
                universe_policy=UniversePolicy(), facts=f, policy=policy(), features=_Features(), decision_as_of=T0,
                created_at=T0, writer=w)
        assert w.calls == [(rel, "ranking", f"ranking:{S.sid('svc-rk')}", built.artifact_hash)]
        assert repo.load_ranking_wire(c, S.sid("svc-rk")) == built.run.to_wire()
        assert set(built.explanations) == {e.explanation_ref for e in built.run.entries}
        # thin coverage (every row missing a feature) blocks: nothing is stored, nothing is registered
        w2 = _Writer()
        with pytest.raises(RankingBlocked):
            with c.transaction():
                rs.build_and_store_ranking(
                    c, ranking_id=S.sid("svc-rk2"), release_id=rel, ranking_scope="market", scope_key="cn.mainboard",
                    universe_policy=UniversePolicy(), facts=f, policy=policy(), features=_Features(drop=[x.instrument_id for x in f]),
                    decision_as_of=T0, created_at=T0, writer=w2)
        assert w2.calls == []
        assert c.execute("SELECT count(*) AS n FROM decision.ranking_run WHERE ranking_id=%s", (S.sid("svc-rk2"),)).fetchone()["n"] == 0


def test_m5_actionable_entry_must_cite_a_plan_stored_as_actionable(pg_dsn, ids):
    """Review fix M5: the application layer checks the cited plan's stored state before it freezes a ranking."""
    from pulse.application.ranking_service import build_and_store_ranking
    with S.conn(pg_dsn) as c:
        with c.transaction():
            rel = S.make_release(c, "m5-act", publish=False)
            comp = compiled()
            plan = {**comp.plan, "plan_id": str(S.sid("plan-m5")), "based_on_release_id": str(rel), "instrument_id": str(ids[0])}
            repo.append_plan_version(c, plan)                                  # stored as 'watching'
        assert repo.plan_state(c, S.sid("plan-m5"), 1) == "watching" and repo.plan_state(c, S.sid("plan-m5"), 9) is None
        lie = ActionAssessment(ids[0], "actionable", (), (S.sid("plan-m5"), 1), "actionable")
        with pytest.raises(ValueError, match="not stored in state"):
            build_and_store_ranking(c, ranking_id=S.sid("rk-m5"), release_id=rel, ranking_scope="market", scope_key="x", universe_policy=None,
                                    facts=[], policy=None, features=None, decision_as_of=T0, created_at=T0, actions={ids[0]: lie})
