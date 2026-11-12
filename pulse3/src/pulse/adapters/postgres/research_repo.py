"""PostgreSQL research registry: hypotheses, contexts, experiments, metrics, league, promotion.

Why: M3-E05 needs every trial recorded (including failures), comparable only inside a sealed
EvaluationContext, league pairings with saved seeds, and promotion decisions that are
separate events. DDL triggers already enforce: sealed contexts only, completed runs/metrics
immutable, one appearance per round, comparable non-blind completed runs in pairings,
promotion experiment belongs to the instance.
Invariants: nothing is deleted; a failed experiment keeps its error_code; the blind-final
access budget of the context is enforced here (DDL has no counter); applying a promotion is a
compare-and-set on the instance lifecycle and only for ``approved`` decisions; the league
rank is stored as pairing outcomes and never touches lifecycle or weights.
Not covered: executing experiments/computing metrics; persistent lifecycle events for steps
that have no experiment (proposal migration).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from pulse.adapters.postgres import strategy_repo
from pulse.adapters.postgres.strategy_repo import LifecycleConflict
from pulse.domain.research.context import EvaluationContextSpec
from pulse.domain.research.experiments import BlindBudgetExceeded, ExperimentRecord
from pulse.domain.research.league import PlannedPairing, to_db_outcome
from pulse.domain.strategy.canonical import canonical_hash
from pulse.domain.strategy.promotion import PromotionDecisionRecord
from pulse.contracts import to_wire


def register_hypothesis(
    conn: psycopg.Connection, *, hypothesis_id: UUID, hypothesis_version: int, claim: str, mechanism: str,
    forecast_target: str, horizon_id: str, input_requirements: dict[str, Any],
    falsification_conditions: dict[str, Any], available_at: datetime,
    strategy: tuple[UUID, int] | None = None,
) -> None:
    spec_hash = canonical_hash([claim, mechanism, forecast_target, horizon_id, input_requirements, falsification_conditions])
    conn.execute(
        """INSERT INTO research.hypothesis(hypothesis_id,hypothesis_version,paper_id,paper_revision,strategy_id,strategy_version,
               claim,mechanism,forecast_target,horizon_id,input_requirements,falsification_conditions,specification_hash,available_at)
           VALUES (%s,%s,NULL,NULL,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
        (hypothesis_id, hypothesis_version, None if strategy is None else strategy[0], None if strategy is None else strategy[1],
         claim, mechanism, forecast_target, horizon_id, Jsonb(input_requirements), Jsonb(falsification_conditions),
         spec_hash, available_at),
    )


def seal_context(conn: psycopg.Connection, spec: EvaluationContextSpec, *, created_at: datetime, sealed_at: datetime) -> UUID:
    """Create (unsealed), attach inputs, seal. Same spec twice returns the same id."""
    ctx_id, ctx_hash = spec.context_id(), spec.context_hash()
    if conn.execute("SELECT 1 FROM research.evaluation_context WHERE evaluation_context_id=%s", (ctx_id,)).fetchone():
        return ctx_id
    conn.execute(
        """INSERT INTO research.evaluation_context(evaluation_context_id,context_hash,forecast_target,horizon_id,decision_as_of,
               availability_policy_version,availability_policy_document,universe_policy_version,market_rules_version,
               execution_policy_version,cost_policy_version,ledger_policy_version,performance_policy_version,numeric_policy_version,
               train_start,train_end,validation_start,validation_end,blind_start,blind_end,embargo_seconds,
               benchmark_specification,access_policy,created_at,sealed_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL)""",
        (ctx_id, ctx_hash, spec.forecast_target, spec.horizon_id, spec.decision_as_of, spec.availability_policy_version,
         Jsonb({"mode": spec.availability_mode, "rule_ref": spec.availability_rule_ref,
                "assumed_ingestion_delay_ms": spec.assumed_ingestion_delay_ms, "assumption_note": spec.assumption_note}),
         spec.universe_policy_version, spec.market_rules_version, spec.execution_policy_version, spec.cost_policy_version,
         spec.ledger_policy_version, spec.performance_policy_version, spec.numeric_policy_version, spec.train_start,
         spec.train_end, spec.validation_start, spec.validation_end, spec.blind_start, spec.blind_end, spec.embargo_seconds,
         Jsonb(spec.benchmark_specification), Jsonb(spec.access_policy()), created_at),
    )
    for i in spec.inputs:
        conn.execute("INSERT INTO research.evaluation_input(evaluation_context_id,snapshot_id,input_role) VALUES (%s,%s,%s)",
                     (ctx_id, i.snapshot_id, i.input_role))
    conn.execute("UPDATE research.evaluation_context SET sealed_at=%s WHERE evaluation_context_id=%s", (sealed_at, ctx_id))
    return ctx_id


def register_experiment(conn: psycopg.Connection, rec: ExperimentRecord) -> bool:
    """Register a run BEFORE it executes. Idempotent; enforces the blind-final access budget."""
    if conn.execute("SELECT 1 FROM research.experiment_run WHERE experiment_id=%s", (rec.experiment_id,)).fetchone():
        return False
    if rec.run_purpose == "blind_final":
        budget = conn.execute("SELECT access_policy->>'blind_access_budget' AS b FROM research.evaluation_context "
                              "WHERE evaluation_context_id=%s", (rec.evaluation_context_id,)).fetchone()
        used = conn.execute("SELECT count(*) AS n FROM research.experiment_run WHERE evaluation_context_id=%s AND run_purpose='blind_final'",
                            (rec.evaluation_context_id,)).fetchone()["n"]
        if used >= int(budget["b"] or 1):
            raise BlindBudgetExceeded("blind_final access budget exhausted for this evaluation context")
    manifest = {**rec.run_manifest, "family_key": rec.family_key}
    conn.execute(
        """INSERT INTO research.experiment_run(experiment_id,instance_id,hypothesis_id,hypothesis_version,evaluation_context_id,
               run_purpose,run_state,implementation_digest,random_seed,run_manifest,lease_epoch,started_at,completed_at,
               result_ref,result_hash,error_code)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,NULL,NULL,NULL,NULL)""",
        (rec.experiment_id, rec.instance_id, rec.hypothesis_id, rec.hypothesis_version, rec.evaluation_context_id,
         rec.run_purpose, rec.run_state, rec.implementation_digest, rec.random_seed, Jsonb(manifest), rec.lease_epoch),
    )
    return True


def insert_metrics(conn: psycopg.Connection, experiment_id: UUID, metrics: list[dict[str, Any]], *, computed_at: datetime) -> None:
    """Metric rows (wire MetricValue dicts) - allowed only while the run is not completed."""
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO research.metric_value(experiment_id,metric_name,metric_definition_version,slice_key,value,unit,lower_bound,
                   upper_bound,confidence_level,sample_count,quality_status,missing_reason,computed_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            [(experiment_id, m["metric_name"], m["metric_definition_version"], m["slice_key"], m["value"], m["unit"],
              m["lower_bound"], m["upper_bound"], m["confidence_level"], m["sample_count"], m["quality_status"],
              m["missing_reason"], computed_at) for m in metrics],
        )


def finish_experiment(conn: psycopg.Connection, rec: ExperimentRecord) -> None:
    """Persist the terminal state of a run (completed / failed / cancelled)."""
    cur = conn.execute(
        """UPDATE research.experiment_run SET run_state=%s,started_at=%s,completed_at=%s,result_ref=%s,result_hash=%s,error_code=%s
            WHERE experiment_id=%s AND run_state IN ('queued','running')""",
        (rec.run_state, rec.started_at, rec.completed_at, rec.result_ref, rec.result_hash, rec.error_code, rec.experiment_id),
    )
    if cur.rowcount != 1:
        raise RuntimeError("experiment is not open (already closed or unknown)")


def trial_count(conn: psycopg.Connection, family_key: str) -> int:
    """All registered trials of a family, failed and cancelled ones included."""
    return conn.execute("SELECT count(*) AS n FROM research.experiment_run WHERE run_manifest->>'family_key'=%s",
                        (family_key,)).fetchone()["n"]


# ------------------------------------------------------------------ league


def create_league(
    conn: psycopg.Connection, *, league_id: UUID, league_version: int, evaluation_context_id: UUID, display_name: str,
    pairing_policy_version: str, multiple_testing_policy_version: str, promotion_policy_version: str, round_budget: int,
    policy_document: dict[str, Any], created_at: datetime,
) -> None:
    """Freeze a league definition; ``policy_document`` must carry the saved ``base_seed``."""
    if "base_seed" not in policy_document:
        raise ValueError("a league must save its pairing base_seed")
    h = canonical_hash([league_id, league_version, evaluation_context_id, pairing_policy_version,
                        multiple_testing_policy_version, promotion_policy_version, round_budget, policy_document])
    conn.execute(
        """INSERT INTO research.league_definition(league_id,league_version,evaluation_context_id,display_name,pairing_policy_version,
               multiple_testing_policy_version,promotion_policy_version,round_budget,policy_document,definition_hash,created_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (league_id, league_version, evaluation_context_id, display_name, pairing_policy_version, multiple_testing_policy_version,
         promotion_policy_version, round_budget, Jsonb(policy_document), h, created_at),
    )


def schedule_round(conn: psycopg.Connection, league: tuple[UUID, int], round_no: int, pairings: list[PlannedPairing],
                   *, scheduled_at: datetime) -> None:
    conn.execute(
        "INSERT INTO research.league_round(league_id,league_version,round_no,round_state,scheduled_at) VALUES (%s,%s,%s,'scheduled',%s)",
        (league[0], league[1], round_no, scheduled_at),
    )
    for p in pairings:
        conn.execute(
            """INSERT INTO research.league_pairing(pairing_id,league_id,league_version,round_no,left_instance_id,right_instance_id,
                   outcome,comparison_policy_version,reason_ref) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (p.pairing_id, league[0], league[1], round_no, UUID(p.left), None if p.right is None else UUID(p.right),
             p.outcome, "pending-comparison", p.reason),
        )


def record_pairing_result(
    conn: psycopg.Connection, pairing_id: UUID, *, result: str, reason: str, comparison_policy_version: str,
    left_experiment_id: UUID | None, right_experiment_id: UUID | None, score_delta: Any, completed_at: datetime,
) -> None:
    """Store win/loss/draw/pending/insufficient_evidence/execution_failed (mapped by ``to_db_outcome``).

    Only a pending pairing can be settled. Re-delivering the same outcome is a no-op; a different outcome for a settled
    pairing raises ``PairingAlreadyResolved`` and leaves the row untouched.
    """
    outcome = to_db_outcome(result)
    cur = conn.execute(
        """UPDATE research.league_pairing SET outcome=%s,comparison_policy_version=%s,left_experiment_id=%s,right_experiment_id=%s,
               score_delta=%s,reason_ref=%s,completed_at=%s WHERE pairing_id=%s AND outcome='pending'""",
        (outcome, comparison_policy_version, left_experiment_id, right_experiment_id, score_delta, reason,
         completed_at if outcome != "pending" else None, pairing_id),
    )
    if cur.rowcount == 0:
        row = conn.execute("SELECT outcome, reason_ref FROM research.league_pairing WHERE pairing_id=%s", (pairing_id,)).fetchone()
        if row is None:
            raise KeyError("unknown pairing")
        if (row["outcome"], row["reason_ref"]) != (outcome, reason):
            raise PairingAlreadyResolved(f"pairing {pairing_id} is already {row['outcome']}")


class PairingAlreadyResolved(ValueError):
    """A settled (non-pending) pairing outcome is immutable."""


def close_round(conn: psycopg.Connection, league: tuple[UUID, int], round_no: int, *, result_ref: str, completed_at: datetime) -> None:
    conn.execute(
        "UPDATE research.league_round SET round_state='completed',completed_at=%s,result_ref=%s "
        "WHERE league_id=%s AND league_version=%s AND round_no=%s",
        (completed_at, result_ref, league[0], league[1], round_no),
    )


def load_league_wire(conn: psycopg.Connection, league_id: UUID, league_version: int) -> dict[str, Any]:
    return load_league(conn, league_id, league_version)[0]


def load_league(conn: psycopg.Connection, league_id: UUID, league_version: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(League wire, contract gaps). CONTRACT GAP: the League contract's pairing outcome enum lacks insufficient_evidence and
    execution_failed (stored as such since migration 0009). The wire shows the nearest contract value with the real one
    as a ``reason_ref`` prefix, and every mapped pairing is listed in ``gaps`` so the API can warn explicitly.
    """
    from pulse import contracts
    from pulse.domain.research.league import CONTRACT_OUTCOME_MAP

    head = conn.execute("SELECT * FROM research.league_definition WHERE league_id=%s AND league_version=%s",
                        (league_id, league_version)).fetchone()
    if head is None:
        raise KeyError("unknown league")
    rounds = conn.execute("SELECT * FROM research.league_round WHERE league_id=%s AND league_version=%s ORDER BY round_no",
                          (league_id, league_version)).fetchall()
    pairs = conn.execute("SELECT * FROM research.league_pairing WHERE league_id=%s AND league_version=%s ORDER BY round_no,pairing_id",
                         (league_id, league_version)).fetchall()
    gaps: list[dict[str, Any]] = []

    def wire_pairing(p: dict) -> dict:
        outcome, reason = p["outcome"], p["reason_ref"]
        if outcome in CONTRACT_OUTCOME_MAP:
            gaps.append({"pairing_id": p["pairing_id"], "db_outcome": outcome, "wire_outcome": CONTRACT_OUTCOME_MAP[outcome]})
            outcome, reason = CONTRACT_OUTCOME_MAP[outcome], f"{outcome}:{reason}"
        return {"pairing_id": p["pairing_id"], "left_instance_id": p["left_instance_id"],
                "right_instance_id": p["right_instance_id"], "left_experiment_id": p["left_experiment_id"],
                "right_experiment_id": p["right_experiment_id"], "outcome": outcome, "reason_ref": reason}

    wire = contracts.to_wire({
        "league_id": league_id, "league_version": league_version, "evaluation_context_id": head["evaluation_context_id"],
        "display_name": head["display_name"], "pairing_policy_version": head["pairing_policy_version"],
        "promotion_policy_version": head["promotion_policy_version"],
        "rounds": [
            {"round_no": r["round_no"], "round_state": r["round_state"],
             "pairings": [wire_pairing(p) for p in pairs if p["round_no"] == r["round_no"]]}
            for r in rounds],
    })
    return contracts.check("League", wire), gaps


# ------------------------------------------------------------------ promotion


def record_promotion(conn: psycopg.Connection, promotion_id: UUID, d: PromotionDecisionRecord) -> None:
    """Persist a decision (approved or rejected); it does NOT change the strategy by itself."""
    conn.execute(
        """INSERT INTO research.promotion_decision(promotion_id,instance_id,evaluation_experiment_id,policy_version,from_lifecycle,
               to_lifecycle,outcome,gate_results,reason,decided_at,applied_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL) ON CONFLICT (promotion_id) DO NOTHING""",
        (promotion_id, UUID(d.instance_id), UUID(d.evaluation_experiment_id), d.policy_version, d.from_state, d.to_state,
         d.outcome, Jsonb(d.gate_results_json()), d.reason, d.decided_at),
    )


def apply_promotion(conn: psycopg.Connection, promotion_id: UUID, *, applied_at: datetime) -> None:
    """CAS the instance to the approved new state, once. Rejected decisions cannot be applied."""
    row = conn.execute("SELECT * FROM research.promotion_decision WHERE promotion_id=%s FOR UPDATE", (promotion_id,)).fetchone()
    if row is None:
        raise KeyError("unknown promotion")
    if row["outcome"] != "approved":
        raise ValueError("only approved decisions can be applied")
    if row["applied_at"] is not None:
        return
    cur = conn.execute("UPDATE strategy.instance SET lifecycle=%s WHERE instance_id=%s AND lifecycle=%s",
                       (row["to_lifecycle"], row["instance_id"], row["from_lifecycle"]))
    if cur.rowcount != 1:
        raise LifecycleConflict("instance lifecycle changed since the decision was made")
    conn.execute("UPDATE research.promotion_decision SET applied_at=%s WHERE promotion_id=%s", (applied_at, promotion_id))


class TrialBudgetNotApproved(ValueError):
    """A trial candidate does not cite an applied, approved promotion decision that admits a ranking share."""


def verify_trial_candidates(conn: psycopg.Connection, candidates: Any) -> None:
    """Every candidate's ``approved_budget`` must rest on an applied promotion of its own instance to a trial-admitting
    lifecycle, and the instance must still be in an admitting lifecycle (not paused/retired since). Raises on the first failure."""
    from pulse.domain.ranking.run import TRIAL_ADMITTED_LIFECYCLES

    for c in candidates:
        row = conn.execute(
            "SELECT d.instance_id, d.outcome, d.applied_at, d.to_lifecycle, i.lifecycle AS current_lifecycle"
            " FROM research.promotion_decision d JOIN strategy.instance i ON i.instance_id=d.instance_id WHERE d.promotion_id=%s",
            (c.promotion_id,)).fetchone()
        if row is None:
            raise TrialBudgetNotApproved(f"{c.candidate_id}: promotion {c.promotion_id} does not exist")
        if row["instance_id"] != c.instance_id:
            raise TrialBudgetNotApproved(f"{c.candidate_id}: promotion {c.promotion_id} belongs to another instance")
        if row["outcome"] != "approved" or row["applied_at"] is None:
            raise TrialBudgetNotApproved(f"{c.candidate_id}: promotion {c.promotion_id} is not an applied approval")
        if row["to_lifecycle"] not in TRIAL_ADMITTED_LIFECYCLES or row["current_lifecycle"] not in TRIAL_ADMITTED_LIFECYCLES:
            raise TrialBudgetNotApproved(
                f"{c.candidate_id}: lifecycle {row['to_lifecycle']}/{row['current_lifecycle']} does not admit a ranking budget")


def promotion_history(conn: psycopg.Connection, instance_id: UUID) -> list[dict[str, Any]]:
    return conn.execute("SELECT * FROM research.promotion_decision WHERE instance_id=%s ORDER BY decided_at", (instance_id,)).fetchall()


# ------------------------------------------------------------------ lineage / combinations


def register_composite(
    conn: psycopg.Connection, definition: Any, parents: tuple[Any, ...], *, composition_policy_version: str, now: datetime
) -> None:
    """Store a combination as a NEW definition version plus its immutable parent lineage rows."""
    from pulse.domain.strategy.seed import CatalogSeed

    strategy_repo.seed_candidates(conn, CatalogSeed((definition,), (), (), {}), now=now)
    for p in parents:
        conn.execute(
            """INSERT INTO research.strategy_parent(strategy_id,strategy_version,parent_strategy_id,parent_strategy_version,
                   contribution_role,composition_policy_version,evidence_ref)
               VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (definition.strategy_id, definition.strategy_version, p.strategy_id, p.strategy_version, p.contribution_role,
             composition_policy_version, p.evidence_ref),
        )


def lineage(conn: psycopg.Connection, strategy_id: UUID, strategy_version: int) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT * FROM research.strategy_parent WHERE strategy_id=%s AND strategy_version=%s ORDER BY parent_strategy_id,contribution_role",
        (strategy_id, strategy_version),
    ).fetchall()


def load_evaluation_wire(conn: psycopg.Connection, experiment_id: UUID) -> dict[str, Any] | None:
    """Rebuild the ``Evaluation`` payload from experiment_run + context + inputs + metric_value (no stored copy)."""
    r = conn.execute("SELECT * FROM research.experiment_run WHERE experiment_id=%s", (experiment_id,)).fetchone()
    if r is None:
        return None
    c = conn.execute("SELECT * FROM research.evaluation_context WHERE evaluation_context_id=%s",
                     (r["evaluation_context_id"],)).fetchone()
    inputs = conn.execute(
        """SELECT i.snapshot_id, m.dataset_id, m.manifest_version, m.content_hash FROM research.evaluation_input i
             JOIN market.dataset_manifest m ON m.snapshot_id=i.snapshot_id
            WHERE i.evaluation_context_id=%s ORDER BY i.snapshot_id""", (c["evaluation_context_id"],)).fetchall()
    ap = c["availability_policy_document"]
    metrics = conn.execute(
        """SELECT metric_name, metric_definition_version, slice_key, value, unit, lower_bound, upper_bound,
                  confidence_level, sample_count, quality_status, missing_reason
             FROM research.metric_value WHERE experiment_id=%s ORDER BY metric_name, slice_key""",
        (experiment_id,)).fetchall()
    context = {
        "evaluation_context_id": c["evaluation_context_id"], "context_hash": c["context_hash"],
        "forecast_target": c["forecast_target"], "horizon_id": c["horizon_id"], "decision_as_of": c["decision_as_of"],
        "availability_policy": {"availability_policy_version": c["availability_policy_version"], "mode": ap.get("mode"),
                                "available_at_semantics": "actual_processing_completion",
                                "rule_ref": ap.get("rule_ref"),
                                "assumed_ingestion_delay_ms": ap.get("assumed_ingestion_delay_ms"),
                                "assumption_note": ap.get("assumption_note")},
        "universe_policy_version": c["universe_policy_version"], "input_snapshot_refs": [dict(i) for i in inputs],
        "train_start": c["train_start"], "train_end": c["train_end"], "validation_start": c["validation_start"],
        "validation_end": c["validation_end"], "blind_start": c["blind_start"], "blind_end": c["blind_end"],
        "embargo_seconds": c["embargo_seconds"], "sealed_at": c["sealed_at"],
    }
    result = None
    if r["result_ref"]:
        result = {"ref": r["result_ref"], "content_hash": r["result_hash"], "contract_id": "urn:pulse:contracts:v3",
                  "schema_version": "3.0.0"}
    return to_wire({
        "experiment_id": r["experiment_id"], "instance_id": r["instance_id"],
        "evaluation_context_id": r["evaluation_context_id"], "context_hash": c["context_hash"],
        "hypothesis_id": r["hypothesis_id"], "hypothesis_version": r["hypothesis_version"],
        "run_purpose": r["run_purpose"], "run_state": r["run_state"],
        "policy_versions": {k: c[k] for k in ("market_rules_version", "execution_policy_version", "cost_policy_version",
                                              "ledger_policy_version", "performance_policy_version",
                                              "numeric_policy_version")},
        "started_at": r["started_at"], "completed_at": r["completed_at"], "result": result,
        "metrics": [dict(m) for m in metrics], "error_code": r["error_code"], "context": context,
    })
