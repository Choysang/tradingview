"""Job scheduling port (W02, ops.job_definition / ops.job_run, M4-E03).

Why: background work must survive restarts and an expired worker must never overwrite a newer result.
Invariants: every claim takes a strictly larger lease epoch (the fencing token); heartbeats, results,
failures and cancellations are compare-and-set on (run id, epoch, running); a terminal run is immutable;
a run's resource id is fixed at enqueue so JobReceipt is answerable before the work starts.
Not covered: backoff scheduling (scheduled_for is immutable in the package), cron expansion.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol
from uuid import UUID


@dataclass(frozen=True)
class JobDefinition:
    job_id: UUID
    job_version: int
    application_operation: str
    schedule_document: Mapping[str, Any]
    parameter_contract_id: str
    parameter_schema_version: str
    enabled: bool
    concurrency_key: str
    max_concurrency: int
    timeout_seconds: int
    retry_policy: Mapping[str, Any]


@dataclass(frozen=True)
class JobRunRecord:
    job_run_id: UUID
    job_id: UUID
    job_version: int
    idempotency_key: str
    stage_id: UUID | None
    parameter_hash: str
    scheduled_for: datetime
    run_state: str
    lease_epoch: int
    attempt_count: int
    started_at: datetime | None
    heartbeat_at: datetime | None
    completed_at: datetime | None
    error_code: str | None
    resource_type: str | None = None
    resource_id: UUID | None = None


class JobStore(Protocol):
    def register_definition(self, definition: JobDefinition) -> JobDefinition: ...

    def enqueue(
        self,
        *,
        job_run_id: UUID,
        job_id: UUID,
        job_version: int,
        idempotency_key: str,
        parameter_hash: str,
        scheduled_for: datetime,
        resource_type: str,
        resource_id: UUID,
        stage_id: UUID | None = None,
    ) -> JobRunRecord: ...

    def claim(self, *, job_id: UUID | None = None, now: datetime | None = None) -> JobRunRecord | None: ...

    def heartbeat(self, job_run_id: UUID, lease_epoch: int, *, now: datetime | None = None) -> JobRunRecord: ...

    def succeed(
        self, job_run_id: UUID, lease_epoch: int, *, output_ref: str, output_hash: str, now: datetime | None = None
    ) -> JobRunRecord: ...

    def fail(
        self, job_run_id: UUID, lease_epoch: int, *, error_code: str, retryable: bool = False, now: datetime | None = None
    ) -> JobRunRecord: ...

    def cancel(self, job_run_id: UUID, *, lease_epoch: int | None = None, now: datetime | None = None) -> JobRunRecord: ...

    def get(self, job_run_id: UUID) -> JobRunRecord: ...

    def receipt(self, job_run_id: UUID) -> dict[str, Any]: ...

    def job_run_wire(self, job_run_id: UUID) -> dict[str, Any]: ...
