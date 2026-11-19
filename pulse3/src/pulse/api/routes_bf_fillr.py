"""Backfill lane ``fillr`` routes: playbook board, custom run, monthly returns, factor series, analog study on demand.

Contract: ``backfill_fillr.schema.json``; every response leaves through ``respond_backfill`` and is validated first. Routers translate HTTP only;
the logic is in ``application.bf_fillr_service``. All reads and commands need a session. Commands carry an ``Idempotency-Key`` (equal to the body's
``idempotency_key`` when given) and only QUEUE a job: ``python -m pulse.workers.fillr run`` executes it (the API does not), so a result stays
``queued`` until a worker ran.
GET  /v1/x/bf/fillr/playbook-board                                                    PlaybookBoard
POST /v1/x/bf/fillr/custom-run  {playbook_id, overrides, label?}  (Idempotency-Key)   CustomRunReceipt (202): registers a draft, queues the run
GET  /v1/x/bf/fillr/custom-runs?limit=20&cursor=                                      CustomRunList (your own runs)
GET  /v1/x/bf/fillr/custom-runs/{job_run_id}                                          CustomRun
GET  /v1/x/bf/fillr/monthly-returns?catalog_id=PB:<id>|experiment_id=<uuid>           MonthlyReturns
GET  /v1/x/bf/fillr/factor-series?experiment_id=<uuid>|factor_id=FL:<id>              FactorSeriesStatus
POST /v1/x/bf/fillr/factor-series  {experiment_id}  (Idempotency-Key)                 JobQueued (202)
GET  /v1/x/bf/fillr/analog-study?instrument_id=&release_id=&selection_policy_version= AnalogStatus
POST /v1/x/bf/fillr/analog-study  {instrument_id, release_id, selection_policy_version}  (Idempotency-Key)   JobQueued (202)
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, Request

from pulse import config, contracts
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres.commands import PgCommandStore
from pulse.api.auth import principal
from pulse.api.deps import conn_dep
from pulse.api.envelope import ApiError, meta, respond_backfill
from pulse.application import bf_fillr_service as F
from pulse.application.ports.errors import Conflict

router = APIRouter(prefix="/v1/x/bf/fillr")
LANE = F.LANE
SESSION = {"x-auth": "session"}


def _violation(e: contracts.ContractViolation) -> ApiError:
    return ApiError(400, "INVALID_CONTRACT", "请求不符合合同",
                    field_errors=[{"path": x.split(":", 1)[0] or "/", "code": "invalid", "message": x.split(":", 1)[-1].strip()[:200]} for x in e.errors[:10]])


def _only(request: Request, allowed: set[str]) -> None:
    unknown = sorted(set(request.query_params) - allowed)
    if unknown:
        raise ApiError(400, "INVALID_CONTRACT", "未知参数: " + ", ".join(unknown),
                       field_errors=[{"path": "/" + u, "code": "unknown_parameter", "message": "不在请求合同中"} for u in unknown])


def _uuid(raw: str | None, name: str) -> UUID | None:
    if not raw:
        return None
    try:
        return UUID(raw)
    except ValueError as e:
        raise ApiError(400, "INVALID_CONTRACT", f"{name} 不是合法的 UUID") from e


def _err(e: Exception) -> ApiError:
    return ApiError(e.status, e.code, e.message)  # type: ignore[attr-defined]


def _command(conn, request: Request, kind: str, def_name: str, body: dict, key: str, handler, out_def: str):
    pid = principal(request)
    if body.get("idempotency_key", key) != key:
        raise ApiError(400, "INVALID_CONTRACT", "body.idempotency_key 与 Idempotency-Key 不一致")
    req = {**body, "idempotency_key": key}
    sid = contracts.backfill_id(LANE)
    try:
        contracts.check(def_name, req, sid)
    except contracts.ContractViolation as e:
        raise _violation(e) from e
    op = F.ensure_command_binding(conn, kind)
    now = F.utc_now()
    try:
        res = PgCommandStore(conn).execute(principal_id=pid, operation_id=op, idempotency_key=key, request=req, response_contract_id=sid,
                                           response_schema_version=F.SCHEMA_VERSION, handler=lambda c: handler(c, pid, req, now), ttl=F.default_ttl())
    except F.FillrError as e:
        raise _err(e) from e
    except Conflict as e:
        raise ApiError(409, "IDEMPOTENCY_CONFLICT", str(e)) from e
    return respond_backfill(LANE, out_def, res.response, meta(), status=202)


# ---- board (#16 #82 #150) ---------------------------------------------------------------------------------------------------------------
@router.get("/playbook-board", operation_id="getFillrPlaybookBoard", summary="Fitness, verdict, out-of-sample average and sparkline of every playbook", openapi_extra=SESSION)
def playbook_board(request: Request, conn=Depends(conn_dep)):
    principal(request)
    _only(request, set())
    data = F.board(conn, LakeStore(config.load().lake_root), F.utc_now())
    warnings = []
    if data["counts"]["evaluated"] == 0:
        warnings.append({"code": "NO_STORED_REPLAY", "message": "没有任何战法的回放结果：运行 research ext-b-run 后才有适配度与判定",
                         "affected_component": "bf_fillr", "evidence_ref": None})
    return respond_backfill(LANE, "PlaybookBoard", data, meta(quality_status="partial", warnings=warnings))


# ---- custom run (#21 #87) ---------------------------------------------------------------------------------------------------------------
@router.post("/custom-run", operation_id="fillrCustomRun", summary="Register a draft of a customised playbook and queue its replay", openapi_extra=SESSION)
def custom_run(request: Request, body: dict = Body(...), key: str = Header(..., alias="Idempotency-Key"), conn=Depends(conn_dep)):
    return _command(conn, request, "custom_run", "CustomRunRequest", body, key, lambda c, pid, req, now: F.create_custom_run(c, pid, req, key, now), "CustomRunReceipt")


@router.get("/custom-runs", operation_id="listFillrCustomRuns", summary="Your custom runs, newest first", openapi_extra=SESSION)
def custom_runs(request: Request, limit: int = 20, cursor: str | None = None, conn=Depends(conn_dep)):
    pid = principal(request)
    _only(request, {"limit", "cursor"})
    try:
        data = F.list_custom_runs(conn, pid, limit, cursor or None)
    except F.FillrError as e:
        raise _err(e) from e
    return respond_backfill(LANE, "CustomRunList", data, meta(next_cursor=data["page"]["next_cursor"]))


@router.get("/custom-runs/{job_run_id}", operation_id="getFillrCustomRun", summary="One custom run with its result", openapi_extra=SESSION)
def custom_run_get(job_run_id: str, request: Request, conn=Depends(conn_dep)):
    pid = principal(request)
    rid = _uuid(job_run_id, "job_run_id")
    assert rid is not None
    try:
        data = F.get_custom_run(conn, pid, rid)
    except F.FillrError as e:
        raise _err(e) from e
    return respond_backfill(LANE, "CustomRun", data, meta(quality_status="partial"))


# ---- monthly returns (#35) --------------------------------------------------------------------------------------------------------------
@router.get("/monthly-returns", operation_id="getFillrMonthlyReturns", summary="Calendar-month returns of a stored playbook replay", openapi_extra=SESSION)
def monthly_returns(request: Request, catalog_id: str | None = None, experiment_id: str | None = None, conn=Depends(conn_dep)):
    principal(request)
    _only(request, {"catalog_id", "experiment_id"})
    eid = _uuid(experiment_id, "experiment_id")
    if (eid is None) == (not catalog_id):
        raise ApiError(400, "INVALID_CONTRACT", "catalog_id 与 experiment_id 必须且只能给一个")
    try:
        data = F.monthly_returns(conn, catalog_id=catalog_id or None, experiment_id=eid)
    except F.FillrError as e:
        raise _err(e) from e
    return respond_backfill(LANE, "MonthlyReturns", data, meta(quality_status="partial"))


# ---- factor series (#37 #80) ------------------------------------------------------------------------------------------------------------
@router.get("/factor-series", operation_id="getFillrFactorSeries", summary="Daily IC and long-short cumulative curve of a factor experiment", openapi_extra=SESSION)
def factor_series(request: Request, experiment_id: str | None = None, factor_id: str | None = None, conn=Depends(conn_dep)):
    principal(request)
    _only(request, {"experiment_id", "factor_id"})
    eid = _uuid(experiment_id, "experiment_id")
    if (eid is None) == (not factor_id):
        raise ApiError(400, "INVALID_CONTRACT", "experiment_id 与 factor_id 必须且只能给一个")
    try:
        data = F.factor_series_status(conn, experiment_id=eid, factor_id=factor_id or None)
    except F.FillrError as e:
        raise _err(e) from e
    return respond_backfill(LANE, "FactorSeriesStatus", data, meta(quality_status="partial"))


@router.post("/factor-series", operation_id="fillrFactorSeries", summary="Queue the computation of a factor experiment's daily series", openapi_extra=SESSION)
def factor_series_queue(request: Request, body: dict = Body(...), key: str = Header(..., alias="Idempotency-Key"), conn=Depends(conn_dep)):
    return _command(conn, request, "factor_series", "FactorSeriesRequest", body, key, lambda c, pid, req, now: F.create_factor_series(c, pid, req, now), "JobQueued")


# ---- analog study (#42 #68 #158) --------------------------------------------------------------------------------------------------------
@router.get("/analog-study", operation_id="getFillrAnalogStudy", summary="Whether the analog study of one instrument and release exists or is queued", openapi_extra=SESSION)
def analog_study(request: Request, instrument_id: str | None = None, release_id: str | None = None, selection_policy_version: str | None = None,
                 conn=Depends(conn_dep)):
    principal(request)
    _only(request, {"instrument_id", "release_id", "selection_policy_version"})
    iid, rid = _uuid(instrument_id, "instrument_id"), _uuid(release_id, "release_id")
    if iid is None or rid is None or not selection_policy_version:
        raise ApiError(400, "INVALID_CONTRACT", "instrument_id、release_id、selection_policy_version 都必须给")
    try:
        data = F.analog_status(conn, iid, rid, selection_policy_version)
    except F.FillrError as e:
        raise _err(e) from e
    return respond_backfill(LANE, "AnalogStatus", data, meta(quality_status="partial"))


@router.post("/analog-study", operation_id="fillrAnalogStudy", summary="Queue the build of an analog study for one instrument and release", openapi_extra=SESSION)
def analog_study_queue(request: Request, body: dict = Body(...), key: str = Header(..., alias="Idempotency-Key"), conn=Depends(conn_dep)):
    return _command(conn, request, "analog_study", "AnalogStudyRequest", body, key, lambda c, pid, req, now: F.create_analog_study(c, pid, req, now), "JobQueued")
