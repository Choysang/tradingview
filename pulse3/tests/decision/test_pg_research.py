"""W09 persistence on real PostgreSQL 18: registry seed, lifecycle CAS, contexts, trials, league, promotion."""
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from uuid import UUID

import psycopg
import pytest

from pulse import contracts
from pulse.adapters.postgres import research_repo as rr
from pulse.adapters.postgres import strategy_repo as sr
from pulse.application import research_service as svc
from pulse.domain.research.experiments import BlindBudgetExceeded, complete, fail, new_run
from pulse.domain.research.league import (
    Candidate,
    ComparisonPolicy,
    LeagueHistory,
    SideEvidence,
    classify_pairing,
)
from pulse.domain.strategy import promotion as P
from pulse.domain.strategy.lifecycle import LifecycleTransition

from . import pg_seed as S
from .helpers import T0, uid
from .test_strategy_research import POLICY, good_validated, spec

pytestmark = pytest.mark.pg
CAT = Path(__file__).resolve().parents[2] / "spec_package" / "catalogs"


@pytest.fixture(scope="module")
def world(pg_dsn):
    S.seed_base(pg_dsn)
    with S.conn(pg_dsn) as c, c.transaction():
        counts = svc.seed_registry(c, CAT, now=T0)
    return counts


def test_seed_registry_counts_candidates_and_idempotent(pg_dsn, world):
    assert world["strategies"] == {"definitions": 61, "instances": 61, "tags": world["strategies"]["tags"]}
    assert world["factors"]["definitions"] == 105 and world["factors"]["instances"] == 105
    with S.conn(pg_dsn) as c:
        counts = sr.lifecycle_counts(c)
        assert counts == {"discovered": 166}  # 61 + 105 candidates, none beyond discovered, none active
        n_tags = c.execute("SELECT count(*) AS n FROM strategy.tag_assignment").fetchone()["n"]
        assert n_tags == world["strategies"]["tags"] + world["factors"]["tags"] > 400
        with c.transaction():
            again = svc.seed_registry(c, CAT, now=T0 + timedelta(days=1))
        assert again["strategies"] == {"definitions": 0, "instances": 0, "tags": 0}
        assert again["factors"] == {"definitions": 0, "instances": 0, "tags": 0}
        roles = {r["role"] for r in c.execute("SELECT DISTINCT role FROM strategy.definition").fetchall()}
        assert roles <= {"selection", "allocation", "entry", "holding", "execution", "exit"}
        # the contract view of a stored definition
        row = c.execute("SELECT * FROM strategy.definition WHERE specification->>'catalog_id'='S001'").fetchone()
        assert row["role"] == "entry" and row["method_family"] == "rule_based"


def _s001(c):
    return c.execute("SELECT i.instance_id,i.strategy_id,i.strategy_version FROM strategy.instance i "
                     "JOIN strategy.definition d USING(strategy_id,strategy_version) WHERE d.specification->>'catalog_id'=%s",
                     ("S001",)).fetchone()


def test_lifecycle_cas_and_legality(pg_dsn, world):
    with S.conn(pg_dsn) as c:
        inst = _s001(c)["instance_id"]
        tr = LifecycleTransition(str(inst), "discovered", "specified", "typed spec reviewed", T0, "promo-v1")
        with c.transaction():
            sr.apply_lifecycle(c, tr)
        assert sr.get_instance(c, inst)["lifecycle"] == "specified"
        with pytest.raises(sr.LifecycleConflict):  # stale decision against the old state
            with c.transaction():
                sr.apply_lifecycle(c, tr)
        with pytest.raises(Exception, match="illegal lifecycle"):
            sr.apply_lifecycle(c, LifecycleTransition(str(inst), "specified", "active", "skip", T0, "p"))


def test_context_trials_blind_budget_and_failures_recorded(pg_dsn, world):
    with S.conn(pg_dsn) as c:
        with c.transaction():
            snap = S.make_manifest(c, "ctx-bars", dataset="bars_raw_1d")
        sp = spec(inputs=(__import__("pulse.domain.research.context", fromlist=["x"]).InputSnapshot(snap, "bars_raw_1d", 1, "b" * 64, "bars"),),
                  resource_budget={"max_trials": 50, "blind_access_budget": 1})
        with c.transaction():
            ctx = rr.seal_context(c, sp, created_at=T0, sealed_at=T0 + timedelta(seconds=1))
        with c.transaction():
            assert rr.seal_context(c, sp, created_at=T0, sealed_at=T0) == ctx  # same spec -> same sealed context
        inst = _s001(c)
        hyp = uid("hyp-s001")
        with c.transaction():
            rr.register_hypothesis(c, hypothesis_id=hyp, hypothesis_version=1, claim="SMA20/60 cross improves entry timing",
                                   mechanism="trend persistence", forecast_target="rel_opportunity_5td", horizon_id="5td",
                                   input_requirements={"bars": "1d"}, falsification_conditions={"holdout_increment_lower": "<=0"},
                                   available_at=T0, strategy=(inst["strategy_id"], inst["strategy_version"]))

        def run(seed, purpose="screening"):
            return new_run(instance_id=inst["instance_id"], hypothesis_id=hyp, hypothesis_version=1, evaluation_context_id=ctx,
                           run_purpose=purpose, implementation_digest="impl-1", random_seed=seed, run_manifest={}, family_key="trend_price_transform")

        r1, r2, r3 = run(1), run(2), run(3)
        with c.transaction():
            for r in (r1, r2, r3):
                assert rr.register_experiment(c, r) is True
            assert rr.register_experiment(c, r1) is False  # idempotent
        # metrics before completion, then completion; failure keeps its error_code
        metric = {"metric_name": "excess_return_twr", "metric_definition_version": "m1", "slice_key": "overall", "value": D("0.01"),
                  "unit": "ratio", "lower_bound": D("0.001"), "upper_bound": D("0.02"), "confidence_level": D("0.95"),
                  "sample_count": 100, "quality_status": "valid", "missing_reason": None}
        with c.transaction():
            rr.insert_metrics(c, r1.experiment_id, [metric], computed_at=T0)
            rr.finish_experiment(c, complete(r1, started_at=T0, completed_at=T0 + timedelta(minutes=1), result_ref="res://1", result_hash="d" * 64))
            rr.finish_experiment(c, fail(r2, started_at=T0, completed_at=T0 + timedelta(minutes=1), error_code="DATA_GAP"))
        row = c.execute("SELECT run_state,error_code,result_ref FROM research.experiment_run WHERE experiment_id=%s", (r2.experiment_id,)).fetchone()
        assert row == {"run_state": "failed", "error_code": "DATA_GAP", "result_ref": None}  # failure is not an empty success
        assert rr.trial_count(c, "trend_price_transform") == 3  # all trials, failed and still-queued ones included
        with pytest.raises(psycopg.errors.RaiseException, match="immutable"):  # completed metrics are sealed
            with c.transaction():
                rr.insert_metrics(c, r1.experiment_id, [{**metric, "slice_key": "x"}], computed_at=T0)
        with pytest.raises(psycopg.errors.RaiseException):  # an unsealed/unknown context cannot host runs
            with c.transaction():
                c.execute("UPDATE research.evaluation_context SET cost_policy_version='x' WHERE evaluation_context_id=%s", (ctx,))
        with c.transaction():
            assert rr.register_experiment(c, run(10, "blind_final"))
        with pytest.raises(BlindBudgetExceeded):
            with c.transaction():
                rr.register_experiment(c, run(11, "blind_final"))


def _league_world(c):
    """Two comparable instances with completed non-blind runs in one sealed context."""
    snap = S.make_manifest(c, "lg-bars", dataset="bars_raw_1d")
    from pulse.domain.research.context import InputSnapshot

    sp = spec(inputs=(InputSnapshot(snap, "bars_raw_1d", 2, "b" * 64, "bars"),), resource_budget={"max_trials": 50, "blind_access_budget": 1})
    ctx = rr.seal_context(c, sp, created_at=T0, sealed_at=T0 + timedelta(seconds=1))
    insts = c.execute("SELECT instance_id,strategy_id,strategy_version FROM strategy.instance ORDER BY instance_id LIMIT 4").fetchall()
    hyp = uid("hyp-lg")
    rr.register_hypothesis(c, hypothesis_id=hyp, hypothesis_version=1, claim="c", mechanism="m", forecast_target="t", horizon_id="5td",
                           input_requirements={}, falsification_conditions={}, available_at=T0)
    runs = {}
    for i in insts:
        r = new_run(instance_id=i["instance_id"], hypothesis_id=hyp, hypothesis_version=1, evaluation_context_id=ctx, run_purpose="screening",
                    implementation_digest="impl-lg", random_seed=5, run_manifest={}, family_key="lg")
        if rr.register_experiment(c, r):  # deterministic ids: a second call reuses the completed runs
            rr.finish_experiment(c, complete(r, started_at=T0, completed_at=T0 + timedelta(minutes=1),
                                             result_ref="res://" + str(i["instance_id"]), result_hash="e" * 64))
        runs[i["instance_id"]] = r.experiment_id
    return ctx, insts, runs


def test_league_rounds_seed_bye_results_and_contract(pg_dsn, world):
    with S.conn(pg_dsn) as c:
        with c.transaction():
            ctx, insts, runs = _league_world(c)
        lid = uid("league-1")
        with c.transaction():
            with pytest.raises(ValueError, match="base_seed"):
                rr.create_league(c, league_id=lid, league_version=1, evaluation_context_id=ctx, display_name="x", pairing_policy_version="swiss-v1",
                                 multiple_testing_policy_version="mt-1", promotion_policy_version="promo-v1", round_budget=3,
                                 policy_document={}, created_at=T0)
            rr.create_league(c, league_id=lid, league_version=1, evaluation_context_id=ctx, display_name="selection/5td",
                             pairing_policy_version="swiss-v1", multiple_testing_policy_version="mt-1", promotion_policy_version="promo-v1",
                             round_budget=3, policy_document={"base_seed": "seed-lg-1", "max_repeat": 1}, created_at=T0)
        cands = [Candidate(str(i["instance_id"]), "selection", "5td", "u1", "t1", "h" * 64, "b1", f"fam{n}", f"id{n}")
                 for n, i in enumerate(insts[:3])]  # 3 candidates -> one bye
        hist = LeagueHistory("seed-lg-1")
        with c.transaction():
            plans = svc.schedule_league_round(c, (lid, 1), cands, hist, 1, scheduled_at=T0)
        pairings = [p for pl in plans for p in pl.pairings]
        assert sum(p.outcome == "bye" for p in pairings) == 1
        real = next(p for p in pairings if p.right)
        pol = ComparisonPolicy("cmp-1", 30, D("0.002"))
        res, reason = classify_pairing(SideEvidence(True, False, True, D("0.001"), D("0.01"), 100), pol)
        assert res == "win"
        with c.transaction():
            rr.record_pairing_result(c, real.pairing_id, result=res, reason=reason, comparison_policy_version="cmp-1",
                                     left_experiment_id=runs[UUID(real.left)], right_experiment_id=runs[UUID(real.right)],
                                     score_delta=D("0.005"), completed_at=T0 + timedelta(hours=1))
        with c.transaction():
            rr.close_round(c, (lid, 1), 1, result_ref="round://1", completed_at=T0 + timedelta(hours=1))
        wire = rr.load_league_wire(c, lid, 1)
        contracts.check("League", wire)
        outs = sorted(p["outcome"] for r in wire["rounds"] for p in r["pairings"])
        assert outs == ["bye", "left"] and wire["rounds"][0]["round_state"] == "completed"
        # closed rounds are sealed; a policy-document seed is stored for replay
        with pytest.raises(psycopg.errors.RaiseException, match="Closed"):
            with c.transaction():
                c.execute("UPDATE research.league_round SET round_state='running' WHERE league_id=%s", (lid,))
        stored = c.execute("SELECT policy_document FROM research.league_definition WHERE league_id=%s", (lid,)).fetchone()
        assert stored["policy_document"]["base_seed"] == "seed-lg-1"
        # insufficient evidence keeps its own outcome value with its reason, never a loss
        with c.transaction():
            plans2 = svc.schedule_league_round(c, (lid, 1), cands, hist, 2, scheduled_at=T0 + timedelta(days=1))
        p2 = next(p for pl in plans2 for p in pl.pairings if p.right)
        with c.transaction():
            rr.record_pairing_result(c, p2.pairing_id, result="insufficient_evidence", reason="interval_straddles_zero_wider_than_tie_margin",
                                     comparison_policy_version="cmp-1", left_experiment_id=runs[UUID(p2.left)],
                                     right_experiment_id=runs[UUID(p2.right)], score_delta=None, completed_at=T0)
        row = c.execute("SELECT outcome,reason_ref FROM research.league_pairing WHERE pairing_id=%s", (p2.pairing_id,)).fetchone()
        assert row["outcome"] == "insufficient_evidence" and row["reason_ref"] == "interval_straddles_zero_wider_than_tie_margin"
        # DDL: the same instance cannot appear twice in one round
        with pytest.raises(psycopg.errors.RaiseException, match="only once"):
            with c.transaction():
                c.execute("""INSERT INTO research.league_pairing(pairing_id,league_id,league_version,round_no,left_instance_id,right_instance_id,
                    outcome,comparison_policy_version,reason_ref) VALUES (%s,%s,1,2,%s,NULL,'bye','x','dup')""",
                          (uid("dup-pair"), lid, UUID(p2.left)))


def test_promotion_decision_apply_and_league_independence(pg_dsn, world):
    with S.conn(pg_dsn) as c:
        with c.transaction():
            ctx, insts, runs = _league_world(c)
        inst = insts[0]["instance_id"]
        before = sr.get_instance(c, inst)["lifecycle"]
        # a rejected decision is stored (with every gate) and cannot be applied
        rej = P.decide_promotion(POLICY, instance_id=str(inst), evaluation_experiment_id=str(runs[inst]), from_state=before,
                                 target_state="specified" if before == "discovered" else "data_ready",
                                 evidence=P.PromotionEvidence(), decided_at=T0)
        assert rej.outcome == "rejected"
        with c.transaction():
            pid = svc.record_and_maybe_apply(c, rej, decided_key="r1", apply_now=True, now=T0)
        assert sr.get_instance(c, inst)["lifecycle"] == before
        with pytest.raises(ValueError, match="approved"):
            with c.transaction():
                rr.apply_promotion(c, pid, applied_at=T0)
        stored = rr.promotion_history(c, inst)[0]
        assert stored["outcome"] == "rejected" and len(stored["gate_results"]) >= 1
        # an approved decision records old/new state and reasons, then applies exactly once (CAS)
        target = "specified" if before == "discovered" else "data_ready"
        ok = P.decide_promotion(POLICY, instance_id=str(inst), evaluation_experiment_id=str(runs[inst]), from_state=before,
                                target_state=target, evidence=P.PromotionEvidence(definition_typed=True, data_requirements_met=True),
                                decided_at=T0 + timedelta(hours=2))
        assert ok.outcome == "approved"
        with c.transaction():
            pid2 = svc.record_and_maybe_apply(c, ok, decided_key="a1", apply_now=True, now=T0 + timedelta(hours=2))
        assert sr.get_instance(c, inst)["lifecycle"] == target
        with c.transaction():
            rr.apply_promotion(c, pid2, applied_at=T0 + timedelta(hours=3))  # second apply is a no-op
        hist = rr.promotion_history(c, inst)
        assert [(h["from_lifecycle"], h["to_lifecycle"], h["outcome"]) for h in hist] == [(before, before, "rejected"), (before, target, "approved")]
        assert hist[1]["applied_at"] == T0 + timedelta(hours=2) and "gates passed" in hist[1]["reason"]
        # DDL: a promotion cannot cite another instance's experiment
        other = insts[1]["instance_id"]
        bad = P.decide_promotion(POLICY, instance_id=str(other), evaluation_experiment_id=str(runs[inst]), from_state="discovered",
                                 target_state="specified", evidence=P.PromotionEvidence(definition_typed=True), decided_at=T0)
        with pytest.raises(psycopg.errors.RaiseException, match="belong"):
            with c.transaction():
                svc.record_and_maybe_apply(c, bad, decided_key="x", apply_now=False, now=T0)
        assert good_validated()


def test_composite_lineage_is_stored_and_immutable(pg_dsn, world):
    from pulse.domain.strategy.composition import CompositionSpec, ParentRef, build_composite_definition

    with S.conn(pg_dsn) as c:
        rows = c.execute("SELECT strategy_id,strategy_version FROM strategy.definition WHERE method_family='rule_based' ORDER BY strategy_id LIMIT 2").fetchall()
        spec_ = CompositionSpec("lineage-test", "selection", "t1", "5td",
                                (ParentRef(rows[0]["strategy_id"], rows[0]["strategy_version"], "selection", D("0.5"), "ablation://1"),
                                 ParentRef(rows[1]["strategy_id"], rows[1]["strategy_version"], "blend", D("0.5"), "ablation://2")),
                                "route-v1", {}, "baseline-v1", "comp-v1")
        d, parents = build_composite_definition(spec_)
        with c.transaction():
            rr.register_composite(c, d, parents, composition_policy_version="comp-v1", now=T0)
            rr.register_composite(c, d, parents, composition_policy_version="comp-v1", now=T0)  # idempotent
        lin = rr.lineage(c, d.strategy_id, d.strategy_version)
        assert len(lin) == 2 and {x["contribution_role"] for x in lin} == {"selection", "blend"}
        assert c.execute("SELECT method_family FROM strategy.definition WHERE strategy_id=%s", (d.strategy_id,)).fetchone()["method_family"] == "composite"
        with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
            with c.transaction():
                c.execute("DELETE FROM research.strategy_parent WHERE strategy_id=%s", (d.strategy_id,))


def test_accepted_schema_proposals_are_migrated(pg_dsn):
    """Proposals were accepted as migrations 0008/0009; the tables exist in every migrated database."""
    with S.conn(pg_dsn) as c:
        exists = c.execute("SELECT to_regclass('decision.plan_body')::text AS a, to_regclass('strategy.lifecycle_event')::text AS b,"
                           " to_regclass('decision.actionability_result')::text AS r").fetchone()
    assert exists == {"a": "decision.plan_body", "b": "strategy.lifecycle_event", "r": "decision.actionability_result"}


# ---- review fix M2: insufficient_evidence / execution_failed use their real DB enum values and are never overwritten
def _two_real_pairings(c, name):
    with c.transaction():
        ctx, insts, runs = _league_world(c)
    lid = uid(name)
    with c.transaction():
        rr.create_league(c, league_id=lid, league_version=1, evaluation_context_id=ctx, display_name=name,
                         pairing_policy_version="swiss-v1", multiple_testing_policy_version="mt-1", promotion_policy_version="promo-v1",
                         round_budget=3, policy_document={"base_seed": "seed-" + name, "max_repeat": 1}, created_at=T0)
    cands = [Candidate(str(i["instance_id"]), "selection", "5td", "u1", "t1", "h" * 64, "b1", f"fam{n}", f"id{n}")
             for n, i in enumerate(insts[:4])]
    with c.transaction():
        plans = svc.schedule_league_round(c, (lid, 1), cands, LeagueHistory("seed-" + name), 1, scheduled_at=T0)
    real = [p for pl in plans for p in pl.pairings if p.right]
    assert len(real) == 2
    return lid, real, runs


def test_m2_unresolved_results_use_real_enum_values_and_are_never_overwritten(pg_dsn, world):
    from pulse.domain.research.league import to_db_outcome
    assert to_db_outcome("insufficient_evidence") == "insufficient_evidence" and to_db_outcome("execution_failed") == "execution_failed"
    with S.conn(pg_dsn) as c:
        lid, (a, b), runs = _two_real_pairings(c, "league-m2")
        with c.transaction():
            rr.record_pairing_result(c, a.pairing_id, result="insufficient_evidence", reason="interval_straddles_zero_wider_than_tie_margin",
                                     comparison_policy_version="cmp-1", left_experiment_id=runs[UUID(a.left)],
                                     right_experiment_id=runs[UUID(a.right)], score_delta=None, completed_at=T0)
            rr.record_pairing_result(c, b.pairing_id, result="execution_failed", reason="execution_failed_not_a_zero_return",
                                     comparison_policy_version="cmp-1", left_experiment_id=None, right_experiment_id=None,
                                     score_delta=None, completed_at=T0)
        rows = {r["pairing_id"]: r for r in c.execute("SELECT * FROM research.league_pairing WHERE league_id=%s AND outcome<>'bye'", (lid,))}
        assert rows[a.pairing_id]["outcome"] == "insufficient_evidence" and rows[a.pairing_id]["completed_at"] is not None
        assert rows[b.pairing_id]["outcome"] == "execution_failed"
        assert rows[a.pairing_id]["reason_ref"] == "interval_straddles_zero_wider_than_tie_margin"
        # a later result cannot rewrite a settled pairing; the same redelivery is a no-op
        with pytest.raises(rr.PairingAlreadyResolved):
            with c.transaction():
                rr.record_pairing_result(c, a.pairing_id, result="win", reason="left_interval_above_zero", comparison_policy_version="cmp-1",
                                         left_experiment_id=runs[UUID(a.left)], right_experiment_id=runs[UUID(a.right)],
                                         score_delta=D("0.01"), completed_at=T0 + timedelta(hours=1))
        with c.transaction():
            rr.record_pairing_result(c, a.pairing_id, result="insufficient_evidence", reason="interval_straddles_zero_wider_than_tie_margin",
                                     comparison_policy_version="cmp-1", left_experiment_id=runs[UUID(a.left)],
                                     right_experiment_id=runs[UUID(a.right)], score_delta=None, completed_at=T0 + timedelta(hours=2))
        assert c.execute("SELECT outcome, completed_at FROM research.league_pairing WHERE pairing_id=%s", (a.pairing_id,)).fetchone() == \
            {"outcome": "insufficient_evidence", "completed_at": T0}


def test_m2_wire_maps_to_contract_values_and_the_api_warns(pg_dsn, world, monkeypatch):
    import uuid
    from fastapi.testclient import TestClient
    with S.conn(pg_dsn) as c:
        lid, (a, b), runs = _two_real_pairings(c, "league-m2-wire")
        with c.transaction():
            rr.record_pairing_result(c, a.pairing_id, result="insufficient_evidence", reason="interval_or_sample_missing",
                                     comparison_policy_version="cmp-1", left_experiment_id=runs[UUID(a.left)],
                                     right_experiment_id=runs[UUID(a.right)], score_delta=None, completed_at=T0)
            rr.record_pairing_result(c, b.pairing_id, result="execution_failed", reason="execution_failed_not_a_zero_return",
                                     comparison_policy_version="cmp-1", left_experiment_id=None, right_experiment_id=None,
                                     score_delta=None, completed_at=T0)
        wire, gaps = rr.load_league(c, lid, 1)
        contracts.check("League", wire)
        by = {p["pairing_id"]: p for r in wire["rounds"] for p in r["pairings"]}
        assert by[str(a.pairing_id)]["outcome"] == "pending" and by[str(a.pairing_id)]["reason_ref"].startswith("insufficient_evidence:")
        assert by[str(b.pairing_id)]["outcome"] == "incomparable" and by[str(b.pairing_id)]["reason_ref"].startswith("execution_failed:")
        assert {g["db_outcome"] for g in gaps} == {"insufficient_evidence", "execution_failed"}
    monkeypatch.setenv("PULSE_DSN", pg_dsn)
    monkeypatch.setenv("PULSE_ENV", "dev")
    monkeypatch.setenv("PULSE_API_HOST", "127.0.0.1")
    monkeypatch.setenv("PULSE_DEV_PRINCIPAL", str(uuid.UUID(int=7)))
    from pulse.api import deps
    from pulse.api.app import create_app
    deps.close_pool()
    with TestClient(create_app()) as client:
        r = client.get(f"/v1/leagues/{lid}/versions/1")
    deps.close_pool()
    assert r.status_code == 200, r.text
    warn = [w for w in r.json()["meta"]["warnings"] if w["code"] == "LEAGUE_OUTCOME_MAPPED"]
    assert len(warn) == 1 and "insufficient_evidence" in warn[0]["message"] and "execution_failed" in warn[0]["message"]


# ---- review fix M5: a trial budget needs an applied, trial-admitting promotion; actionable needs an actionable plan state
def _approved(c, instance_id, exp_id, to_lifecycle, *, applied, name):
    from psycopg.types.json import Jsonb
    pid = uid("promo-" + name)
    c.execute("INSERT INTO research.promotion_decision(promotion_id,instance_id,evaluation_experiment_id,policy_version,from_lifecycle,to_lifecycle,"
              "outcome,gate_results,reason,decided_at,applied_at) VALUES (%s,%s,%s,'promo-v1','validated',%s,'approved',%s,'test',%s,%s)",
              (pid, instance_id, exp_id, to_lifecycle, Jsonb([]), T0, T0 if applied else None))
    return pid


def test_m5_trial_budget_requires_applied_promotion_that_admits_trial(pg_dsn, world):
    from pulse.domain.ranking.run import TrialCandidate
    with S.conn(pg_dsn) as c:
        with c.transaction():
            ctx, insts, runs = _league_world(c)
        a, b = insts[2]["instance_id"], insts[3]["instance_id"]

        def cand(inst, promo):
            return TrialCandidate(str(inst), D("0.05"), {}, "s", inst, promo)
        with c.transaction():
            shadow = _approved(c, a, runs[a], "shadow", applied=True, name="m5-shadow")
            unapplied = _approved(c, a, runs[a], "shadow", applied=False, name="m5-unapplied")
            validated = _approved(c, b, runs[b], "validated", applied=True, name="m5-validated")
            c.execute("UPDATE strategy.instance SET lifecycle='shadow' WHERE instance_id=%s", (a,))
            c.execute("UPDATE strategy.instance SET lifecycle='validated' WHERE instance_id=%s", (b,))
        rr.verify_trial_candidates(c, [cand(a, shadow)])                                       # applied, admitting: ok
        for bad, why in ((cand(a, uid("promo-missing")), "does not exist"), (cand(a, unapplied), "not an applied approval"),
                         (cand(b, validated), "does not admit"), (cand(b, shadow), "another instance")):
            with pytest.raises(rr.TrialBudgetNotApproved, match=why):
                rr.verify_trial_candidates(c, [bad])
        with c.transaction():                                                                   # paused afterwards: the budget is gone
            c.execute("UPDATE strategy.instance SET lifecycle='paused' WHERE instance_id=%s", (a,))
        with pytest.raises(rr.TrialBudgetNotApproved, match="does not admit"):
            rr.verify_trial_candidates(c, [cand(a, shadow)])
        with c.transaction():
            c.execute("UPDATE strategy.instance SET lifecycle='discovered' WHERE instance_id IN (%s,%s)", (a, b))


def test_m5_actionable_entry_needs_an_actionable_plan_state(pg_dsn, world):
    from pulse.domain.ranking.run import ActionAssessment
    ref = (uid("m5-plan"), 1)
    with pytest.raises(ValueError, match="actionable plan"):
        ActionAssessment(uid("m5-i"), "actionable", (), ref)                       # plan state unknown
    with pytest.raises(ValueError, match="actionable plan"):
        ActionAssessment(uid("m5-i"), "actionable", (), ref, "watching")
    assert ActionAssessment(uid("m5-i"), "actionable", (), ref, "actionable").status == "actionable"


def test_m5_compiler_has_no_admitted_lifecycles_override():
    import inspect
    from pulse.domain.plans import compiler
    assert "admitted_lifecycles" not in inspect.signature(compiler.compile_plan).parameters
    assert "admitted_lifecycles" not in inspect.signature(compiler.attach_validation).parameters
