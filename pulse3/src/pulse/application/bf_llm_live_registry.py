"""Registration of the live web-search LLM judgement experiments (E092 closed, E100 open) in the research registry (purpose ``shadow``) with the pre-registration's sha256.

Why: BACKFILL_BRIEF batch 3 rule - a forward experiment is registered with the hash of its pre-registration BEFORE its first row. Same mechanism as ``bf_batch7_registry``; the sealed
context is anchored to the READY ``fusion_scores_current`` snapshot, the input the universe is drawn from.
Invariants: identities are deterministic (a second registration is a no-op); the run stays ``running`` (a forward record never completes; the log holds its numbers); nothing here books a trade,
changes a ranking weight or the frozen main book (shadow).
Not covered: closing the run with metrics (the judgement row in ``llm_live_log`` holds the verdict), HTTP.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from pulse.adapters.postgres import research_repo
from pulse.application import bindings as B
from pulse.application.bf_fusion_service import DS_CURRENT
from pulse.domain.execution import KERNEL_VERSION
from pulse.domain.execution.numeric import NUMERIC_POLICY_VERSION, POLICY_VERSION
from pulse.domain.llm_live import experiments as X
from pulse.domain.llm_live import judge as J
from pulse.domain.llm_live import rules as R
from pulse.domain.market_rules import MARKET_RULES_VERSION
from pulse.domain.market_rules.costs import user_account_2026
from pulse.domain.research import experiments as E
from pulse.domain.research.context import EvaluationContextSpec, InputSnapshot
from pulse.domain.strategy.canonical import canonical_hash, stable_uuid

LANE = "llm_live"
FAMILY = "llm-live-forward-shadow"
KEY = R.EXPERIMENT                                             # E092; the functions below take the experiment
UNIVERSE_POLICY = "cn-mainboard-fusion-static40-safe-top30-plus-10-controls-v1"
PERFORMANCE_POLICY = "performance-twr-external-flow-v1"
RULES_VERSION = "llm-live-v1"
TARGET = "llm_live_e092_ex5_open_d1_to_open_d6_vs_main_board_mean_v1"
HORIZON = "5td"
CLAIMS = {
    "E092": ("A web-searching model that reads the day's news after the close ranks the 30 best fusion_static40_safe main-board names by 5-day excess: the mean daily rank correlation of "
             "p_outperform and ex5 is above 0 (one-sided 5-day block bootstrap p < 0.05) and the non-avoid names beat the avoid names by more than 0.252 percent (forward only, 20 decision days)."),
}
CLAIMS["E100"] = CLAIMS["E092"].replace("A web-searching model", "A web-searching gpt-6-luna")


def parameters(exp: X.Experiment = X.E092) -> dict[str, Any]:
    return {"preregistration_frozen_sha256": exp.spec_sha256, "model": exp.model, "effort": exp.effort, "web_search": True, "n_top": R.N_TOP, "n_controls": R.N_CONTROLS,
            "max_requests_per_day": R.MAX_CALLS, "max_decision_days": R.MAX_DECISION_DAYS, "deadline": "next session 09:25 Asia/Shanghai", "cost_round_trip_pct": J.ROUND_TRIP_PCT,
            "alpha": J.MAX_P, "bootstrap_block": J.BLOCK, "n_boot": J.N_BOOT, "bootstrap_seed": J.SEED, "min_days": J.MIN_DAYS, "min_names_per_day": J.MIN_NAMES,
            "min_non_avoid": J.MIN_NON_AVOID, "universe": "main board, fusion_static40_safe promoted score of the decision day, tradable names only",
            **({"service_tier": exp.service_tier} if exp.key != "E092" else {})}


def _ids(exp: X.Experiment = X.E092) -> tuple:
    return (stable_uuid("llm-live-strategy", exp.key), stable_uuid("llm-live-instance", exp.key, RULES_VERSION), stable_uuid("llm-live-hypothesis", exp.key))


def registered(conn: psycopg.Connection, exp: X.Experiment = X.E092) -> dict | None:
    _, iid, _ = _ids(exp)
    return conn.execute("SELECT * FROM research.experiment_run WHERE instance_id=%s AND run_purpose='shadow' ORDER BY lease_epoch LIMIT 1", (iid,)).fetchone()


def implementation_digest(exp: X.Experiment = X.E092) -> str:
    return canonical_hash([exp.key, RULES_VERSION, exp.spec_sha256, parameters(exp)])


def anchor(conn: psycopg.Connection, as_of: datetime) -> dict:
    row = conn.execute("SELECT * FROM market.dataset_manifest WHERE dataset_id=%s AND manifest_state='READY' AND ready_at <= %s ORDER BY manifest_version DESC LIMIT 1", (DS_CURRENT, as_of)).fetchone()
    if row is None:
        raise RuntimeError("no READY fusion_scores_current manifest: nothing to anchor the registration to")
    return row


def ensure_registered(conn: psycopg.Connection, first_input: dict, *, now: datetime, exp: X.Experiment = X.E092) -> dict:
    """Register the experiment (definition, instance, hypothesis, sealed context, shadow run). Idempotent: returns the stored run."""
    if (row := registered(conn, exp)) is not None:
        return row
    sid, iid, hid = _ids(exp)
    params = parameters(exp)
    claim, prereg = CLAIMS[exp.key], {exp.spec_path: exp.spec_sha256}
    datasets = ["fusion_scores_current", exp.dataset]
    definition_spec = {"catalog_id": f"{LANE}:{exp.key}", "family_id": FAMILY, "title": exp.title, "ledger_ids": [exp.key, "E002"] if exp.key == "E092" else [exp.key, "E092"],
                       "rules_version": RULES_VERSION, "preregistration": prereg, "hypothesis": claim}
    def_hash = canonical_hash([exp.key, "selection", TARGET, HORIZON, definition_spec])
    reg = now.astimezone(timezone.utc)          # the day the experiment is registered here; the document's own date is in the definition's preregistration entry
    with conn.transaction():
        conn.execute(
            """INSERT INTO strategy.definition(strategy_id,strategy_version,display_name,role,method_family,forecast_target,horizon_id,specification,definition_hash,created_at)
               VALUES (%s,1,%s,%s,'rule_forward_log',%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (sid, exp.title, "selection", TARGET, HORIZON, Jsonb(definition_spec), def_hash, now))
        conn.execute(
            """INSERT INTO strategy.instance(instance_id,strategy_id,strategy_version,parameters,parameter_hash,universe_policy_version,lifecycle,implementation_status,
                   reproduction_status,health,created_at) VALUES (%s,%s,1,%s,%s,%s,'shadow','implemented','not_started','healthy',%s) ON CONFLICT DO NOTHING""",
            (iid, sid, Jsonb(params), canonical_hash(params), UNIVERSE_POLICY, now))
        research_repo.register_hypothesis(
            conn, hypothesis_id=hid, hypothesis_version=1, claim=claim, mechanism=exp.title, forecast_target=TARGET, horizon_id=HORIZON,
            input_requirements={"datasets": datasets, "preregistration": prereg},
            falsification_conditions={"alpha": J.MAX_P, "min_days": J.MIN_DAYS, "min_names_per_day": J.MIN_NAMES, "round_trip_pct": J.ROUND_TRIP_PCT, "max_decision_days": R.MAX_DECISION_DAYS},
            available_at=reg, strategy=(sid, 1))
        rule_hash = canonical_hash(B.AVAILABILITY_POLICY)
        spec_ctx = EvaluationContextSpec(
            forecast_target=TARGET, horizon_id=HORIZON, decision_as_of=reg, availability_policy_version=B.AVAILABILITY_POLICY, availability_mode="observed",
            availability_rule_ref={"ref": f"policy://{B.AVAILABILITY_POLICY}", "content_hash": rule_hash, "contract_id": "pulse.availability_policy", "schema_version": "1.0"},
            assumed_ingestion_delay_ms=None, assumption_note=None, universe_policy_version=UNIVERSE_POLICY,
            inputs=(InputSnapshot(first_input["snapshot_id"], first_input["dataset_id"], first_input["manifest_version"], first_input["content_hash"], "inputs"),),
            market_rules_version=MARKET_RULES_VERSION, execution_policy_version=KERNEL_VERSION, cost_policy_version=user_account_2026().version,
            ledger_policy_version=POLICY_VERSION, performance_policy_version=PERFORMANCE_POLICY, numeric_policy_version=NUMERIC_POLICY_VERSION,
            train_start=reg - timedelta(days=1), train_end=reg, validation_start=reg, validation_end=reg + timedelta(days=365), blind_start=None, blind_end=None,
            embargo_seconds=0, benchmark_specification={"benchmarks": [{"id": "same_day_main_board_tradable_mean", "kind": "cross_section"}]},
            resource_budget={"max_trials": 1, "blind_access_budget": 0},
            extra={"note": "shadow research: no training interval exists; the placeholder 'train' day is required by the context contract", "experiment": exp.key})
        ctx = research_repo.seal_context(conn, spec_ctx, created_at=now, sealed_at=now)
        rec = E.new_run(instance_id=iid, hypothesis_id=hid, hypothesis_version=1, evaluation_context_id=ctx, run_purpose="shadow",
                        implementation_digest=implementation_digest(exp), random_seed=J.SEED,
                        run_manifest={"lane": LANE, "experiment": exp.key, "rules_version": RULES_VERSION, "preregistration": prereg, "datasets": datasets},
                        family_key=FAMILY)
        research_repo.register_experiment(conn, rec)
        conn.execute("UPDATE research.experiment_run SET run_state='running', started_at=%s WHERE experiment_id=%s AND run_state='queued'", (now, rec.experiment_id))
    return registered(conn, exp)


def register(conn: psycopg.Connection, as_of: datetime, exp: X.Experiment = X.E092) -> dict:
    if exp.closed:
        raise RuntimeError(f"{exp.key} is closed: {exp.closed}")
    return ensure_registered(conn, anchor(conn, as_of), now=as_of, exp=exp)
