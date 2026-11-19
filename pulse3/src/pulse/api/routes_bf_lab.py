"""Backfill lane ``lab`` routes: historical experiment ledger + retired-tool lookup (#177 #178), tear sheet (#160 #161), batch comparison (#36 #77).

Contract: ``backfill_lab.schema.json``; every response leaves through ``respond_backfill`` and is validated first. Routers translate HTTP only;
the logic is in ``application.bf_lab_ledger`` and ``application.bf_lab_service``. All reads need a session.
GET  /v1/x/bf/lab/ledger?verdict=&track=&round=&q=&ledger_sha256=&offset=0&limit=100      LedgerList
GET  /v1/x/bf/lab/ledger/imports                                                           LedgerImports
GET  /v1/x/bf/lab/ledger/{entry_id}?ledger_sha256=                                         LedgerEntryDetail
GET  /v1/x/bf/lab/retired-tools?tool=&ledger_id=                                           RetiredTools
GET  /v1/x/bf/lab/tearsheet?catalog_id=PB:<id>|experiment_id=<uuid>|book_key=<key>         TearSheetData
POST /v1/x/bf/lab/batch-compare  (Idempotency-Key)  {screen_run_id, playbook_id, top_n?, window_sessions?}   BatchCompareReceipt (202)
GET  /v1/x/bf/lab/batch-compare?limit=20&cursor=                                           BatchRunList (your own runs)
GET  /v1/x/bf/lab/batch-compare/{job_run_id}                                               BatchRun
The ledger import is a CLI step (``python -m pulse.workers.lab import-ledger``), not an HTTP command: it reads only the frozen files in the package.
"""
from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Header, Request

from pulse import config, contracts
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres.commands import PgCommandStore
from pulse.api import deps
from pulse.api.auth import principal
from pulse.api.deps import conn_dep
from pulse.api.envelope import ApiError, meta, respond_backfill
from pulse.application import bf_lab_ledger as LED
from pulse.application import bf_lab_service as LAB
from pulse.application.ports.errors import Conflict

router = APIRouter(prefix="/v1/x/bf/lab")
LANE = LAB.LANE
SESSION = {"x-auth": "session"}
TTL = timedelta(days=7)


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


def _sha(raw: str | None) -> str | None:
    if raw and not (len(raw) == 64 and all(c in "0123456789abcdef" for c in raw)):
        raise ApiError(400, "INVALID_CONTRACT", "ledger_sha256 必须是 64 位小写十六进制")
    return raw or None


def _err(e: LED.LedgerError | LAB.LabError) -> ApiError:
    return ApiError(e.status, e.code, e.message)


# ---- ledger (#177 / #178) -------------------------------------------------------------------------------------------------------------
@router.get("/ledger", operation_id="getLabLedger", summary="Historical experiment ledger of the old lab (read-only)", openapi_extra=SESSION)
def ledger(request: Request, verdict: str | None = None, track: str | None = None, round: str | None = None, q: str | None = None,
           ledger_sha256: str | None = None, offset: int = 0, limit: int = 100, conn=Depends(conn_dep)):
    principal(request)
    _only(request, {"verdict", "track", "round", "q", "ledger_sha256", "offset", "limit"})
    try:
        data = LED.ledger_list(conn, ledger_sha256=_sha(ledger_sha256), verdict=verdict or None, track=track or None, rnd=round or None, q=q or None,
                               offset=offset, limit=limit)
    except LED.LedgerError as e:
        raise _err(e) from e
    return respond_backfill(LANE, "LedgerList", data, meta(quality_status="valid" if data["ledger"] else "partial"))


@router.get("/ledger/imports", operation_id="getLabLedgerImports", summary="Which ledger versions are imported", openapi_extra=SESSION)
def ledger_imports(request: Request, conn=Depends(conn_dep)):
    principal(request)
    return respond_backfill(LANE, "LedgerImports", {"imports": LED.list_imports(conn), "not_imported": LED.not_imported(conn)}, meta())


@router.get("/ledger/{entry_id}", operation_id="getLabLedgerEntry", summary="One ledger entry with what it supersedes and which retired tools cite it",
            openapi_extra=SESSION)
def ledger_entry(entry_id: str, request: Request, ledger_sha256: str | None = None, conn=Depends(conn_dep)):
    principal(request)
    _only(request, {"ledger_sha256"})
    try:
        data = LED.ledger_entry(conn, entry_id, _sha(ledger_sha256))
    except LED.LedgerError as e:
        raise _err(e) from e
    return respond_backfill(LANE, "LedgerEntryDetail", data, meta())


@router.get("/retired-tools", operation_id="getLabRetiredTools", summary="Which ledger entries explain why an old research tool was retired",
            openapi_extra=SESSION)
def retired_tools(request: Request, tool: str | None = None, ledger_id: str | None = None, conn=Depends(conn_dep)):
    principal(request)
    _only(request, {"tool", "ledger_id"})
    return respond_backfill(LANE, "RetiredTools", LED.retired_tools(conn, tool=tool or None, ledger_id=ledger_id or None), meta())


# ---- tear sheet (#160 / #161) -----------------------------------------------------------------------------------------------------------
@router.get("/tearsheet", operation_id="getLabTearSheet", summary="Tear sheet of a stored playbook replay or of a paper book", openapi_extra=SESSION)
def tearsheet(request: Request, catalog_id: str | None = None, experiment_id: str | None = None, book_key: str | None = None, conn=Depends(conn_dep)):
    pid = principal(request)
    _only(request, {"catalog_id", "experiment_id", "book_key"})
    given = [x for x in (catalog_id, experiment_id, book_key) if x]
    if len(given) != 1:
        raise ApiError(400, "INVALID_CONTRACT", "catalog_id、experiment_id、book_key 必须且只能给一个")
    try:
        if book_key:
            data = LAB.book_tearsheet(conn, pid, book_key)
        else:
            data = LAB.replay_tearsheet(conn, catalog_id=catalog_id or None, experiment_id=_uuid(experiment_id, "experiment_id"))
    except LAB.LabError as e:
        raise _err(e) from e
    return respond_backfill(LANE, "TearSheetData", data, meta(quality_status="partial"))


# ---- batch comparison (#36 / #77) -------------------------------------------------------------------------------------------------------
def _run_pending(max_runs: int = 2) -> None:
    """Background executor (the worker CLI does the same work out of process)."""
    lake = LakeStore(config.load().lake_root)
    with deps.pool().connection() as conn:
        for _ in range(max_runs):
            if LAB.execute_pending(conn, lake) is None:
                break


@router.post("/batch-compare", operation_id=LAB.OPERATION_ID, summary="Replay one playbook on the top rows of a screen run (queued job)", openapi_extra=SESSION)
def batch_compare(request: Request, tasks: BackgroundTasks, body: dict = Body(...), key: str = Header(..., alias="Idempotency-Key"), conn=Depends(conn_dep)):
    pid = principal(request)
    if body.get("idempotency_key", key) != key:
        raise ApiError(400, "INVALID_CONTRACT", "body.idempotency_key 与 Idempotency-Key 不一致")
    req = {**body, "idempotency_key": key}
    sid = contracts.backfill_id(LANE)
    try:
        contracts.check("BatchCompareRequest", req, sid)
    except contracts.ContractViolation as e:
        raise _violation(e) from e
    LAB.ensure_command_binding(conn)
    now = LAB.utc_now()
    try:
        res = PgCommandStore(conn).execute(
            principal_id=pid, operation_id=LAB.OPERATION_ID, idempotency_key=key, request=req, response_contract_id=sid, response_schema_version=LAB.SCHEMA_VERSION,
            handler=lambda c: LAB.create_batch(c, pid, req, key, now), ttl=TTL)
    except LAB.LabError as e:
        raise _err(e) from e
    except Conflict as e:
        raise ApiError(409, "IDEMPOTENCY_CONFLICT", str(e)) from e
    tasks.add_task(_run_pending)
    return respond_backfill(LANE, "BatchCompareReceipt", res.response, meta(), status=202)


@router.get("/batch-compare", operation_id="listLabBatchCompare", summary="Your batch comparison runs, newest first", openapi_extra=SESSION)
def batch_list(request: Request, limit: int = 20, cursor: str | None = None, conn=Depends(conn_dep)):
    pid = principal(request)
    _only(request, {"limit", "cursor"})
    try:
        data = LAB.list_runs(conn, pid, limit, cursor or None)
    except LAB.LabError as e:
        raise _err(e) from e
    return respond_backfill(LANE, "BatchRunList", data, meta(next_cursor=data["page"]["next_cursor"]))


@router.get("/batch-compare/{job_run_id}", operation_id="getLabBatchCompare", summary="One batch comparison run with its result", openapi_extra=SESSION)
def batch_get(job_run_id: str, request: Request, conn=Depends(conn_dep)):
    pid = principal(request)
    rid = _uuid(job_run_id, "job_run_id")
    assert rid is not None
    try:
        data = LAB.get_run(conn, pid, rid)
    except LAB.LabError as e:
        raise _err(e) from e
    return respond_backfill(LANE, "BatchRun", data, meta(quality_status="partial"))
