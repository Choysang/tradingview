"""Registration of E101 in the research registry (purpose ``shadow``) with the pre-registration's frozen-prefix sha256.

Why: registered BEFORE the first row, with the document hash (same mechanism as ``bf_batch8_registry``). One experiment carries H1-H4, one Holm family of four.
Invariants: identities are deterministic (a second registration is a no-op); the run stays ``running``; nothing here books a trade or touches a ranking weight.
Not covered: closing a run with metrics.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

import psycopg
from psycopg.types.json import Jsonb

from pulse.adapters.postgres import research_repo
from pulse.application import bindings as B
from pulse.domain.e101 import rules as R
from pulse.domain.execution import KERNEL_VERSION
from pulse.domain.execution.numeric import NUMERIC_POLICY_VERSION, POLICY_VERSION
from pulse.domain.market_rules import MARKET_RULES_VERSION
from pulse.domain.market_rules.costs import user_account_2026
from pulse.domain.research import experiments as E
from pulse.domain.research.context import EvaluationContextSpec, InputSnapshot
from pulse.domain.strategy.canonical import canonical_hash, stable_uuid

LANE = "e101"
FAMILY = "e101-forward-confirmation-shadow"
UNIVERSE_POLICY = "cn-mainboard-non-st-e101-v1"
PERFORMANCE_POLICY = "performance-twr-external-flow-v1"
RULES_VERSION = "e101-v1"
CST = timezone(timedelta(hours=8))
REGISTERED_ON = date(2026, 11, 25)
KEY = "E101"
TITLE = "US macro releases: trading ahead (D0), surprise reaction (R gap) and leader crowding"
CLAIM = ("Before a US core PCE, CPI, non-farm payrolls or FOMC release, the main board's equal-weight return on D0 is below same-weekday non-event days (H1); the R open gaps against "
         "the surprise sign (H2); the leader and trend-core groups beat the matched baseline on D0 (H3) and after a dovish surprise from the R open to the R+2 close (H4); "
         "effect of at least 0.252 percent, Holm over four hypotheses (forward confirmation, 40 events).")
TARGET = "e101_us_macro_d0_market_and_group_excess_v1"
HORIZON = "1td"
DATASETS = ("e101_forward", "e101_history")


def _ids() -> tuple:
    return (stable_uuid("e101-strategy", KEY), stable_uuid("e101-instance", KEY, RULES_VERSION), stable_uuid("e101-hypothesis", KEY))


def parameters() -> dict[str, Any]:
    return {"preregistration_frozen_sha256": R.SPEC_SHA256, "cost_round_trip": str(R.COST), "bootstrap_seed": R.SEED, "n_boot": R.N_BOOT, "block": R.BLOCK,
            "forward_start": R.FORWARD_START.isoformat(), "deadline": R.DEADLINE.isoformat(), "min_events": R.MIN_EVENTS, "hypotheses": list(R.HYPOTHESES),
            "families": "one family, Holm over four", "classes": [c for c, _ in R.CLASSES], "universe": "main board 00/60, non-ST, listed >= 60 sessions, traded"}


def registered(conn: psycopg.Connection) -> dict | None:
    _, iid, _ = _ids()
    return conn.execute("SELECT * FROM research.experiment_run WHERE instance_id=%s AND run_purpose='shadow' ORDER BY lease_epoch LIMIT 1", (iid,)).fetchone()


def implementation_digest() -> str:
    return canonical_hash([KEY, RULES_VERSION, R.SPEC_SHA256, parameters()])


def ensure_registered(conn: psycopg.Connection, first_input: Mapping, *, now: datetime) -> dict:
    """Register the experiment (definition, instance, hypothesis, sealed context, shadow run). Idempotent: returns the stored run."""
    if (row := registered(conn)) is not None:
        return row
    sid, iid, hid = _ids()
    params = parameters()
    definition_spec = {"catalog_id": f"e101:{KEY}", "family_id": FAMILY, "title": TITLE, "ledger_ids": [KEY], "rules_version": RULES_VERSION,
                       "preregistration": {R.SPEC_PATH: R.SPEC_SHA256}, "hypothesis": CLAIM, "hypotheses": list(R.HYPOTHESES)}
    def_hash = canonical_hash([KEY, "selection", TARGET, HORIZON, definition_spec])
    reg = datetime.combine(REGISTERED_ON, datetime.min.time(), CST).astimezone(timezone.utc)
    with conn.transaction():
        conn.execute(
            """INSERT INTO strategy.definition(strategy_id,strategy_version,display_name,role,method_family,forecast_target,horizon_id,specification,definition_hash,created_at)
               VALUES (%s,1,%s,%s,'rule_forward_log',%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (sid, TITLE, "selection", TARGET, HORIZON, Jsonb(definition_spec), def_hash, now))
        conn.execute(
            """INSERT INTO strategy.instance(instance_id,strategy_id,strategy_version,parameters,parameter_hash,universe_policy_version,lifecycle,implementation_status,
                   reproduction_status,health,created_at) VALUES (%s,%s,1,%s,%s,%s,'shadow','implemented','not_started','healthy',%s) ON CONFLICT DO NOTHING""",
            (iid, sid, Jsonb(params), canonical_hash(params), UNIVERSE_POLICY, now))
        research_repo.register_hypothesis(
            conn, hypothesis_id=hid, hypothesis_version=1, claim=CLAIM, mechanism=TITLE, forecast_target=TARGET, horizon_id=HORIZON,
            input_requirements={"datasets": list(DATASETS), "preregistration": {R.SPEC_PATH: R.SPEC_SHA256}},
            falsification_conditions={"alpha": R.ALPHA, "min_events": R.MIN_EVENTS, "deadline": R.DEADLINE.isoformat(), "min_effect": str(R.COST), "family": "Holm over H1-H4"}, available_at=reg, strategy=(sid, 1))
        rule_hash = canonical_hash(B.AVAILABILITY_POLICY)
        spec_ctx = EvaluationContextSpec(
            forecast_target=TARGET, horizon_id=HORIZON, decision_as_of=reg, availability_policy_version=B.AVAILABILITY_POLICY, availability_mode="observed",
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
                        implementation_digest=implementation_digest(), random_seed=R.SEED,
                        run_manifest={"lane": LANE, "rules_version": RULES_VERSION, "preregistration": {R.SPEC_PATH: R.SPEC_SHA256}, "hypotheses": list(R.HYPOTHESES),
                                      "datasets": list(DATASETS)}, family_key=FAMILY)
        research_repo.register_experiment(conn, rec)
        conn.execute("UPDATE research.experiment_run SET run_state='running', started_at=%s WHERE experiment_id=%s AND run_state='queued'", (now, rec.experiment_id))
    return registered(conn)
