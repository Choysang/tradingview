"""Durable job runs with lease + fencing epoch on ops.job_definition / ops.job_run (W02, M4-E03).

Why: work must survive process restarts and an expired worker must not overwrite a newer result
(database_design.md section 5 "worker uses lease_epoch"). Implements ``pulse.application.ports.jobs``.
Invariants:
  * every claim (first, or re-claim after the heartbeat lease expired) increments ``lease_epoch`` and
    ``attempt_count`` by one; the epoch is the fencing token;
  * heartbeat / succeed / fail are single-statement compare-and-set on (run id, epoch, state='running');
    zero rows updated means FencedOut and the caller must drop its result;
  * the lease is ``heartbeat_at + timeout_seconds`` of the definition (no extra column);
  * concurrency_key / max_concurrency are enforced per claim under an advisory lock;
  * enqueue is idempotent on (job, version, idempotency_key); same key + different parameters is Conflict;
  * the resource id is fixed at enqueue, so JobReceipt exists before the work starts.
Retry policy: ``retry_policy.max_attempts`` (default 3) bounds claims; backoff is not implemented because
``scheduled_for`` is immutable in the package schema.
Not covered: cron expansion, cross-run dependencies.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from pulse.application.ports.errors import Conflict, FencedOut, NotFound
from pulse.application.ports.jobs import JobDefinition, JobRunRecord
from pulse.contracts import utc_z

DEFAULT_MAX_ATTEMPTS = 3
MAX_CLAIM_SCAN = 20          # candidates examined per claim (locked one at a time, SKIP LOCKED)
POLL_OPERATION_ID = "getJobRun"
_RUN_SELECT = (
    "SELECT r.job_run_id, r.job_id, r.job_version, r.idempotency_key, r.stage_id, r.parameter_hash,"
    " r.scheduled_for, r.run_state, r.lease_epoch, r.attempt_count, r.started_at, r.heartbeat_at,"
    " r.completed_at, r.error_code, x.resource_type, x.resource_id"
    " FROM ops.job_run r LEFT JOIN ops.job_run_resource x USING (job_run_id)"
)
_DEF_FIELDS = ("application_operation", "schedule_document", "parameter_contract_id", "parameter_schema_version",
               "enabled", "concurrency_key", "max_concurrency", "timeout_seconds", "retry_policy")


def _now(value: datetime | None) -> datetime:
    return value or datetime.now(timezone.utc)


class PgJobStore:
    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    # ---- definitions ---------------------------------------------------------------------------
    def register_definition(self, definition: JobDefinition) -> JobDefinition:
        with self.conn.transaction():
            inserted = self.conn.execute(
                "INSERT INTO ops.job_definition(job_id, job_version, application_operation, schedule_document,"
                " parameter_contract_id, parameter_schema_version, enabled, concurrency_key, max_concurrency,"
                " timeout_seconds, retry_policy, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,clock_timestamp())"
                " ON CONFLICT (job_id, job_version) DO NOTHING RETURNING job_id",
                (definition.job_id, definition.job_version, definition.application_operation,
                 Jsonb(dict(definition.schedule_document)), definition.parameter_contract_id,
                 definition.parameter_schema_version, definition.enabled, definition.concurrency_key,
                 definition.max_concurrency, definition.timeout_seconds, Jsonb(dict(definition.retry_policy)))
            ).fetchone()
            if inserted is None:
                row = self.conn.execute(
                    "SELECT * FROM ops.job_definition WHERE job_id=%s AND job_version=%s",
                    (definition.job_id, definition.job_version)).fetchone()
                for f in _DEF_FIELDS:
                    if row[f] != getattr(definition, f):
                        raise Conflict(f"job definition {definition.job_id} v{definition.job_version}"
                                       f" exists with a different {f}; register a new version")
        return definition

    # ---- enqueue / read ------------------------------------------------------------------------
    def enqueue(self, *, job_run_id: UUID, job_id: UUID, job_version: int, idempotency_key: str,
                parameter_hash: str, scheduled_for: datetime, resource_type: str, resource_id: UUID,
                stage_id: UUID | None = None) -> JobRunRecord:
        with self.conn.transaction():
            inserted = self.conn.execute(
                "INSERT INTO ops.job_run(job_run_id, job_id, job_version, idempotency_key, stage_id, parameter_hash,"
                " scheduled_for, run_state, lease_epoch, attempt_count) VALUES (%s,%s,%s,%s,%s,%s,%s,'queued',1,0)"
                " ON CONFLICT DO NOTHING RETURNING job_run_id",
                (job_run_id, job_id, job_version, idempotency_key, stage_id, parameter_hash, scheduled_for)
            ).fetchone()
            if inserted is None:
                existing = self.conn.execute(
                    _RUN_SELECT + " WHERE r.job_id=%s AND r.job_version=%s AND r.idempotency_key=%s",
                    (job_id, job_version, idempotency_key)).fetchone()
                if existing is None:
                    raise Conflict(f"job_run_id {job_run_id} is already used by another run")
                if (existing["job_run_id"], existing["parameter_hash"], existing["stage_id"],
                        existing["resource_type"], existing["resource_id"]) != (
                        job_run_id, parameter_hash, stage_id, resource_type, resource_id):
                    raise Conflict(f"idempotency key {idempotency_key!r} was used with different job parameters")
                return JobRunRecord(**existing)
            self.conn.execute(
                "INSERT INTO ops.job_run_resource(job_run_id, resource_type, resource_id) VALUES (%s,%s,%s)",
                (job_run_id, resource_type, resource_id))
        return self.get(job_run_id)

    def get(self, job_run_id: UUID) -> JobRunRecord:
        row = self.conn.execute(_RUN_SELECT + " WHERE r.job_run_id=%s", (job_run_id,)).fetchone()
        if row is None:
            raise NotFound(f"job run {job_run_id}")
        return JobRunRecord(**row)

    def receipt(self, job_run_id: UUID) -> dict[str, Any]:
        """Wire ``JobReceipt``: ties the run id to the produced resource and the query operation."""
        run = self.get(job_run_id)
        return {"job_run_id": str(run.job_run_id), "resource_id": str(run.resource_id),
                "run_state": run.run_state, "poll_operation_id": POLL_OPERATION_ID}

    def initiators(self, job_run_id: UUID) -> set[UUID]:
        """Principals whose committed command receipt created this run (empty: system or scheduled run, no recorded initiator)."""
        rows = self.conn.execute("SELECT principal_id FROM ops.command_receipt WHERE response_document->>'job_run_id' = %s",
                                 (str(job_run_id),)).fetchall()
        return {r["principal_id"] for r in rows}

    def job_run_wire(self, job_run_id: UUID) -> dict[str, Any]:
        """Wire ``JobRun`` (data of getJobRunResponse)."""
        r = self.get(job_run_id)
        return {"job_run_id": str(r.job_run_id), "job_id": str(r.job_id), "job_version": r.job_version,
                "stage_id": str(r.stage_id) if r.stage_id else None, "run_state": r.run_state,
                "lease_epoch": r.lease_epoch, "attempt_count": r.attempt_count,
                "scheduled_for": utc_z(r.scheduled_for), "heartbeat_at": utc_z(r.heartbeat_at),
                "completed_at": utc_z(r.completed_at), "error_code": r.error_code}

    # ---- claiming ------------------------------------------------------------------------------
    def claim(self, *, job_id: UUID | None = None, now: datetime | None = None) -> JobRunRecord | None:
        stamp = _now(now)
        with self.conn.transaction():
            skipped: list[UUID] = []
            for _ in range(MAX_CLAIM_SCAN):
                c = self.conn.execute(
                    "SELECT r.job_run_id, r.run_state, r.attempt_count, d.concurrency_key, d.max_concurrency,"
                    " d.retry_policy FROM ops.job_run r JOIN ops.job_definition d USING (job_id, job_version)"
                    " WHERE d.enabled AND (%(job)s::uuid IS NULL OR r.job_id=%(job)s)"
                    " AND r.job_run_id <> ALL(%(skip)s)"
                    " AND ((r.run_state='queued' AND r.scheduled_for <= %(now)s)"
                    "   OR (r.run_state='running' AND r.heartbeat_at + make_interval(secs => d.timeout_seconds) <= %(now)s))"
                    " ORDER BY r.scheduled_for, r.job_run_id LIMIT 1 FOR UPDATE OF r SKIP LOCKED",
                    {"now": stamp, "job": job_id, "skip": skipped}).fetchone()
                if c is None:
                    break
                skipped.append(c["job_run_id"])
                max_attempts = int(c["retry_policy"].get("max_attempts", DEFAULT_MAX_ATTEMPTS))
                if c["run_state"] == "running" and c["attempt_count"] >= max_attempts:
                    # The holder vanished and the budget is spent: close the run instead of looping forever.
                    self.conn.execute(
                        "UPDATE ops.job_run SET run_state='failed', completed_at=%s, error_code='LEASE_EXPIRED'"
                        " WHERE job_run_id=%s", (stamp, c["job_run_id"]))
                    continue
                self.conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 1))", (c["concurrency_key"],))
                running = self.conn.execute(
                    "SELECT count(*) AS n FROM ops.job_run r JOIN ops.job_definition d USING (job_id, job_version)"
                    " WHERE d.concurrency_key=%s AND r.run_state='running'"
                    " AND r.heartbeat_at + make_interval(secs => d.timeout_seconds) > %s",
                    (c["concurrency_key"], stamp)).fetchone()["n"]
                if running >= c["max_concurrency"]:
                    continue
                self.conn.execute(
                    "UPDATE ops.job_run SET run_state='running', lease_epoch=lease_epoch+1,"
                    " attempt_count=attempt_count+1, started_at=COALESCE(started_at, %s), heartbeat_at=%s,"
                    " error_code=NULL WHERE job_run_id=%s", (stamp, stamp, c["job_run_id"]))
                return self.get(c["job_run_id"])
        return None

    # ---- fenced updates ------------------------------------------------------------------------
    def _fenced(self, job_run_id: UUID, lease_epoch: int) -> FencedOut:
        row = self.conn.execute(
            "SELECT run_state, lease_epoch FROM ops.job_run WHERE job_run_id=%s", (job_run_id,)).fetchone()
        if row is None:
            raise NotFound(f"job run {job_run_id}")
        return FencedOut(f"job run {job_run_id}: epoch {lease_epoch} no longer owns it"
                         f" (state {row['run_state']}, epoch {row['lease_epoch']})")

    def heartbeat(self, job_run_id: UUID, lease_epoch: int, *, now: datetime | None = None) -> JobRunRecord:
        cur = self.conn.execute(
            "UPDATE ops.job_run SET heartbeat_at=%s WHERE job_run_id=%s AND lease_epoch=%s AND run_state='running'",
            (_now(now), job_run_id, lease_epoch))
        if cur.rowcount == 0:
            raise self._fenced(job_run_id, lease_epoch)
        return self.get(job_run_id)

    def succeed(self, job_run_id: UUID, lease_epoch: int, *, output_ref: str, output_hash: str,
                now: datetime | None = None) -> JobRunRecord:
        stamp = _now(now)
        with self.conn.transaction():
            cur = self.conn.execute(
                "UPDATE ops.job_run SET run_state='succeeded', completed_at=%s, heartbeat_at=%s, error_code=NULL"
                " WHERE job_run_id=%s AND lease_epoch=%s AND run_state='running'",
                (stamp, stamp, job_run_id, lease_epoch))
            if cur.rowcount == 0:
                raise self._fenced(job_run_id, lease_epoch)
            self.conn.execute(
                "UPDATE ops.job_run_resource SET result_lease_epoch=%s, output_ref=%s, output_hash=%s,"
                " result_recorded_at=%s WHERE job_run_id=%s", (lease_epoch, output_ref, output_hash, stamp, job_run_id))
        return self.get(job_run_id)

    def fail(self, job_run_id: UUID, lease_epoch: int, *, error_code: str, retryable: bool = False,
             now: datetime | None = None) -> JobRunRecord:
        stamp = _now(now)
        with self.conn.transaction():
            row = self.conn.execute(
                "SELECT r.attempt_count, d.retry_policy FROM ops.job_run r"
                " JOIN ops.job_definition d USING (job_id, job_version)"
                " WHERE r.job_run_id=%s AND r.lease_epoch=%s AND r.run_state='running' FOR UPDATE OF r",
                (job_run_id, lease_epoch)).fetchone()
            if row is None:
                raise self._fenced(job_run_id, lease_epoch)
            max_attempts = int(row["retry_policy"].get("max_attempts", DEFAULT_MAX_ATTEMPTS))
            if retryable and row["attempt_count"] < max_attempts:
                self.conn.execute(
                    "UPDATE ops.job_run SET run_state='queued', error_code=%s WHERE job_run_id=%s",
                    (error_code, job_run_id))
            else:
                self.conn.execute(
                    "UPDATE ops.job_run SET run_state='failed', completed_at=%s, error_code=%s WHERE job_run_id=%s",
                    (stamp, error_code, job_run_id))
        return self.get(job_run_id)

    def cancel(self, job_run_id: UUID, *, lease_epoch: int | None = None, now: datetime | None = None) -> JobRunRecord:
        with self.conn.transaction():
            cur = self.conn.execute(
                "UPDATE ops.job_run SET run_state='cancelled', completed_at=%s"
                " WHERE job_run_id=%s AND run_state IN ('queued','running')"
                " AND (%s::bigint IS NULL OR lease_epoch=%s)",
                (_now(now), job_run_id, lease_epoch, lease_epoch))
        if cur.rowcount == 0:
            run = self.get(job_run_id)
            if run.run_state == "cancelled" and lease_epoch is None:
                return run                                   # repeated cancel is idempotent
            raise self._fenced(job_run_id, lease_epoch if lease_epoch is not None else run.lease_epoch)
        return self.get(job_run_id)
