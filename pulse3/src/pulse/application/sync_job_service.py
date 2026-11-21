"""Data-sync jobs started from the web: queue, validation, worker execution and the run log (legacy backfill lane ``sync``, rows #49 #89).

Why: the old warehouse panel could trigger a data sync / backfill from the screen; pulse3 only had the ``ingest`` command line. A
user with the ops permission now queues one of four bounded sync commands, a worker (``pulse.workers.sync_jobs run``) executes it
through the very same ``pulse.workers.ingest`` / ``pulse.workers.refdata`` code path (so the rate-limited client and the stop-at-first-block rule are not
re-implemented), and every run keeps its parameters, start / end, exit status and summary text in ``ops.sync_job_log``.
Invariants:
* four job kinds, each its own registered manual job (``sync_daily_bars`` = ingest live-daily, ``sync_legacy_import`` = ingest
  legacy-import (legacy: retires with the old site), ``sync_reconcile`` = ingest reconcile, ``sync_refdata`` = the refdata worker in mode
  daily / weekly / adjusted_backfill / raw_backfill, the same steps as ``run_refdata.ps1``); a worker only ever claims these job ids;
* at most ONE queued or running run per kind: a second request is refused with 409 ``SYNC_ALREADY_QUEUED`` (checked under an
  advisory lock, so two simultaneous requests cannot both pass);
* every parameter is bounded (``MAX_CODES``, ``MAX_SINCE_DAYS``, ``MAX_COUNT``, ``MAX_LAST``); unknown keys and unknown kinds are
  refused (400), never ignored;
* requesting needs the ops permission (``ops_service.is_ops``, same as the registered-job runner); an anonymous caller never reaches
  this module (the route resolves the principal first);
* a run is ``succeeded`` only when the command exited 0; exit 2 (source blocked us) and 3 (source unreachable) are ``failed`` with
  error codes ``SOURCE_BLOCKED`` / ``SOURCE_UNREACHABLE``; the job is never retried (``max_attempts`` = 1), so a block is not retried;
* the runner is injectable (tests pass a fake; the default calls ``ingest.run``), the worker never touches the network itself.
* the lease is short (``LEASE_SECONDS`` = 15 min) and renewed every ``HEARTBEAT_SECONDS`` by a thread on its own connection while the
  runner works, so a killed worker frees its kind after at most one lease: an expired ``running`` run no longer counts as active, and the
  next claim closes it as ``failed`` / ``LEASE_EXPIRED`` (never re-run: ``max_attempts`` = 1);
* the executor switch ``PULSE_SYNC_EXECUTOR=1`` is set only where a worker is scheduled; the list response reports it
  (``executor_enabled``) and a submission is refused (409 ``SYNC_EXECUTOR_DISABLED``) while it is off, so nothing queues up unexecuted.
Not covered: scheduling (the integrator registers ``sync_jobs run --max 1``), cancelling a running sync.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import logging
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from uuid import UUID
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from pulse import contracts
from pulse.adapters.postgres._canon import sha256_of
from pulse.adapters.postgres.jobs import PgJobStore
from pulse.application import ops_service
from pulse.application.ports.jobs import JobDefinition

LANE = "sync"
CONTRACT_ID = contracts.backfill_id(LANE)
SCHEMA_VERSION = "1.1.0"   # 1.1.0: executor_enabled + sync_refdata (2026-11-21); 1.0.0 stays registered
JOB_VERSION = 2              # v1 (schema 1.0.0) is registered on the server since r17; v2 carries schema 1.1.0
JOB_NS = uuid.UUID("6c2f8d14-3b7e-4a95-9e01-5d8a7c3b2f60")
KINDS = ("sync_daily_bars", "sync_legacy_import", "sync_reconcile", "sync_refdata")
JOB_IDS = {k: uuid.uuid5(JOB_NS, k) for k in KINDS}
JOB_OPERATION = "SyncJobService.run_job"
OPERATION = ("syncQueueJob", "/v1/x/bf/sync/jobs")
RESOURCE_TYPE = "sync_job"
WORKER_HINT = "uv run python -m pulse.workers.sync_jobs run --max 1"
EXECUTOR_ENV = "PULSE_SYNC_EXECUTOR"
LEASE_SECONDS = 900              # definition timeout = lease: a run whose heartbeat is older than this is dead
HEARTBEAT_SECONDS = 60.0
SHANGHAI = ZoneInfo("Asia/Shanghai")

MAX_CODES = 300                  # codes of one request (a full-universe daily sync uses ``universe`` instead)
MAX_SINCE_DAYS = 400
MAX_COUNT = 320                  # daily bars per code (the ingest command default)
MAX_LAST = 30
MAX_REFDATA_CODES = 5000         # codes of one refdata backfill pass (2 requests per code for adjusted, 1 for raw-only)
REFDATA_MODES = ("daily", "weekly", "adjusted_backfill", "raw_backfill")
REFDATA_BACKFILL_DEFAULT = {"adjusted_backfill": 300, "raw_backfill": 600}
MAX_SUMMARY_CHARS = 8000
SOURCES = ("sina", "tencent", "both")
BOARDS = ("main", "all")
DEFAULTS = {"sync_daily_bars": {"source": "sina", "count": 320}, "sync_legacy_import": {"boards": "main"}, "sync_reconcile": {"last": 5}, "sync_refdata": {"mode": "daily"}}
BOUNDS = {"max_codes": MAX_CODES, "max_since_days": MAX_SINCE_DAYS, "max_count": MAX_COUNT, "max_last": MAX_LAST, "max_refdata_codes": MAX_REFDATA_CODES}
log = logging.getLogger(__name__)


class SyncError(Exception):
    """Maps to an API error: (http status, code, message)."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass(frozen=True)
class Outcome:
    exit_status: int
    summary: str


Runner = Callable[[str, dict[str, Any]], Outcome]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def executor_enabled() -> bool:
    """True only where a sync worker is scheduled (env ``PULSE_SYNC_EXECUTOR=1``)."""
    return os.environ.get(EXECUTOR_ENV) == "1"


# ---- job registration -----------------------------------------------------------------------------------------------------
def job_definition(kind: str) -> JobDefinition:
    return JobDefinition(
        job_id=JOB_IDS[kind], job_version=JOB_VERSION, application_operation=JOB_OPERATION,
        schedule_document={"trigger": "manual", "manual_trigger": True, "timezone": "Asia/Shanghai"}, parameter_contract_id=CONTRACT_ID,
        parameter_schema_version=SCHEMA_VERSION, enabled=True, concurrency_key=kind, max_concurrency=1,
        timeout_seconds=LEASE_SECONDS, retry_policy={"max_attempts": 1})


_READY: set[str] = set()


def ensure_jobs(conn: psycopg.Connection) -> None:
    key = str(conn.info.dbname)
    if key in _READY:
        return
    store = PgJobStore(conn)
    for kind in KINDS:
        store.register_definition(job_definition(kind))
    _READY.add(key)


def ensure_command_binding(conn: psycopg.Connection) -> str:
    op, path = OPERATION
    if conn.execute("SELECT 1 FROM core.api_binding WHERE operation_id=%s", (op,)).fetchone() is None:
        conn.execute("INSERT INTO core.api_binding(operation_id, http_method, path_template, response_contract_id, response_schema_version,"
                     " application_service, read_model, freshness_policy_version) VALUES (%s,'POST',%s,%s,%s,%s,'none','backfill-v1') ON CONFLICT (operation_id) DO NOTHING",
                     (op, path, CONTRACT_ID, SCHEMA_VERSION, JOB_OPERATION))
    return op


# ---- validation -----------------------------------------------------------------------------------------------------------
def _bad(message: str) -> SyncError:
    return SyncError(400, "INVALID_PARAMETERS", message)


def _codes(raw: Any, *, required: bool) -> list[str]:
    if raw is None:
        if required:
            raise _bad("codes 必填")
        return []
    if not isinstance(raw, list) or not all(isinstance(c, str) for c in raw):
        raise _bad("codes 必须是 6 位数字代码的列表")
    out = list(dict.fromkeys(c.strip() for c in raw))
    if any(len(c) != 6 or not c.isdigit() for c in out):
        raise _bad("codes 只能是 6 位数字代码")
    if required and not out:
        raise _bad("codes 至少一个")
    if len(out) > MAX_CODES:
        raise _bad(f"一次最多 {MAX_CODES} 个代码，收到 {len(out)}")
    return out


def _int(params: dict[str, Any], name: str, default: int, low: int, high: int) -> int:
    v = params.get(name, default)
    if isinstance(v, bool) or not isinstance(v, int) or not low <= v <= high:
        raise _bad(f"{name} 必须是 {low}..{high} 的整数")
    return v


def _unknown(kind: str, params: dict[str, Any], allowed: set[str]) -> None:
    extra = sorted(set(params) - allowed)
    if extra:
        raise _bad(f"{kind} 不接受参数: {', '.join(extra)}")


def validate(kind: str, params: Any) -> dict[str, Any]:
    """The normalised, bounded parameters of a request; raises ``SyncError`` 400 otherwise."""
    if kind not in KINDS:
        raise _bad(f"未知同步类型 {kind!r}")
    if not isinstance(params, dict):
        raise _bad("parameters 必须是对象")
    if kind == "sync_daily_bars":
        _unknown(kind, params, {"codes", "universe", "source", "count"})
        universe = params.get("universe", False)
        if not isinstance(universe, bool):
            raise _bad("universe 必须是布尔值")
        codes = _codes(params.get("codes"), required=False)
        if bool(codes) == universe:
            raise _bad("codes 与 universe 二选一")
        source = params.get("source", "sina")
        if source not in SOURCES:
            raise _bad(f"source 只能是 {', '.join(SOURCES)}")
        out: dict[str, Any] = {"source": source, "count": _int(params, "count", 320, 1, MAX_COUNT)}
        out.update({"universe": True} if universe else {"codes": codes})
        return out
    if kind == "sync_legacy_import":
        _unknown(kind, params, {"since_days", "codes", "boards"})
        boards = params.get("boards", "main")
        if boards not in BOARDS:
            raise _bad(f"boards 只能是 {', '.join(BOARDS)}")
        if "since_days" not in params:
            raise _bad("since_days 必填")
        out = {"since_days": _int(params, "since_days", 0, 1, MAX_SINCE_DAYS), "boards": boards}
        codes = _codes(params.get("codes"), required=False)
        if codes:
            out["codes"] = codes
        return out
    if kind == "sync_refdata":
        _unknown(kind, params, {"mode", "max_codes"})
        mode = params.get("mode")
        if mode not in REFDATA_MODES:
            raise _bad(f"mode 只能是 {', '.join(REFDATA_MODES)}")
        if mode in REFDATA_BACKFILL_DEFAULT:
            return {"mode": mode, "max_codes": _int(params, "max_codes", REFDATA_BACKFILL_DEFAULT[mode], 1, MAX_REFDATA_CODES)}
        if "max_codes" in params:
            raise _bad(f"mode={mode} 不接受 max_codes（只有回填模式有）")
        return {"mode": mode}
    _unknown(kind, params, {"codes", "last"})
    return {"codes": _codes(params.get("codes"), required=True), "last": _int(params, "last", 5, 1, MAX_LAST)}


def ingest_argv(kind: str, params: dict[str, Any], today: date) -> list[str]:
    """The ``pulse.workers.ingest`` command line of a validated run. The rate limit is the command's own (1 request per second)."""
    if kind == "sync_daily_bars":
        argv = ["live-daily", "--source", params["source"], "--count", str(params["count"])]
        return argv + (["--universe"] if params.get("universe") else ["--codes", *params["codes"]])
    if kind == "sync_legacy_import":
        argv = ["legacy-import", "--since", (today - timedelta(days=params["since_days"])).isoformat(), "--boards", params["boards"]]
        return argv + (["--codes", *params["codes"]] if params.get("codes") else [])
    return ["reconcile", "--last", str(params["last"]), "--codes", *params["codes"]]


def refdata_steps(params: dict[str, Any]) -> list[tuple[str, list[str]]]:
    """(step name, ``pulse.workers.refdata`` argv) of a validated run: the same steps as ``deploy/windows/run_refdata.ps1``."""
    mode = params["mode"]
    if mode == "daily":
        return [("index-bars", ["index-bars", "--years", "3"]), ("adjusted-refresh", ["adjusted-bars", "--mode", "refresh", "--universe-listings"])]
    if mode == "weekly":
        return [("listings", ["listings"]), ("boards", ["boards"])]
    argv = ["adjusted-bars", "--universe-listings", "--max-codes", str(params["max_codes"])]
    return [("adjusted-backfill" if mode == "adjusted_backfill" else "raw-backfill", argv + (["--raw-only"] if mode == "raw_backfill" else []))]


# ---- queueing (API side) --------------------------------------------------------------------------------------------------
def active_run(conn: psycopg.Connection, kind: str, now: datetime) -> UUID | None:
    """A queued run, or a running one whose lease is still alive (an expired one is dead: the next claim closes it as LEASE_EXPIRED)."""
    row = conn.execute("SELECT job_run_id FROM ops.job_run WHERE job_id=%s AND (run_state='queued' OR (run_state='running'"
                       " AND heartbeat_at + make_interval(secs => %s) > %s)) LIMIT 1", (JOB_IDS[kind], LEASE_SECONDS, now)).fetchone()
    return None if row is None else row["job_run_id"]


def enqueue(conn: psycopg.Connection, pid: UUID, req: dict[str, Any], key: str, now: datetime) -> dict[str, Any]:
    """Command handler body (inside the idempotent command transaction): validate, refuse a duplicate, queue, log."""
    if not ops_service.is_ops(pid):
        raise SyncError(403, "FORBIDDEN", "无运行权限（ops:run）")
    if not executor_enabled():
        raise SyncError(409, "SYNC_EXECUTOR_DISABLED", f"同步执行器未启用（{EXECUTOR_ENV}≠1）：切换到新系统后才会执行同步，现在不接受提交")
    kind = req["kind"]
    params = validate(kind, req.get("parameters", {}))
    ensure_jobs(conn)
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 2))", (f"sync-enqueue:{kind}",))
    if (current := active_run(conn, kind, now)) is not None:
        raise SyncError(409, "SYNC_ALREADY_QUEUED", f"同类同步正在排队或运行（运行编号 {str(current)[:8]}），请等它结束后再提交")
    run_id = uuid.uuid5(JOB_IDS[kind], f"{pid}:{key}")
    store = PgJobStore(conn)
    store.enqueue(job_run_id=run_id, job_id=JOB_IDS[kind], job_version=JOB_VERSION, idempotency_key=f"{pid}:{key}",
                  parameter_hash=sha256_of(params), scheduled_for=now,
                  resource_type=RESOURCE_TYPE, resource_id=run_id)
    conn.execute("INSERT INTO ops.sync_job_log(job_run_id, kind, parameters, requested_by, requested_at) VALUES (%s,%s,%s,%s,%s)"
                 " ON CONFLICT (job_run_id) DO NOTHING", (run_id, kind, Jsonb(params), pid, now))
    return {"receipt": {**store.receipt(run_id)}, "kind": kind, "parameters": params, "worker_hint": WORKER_HINT}


_LIST_SQL = ("SELECT l.job_run_id, l.kind, l.parameters, l.requested_at, l.started_at, l.finished_at, l.exit_status, l.summary,"
             " r.run_state, r.error_code FROM ops.sync_job_log l JOIN ops.job_run r USING (job_run_id)")


def _wire(row: dict[str, Any]) -> dict[str, Any]:
    return {"job_run_id": row["job_run_id"], "kind": row["kind"], "run_state": row["run_state"], "parameters": row["parameters"],
            "requested_at": row["requested_at"], "started_at": row["started_at"], "finished_at": row["finished_at"],
            "exit_status": row["exit_status"], "error_code": row["error_code"], "summary": row["summary"]}


def list_jobs(conn: psycopg.Connection, pid: UUID, limit: int, kind: str | None) -> dict[str, Any]:
    if not ops_service.is_ops(pid):
        raise SyncError(403, "FORBIDDEN", "无运行权限（ops:read）")
    if not 1 <= limit <= 100:
        raise SyncError(400, "INVALID_PARAMETERS", "limit 必须在 1..100")
    if kind is not None and kind not in KINDS:
        raise SyncError(400, "INVALID_PARAMETERS", f"未知同步类型 {kind!r}")
    rows = conn.execute(_LIST_SQL + " WHERE (%s::text IS NULL OR l.kind=%s) ORDER BY l.requested_at DESC, l.job_run_id DESC LIMIT %s",
                        (kind, kind, limit)).fetchall()
    return {"items": [_wire(r) for r in rows], "kinds": list(KINDS), "defaults": DEFAULTS, "bounds": BOUNDS, "executor_enabled": executor_enabled(), "worker_hint": WORKER_HINT}


# ---- worker ---------------------------------------------------------------------------------------------------------------
_ERROR_CODES = {2: "SOURCE_BLOCKED", 3: "SOURCE_UNREACHABLE"}


def _clip(text: str) -> str:
    text = text.strip()
    if len(text) <= MAX_SUMMARY_CHARS:
        return text
    half = MAX_SUMMARY_CHARS // 2
    return f"{text[:half]}\n... [{len(text) - MAX_SUMMARY_CHARS} chars cut] ...\n{text[-half:]}"


def _run_cli(run: Callable[[list[str]], int], argv: list[str]) -> Outcome:
    """One worker command in this process, its output captured; a ``SystemExit("message")`` becomes exit status 1 with the message."""
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run(argv)
    except SystemExit as exc:               # the commands raise SystemExit("no codes ...") for an empty universe
        code = exc.code if isinstance(exc.code, int) else 1
        if not isinstance(exc.code, int):
            err.write(str(exc.code))
    return Outcome(int(code), "\n".join(x for x in (out.getvalue(), err.getvalue()) if x.strip()))


def ingest_runner(kind: str, params: dict[str, Any]) -> Outcome:
    """Default runner: the ingest / refdata command in this process (its own connection, rate-limited client and block handling).

    ``sync_refdata`` runs its steps in order like ``run_refdata.ps1``: a block (exit 2) skips the later steps, any other failure lets them
    run, and the exit status is the first non-zero one."""
    if kind == "sync_refdata":
        from pulse.workers import refdata

        final, parts = 0, []
        for name, argv in refdata_steps(params):
            if final == 2:
                parts.append(f"-- {name} skipped: an earlier step was blocked")
                continue
            step = _run_cli(refdata.run, argv)
            parts.append(f"== {name} exit {step.exit_status} ==\n{step.summary}")
            final = final or step.exit_status
        return Outcome(final, _clip("\n".join(parts)))
    from pulse.workers import ingest

    argv = ingest_argv(kind, params, datetime.now(timezone.utc).astimezone(SHANGHAI).date())
    res = _run_cli(ingest.run, argv)
    return Outcome(res.exit_status, _clip(res.summary))


@contextlib.contextmanager
def _heartbeat(connect: Callable[[], psycopg.Connection] | None, job_run_id: UUID, epoch: int, interval: float):
    """Renew the lease of a running run every ``interval`` seconds from a thread on its OWN connection; a no-op when ``connect`` is None.

    A failed renewal (fenced out, database gone) is logged and stops the thread: the run then simply loses its lease like a dead worker."""
    if connect is None:
        yield
        return
    stop = threading.Event()

    def beat() -> None:
        try:
            with contextlib.closing(connect()) as c:
                store = PgJobStore(c)
                while not stop.wait(interval):
                    store.heartbeat(job_run_id, epoch)
        except Exception:  # noqa: BLE001 - the heartbeat must never take the runner down; the lease just runs out
            log.exception("sync job %s: heartbeat stopped", job_run_id)

    t = threading.Thread(target=beat, name=f"sync-heartbeat-{job_run_id}", daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join(timeout=10)


def execute_pending(conn: psycopg.Connection, runner: Runner = ingest_runner, *, clock: Callable[[], datetime] = utc_now,
                    connect: Callable[[], psycopg.Connection] | None = None, heartbeat_seconds: float = HEARTBEAT_SECONDS) -> tuple[UUID, str, str] | None:
    """Claim the next queued sync run (any of the sync kinds) and run it. Returns (job_run_id, kind, state) or None when nothing is queued.

    ``connect`` opens a second connection for the lease heartbeat while the runner works (the worker passes one; tests may omit it)."""
    ensure_jobs(conn)
    store = PgJobStore(conn)
    job = None
    for kind in KINDS:
        job = store.claim(job_id=JOB_IDS[kind], now=clock())
        if job is not None:
            break
    if job is None:
        return None
    row = conn.execute("SELECT kind, parameters FROM ops.sync_job_log WHERE job_run_id=%s", (job.job_run_id,)).fetchone()
    if row is None:
        store.fail(job.job_run_id, job.lease_epoch, error_code="NO_LOG_ROW", now=clock())
        return job.job_run_id, "unknown", "failed"
    kind = row["kind"]
    conn.execute("UPDATE ops.sync_job_log SET started_at=%s WHERE job_run_id=%s", (clock(), job.job_run_id))
    try:
        with _heartbeat(connect, job.job_run_id, job.lease_epoch, heartbeat_seconds):
            outcome = runner(kind, row["parameters"])
        error = None if outcome.exit_status == 0 else _ERROR_CODES.get(outcome.exit_status, f"EXIT_{outcome.exit_status}")
    except Exception as exc:  # noqa: BLE001 - a sync failure is recorded on the run with its message, never swallowed
        log.exception("sync job %s (%s) failed", job.job_run_id, kind)
        outcome, error = Outcome(1, _clip(f"{type(exc).__name__}: {exc}")), type(exc).__name__[:60]
    end = clock()
    conn.execute("UPDATE ops.sync_job_log SET finished_at=%s, exit_status=%s, summary=%s WHERE job_run_id=%s",
                 (end, outcome.exit_status, outcome.summary, job.job_run_id))
    if error is None:
        store.succeed(job.job_run_id, job.lease_epoch, output_ref=f"sync-log:{job.job_run_id}",
                      output_hash=hashlib.sha256(outcome.summary.encode()).hexdigest(), now=end)
        return job.job_run_id, kind, "succeeded"
    store.fail(job.job_run_id, job.lease_epoch, error_code=error, now=end)
    return job.job_run_id, kind, "failed"


def default_ttl() -> timedelta:
    return timedelta(days=7)
