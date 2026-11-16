"""Operations backfill routes (legacy triage #63/#64 agent access, #97 test push, #169 forward monitor).

Paths: ``GET /llms.txt`` (plain text), ``GET /v1/x/bf/ops/manifest``, ``POST /v1/x/bf/ops/test-push`` (Idempotency-Key),
``GET /v1/x/bf/ops/forward-monitor``. Payloads are checked against ``backfill_ops.schema.json`` before they leave
(``respond_backfill``). The manifest and llms.txt are public (they describe the API, they hold no user data); the push and the
monitor need a session. ``router`` is what ``pulse.api.app`` mounts: it holds the ``/v1/x/bf/ops`` routes and, through
``include_router`` of a prefix-less second router, the absolute ``/llms.txt`` route.
Routers translate HTTP only; the work is in ``bf_ops_service``, ``alert_service.send_test_push`` and ``forward_monitor``.
"""
from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, Request
from fastapi.responses import PlainTextResponse

from pulse import config, contracts
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres.commands import PgCommandStore
from pulse.api.auth import principal
from pulse.api.deps import conn_dep
from pulse.api.envelope import ApiError, meta, respond_backfill
from pulse.application import alert_service, bf_ops_service, forward_monitor
from pulse.application import bindings as B
from pulse.application.ports.errors import Conflict

LANE = "ops"
#: test seam: an ``httpx`` transport used instead of the network for the test push (real sends are refused under pytest)
PUSH_TRANSPORT = None
MAX_RELEASES = 100

ops = APIRouter(prefix="/v1/x/bf/ops")
root = APIRouter()                                   # absolute paths (llms.txt)
router = APIRouter()


def _version() -> str:
    return contracts._load("backfill_ops.schema.json")["x-contract-version"]


def _violation(e: contracts.ContractViolation) -> ApiError:
    return ApiError(400, "INVALID_CONTRACT", "请求不符合合同",
                    field_errors=[{"path": x.split(":", 1)[0] or "/", "code": "invalid", "message": x.split(":", 1)[-1].strip()[:200]} for x in e.errors[:10]])


def _no_unknown_params(request: Request, allowed: set[str]) -> None:
    unknown = sorted(set(request.query_params) - allowed)
    if unknown:
        raise ApiError(400, "INVALID_CONTRACT", "未知参数: " + ", ".join(unknown),
                       field_errors=[{"path": "/" + u, "code": "unknown_parameter", "message": "不在请求合同中"} for u in unknown])


# ---- agent access -------------------------------------------------------------------------------------------------------------------
@ops.get("/manifest", operation_id="getAgentManifest", summary="Machine-readable list of every registered operation", openapi_extra={"x-auth": "public"})
def manifest(request: Request, conn=Depends(conn_dep)):
    return respond_backfill(LANE, "ManifestData", bf_ops_service.build_manifest(conn, request.app.openapi()["paths"]), meta())


@root.get(bf_ops_service.LLMS_PATH, operation_id="getLlmsTxt", summary="Plain-text API description for agents",
          openapi_extra={"x-auth": "public"})
def llms_txt(request: Request, conn=Depends(conn_dep)):
    text = bf_ops_service.render_llms_txt(bf_ops_service.build_manifest(conn, request.app.openapi()["paths"]))
    return PlainTextResponse(text, media_type="text/plain; charset=utf-8")


# ---- test push ------------------------------------------------------------------------------------------------------------------------
@ops.post("/test-push", operation_id="sendTestPush", summary="Send a test message to one of your alert destinations", openapi_extra={"x-auth": "session"})
def test_push(request: Request, body: dict = Body(...), key: str = Header(..., alias="Idempotency-Key"), conn=Depends(conn_dep)):
    pid = principal(request)
    if body.get("idempotency_key", key) != key:
        raise ApiError(400, "INVALID_CONTRACT", "body.idempotency_key 与 Idempotency-Key 不一致")
    req = {**body, "idempotency_key": key}
    sid = contracts.backfill_id(LANE)
    try:
        contracts.check("TestPushRequest", req, sid)
    except contracts.ContractViolation as e:
        raise _violation(e) from e
    bf_ops_service.ensure_command_binding(conn, "sendTestPush", "POST", "/v1/x/bf/ops/test-push", "AlertService.send_test_push")
    try:
        res = PgCommandStore(conn).execute(
            principal_id=pid, operation_id="sendTestPush", idempotency_key=key, request=req, response_contract_id=sid, response_schema_version=_version(),
            handler=lambda c: alert_service.send_test_push(c, pid, UUID(req["destination_id"]), key, transport=PUSH_TRANSPORT), ttl=timedelta(days=7))
    except alert_service.AlertError as e:
        raise ApiError(e.status, e.code, e.message) from e
    except Conflict as e:
        raise ApiError(409, "IDEMPOTENCY_CONFLICT", str(e)) from e
    return respond_backfill(LANE, "TestPushData", res.response, meta())


# ---- forward monitor ------------------------------------------------------------------------------------------------------------------
@ops.get("/forward-monitor", operation_id="getForwardMonitor", summary="Rank IC and top-decile excess of published rankings (gross, pending until mature)",
         openapi_extra={"x-auth": "session"})
def forward_monitor_read(request: Request, slot: str = B.SLOT_KEY, release_id: UUID | None = None, limit: int = 30, conn=Depends(conn_dep)):
    principal(request)
    _no_unknown_params(request, {"slot", "release_id", "limit"})
    if not 1 <= limit <= MAX_RELEASES:
        raise ApiError(400, "INVALID_CONTRACT", f"limit 必须在 1 到 {MAX_RELEASES} 之间")
    rows = forward_monitor.read_rows(conn, LakeStore(config.load().lake_root), slot=slot, release_id=release_id, limit_releases=limit)
    resolved = sum(1 for r in rows if r["status"] == "resolved")
    data = {"slot_key": slot, "method_version": forward_monitor.F.METHOD_VERSION, "return_basis": forward_monitor.RETURN_BASIS,
            "note": forward_monitor.NOTE, "resolved_count": resolved, "pending_count": len(rows) - resolved, "rows": rows}
    return respond_backfill(LANE, "ForwardMonitorData", data, meta(release_id=release_id))


router.include_router(ops)          # after the decorators above: include_router copies the routes present at call time
router.include_router(root)
