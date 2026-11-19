"""Backfill lane ``sync`` routes: queue a bounded data sync and read the run log (legacy rows #49 #89).

Contract: ``backfill_sync.schema.json``; every response leaves through ``respond_backfill`` and is validated first. Routers translate HTTP
only; the logic is in ``application.sync_job_service``. Both routes need a session AND the ops permission (``PULSE_OPS_PRINCIPALS``);
the POST carries an ``Idempotency-Key`` (equal to the body's ``idempotency_key`` when given) and only QUEUES a run:
``python -m pulse.workers.sync_jobs run`` executes it (the API never fetches), so a run stays ``queued`` until a worker ran.
POST /v1/x/bf/sync/jobs  {kind, parameters}  (Idempotency-Key)   SyncJobReceipt (202)   409 SYNC_ALREADY_QUEUED while one of that kind is queued/running
GET  /v1/x/bf/sync/jobs?limit=30&kind=                            SyncJobList (newest first: parameters, start / end, exit status, summary)
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Header, Request

from pulse import contracts
from pulse.adapters.postgres.commands import PgCommandStore
from pulse.api.auth import principal
from pulse.api.deps import conn_dep
from pulse.api.envelope import ApiError, meta, respond_backfill
from pulse.application import sync_job_service as S
from pulse.application.ports.errors import Conflict

router = APIRouter(prefix="/v1/x/bf/sync")
SESSION = {"x-auth": "session"}


def _only(request: Request, allowed: set[str]) -> None:
    unknown = sorted(set(request.query_params) - allowed)
    if unknown:
        raise ApiError(400, "INVALID_CONTRACT", "未知参数: " + ", ".join(unknown),
                       field_errors=[{"path": "/" + u, "code": "unknown_parameter", "message": "不在请求合同中"} for u in unknown])


def _err(e: S.SyncError) -> ApiError:
    return ApiError(e.status, e.code, e.message)


@router.post("/jobs", operation_id="syncQueueJob", summary="Queue one bounded data sync (daily bars, legacy import or reconcile)", openapi_extra=SESSION, status_code=202)
def queue_job(request: Request, body: dict = Body(...), key: str = Header(..., alias="Idempotency-Key"), conn=Depends(conn_dep)):
    pid = principal(request)
    if body.get("idempotency_key", key) != key:
        raise ApiError(400, "INVALID_CONTRACT", "body.idempotency_key 与 Idempotency-Key 不一致")
    req = {**body, "idempotency_key": key}
    sid = contracts.backfill_id(S.LANE)
    try:
        contracts.check("SyncJobRequest", req, sid)
    except contracts.ContractViolation as e:
        raise ApiError(400, "INVALID_CONTRACT", "请求不符合合同",
                       field_errors=[{"path": x.split(":", 1)[0] or "/", "code": "invalid", "message": x.split(":", 1)[-1].strip()[:200]} for x in e.errors[:10]]) from e
    now = S.utc_now()
    try:
        op = S.ensure_command_binding(conn)
        res = PgCommandStore(conn).execute(principal_id=pid, operation_id=op, idempotency_key=key, request=req, response_contract_id=sid,
                                           response_schema_version=S.SCHEMA_VERSION, handler=lambda c: S.enqueue(c, pid, req, key, now), ttl=S.default_ttl())
    except S.SyncError as e:
        raise _err(e) from e
    except Conflict as e:
        raise ApiError(409, "IDEMPOTENCY_CONFLICT", str(e)) from e
    return respond_backfill(S.LANE, "SyncJobReceipt", res.response, meta(), status=202)


@router.get("/jobs", operation_id="syncListJobs", summary="Recent data-sync runs with parameters, start / end, exit status and summary", openapi_extra=SESSION)
def list_jobs(request: Request, limit: int = 30, kind: str | None = None, conn=Depends(conn_dep)):
    pid = principal(request)
    _only(request, {"limit", "kind"})
    try:
        data = S.list_jobs(conn, pid, limit, kind)
    except S.SyncError as e:
        raise _err(e) from e
    return respond_backfill(S.LANE, "SyncJobList", data, meta())
