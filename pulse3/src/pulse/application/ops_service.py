"""Operations shared by the group-C cards: immutable artifacts, registered jobs, data health (PEX15; helpers for PEX05/12/13/14).

Why: every ``ArtifactRef`` of these cards must point at an immutable, owner-scoped object (``ops.ext_artifact``, content
addressed); every asynchronous command (plan proposal, export, manual job) is one ``ops.job_run`` with its complete
arguments stored in ``ops.job_run_argument`` (D25) and a fenced worker; data health is a read projection that keeps snapshot
state, source runtime state and the real arrival of new content apart.
Invariants:
* artifact ``ref`` = ``<kind>:<sha256[:40]>``, ``content_hash`` = sha256 of the canonical JSON body (same body = same ref);
  an artifact with an owner is invisible to everyone else (reads return None, never "forbidden", so ids leak nothing);
* ``runRegisteredJob`` runs only registered, enabled definitions that allow a manual trigger, with the exact definition hash,
  a parameter artifact of the definition's contract whose hash matches its body; same job + same key is idempotent in
  ``ops.job_run`` (``ON CONFLICT`` via ``PgJobStore.enqueue``); the worker claim uses the fencing lease epoch;
* ops permission (``ops:run``/``ops:read``): the principals in ``PULSE_OPS_PRINCIPALS`` (comma-separated UUIDs) plus the dev
  principal when ``PULSE_ENV=dev``; others may only read the runs their own commands started;
* ``DatasetStatus.manifest_state`` is the snapshot state; source runtime state, freshness and "last new record" appear as
  ``issues`` rows, so READY never reads as "all sources complete" and ``enabled`` never reads as "data received".
Not covered: cron expansion, cross-job dependencies, retries with backoff.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from pulse.adapters.postgres._canon import sha256_of
from pulse.adapters.postgres.jobs import PgJobStore
from pulse.application.ports.errors import Conflict
from pulse.application.ports.jobs import JobDefinition
from pulse.contracts import CONTRACT_VERSION, to_wire

JOB_NS = uuid.UUID("0f3b7a52-9d41-4c0e-8b6a-1e5d2c9a7f10")
ZERO_UUID = UUID(int=0)
PARAM_CONTRACT = "pulse.job_parameters"
PARAM_VERSION = "1"
GENERIC_PARAM_SCHEMA_ID = "urn:pulse:contracts:v3"


class OpsError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# ---- registered job definitions ---------------------------------------------------------------------------------------------
def _job(name: str, operation: str, *, manual: bool, key: str, timeout: int = 1800) -> JobDefinition:
    return JobDefinition(
        job_id=uuid.uuid5(JOB_NS, name), job_version=1, application_operation=operation,
        schedule_document={"trigger": "manual" if manual else "command", "manual_trigger": manual, "timezone": "Asia/Shanghai"},
        parameter_contract_id=PARAM_CONTRACT, parameter_schema_version=PARAM_VERSION, enabled=True, concurrency_key=key,
        max_concurrency=1, timeout_seconds=timeout, retry_policy={"max_attempts": 1})


DEFINITIONS: dict[str, JobDefinition] = {d.application_operation: d for d in (
    _job("propose_trade_plan", "PortfolioService.compile_proposal", manual=False, key="ext-c-plan"),
    _job("workspace_export", "WorkspaceService.run_export", manual=False, key="ext-c-export"),
    _job("alerts_scan", "AlertService.scan", manual=True, key="ext-c-alerts"),
    _job("alerts_deliver", "AlertService.deliver", manual=True, key="ext-c-alerts-deliver"),
    _job("calendar_sync", "CalendarService.sync", manual=True, key="ext-c-calendar"),
    _job("forward_book_daily", "PortfolioService.forward_daily", manual=True, key="ext-c-forward"),
    _job("account_readmodel_build", "PortfolioService.build_readmodels", manual=True, key="ext-c-readmodel"),
)}
DEFINITIONS_BY_ID = {d.job_id: d for d in DEFINITIONS.values()}
JOB_IDS = {name: d.job_id for name, d in ((d.application_operation.split(".")[1], d) for d in DEFINITIONS.values())}


def definition_hash(d: JobDefinition | dict) -> str:
    g = (lambda k: d[k]) if isinstance(d, dict) else (lambda k: getattr(d, k))
    return sha256_of({k: g(k) for k in ("job_id", "job_version", "application_operation", "parameter_contract_id",
                                        "parameter_schema_version", "concurrency_key", "max_concurrency", "timeout_seconds", "enabled")})


PARAM_SCHEMA = {"type": "object", "description": "free-form job parameters; each job validates its own keys", "x-owner": "extension group C"}


def ensure_jobs(conn: psycopg.Connection) -> None:
    conn.execute("INSERT INTO core.contract_version(contract_id, schema_version, schema_document, schema_hash, compatibility_policy, created_at)"
                 " VALUES (%s,%s,%s,%s,'strict-object',clock_timestamp()) ON CONFLICT DO NOTHING",
                 (PARAM_CONTRACT, PARAM_VERSION, Jsonb(PARAM_SCHEMA), sha256_of(PARAM_SCHEMA)))
    store = PgJobStore(conn)
    for d in DEFINITIONS.values():
        store.register_definition(d)


def definitions_wire(conn: psycopg.Connection) -> list[dict]:
    ensure_jobs(conn)
    rows = conn.execute("SELECT * FROM ops.job_definition WHERE application_operation = ANY(%s) ORDER BY application_operation",
                        ([d.application_operation for d in DEFINITIONS.values()],)).fetchall()
    return [{"job_id": r["job_id"], "job_version": r["job_version"], "application_operation": r["application_operation"],
             "manual_trigger": bool(r["schedule_document"].get("manual_trigger")), "enabled": r["enabled"],
             "definition_hash": definition_hash(r), "parameter_contract_id": r["parameter_contract_id"],
             "parameter_schema_version": r["parameter_schema_version"]} for r in rows]


# ---- authorization ------------------------------------------------------------------------------------------------------------
def ops_principals() -> set[UUID]:
    out = {UUID(x.strip()) for x in os.environ.get("PULSE_OPS_PRINCIPALS", "").split(",") if x.strip()}
    if os.environ.get("PULSE_ENV") == "dev" and os.environ.get("PULSE_DEV_PRINCIPAL"):
        out.add(UUID(os.environ["PULSE_DEV_PRINCIPAL"]))
    return out


def is_ops(principal: UUID) -> bool:
    return principal in ops_principals()


# ---- artifacts ----------------------------------------------------------------------------------------------------------------
def put_artifact(conn: psycopg.Connection, *, kind: str, contract_id: str, schema_version: str, body: dict,
                 owner: UUID | None = None, scope_account: UUID | None = None) -> dict:
    body = to_wire(body)
    digest = sha256_of(body)
    ref = f"{kind}:{digest[:40]}"
    if owner is not None or scope_account is not None:      # private bodies get a ref per owner/account scope
        ref = f"{kind}:{digest[:30]}:{sha256_of([str(owner), str(scope_account)])[:10]}"
    conn.execute("INSERT INTO ops.ext_artifact(ref, content_hash, contract_id, schema_version, owner_principal_id, scope_account_id, body)"
                 " VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (ref) DO NOTHING",
                 (ref, digest, contract_id, schema_version, owner, scope_account, Jsonb(body)))
    return {"ref": ref, "content_hash": digest, "contract_id": contract_id, "schema_version": schema_version}


def get_artifact(conn: psycopg.Connection, ref: str, principal: UUID | None) -> dict | None:
    row = conn.execute("SELECT * FROM ops.ext_artifact WHERE ref=%s", (ref,)).fetchone()
    if row is None:
        return None
    if row["owner_principal_id"] is not None and row["owner_principal_id"] != principal:
        return None
    if row["scope_account_id"] is not None and row["owner_principal_id"] is None:
        from pulse.application.portfolio_service import has_grant
        if principal is None or not has_grant(conn, principal, row["scope_account_id"], "read"):
            return None
    return {"ref": row["ref"], "content_hash": row["content_hash"], "contract_id": row["contract_id"],
            "schema_version": row["schema_version"], "body": row["body"]}


def inline_artifact(ref: str, content_hash: str, contract_id: str, schema_version: str) -> dict:
    """Decode a client-built ``inline-json:<base64url>`` parameter artifact and verify its hash. Raises OpsError."""
    import base64
    import json

    if not ref.startswith("inline-json:"):
        raise OpsError(400, "INVALID_CONTRACT", "参数产物必须是 inline-json 引用")
    try:
        raw = base64.urlsafe_b64decode(ref[len("inline-json:"):] + "=" * (-len(ref) % 4))
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise OpsError(400, "INVALID_CONTRACT", "参数产物不是合法的 base64url JSON") from e
    import hashlib

    if hashlib.sha256(raw).hexdigest() != content_hash or sha256_of(body) != content_hash:
        raise OpsError(400, "INVALID_CONTRACT", "参数产物哈希与内容不符（需规范 JSON：键排序、无空格、UTF-8）")
    if not isinstance(body, dict):
        raise OpsError(400, "INVALID_CONTRACT", "参数必须是 JSON 对象")
    return body


# ---- runRegisteredJob (PEX15) -----------------------------------------------------------------------------------------------
def run_registered_job(conn: psycopg.Connection, principal: UUID, req: dict, idempotency_key: str) -> dict:
    """Command handler body (runs inside the idempotent command transaction). Returns the JobReceipt dict."""
    if not is_ops(principal):
        raise OpsError(403, "FORBIDDEN", "无运行权限（ops:run）")
    ensure_jobs(conn)
    d = conn.execute("SELECT * FROM ops.job_definition WHERE job_id=%s AND job_version=%s",
                     (req["job_id"], req["job_version"])).fetchone()
    if d is None or d["application_operation"] not in DEFINITIONS:
        raise OpsError(404, "NOT_FOUND", "任务未登记")
    if not d["enabled"] or not d["schedule_document"].get("manual_trigger"):
        raise OpsError(403, "FORBIDDEN", "该任务不允许手动触发")
    if definition_hash(d) != req["expected_definition_hash"]:
        raise OpsError(409, "VERSION_CONFLICT", "任务定义已变化，请刷新后重试")
    art = req["parameter_artifact"]
    if art["contract_id"] != d["parameter_contract_id"] or art["schema_version"] != d["parameter_schema_version"]:
        raise OpsError(400, "INVALID_CONTRACT", "参数合同与任务定义不一致")
    params = inline_artifact(art["ref"], art["content_hash"], art["contract_id"], art["schema_version"])
    stored = put_artifact(conn, kind="job-param", contract_id=art["contract_id"], schema_version=art["schema_version"], body=params, owner=principal)
    run_id = uuid.uuid5(JOB_NS, f"{principal}|{req['job_id']}|{idempotency_key}")
    try:
        PgJobStore(conn).enqueue(job_run_id=run_id, job_id=req["job_id"], job_version=req["job_version"],
                                 idempotency_key=f"{principal}:{idempotency_key}",
                                 parameter_hash=sha256_of({"params": params, "snapshots": req["input_snapshot_refs"]}),
                                 scheduled_for=req["scheduled_for"] if isinstance(req["scheduled_for"], datetime)
                                 else datetime.fromisoformat(req["scheduled_for"].replace("Z", "+00:00")),
                                 resource_type="job_run", resource_id=run_id)
    except Conflict as e:
        raise OpsError(409, "IDEMPOTENCY_CONFLICT", str(e)) from e
    conn.execute("INSERT INTO ops.job_run_argument(job_run_id, principal_id, parameter_contract_id, parameter_schema_version,"
                 " parameter_artifact, parameter_hash, input_snapshot_refs, release_id) VALUES (%s,%s,%s,%s,%s,%s,%s,NULL)"
                 " ON CONFLICT (job_run_id) DO NOTHING",
                 (run_id, principal, art["contract_id"], art["schema_version"], stored["ref"], stored["content_hash"],
                  Jsonb(req["input_snapshot_refs"])))
    r = PgJobStore(conn).receipt(run_id)
    return {**r, "poll_operation_id": "getJobRun"}


# ---- worker side ------------------------------------------------------------------------------------------------------------
HANDLERS: dict[str, Callable[[psycopg.Connection, Any, dict], dict]] = {}


def register_handler(operation: str):
    def deco(fn):
        HANDLERS[operation] = fn
        return fn
    return deco


def run_next(conn: psycopg.Connection, *, job_id: UUID | None = None, now: datetime | None = None) -> dict | None:
    """Claim and execute one queued job of this group. Returns {"job_run_id", "state"} or None when nothing is queued."""
    _load_handlers()
    ensure_jobs(conn)
    store = PgJobStore(conn)
    run = None                                            # only this group's registered jobs: never claim (and fail) another package's run
    for d in ([DEFINITIONS_BY_ID[job_id]] if job_id else DEFINITIONS.values()):
        run = store.claim(job_id=d.job_id, now=now)
        if run is not None:
            break
    if run is None:
        return None
    d = conn.execute("SELECT application_operation FROM ops.job_definition WHERE job_id=%s AND job_version=%s",
                     (run.job_id, run.job_version)).fetchone()
    handler = HANDLERS.get(d["application_operation"])
    if handler is None:
        store.fail(run.job_run_id, run.lease_epoch, error_code="NO_HANDLER")
        return {"job_run_id": run.job_run_id, "state": "failed", "error": "NO_HANDLER"}
    arg = conn.execute("SELECT a.input_snapshot_refs, e.body FROM ops.job_run_argument a JOIN ops.ext_artifact e ON e.ref=a.parameter_artifact"
                       " WHERE a.job_run_id=%s", (run.job_run_id,)).fetchone()
    args = {"params": arg["body"] if arg else {}, "snapshots": arg["input_snapshot_refs"] if arg else [], "run": run}
    try:
        result = handler(conn, run, args)
        digest = sha256_of(result)
        store.succeed(run.job_run_id, run.lease_epoch, output_ref=f"job-result:{digest[:40]}", output_hash=digest)
        return {"job_run_id": run.job_run_id, "state": "succeeded", "result": result}
    except Exception as exc:  # noqa: BLE001 - a job failure is recorded, never swallowed
        store.fail(run.job_run_id, run.lease_epoch, error_code=type(exc).__name__[:60])
        return {"job_run_id": run.job_run_id, "state": "failed", "error": f"{type(exc).__name__}: {str(exc)[:200]}"}


def _load_handlers() -> None:
    from pulse.application import alert_service, calendar_service, portfolio_service, workspace_service  # noqa: F401  (register)


# ---- queries: job runs (PEX15) ------------------------------------------------------------------------------------------------
def query_job_runs(conn: psycopg.Connection, principal: UUID, job_id: UUID | None, window: dict, limit: int, after: tuple | None) -> tuple[list[dict], tuple | None]:
    sql = ("SELECT r.job_run_id FROM ops.job_run r WHERE r.scheduled_for >= %s AND r.scheduled_for < %s"
           " AND (%s::uuid IS NULL OR r.job_id=%s)")
    args: list[Any] = [window["start"], window["end"], job_id, job_id]
    if not is_ops(principal):                 # ordinary users see only the runs their own commands started
        sql += (" AND (EXISTS (SELECT 1 FROM ops.job_run_argument a WHERE a.job_run_id=r.job_run_id AND a.principal_id=%s)"
                " OR EXISTS (SELECT 1 FROM decision.plan_proposal p WHERE p.job_run_id=r.job_run_id AND p.principal_id=%s)"
                " OR EXISTS (SELECT 1 FROM ops.export_request x WHERE x.job_run_id=r.job_run_id AND x.principal_id=%s))")
        args += [principal, principal, principal]
    if after is not None:
        sql += " AND (r.scheduled_for, r.job_run_id) > (%s, %s)"
        args += list(after)
    sql += " ORDER BY r.scheduled_for, r.job_run_id LIMIT %s"
    args.append(limit + 1)
    ids = [r["job_run_id"] for r in conn.execute(sql, args).fetchall()]
    more = len(ids) > limit
    ids = ids[:limit]
    store = PgJobStore(conn)
    wire = [store.job_run_wire(i) for i in ids]
    last = None
    if more and ids:
        row = conn.execute("SELECT scheduled_for FROM ops.job_run WHERE job_run_id=%s", (ids[-1],)).fetchone()
        last = (row["scheduled_for"], ids[-1])
    return wire, last


# ---- queries: dataset status (PEX15) --------------------------------------------------------------------------------------------
def query_dataset_status(conn: psycopg.Connection, dataset_id: str, limit: int, offset: int) -> tuple[list[dict], bool]:
    where, args = ("", []) if dataset_id in ("all", "*") else (" WHERE dataset_id=%s", [dataset_id])
    ids = [r["dataset_id"] for r in conn.execute(f"SELECT DISTINCT dataset_id FROM market.dataset_manifest{where} ORDER BY 1 LIMIT %s OFFSET %s",
                                                 args + [limit + 1, offset]).fetchall()]
    more = len(ids) > limit
    out = []
    for ds in ids[:limit]:
        ready = conn.execute("SELECT * FROM market.dataset_manifest WHERE dataset_id=%s AND manifest_state='READY' ORDER BY manifest_version DESC LIMIT 1", (ds,)).fetchone()
        latest = conn.execute("SELECT * FROM market.dataset_manifest WHERE dataset_id=%s ORDER BY manifest_version DESC LIMIT 1", (ds,)).fetchone()
        row = ready or latest
        issues = _dataset_issues(conn, ds, row, latest)
        cov = row["coverage_document"] if isinstance(row["coverage_document"], dict) else {}
        out.append({
            "dataset_id": ds, "snapshot_id": row["snapshot_id"], "manifest_version": row["manifest_version"],
            "manifest_state": row["manifest_state"],
            "ready_snapshot": None if ready is None else {"snapshot_id": ready["snapshot_id"], "dataset_id": ds,
                                                         "manifest_version": ready["manifest_version"], "content_hash": ready["content_hash"]},
            "row_count": row["row_count"], "available_watermark": row["available_watermark"],
            "coverage": {"policy_version": str(cov.get("policy") or cov.get("quote_policy_version") or "dataset-status-v1"),
                         "expected_count": cov.get("requested") if isinstance(cov.get("requested"), int) else None,
                         "observed_count": int(cov.get("instruments") or row["row_count"] or 0) if isinstance(cov.get("instruments", 0), int) else int(row["row_count"] or 0),
                         "missing_count": len(cov.get("missing_instruments") or []) if "missing_instruments" in cov else None,
                         "unverifiable_reasons": [] if ready is not None else ["no READY snapshot"]},
            "issues": issues})
    return out, more


def _dataset_issues(conn: psycopg.Connection, ds: str, row: dict, latest: dict) -> list[dict]:
    """Gaps in plain codes: snapshot newer than READY, FAILED newest, no watermark, and the runtime state of sources that feed it."""
    issues = []
    if latest["manifest_state"] != "READY":
        issues.append({"dataset_id": ds, "requirement": "latest_snapshot_ready", "reason_code": f"LATEST_{latest['manifest_state']}"})
    if row["available_watermark"] is None:
        issues.append({"dataset_id": ds, "requirement": "available_watermark", "reason_code": "WATERMARK_MISSING"})
    src = conn.execute(                                   # the sources behind a dataset are the ones whose batches built its snapshots
        "SELECT DISTINCT h.source_id, h.endpoint_id, h.runtime_state, h.freshness_state, h.last_new_record_at, h.last_error_code"
        " FROM market.dataset_manifest m JOIN ingest.batch b ON b.batch_id=m.batch_id JOIN ingest.source_health h ON h.source_id=b.source_id"
        " AND h.endpoint_id=b.endpoint_id AND h.source_schema_version=b.source_schema_version AND h.mapping_version=b.mapping_version"
        " WHERE m.dataset_id=%s", (ds,)).fetchall()
    for s in src:
        if s["freshness_state"] == "not_expected":
            continue                                     # market closed is not an outage
        if s["runtime_state"] != "healthy":
            issues.append({"dataset_id": ds, "requirement": f"source:{s['source_id']}/{s['endpoint_id']}",
                           "reason_code": f"RUNTIME_{s['runtime_state'].upper()}"})
        elif s["freshness_state"] in ("stale", "unknown"):
            issues.append({"dataset_id": ds, "requirement": f"source:{s['source_id']}/{s['endpoint_id']}",
                           "reason_code": f"FRESHNESS_{s['freshness_state'].upper()}"})
    return issues


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def days(n: int) -> timedelta:
    return timedelta(days=n)
