"""Registration of E097 and E098 in the research registry (purpose ``shadow``) with the pre-registration's frozen-prefix sha256.

Why: BACKFILL_BRIEF batch 3 rule and section 4 of the pre-registration - registered BEFORE the first row, with the document hash. Same mechanism as ``bf_batch6_registry``.
Invariants: identities are deterministic (a second registration is a no-op); the run stays ``running``; nothing here books a trade or touches a ranking weight (shadow).
Not covered: closing a run with metrics (research workers).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

import psycopg
from psycopg.types.json import Jsonb

from pulse.adapters.postgres import research_repo
from pulse.application import bindings as B
from pulse.domain.batch7 import rules as R
from pulse.domain.execution import KERNEL_VERSION
from pulse.domain.execution.numeric import NUMERIC_POLICY_VERSION, POLICY_VERSION
from pulse.domain.market_rules import MARKET_RULES_VERSION
from pulse.domain.market_rules.costs import user_account_2026
from pulse.domain.research import experiments as E
from pulse.domain.research.context import EvaluationContextSpec, InputSnapshot
from pulse.domain.strategy.canonical import canonical_hash, stable_uuid

LANE = "batch7"
FAMILY = "batch7-forward-confirmation-shadow"
UNIVERSE_POLICY = "cn-mainboard-non-st-batch7-v1"
PERFORMANCE_POLICY = "performance-twr-external-flow-v1"
RULES_VERSION = "batch7-v1"
CST = timezone(timedelta(hours=8))
REGISTERED_ON = date(2026, 11, 20)


@dataclass(frozen=True)
class Spec:
    key: str
    title: str
    claim: str
    role: str
    horizon: str
    target: str
    hypotheses: tuple[str, ...]
    datasets: tuple[str, ...]
    forward: bool


EXPERIMENTS: tuple[Spec, ...] = (
    Spec("E097", "Leader break: same-industry core and follower stocks weaken (exact limit-up retest of E093 H1)",
         "After a 3+ board leader closes without a limit-up, same-industry core and first-board stocks, bought at the D+1 open and held to the D+3 close, underperform the matched baseline by more than 0.252 percent (forward confirmation, exact limit flags from raw prices).",
         "selection", "3td", "batch7_e097_matched_excess_d1_open_to_d3_close_v1", ("H1p",), ("batch7_forward", "batch7_history"), True),
    Spec("E098", "Institution net buying of at least 5 percent of turnover is followed by underperformance (H4 reversed as a risk)",
         "After an LHB day with institution-seat net buying of at least 5 percent of turnover, the stock bought at the D+1 open and held 5 sessions underperforms the matched baseline by more than 0.252 percent (forward confirmation).",
         "selection", "5td", "batch7_e098_matched_excess_d1_open_to_d5_close_v1", ("H4p",), ("batch7_forward",), True),
)
BY_KEY = {s.key: s for s in EXPERIMENTS}


def _ids(spec: Spec) -> tuple:
    return (stable_uuid("batch7-strategy", spec.key), stable_uuid("batch7-instance", spec.key, RULES_VERSION), stable_uuid("batch7-hypothesis", spec.key))


def parameters(spec: Spec) -> dict[str, Any]:
    return {"preregistration_frozen_sha256": R.SPEC_SHA256, "cost_round_trip": str(R.COST), "bootstrap_seed": R.SEED, "n_boot": R.N_BOOT, "forward_start": R.FORWARD_START.isoformat(),
            "deadline": R.DEADLINE.isoformat(), "hypotheses": list(spec.hypotheses), "universe": "main board 00/60, non-ST, listed >= 60 sessions, traded", "families": "independent, no Holm"}


def registered(conn: psycopg.Connection, spec: Spec) -> dict | None:
    _, iid, _ = _ids(spec)
    return conn.execute("SELECT * FROM research.experiment_run WHERE instance_id=%s AND run_purpose='shadow' ORDER BY lease_epoch LIMIT 1", (iid,)).fetchone()


def implementation_digest(spec: Spec) -> str:
    return canonical_hash([spec.key, RULES_VERSION, R.SPEC_SHA256, parameters(spec)])


def ensure_registered(conn: psycopg.Connection, spec: Spec, first_input: Mapping, *, now: datetime) -> dict:
    """Register the experiment (definition, instance, hypothesis, sealed context, shadow run). Idempotent: returns the stored run."""
    if (row := registered(conn, spec)) is not None:
        return row
    sid, iid, hid = _ids(spec)
    params = parameters(spec)
    definition_spec = {"catalog_id": f"batch7:{spec.key}", "family_id": FAMILY, "title": spec.title, "ledger_ids": [spec.key], "rules_version": RULES_VERSION,
                       "preregistration": {R.SPEC_PATH: R.SPEC_SHA256}, "hypothesis": spec.claim, "hypotheses": list(spec.hypotheses)}
    def_hash = canonical_hash([spec.key, spec.role, spec.target, spec.horizon, definition_spec])
    reg = datetime.combine(REGISTERED_ON, datetime.min.time(), CST).astimezone(timezone.utc)
    with conn.transaction():
        conn.execute(
            """INSERT INTO strategy.definition(strategy_id,strategy_version,display_name,role,method_family,forecast_target,horizon_id,specification,definition_hash,created_at)
               VALUES (%s,1,%s,%s,'rule_forward_log',%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (sid, spec.title, spec.role, spec.target, spec.horizon, Jsonb(definition_spec), def_hash, now))
        conn.execute(
            """INSERT INTO strategy.instance(instance_id,strategy_id,strategy_version,parameters,parameter_hash,universe_policy_version,lifecycle,implementation_status,
                   reproduction_status,health,created_at) VALUES (%s,%s,1,%s,%s,%s,'shadow','implemented','not_started','healthy',%s) ON CONFLICT DO NOTHING""",
            (iid, sid, Jsonb(params), canonical_hash(params), UNIVERSE_POLICY, now))
        research_repo.register_hypothesis(
            conn, hypothesis_id=hid, hypothesis_version=1, claim=spec.claim, mechanism=spec.title, forecast_target=spec.target, horizon_id=spec.horizon,
            input_requirements={"datasets": list(spec.datasets), "preregistration": {R.SPEC_PATH: R.SPEC_SHA256}},
            falsification_conditions={"alpha": R.ALPHA, "min_filled_signals": R.HYPOTHESES[spec.hypotheses[0]][3], "min_sessions": R.MIN_SESSIONS, "deadline": R.DEADLINE.isoformat()}, available_at=reg, strategy=(sid, 1))
        rule_hash = canonical_hash(B.AVAILABILITY_POLICY)
        spec_ctx = EvaluationContextSpec(
            forecast_target=spec.target, horizon_id=spec.horizon, decision_as_of=reg, availability_policy_version=B.AVAILABILITY_POLICY, availability_mode="observed",
            availability_rule_ref={"ref": f"policy://{B.AVAILABILITY_POLICY}", "content_hash": rule_hash, "contract_id": "pulse.availability_policy", "schema_version": "1.0"},
            assumed_ingestion_delay_ms=None, assumption_note=None, universe_policy_version=UNIVERSE_POLICY,
            inputs=(InputSnapshot(first_input["snapshot_id"], first_input["dataset_id"], first_input["manifest_version"], first_input["content_hash"], "inputs"),),
            market_rules_version=MARKET_RULES_VERSION, execution_policy_version=KERNEL_VERSION, cost_policy_version=user_account_2026().version,
            ledger_policy_version=POLICY_VERSION, performance_policy_version=PERFORMANCE_POLICY, numeric_policy_version=NUMERIC_POLICY_VERSION,
            train_start=reg - timedelta(days=1), train_end=reg, validation_start=reg, validation_end=reg + timedelta(days=365), blind_start=None, blind_end=None,
            embargo_seconds=0, benchmark_specification={"benchmarks": [{"id": "same_day_matched_baseline", "kind": "cross_section"}]},
            resource_budget={"max_trials": 1, "blind_access_budget": 0},
            extra={"note": "shadow research: no training interval exists; the placeholder 'train' day is required by the context contract"})
        ctx = research_repo.seal_context(conn, spec_ctx, created_at=now, sealed_at=now)
        rec = E.new_run(instance_id=iid, hypothesis_id=hid, hypothesis_version=1, evaluation_context_id=ctx, run_purpose="shadow",
                        implementation_digest=implementation_digest(spec), random_seed=R.SEED,
                        run_manifest={"lane": LANE, "rules_version": RULES_VERSION, "preregistration": {R.SPEC_PATH: R.SPEC_SHA256}, "hypotheses": list(spec.hypotheses),
                                      "datasets": list(spec.datasets)}, family_key=FAMILY)
        research_repo.register_experiment(conn, rec)
        conn.execute("UPDATE research.experiment_run SET run_state='running', started_at=%s WHERE experiment_id=%s AND run_state='queued'", (now, rec.experiment_id))
    return registered(conn, spec)
