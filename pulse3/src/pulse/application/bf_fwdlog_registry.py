"""The four forward experiments of the fwdlog lane and their registration in the research registry (purpose ``shadow``).

Why: BACKFILL_BRIEF (batch 3): a forward experiment keeps its pre-registered rules unchanged and is registered in the pulse3 registry with the
pre-registration document's sha256, BEFORE its first row. Experiments: E077 (tail-of-day picks), E078 (index first / last half hour), E089 (market-state log with
recorder health), PAPER (intraday paper replay, triggers v2.1: E053-E057 and the E059 box shadow).
Invariants: identities are deterministic (a second registration is a no-op); the run stays ``running`` while it records (it is closed by the user when the pre-registered
decision day passes, never by this lane); the definition, the hypothesis text and the document hashes come from the frozen data, not from prose written here;
a shadow experiment books nothing and reaches no ranking weight.
Not covered: closing a run with metrics, promotion (research workers).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import psycopg
from psycopg.types.json import Jsonb

from pulse.adapters.postgres import research_repo
from pulse.application import bf_fwdlog_inputs as IN
from pulse.application import bindings as B
from pulse.domain.execution import KERNEL_VERSION
from pulse.domain.execution.numeric import NUMERIC_POLICY_VERSION, POLICY_VERSION
from pulse.domain.fwdlog import common as C
from pulse.domain.fwdlog import tail as TL
from pulse.domain.fwdlog import triggers as TR
from pulse.domain.market_rules import MARKET_RULES_VERSION
from pulse.domain.market_rules.costs import user_account_2026
from pulse.domain.research import experiments as E
from pulse.domain.research.context import EvaluationContextSpec, InputSnapshot
from pulse.domain.strategy.canonical import canonical_hash, stable_uuid

LANE = "fwdlog"
PERFORMANCE_POLICY = "performance-twr-external-flow-v1"
FAMILY = "fwdlog-forward-shadow"
UNIVERSE_POLICY = "cn-mainboard-non-st-forward-log-v1"
CST = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class ExperimentSpec:
    key: str                         # E077 | E078 | E089 | PAPER
    title: str
    ledger_ids: tuple[str, ...]
    prereg: tuple[str, ...]          # files under fwdlog_data/ (relative), sha256 from the manifest
    role: str                        # strategy.definition.role
    horizon: str
    forecast_target: str
    check_at: int                    # sessions (E089: independent episodes) before the pre-registered decision
    check_unit: str
    registered_on: date              # when the rules were frozen (the pre-registration date)
    rules_version: str
    datasets: tuple[str, ...]
    parameters: Mapping[str, Any]


EXPERIMENTS: tuple[ExperimentSpec, ...] = (
    ExperimentSpec("E077", "Tail-of-day stock picks (forward)", ("E077",), ("prereg/2026-11-08-batch4-preregistration.md",), "selection", "1td",
                   "fwdlog_e077_next_10am_excess_over_same_cap_quintile_v1", TL.E077_CHECK_AT, "decision_days", date(2026, 11, 8), C.RULES_VERSION,
                   ("fwdlog_e077_tail",),
                   {"pct_band": ["3", "5"], "volume_ratio_min": "1", "turnover_band": ["5", "10"], "float_cap_max": "200", "price_min": "2", "limit_history_sessions": 20,
                    "limit_move": "0.097", "top": 5, "cost_pct": "0.252", "min_signal_days": 25, "bootstrap_seed": 20261108}),
    ExperimentSpec("E078", "Index first half hour -> last half hour (forward)", ("E078",), ("prereg/2026-11-08-batch4-preregistration.md",), "entry", "1td",
                   "fwdlog_e078_last_half_hour_return_on_first_half_hour_slope_v1", TL.E078_CHECK_AT, "sessions", date(2026, 11, 8), C.RULES_VERSION,
                   ("fwdlog_e078_halfhour",), {"x": "10:00 / previous close - 1", "x2": "10:00 / open - 1", "y": "close / 14:30 - 1", "newey_west_lag": 1, "t_min": 2}),
    ExperimentSpec("E089", "Market-state forward log with recorder health", ("E089", "E087", "E076"), ("prereg/2026-11-09-batch5-preregistration.md",), "allocation", "5td",
                   "fwdlog_e089_equal_weight_5d_after_panic_support_v1", 10, "independent_episodes", date(2026, 11, 9), C.RULES_VERSION, ("fwdlog_e089_state",),
                   {"surge_ratio": "2.0", "panic_pct": "-3", "late_share": "0.30", "baseline_window": 20, "episode_gap": 10}),
    ExperimentSpec("PAPER", "Intraday paper replay, triggers v2.1 (B1/B3 shadow, S1/S3/M3, box shadow)", ("E053", "E055", "E056", "E057", "E059"),
                   ("prereg/2026-11-07-week1-review.md", "prereg/2026-11-03-three-pillars-spec.md"), "exit", "1td",
                   "fwdlog_paper_sell_rules_vs_hold_to_close_d0_v1", 20, "recorded_sessions", date(2026, 11, 7), TR.TRIGGERS_VERSION,
                   ("fwdlog_paper_days", "fwdlog_paper_buys", "fwdlog_paper_sells"),
                   {"block_yuan": "10000", "b1_gap": ["2", "7"], "b1_auction_share": "0.03", "b3_min_break_s": 30, "b3_latest": "14:30", "s1_decay": "0.3", "s1_min_peak_lots": 2000,
                    "vwap_grace_s": 90, "sector_dump_pct": "-1.0", "active": dict(TR.ACTIVE)}),
)
BY_KEY = {e.key: e for e in EXPERIMENTS}


def ledger(frozen_root: Path | None = None) -> dict[str, dict]:
    root = Path(frozen_root or IN.DATA_DIR)
    return {e["id"]: e for e in json.loads((root / "ledger_entries.json").read_text(encoding="utf-8"))}


def prereg_shas(spec: ExperimentSpec, frozen: IN.Frozen) -> dict[str, str]:
    return {rel: frozen.files[rel] for rel in spec.prereg}


def _ids(spec: ExperimentSpec) -> tuple:
    return (stable_uuid("fwdlog-strategy", spec.key), stable_uuid("fwdlog-instance", spec.key, spec.rules_version), stable_uuid("fwdlog-hypothesis", spec.key))


def implementation_digest(spec: ExperimentSpec, frozen: IN.Frozen) -> str:
    return canonical_hash([spec.key, spec.rules_version, TR.TRIGGERS_VERSION, frozen.manifest_sha256, dict(spec.parameters)])


def registered(conn: psycopg.Connection, spec: ExperimentSpec) -> dict | None:
    sid, iid, _ = _ids(spec)
    return conn.execute("SELECT * FROM research.experiment_run WHERE instance_id=%s AND run_purpose='shadow' ORDER BY lease_epoch LIMIT 1", (iid,)).fetchone()


def ensure_registered(conn: psycopg.Connection, spec: ExperimentSpec, frozen: IN.Frozen, first_input: Mapping, *, now: datetime) -> dict:
    """Register the experiment (definition, instance, hypothesis, sealed context, shadow run) BEFORE its first row. Idempotent: returns the stored run.

    ``first_input`` is a READY quote-snapshot manifest row: the context needs at least one input snapshot, and the run is anchored to the first one seen."""
    if (row := registered(conn, spec)) is not None:
        return row
    sid, iid, hid = _ids(spec)
    led = ledger()
    entries = [led[i] for i in spec.ledger_ids if i in led]
    primary = entries[0]
    definition_spec = {"catalog_id": f"fwdlog:{spec.key}", "family_id": FAMILY, "title": spec.title, "ledger_ids": list(spec.ledger_ids), "rules_version": spec.rules_version,
                       "preregistration": prereg_shas(spec, frozen), "check_at": spec.check_at, "check_unit": spec.check_unit,
                       "hypothesis": primary.get("hypothesis", ""), "legacy_ledger_verdict": {e["id"]: e.get("verdict") for e in entries}}
    def_hash = canonical_hash([spec.key, spec.role, spec.forecast_target, spec.horizon, definition_spec])
    with conn.transaction():
        conn.execute(
            """INSERT INTO strategy.definition(strategy_id,strategy_version,display_name,role,method_family,forecast_target,horizon_id,specification,definition_hash,created_at)
               VALUES (%s,1,%s,%s,'rule_forward_log',%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (sid, spec.title, spec.role, spec.forecast_target, spec.horizon, Jsonb(definition_spec), def_hash, now))
        conn.execute(
            """INSERT INTO strategy.instance(instance_id,strategy_id,strategy_version,parameters,parameter_hash,universe_policy_version,lifecycle,implementation_status,
                   reproduction_status,health,created_at) VALUES (%s,%s,1,%s,%s,%s,'shadow','implemented','not_started','healthy',%s) ON CONFLICT DO NOTHING""",
            (iid, sid, Jsonb(dict(spec.parameters)), canonical_hash(dict(spec.parameters)), UNIVERSE_POLICY, now))
        reg = datetime.combine(spec.registered_on, datetime.min.time(), CST).astimezone(timezone.utc)
        research_repo.register_hypothesis(
            conn, hypothesis_id=hid, hypothesis_version=1, claim=primary.get("hypothesis") or spec.title, mechanism=str(primary.get("why") or primary.get("name"))[:500],
            forecast_target=spec.forecast_target, horizon_id=spec.horizon,
            input_requirements={"datasets": ["quote_snapshots_60s", "ext_quote_snapshots"], "preregistration": prereg_shas(spec, frozen)},
            falsification_conditions={"reactivate_when": primary.get("reactivate_when"), "check_at": spec.check_at, "check_unit": spec.check_unit},
            available_at=reg, strategy=(sid, 1))
        rule_hash = canonical_hash(B.AVAILABILITY_POLICY)
        start = reg
        spec_ctx = EvaluationContextSpec(
            forecast_target=spec.forecast_target, horizon_id=spec.horizon, decision_as_of=reg, availability_policy_version=B.AVAILABILITY_POLICY, availability_mode="observed",
            availability_rule_ref={"ref": f"policy://{B.AVAILABILITY_POLICY}", "content_hash": rule_hash, "contract_id": "pulse.availability_policy", "schema_version": "1.0"},
            assumed_ingestion_delay_ms=None, assumption_note=None, universe_policy_version=UNIVERSE_POLICY,
            inputs=(InputSnapshot(first_input["snapshot_id"], first_input["dataset_id"], first_input["manifest_version"], first_input["content_hash"], "quote_snapshots"),),
            market_rules_version=MARKET_RULES_VERSION, execution_policy_version=KERNEL_VERSION, cost_policy_version=user_account_2026().version,
            ledger_policy_version=POLICY_VERSION, performance_policy_version=PERFORMANCE_POLICY, numeric_policy_version=NUMERIC_POLICY_VERSION,
            train_start=start - timedelta(days=1), train_end=start, validation_start=start, validation_end=start + timedelta(days=365), blind_start=None, blind_end=None,
            embargo_seconds=0, benchmark_specification={"benchmarks": [{"id": "same_day_matched_baseline", "kind": "cross_section"}]},
            resource_budget={"max_trials": 1, "blind_access_budget": 0},
            extra={"note": "forward shadow: no training interval exists; the 'train' day is a placeholder required by the context contract, the forward log runs in the validation interval"})
        ctx = research_repo.seal_context(conn, spec_ctx, created_at=now, sealed_at=now)
        rec = E.new_run(instance_id=iid, hypothesis_id=hid, hypothesis_version=1, evaluation_context_id=ctx, run_purpose="shadow",
                        implementation_digest=implementation_digest(spec, frozen), random_seed=int(spec.parameters.get("bootstrap_seed", 0)),
                        run_manifest={"lane": LANE, "rules_version": spec.rules_version, "preregistration": prereg_shas(spec, frozen), "ledger_ids": list(spec.ledger_ids),
                                      "datasets": list(spec.datasets), "frozen_manifest_sha256": frozen.manifest_sha256, "check_at": spec.check_at, "check_unit": spec.check_unit},
                        family_key=FAMILY)
        research_repo.register_experiment(conn, rec)
        conn.execute("UPDATE research.experiment_run SET run_state='running', started_at=%s WHERE experiment_id=%s AND run_state='queued'", (now, rec.experiment_id))
    return registered(conn, spec)
