"""Registration of E093-E096 in the research registry (purpose ``shadow``) with the pre-registration's sha256.

Why: BACKFILL_BRIEF batch 3 rule - every experiment is registered BEFORE its first result, with the document hash; section 6 of the pre-registration asks the same.
Invariants: identities are deterministic (a second registration is a no-op); the run stays ``running`` (closing it is a user act when the pre-registered decision is made); nothing here
books a trade or touches a ranking weight (shadow).
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
from pulse.domain.batch6 import rules as R
from pulse.domain.execution import KERNEL_VERSION
from pulse.domain.execution.numeric import NUMERIC_POLICY_VERSION, POLICY_VERSION
from pulse.domain.market_rules import MARKET_RULES_VERSION
from pulse.domain.market_rules.costs import user_account_2026
from pulse.domain.research import experiments as E
from pulse.domain.research.context import EvaluationContextSpec, InputSnapshot
from pulse.domain.strategy.canonical import canonical_hash, stable_uuid

LANE = "batch6"
FAMILY = "batch6-short-term-shadow"
UNIVERSE_POLICY = "cn-mainboard-non-st-batch6-v1"
PERFORMANCE_POLICY = "performance-twr-external-flow-v1"
RULES_VERSION = "batch6-v1"
CST = timezone(timedelta(hours=8))
REGISTERED_ON = date(2026, 11, 18)


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
    Spec("E093", "Leader linkage to core and follower stocks (history)", "After a 3+ board leader breaks, same-industry core and follower stocks underperform the matched baseline (H1); after a promotion the core outperforms (H2).",
         "selection", "3td", "batch6_e093_matched_excess_d1_open_to_d3_close_v1", ("H1", "H2"), ("batch6_history",), False),
    Spec("E094", "Seat expansion: Lhasa industry crowd and institution seats (history)", "Other members of an industry with 3+ Lhasa seats on one stock underperform (H3); institution net buy of at least 5 percent of turnover outperforms (H4).",
         "selection", "5td", "batch6_e094_matched_excess_d1_open_to_d5_close_v1", ("H3", "H4"), ("batch6_history",), False),
    Spec("E095", "Call auction: real strength and fake strength (forward)", "A 2-7 percent auction gap with top-20 percent auction turnover beats the matched baseline open to close (H5); a 09:19 gap of 5 percent that fades 3 points by 09:25 underperforms (H6).",
         "entry", "1td", "batch6_e095_matched_excess_open_to_close_v1", ("H5", "H6"), ("batch6_forward",), True),
    Spec("E096", "Intraday active buying 09:30-10:00 (forward)", "Names up 0-5 percent at 10:00 with top-10 percent active buy ratio over 09:30-10:00 beat the matched baseline from the 10:00 ask to the close (H7).",
         "entry", "1td", "batch6_e096_matched_excess_ask1000_to_close_v1", ("H7",), ("batch6_forward",), True),
)
BY_KEY = {s.key: s for s in EXPERIMENTS}


def _ids(spec: Spec) -> tuple:
    return (stable_uuid("batch6-strategy", spec.key), stable_uuid("batch6-instance", spec.key, RULES_VERSION), stable_uuid("batch6-hypothesis", spec.key))


def parameters(spec: Spec) -> dict[str, Any]:
    return {"preregistration_sha256": R.SPEC_SHA256, "cost_round_trip": str(R.COST), "bootstrap_seed": R.SEED, "n_boot": R.N_BOOT, "holm_family": R.FAMILY_SIZE,
            "pre_end": R.PRE_END.isoformat(), "hypotheses": list(spec.hypotheses), "universe": "main board 00/60, non-ST, listed >= 60 sessions, traded"}


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
    definition_spec = {"catalog_id": f"batch6:{spec.key}", "family_id": FAMILY, "title": spec.title, "ledger_ids": [spec.key], "rules_version": RULES_VERSION,
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
            falsification_conditions={"alpha": R.ALPHA, "holm_family": R.FAMILY_SIZE, "min_signal_days": 40 if spec.forward else None}, available_at=reg, strategy=(sid, 1))
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
