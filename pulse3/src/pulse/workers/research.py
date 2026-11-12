"""Research worker: claims queued experiment jobs and runs them through one evaluation path (I1, M3-E04, M5 input).

Why: createExperiment only enqueues a job (JobReceipt). This worker claims it with a lease (fencing epoch), seals ONE fixed
EvaluationContext, registers the experiment BEFORE running, runs the evaluator for the instance and records metrics or the
failure. Every trial is recorded, a failed one keeps its error_code (a failure is not an empty success).
Invariants: the claim epoch is the fencing token for the job result AND the experiment row; a stale worker cannot overwrite
a newer result (heartbeat/succeed/fail are compare-and-set); an instance whose implementation is not ``implemented`` is a
recorded failed trial (``strategy_not_implemented``), never a fabricated metric; league points never feed promotion.
Evaluators are injected (``{implementation key: callable}``); no evaluator ships because no catalog strategy is implemented.
Usage::

    uv run python -m pulse.workers.research seed
    uv run python -m pulse.workers.research enqueue --catalog-id S001
    uv run python -m pulse.workers.research run [--max 10]
Not covered: league scheduling (research_service.schedule_league_round), promotion decisions, blind-final automation.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg

from pulse import config
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db, research_repo, strategy_repo
from pulse.adapters.postgres._canon import sha256_of
from pulse.adapters.postgres.jobs import PgJobStore
from pulse.application import bindings as B
from pulse.application.bootstrap import RESEARCH_EXPERIMENT_JOB, seed_jobs
from pulse.application.bootstrap import seed_registry as seed_contract_registry
from pulse.application.paper_service import seed_papers
from pulse.application.ports.errors import FencedOut
from pulse.application.research_service import seed_registry
from pulse.domain.execution import KERNEL_VERSION
from pulse.domain.execution.numeric import NUMERIC_POLICY_VERSION, POLICY_VERSION
from pulse.domain.market_rules import MARKET_RULES_VERSION
from pulse.domain.market_rules.costs import user_account_2026
from pulse.domain.research import experiments as E
from pulse.domain.research.context import EvaluationContextSpec, InputSnapshot
from pulse.domain.strategy.canonical import canonical_hash, stable_uuid

ROOT = Path(__file__).resolve().parents[3]
JOB_ID = uuid.UUID(RESEARCH_EXPERIMENT_JOB)
PERFORMANCE_POLICY = "performance-twr-external-flow-v1"
Evaluator = Callable[[psycopg.Connection, Mapping[str, Any], "E.ExperimentRecord"], list[dict]]


class EvaluationUnavailable(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def fixed_context(conn: psycopg.Connection, lake: LakeStore, *, now: datetime) -> EvaluationContextSpec:
    """The single comparison environment: current research bars + benchmark + membership, sessions split 60/25/15."""
    inputs = B.load_research_inputs(conn, lake, None)
    snaps = [InputSnapshot(inputs.manifest["snapshot_id"], inputs.manifest["dataset_id"], inputs.manifest["manifest_version"],
                           inputs.manifest["content_hash"], "bars")]
    if inputs.benchmark_manifest:
        bm = inputs.benchmark_manifest
        snaps.append(InputSnapshot(bm["snapshot_id"], bm["dataset_id"], bm["manifest_version"], bm["content_hash"], "benchmark"))
    cur = conn.execute("SELECT decision_as_of FROM decision.release WHERE release_state IN ('READY','SUPERSEDED') ORDER BY published_at DESC LIMIT 1").fetchone()
    as_of = cur["decision_as_of"] if cur else inputs.manifest["ready_at"]
    s = inputs.sessions
    tz = B.CST

    def at(i: int) -> datetime:
        d = s[min(i, len(s) - 1)]
        return datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(timezone.utc)

    n = len(s)
    a, b = int(n * 0.60), int(n * 0.85)
    rule_hash = canonical_hash(B.AVAILABILITY_POLICY)
    return EvaluationContextSpec(
        forecast_target="rel_return_20td_vs_universe_median_after_declared_costs_v1", horizon_id="20td", decision_as_of=as_of,
        availability_policy_version=B.AVAILABILITY_POLICY, availability_mode="observed",
        availability_rule_ref={"ref": f"policy://{B.AVAILABILITY_POLICY}", "content_hash": rule_hash, "contract_id": "pulse.availability_policy",
                               "schema_version": "1.0"},
        assumed_ingestion_delay_ms=None, assumption_note=None, universe_policy_version="cn-mainboard-rules-v1+default-pref-v1",
        inputs=tuple(snaps), market_rules_version=MARKET_RULES_VERSION, execution_policy_version=KERNEL_VERSION,
        cost_policy_version=user_account_2026().version, ledger_policy_version=POLICY_VERSION, performance_policy_version=PERFORMANCE_POLICY,
        numeric_policy_version=NUMERIC_POLICY_VERSION, train_start=at(0), train_end=at(a), validation_start=at(a), validation_end=at(b),
        blind_start=at(b), blind_end=at(n - 1) + timedelta(days=1), embargo_seconds=5 * 86400,
        benchmark_specification={"benchmarks": [{"id": "000300", "kind": "index_points"}, {"id": "universe_median", "kind": "cross_section"}]},
        resource_budget={"max_trials": 200, "blind_access_budget": 1})


def run_one(conn: psycopg.Connection, lake: LakeStore, evaluators: Mapping[str, Evaluator] | None = None, *,
            clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> dict | None:
    """Claim and process one queued experiment job. Returns a result record or None when the queue is empty."""
    store = PgJobStore(conn)
    job = store.claim(job_id=JOB_ID, now=clock())
    if job is None:
        return None
    epoch = job.lease_epoch
    inst = strategy_repo.get_instance(conn, job.resource_id)
    if inst is None:
        store.fail(job.job_run_id, epoch, error_code="instance_not_found")
        return {"job_run_id": job.job_run_id, "state": "failed", "error": "instance_not_found"}
    definition = conn.execute("SELECT * FROM strategy.definition WHERE strategy_id=%s AND strategy_version=%s",
                              (inst["strategy_id"], inst["strategy_version"])).fetchone()
    spec = fixed_context(conn, lake, now=clock())
    with conn.transaction():
        ctx_id = research_repo.seal_context(conn, spec, created_at=clock(), sealed_at=clock())
    hyp_id = stable_uuid("hypothesis", inst["instance_id"], definition["forecast_target"])
    with conn.transaction():
        research_repo.register_hypothesis(
            conn, hypothesis_id=hyp_id, hypothesis_version=1, claim=f"{definition['display_name']} has a positive net edge in {spec.horizon_id}",
            mechanism=str(definition["specification"].get("hypothesis", "unspecified"))[:500], forecast_target=spec.forecast_target,
            horizon_id=spec.horizon_id, input_requirements={"context_hash": spec.context_hash()},
            falsification_conditions={"net_excess_after_costs": "<= 0 on validation and blind slices"}, available_at=clock(),
            strategy=(inst["strategy_id"], inst["strategy_version"]))
    family = str(definition["specification"].get("family_id", "unknown"))
    impl_digest = canonical_hash([str(inst["instance_id"]), inst["parameter_hash"], inst["implementation_status"]])
    rec = E.new_run(instance_id=inst["instance_id"], hypothesis_id=hyp_id, hypothesis_version=1, evaluation_context_id=ctx_id,
                    run_purpose="screening", implementation_digest=impl_digest, random_seed=int(job.job_run_id.int % 2**31),
                    run_manifest={"job_run_id": str(job.job_run_id), "lease_epoch": epoch}, family_key=family, lease_epoch=epoch)
    with conn.transaction():
        research_repo.register_experiment(conn, rec)           # before it runs: failures are trials too
    started = clock()
    try:
        store.heartbeat(job.job_run_id, epoch, now=started)
        if inst["implementation_status"] != "implemented":
            raise EvaluationUnavailable("strategy_not_implemented")
        fn = (evaluators or {}).get(inst["implementation_status"])
        if fn is None:
            raise EvaluationUnavailable("no_evaluator_registered")
        metrics = fn(conn, {"context": spec, "context_id": ctx_id}, rec)
        store.heartbeat(job.job_run_id, epoch, now=clock())     # fencing check just before writing the result
        digest = sha256_of(metrics)
        with conn.transaction():
            research_repo.insert_metrics(conn, rec.experiment_id, metrics, computed_at=clock())
            done = E.complete(rec, started_at=started, completed_at=clock(), result_ref=f"evaluation:{digest}", result_hash=digest)
            research_repo.finish_experiment(conn, done)
        store.succeed(job.job_run_id, epoch, output_ref=f"experiment:{rec.experiment_id}", output_hash=digest, now=clock())
        return {"job_run_id": job.job_run_id, "experiment_id": rec.experiment_id, "state": "completed", "metrics": len(metrics)}
    except FencedOut:
        # A newer claim owns the job: drop this result, write nothing.
        return {"job_run_id": job.job_run_id, "experiment_id": rec.experiment_id, "state": "fenced_out"}
    except EvaluationUnavailable as exc:
        failed = E.fail(rec, started_at=started, completed_at=clock(), error_code=exc.code)
        with conn.transaction():
            research_repo.finish_experiment(conn, failed)
        try:
            store.fail(job.job_run_id, epoch, error_code=exc.code, retryable=False, now=clock())
        except FencedOut:
            pass
        return {"job_run_id": job.job_run_id, "experiment_id": rec.experiment_id, "state": "failed", "error": exc.code}


def seed_all(conn: psycopg.Connection) -> dict:
    now = datetime.now(timezone.utc)
    seed_contract_registry(conn)          # contract_version + api_binding rows the job definition references
    reg = seed_registry(conn, ROOT / "spec_package" / "catalogs", now=now)
    papers = seed_papers(conn, ROOT / "spec_package" / "research" / "papers_registry.json")
    seed_jobs(conn)
    return {"registry_new_rows": reg, "papers_new": papers}


def enqueue(conn: psycopg.Connection, catalog_id: str, *, key: str | None = None) -> dict:
    row = conn.execute(
        "SELECT i.instance_id FROM strategy.instance i JOIN strategy.definition d USING (strategy_id, strategy_version)"
        " WHERE d.specification->>'catalog_id'=%s ORDER BY i.created_at LIMIT 1", (catalog_id,)).fetchone()
    if row is None:
        raise LookupError(f"catalog id {catalog_id} is not seeded")
    body = {"instance_id": str(row["instance_id"]), "catalog_id": catalog_id, "run_purpose": "screening"}
    run_id = uuid.uuid5(JOB_ID, f"worker:{catalog_id}:{key or 'default'}")
    store = PgJobStore(conn)
    store.enqueue(job_run_id=run_id, job_id=JOB_ID, job_version=1, idempotency_key=f"exp:{run_id}", parameter_hash=sha256_of(body),
                  scheduled_for=datetime.now(timezone.utc), resource_type="strategy_instance", resource_id=row["instance_id"])
    return store.receipt(run_id)


# ==== ext group B additions (marked) ================================================================================
def ext_b_evaluators(lake: LakeStore) -> dict[str, Evaluator]:
    """Evaluator registry for playbooks, factor-lab factors (and anything else ``implemented`` in group B's registry)."""
    from pulse.application import playbook_service as PS

    def evaluate(conn: psycopg.Connection, bundle: Mapping[str, Any], rec: "E.ExperimentRecord") -> list[dict]:
        try:
            return PS.evaluate_instance(conn, lake, bundle["context"], rec)
        except PS.DataUnavailable as exc:        # a recorded FAILED trial with a machine-readable reason, never a fabricated metric
            raise EvaluationUnavailable(exc.code) from exc
        except EvaluationUnavailable:
            raise
        except Exception as exc:                 # an evaluator bug is a recorded failed trial (with the error), not a lost run
            raise EvaluationUnavailable(f"evaluator_error:{type(exc).__name__}:{str(exc)[:80]}") from exc
    return {"implemented": evaluate}


def ext_b_register(conn: psycopg.Connection) -> dict:
    from pulse.application import playbook_service as PS
    seed_all(conn)
    return PS.register_all(conn)


def ext_b_run_catalog(conn: psycopg.Connection, lake: LakeStore, catalog_ids: list[str], *, key: str | None = None) -> list[dict]:
    """Enqueue + run experiments (screening purpose) for the given catalog ids through the ONE worker path."""
    out = []
    for cid in catalog_ids:
        receipt = enqueue(conn, cid, key=key)
        res = run_one(conn, lake, ext_b_evaluators(lake))
        out.append({"catalog_id": cid, "receipt": receipt, "result": res})
    return out
# ==== end ext group B additions ====================================================================================


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pulse.workers.research")
    ap.add_argument("--dsn")
    ap.add_argument("--lake")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed")
    p = sub.add_parser("enqueue")
    p.add_argument("--catalog-id", required=True)
    p.add_argument("--key")
    p = sub.add_parser("run")
    p.add_argument("--max", type=int, default=10)
    # ---- ext group B commands (marked) ----
    sub.add_parser("ext-b-register")
    p = sub.add_parser("ext-b-run")
    p.add_argument("--ids", nargs="*", help="catalog ids such as PB:turtle_55 FL:momentum_20; default: all 52 playbooks + 6 factors")
    p.add_argument("--key")
    p = sub.add_parser("screen-run")
    p.add_argument("--max", type=int, default=5)
    p = sub.add_parser("tactic-run")
    p.add_argument("--max", type=int, default=5)
    p = sub.add_parser("analog-study")
    p.add_argument("--code", required=True, help="stock code such as 000001")
    p.add_argument("--release-id")
    p.add_argument("--policy", default="analog-knn-matured-v1")
    a = ap.parse_args(argv)
    cfg = config.load()
    conn = db.connect(a.dsn or cfg.dsn, autocommit=True)
    try:
        if a.cmd == "seed":
            print(json.dumps(seed_all(conn), indent=2))
        elif a.cmd == "enqueue":
            print(json.dumps(enqueue(conn, a.catalog_id, key=a.key), indent=2))
        elif a.cmd == "ext-b-register":
            print(json.dumps(ext_b_register(conn), indent=2))
        elif a.cmd in ("ext-b-run", "screen-run", "tactic-run", "analog-study"):
            lake = LakeStore(a.lake or cfg.lake_root)
            from pulse.application import playbook_service as PS
            from pulse.application import screen_service as SS
            if a.cmd == "ext-b-run":
                from pulse.domain.playbooks import library as L
                from pulse.domain.screening import factor_lab as FL
                ids = a.ids or [f"PB:{i}" for i in L.ORDER] + [f"FL:{f.id}" for f in FL.FACTORS]
                for r in ext_b_run_catalog(conn, lake, ids, key=a.key):
                    print(json.dumps(r, default=str, ensure_ascii=False), flush=True)
            elif a.cmd == "screen-run":
                SS.ensure_jobs(conn)
                for _ in range(a.max):
                    res = SS.execute_screen_run(conn, lake)
                    if res is None:
                        break
                    print(json.dumps({"screen_run_id": str(res[0]), "state": res[1]}), flush=True)
            elif a.cmd == "tactic-run":
                PS.ensure_jobs(conn)
                for _ in range(a.max):
                    res = PS.run_tactic_replay(conn, lake)
                    if res is None:
                        break
                    print(json.dumps(res, default=str), flush=True)
            else:
                from pulse.application.data_service import DataService
                row = conn.execute("SELECT a.instrument_id FROM core.instrument_alias a WHERE a.source_symbol=%s AND a.source_id='legacy_warehouse'", (a.code,)).fetchone()
                rel = a.release_id or conn.execute("SELECT release_id FROM decision.release WHERE release_state IN ('READY','SUPERSEDED') ORDER BY published_at DESC LIMIT 1").fetchone()["release_id"]
                man = PS.build_analog_study(DataService(conn, lake), row["instrument_id"], uuid.UUID(str(rel)), a.policy)
                print(json.dumps({"snapshot_id": str(man["snapshot_id"]), "content_hash": man["content_hash"]}))
        else:
            lake = LakeStore(a.lake or cfg.lake_root)
            for _ in range(a.max):
                out = run_one(conn, lake)
                if out is None:
                    break
                print(json.dumps(out, default=str))
        return 0
    finally:
        conn.close()


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
