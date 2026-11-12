"""Extension group C routes (contract ``urn:pulse:contracts:ext:v1``, paths ``/v1/x/<kebab-op>``).

25 operations: proposeTradePlan, queryPlanVersions, listTradePlans, queryPlanProposal (PEX05); queryEventClusters, queryCalendarEvents
(PEX07); querySeatDirectory, querySeatProfile, querySeatReports (PEX08); queryCommunityOpinions (PEX09); listAuthorizedAccounts,
importBrokerFills, queryAccountOrders, queryAccountPerformance, queryOpportunities (PEX12); saveAlertRule, listUserAlerts (PEX13);
deleteWorkspaceItem, scheduleWorkspaceExport, queryWorkspaceRevision, queryExportArtifact, queryWorkspaceIndex (PEX14);
runRegisteredJob, queryDatasetStatus, queryJobRuns (PEX15).
HTTP shape: GET queries take every ``<op>Request`` field as a query parameter (object/array values are JSON text); POST commands take the
JSON body plus the ``Idempotency-Key`` header (the key is injected into the body for contract validation; a body key must match).
Every response leaves through ``respond_ext`` (checked against ``<op>Response``); every request is checked against ``<op>Request``.
Supplementary reads (NOT contract operations - proposed additions, see docs/ext/C_evidence.md): ``/v1/x/c/artifact`` resolves the
``ArtifactRef`` the typed responses carry, ``/v1/x/c/alert-rules`` lists rules and destinations, ``/v1/x/c/calendar-links`` shows the
event->industry->stock links, ``/v1/x/c/forward-books`` the LEGACY_FORWARD books, ``/v1/x/c/job-definitions`` and
``/v1/x/c/readmodel-context`` what ``runRegisteredJob`` / the account queries need as input.
Routers translate HTTP only; no business logic lives here.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, Request

from pulse import contracts
from pulse.adapters.postgres._canon import sha256_of
from pulse.adapters.postgres.commands import PgCommandStore
from pulse.api.auth import principal
from pulse.api.deps import conn_dep
from pulse.api.envelope import ApiError, meta, respond, respond_ext
from pulse.application import alert_service, calendar_service, ops_service, portfolio_service, workspace_service
from pulse.application.ports.errors import Conflict

router = APIRouter(prefix="/v1/x")
MAX_PAGE = 200
INT_REFS = {"SafeInteger", "PositiveInteger"}
UUID_KEYS = {"rule_id", "object_id", "account_id", "instrument_id", "release_id", "based_on_release_id", "plan_id", "snapshot_id", "raw_object_id", "job_id",
             "proposal_id", "export_id", "seat_id", "cluster_id", "experiment_id", "annotation_id", "alert_id", "calendar_event_id"}
SERVICE_ERRORS = (portfolio_service.PortfolioError, calendar_service.IntelError, workspace_service.WorkspaceError, alert_service.AlertError, ops_service.OpsError)


def _api(e: Exception) -> ApiError:
    return ApiError(e.status, e.code, e.message)           # type: ignore[attr-defined]


def _ext_version() -> str:
    return contracts._load("extension.schema.json")["x-contract-version"]


def _ts(s: Any) -> datetime:
    return s if isinstance(s, datetime) else datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def _win(w: dict) -> dict:
    return {"start": _ts(w["start"]), "end": _ts(w["end"])}


def _kind(prop: dict) -> str:
    refs: list[str] = []

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            if "$ref" in x:
                refs.append(x["$ref"].split("/")[-1])
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(prop)
    return "int" if INT_REFS & set(refs) else "str"


def _violation(e: contracts.ContractViolation) -> ApiError:
    return ApiError(400, "INVALID_CONTRACT", "请求不符合合同",
                    field_errors=[{"path": x.split(":", 1)[0] or "/", "code": "invalid", "message": x.split(":", 1)[-1].strip()[:200]} for x in e.errors[:10]])


def query_params(op: str, request: Request) -> dict[str, Any]:
    props = contracts._load("extension.schema.json")["$defs"][f"{op}Request"]["properties"]
    q = request.query_params
    unknown = sorted(set(q) - set(props))
    if unknown:
        raise ApiError(400, "INVALID_CONTRACT", "未知参数: " + ", ".join(unknown),
                       field_errors=[{"path": "/" + u, "code": "unknown_parameter", "message": "不在请求合同中"} for u in unknown])
    out: dict[str, Any] = {}
    for name, prop in props.items():
        raw = q.get(name)
        if raw is None or raw == "":
            continue
        if raw[:1] in ("{", "["):
            try:
                out[name] = json.loads(raw)
            except ValueError as e:
                raise ApiError(400, "INVALID_CONTRACT", f"{name} 不是合法 JSON") from e
        elif raw == "null":
            out[name] = None
        elif _kind(prop) == "int":
            try:
                out[name] = int(raw)
            except ValueError as e:
                raise ApiError(400, "INVALID_CONTRACT", f"{name} 必须是整数") from e
        else:
            out[name] = raw
    for name, prop in props.items():                      # nullable-but-required fields may be omitted: absent means null
        if name not in out and name in contracts._load("extension.schema.json")["$defs"][f"{op}Request"].get("required", []):
            if "anyOf" in json.dumps(prop) and "null" in json.dumps(prop):
                out[name] = None
    try:
        contracts.check(f"{op}Request", out, contracts.EXT_ID)
    except contracts.ContractViolation as e:
        raise _violation(e) from e
    return out


def _page(req: dict) -> tuple[int, str | None]:
    p = req.get("page") or {"limit": 50}
    return min(int(p["limit"]), MAX_PAGE), p.get("cursor")


def _qhash(op: str, req: dict) -> str:
    return sha256_of({"op": op, "q": {k: v for k, v in req.items() if k != "page"}, "limit": (req.get("page") or {}).get("limit")})


def _enc(op: str, qh: str, key: Any) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": op, "h": qh, "k": contracts.to_wire(key)}, separators=(",", ":")).encode()).decode().rstrip("=")


def _dec(op: str, qh: str, cursor: str | None) -> Any:
    if cursor is None:
        return None
    try:
        d = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except ValueError as e:
        raise ApiError(400, "CURSOR_EXPIRED", "游标无效") from e
    if d.get("o") != op or d.get("h") != qh:
        raise ApiError(400, "CURSOR_EXPIRED", "游标与当前查询不一致，需重新查询")
    return d["k"]


def _token(op: str, qh: str, key: Any) -> dict:
    return {"next_cursor": None if key is None else _enc(op, qh, key), "snapshot_hash": sha256_of({"q": qh, "scope": "ext-c-v1"})}


def _ok(op: str, data: dict, *, release_id=None, decision_as_of=None, coverage=None, next_cursor=None, quality="valid") -> Any:
    return respond_ext(op, data, meta(release_id=release_id, decision_as_of=decision_as_of, coverage=coverage, next_cursor=next_cursor, quality_status=quality))


def _guard(fn):
    try:
        return fn()
    except SERVICE_ERRORS as e:
        raise _api(e) from e
    except Conflict as e:
        raise ApiError(409, "IDEMPOTENCY_CONFLICT", str(e)) from e


def _command(op: str, request: Request, body: dict, key: str, conn, handler, status: int = 200):
    pid = principal(request)
    if body.get("idempotency_key", key) != key:
        raise ApiError(400, "INVALID_CONTRACT", "body.idempotency_key 与 Idempotency-Key 不一致")
    req = {**body, "idempotency_key": key}
    try:
        contracts.check(f"{op}Request", req, contracts.EXT_ID)
    except contracts.ContractViolation as e:
        raise _violation(e) from e
    typed = _typed(req)
    res = _guard(lambda: PgCommandStore(conn).execute(
        principal_id=pid, operation_id=op, idempotency_key=key, request=req, response_contract_id=contracts.EXT_ID, response_schema_version=_ext_version(),
        handler=lambda c: handler(c, pid, typed, key), ttl=timedelta(days=7)))
    return respond_ext(op, res.response, meta(), status=status)


def _typed(x: Any) -> Any:
    """Wire strings that the services want as UUID/datetime stay strings except the well-known keys (converted by name)."""
    if isinstance(x, dict):
        out = {}
        for k, v in x.items():
            if k in UUID_KEYS and isinstance(v, str) and len(v) == 36:
                out[k] = UUID(v)
            elif k in ("scheduled_for", "effective_at", "knowledge_as_of", "decision_as_of", "as_of", "outcome_as_of") and isinstance(v, str):
                out[k] = _ts(v)
            elif k == "window" and isinstance(v, dict):
                out[k] = _win(v)
            else:
                out[k] = _typed(v)
        return out
    if isinstance(x, list):
        return [_typed(v) for v in x]
    return x


# ================================================================ PEX05 plans
@router.post("/propose-trade-plan", status_code=202)
def propose_trade_plan(request: Request, body: dict = Body(...), key: str = Header(..., alias="Idempotency-Key"), conn=Depends(conn_dep)):
    return _command("proposeTradePlan", request, body, key, conn, lambda c, pid, req, k: {"receipt": portfolio_service.propose_trade_plan(c, pid, req, k)}, 202)


@router.get("/query-plan-versions")
def query_plan_versions(request: Request, conn=Depends(conn_dep)):
    pid = principal(request)
    req = query_params("queryPlanVersions", request)
    limit, cur = _page(req)
    qh = _qhash("queryPlanVersions", req)
    after = _dec("queryPlanVersions", qh, cur)
    plans, origins, nxt = _guard(lambda: portfolio_service.query_plan_versions(conn, pid, UUID(req["plan_id"]), limit, int(after or 0)))
    return _ok("queryPlanVersions", {"plans": plans, "origins": origins, "page": _token("queryPlanVersions", qh, nxt)}, next_cursor=None if nxt is None else _enc("queryPlanVersions", qh, nxt))


@router.get("/list-trade-plans")
def list_trade_plans(request: Request, conn=Depends(conn_dep)):
    pid = principal(request)
    req = query_params("listTradePlans", request)
    limit, cur = _page(req)
    qh = _qhash("listTradePlans", req)
    after = _dec("listTradePlans", qh, cur)
    plans, origins, nxt = _guard(lambda: portfolio_service.list_trade_plans(conn, pid, UUID(req["instrument_id"]), UUID(req["account_id"]) if req.get("account_id") else None, limit,
                                                                           UUID(after) if after else None))
    return _ok("listTradePlans", {"plans": plans, "origins": origins, "page": _token("listTradePlans", qh, nxt)})


@router.get("/query-plan-proposal")
def query_plan_proposal(request: Request, conn=Depends(conn_dep)):
    pid = principal(request)
    req = query_params("queryPlanProposal", request)
    data = _guard(lambda: portfolio_service.query_plan_proposal(conn, pid, UUID(req["proposal_id"])))
    return _ok("queryPlanProposal", data)


# ================================================================ PEX07 / PEX08 / PEX09 intelligence
@router.get("/query-event-clusters")
def query_event_clusters(request: Request, conn=Depends(conn_dep)):
    principal(request)
    req = query_params("queryEventClusters", request)
    limit, cur = _page(req)
    qh = _qhash("queryEventClusters", req)
    after = _dec("queryEventClusters", qh, cur)
    typed = _typed(req)
    typed = {**typed, "release_id": UUID(req["release_id"]), "cluster_id": UUID(req["cluster_id"]) if req.get("cluster_id") else None}
    rows, nxt = _guard(lambda: calendar_service.query_event_clusters(conn, typed, limit, None if after is None else (_ts(after[0]), after[1])))
    return _ok("queryEventClusters", {"clusters": rows, "page": _token("queryEventClusters", qh, nxt)}, release_id=typed["release_id"])


@router.get("/query-calendar-events")
def query_calendar_events(request: Request, conn=Depends(conn_dep)):
    principal(request)
    req = query_params("queryCalendarEvents", request)
    limit, cur = _page(req)
    qh = _qhash("queryCalendarEvents", req)
    offset = int(_dec("queryCalendarEvents", qh, cur) or 0)
    t = _typed(req)
    rows, more = _guard(lambda: calendar_service.query_calendar_events(conn, UUID(req["snapshot_id"]), t["decision_as_of"], t["window"], limit, offset))
    return _ok("queryCalendarEvents", {"events": [{k: v for k, v in r.items() if k != "name"} for r in rows], "page": _token("queryCalendarEvents", qh, offset + limit if more else None)},
               decision_as_of=t["decision_as_of"])


@router.get("/query-seat-directory")
def query_seat_directory(request: Request, conn=Depends(conn_dep)):
    principal(request)
    req = query_params("querySeatDirectory", request)
    limit, cur = _page(req)
    qh = _qhash("querySeatDirectory", req)
    offset = int(_dec("querySeatDirectory", qh, cur) or 0)
    rows, more = _guard(lambda: calendar_service.query_seat_directory(conn, UUID(req["snapshot_id"]), req["name_query"], limit, offset))
    return _ok("querySeatDirectory", {"seats": rows, "page": _token("querySeatDirectory", qh, offset + limit if more else None)})


@router.get("/query-seat-profile")
def query_seat_profile(request: Request, conn=Depends(conn_dep)):
    pid = principal(request)
    req = _typed(query_params("querySeatProfile", request))
    data = _guard(lambda: calendar_service.query_seat_profile(conn, pid, req))
    return _ok("querySeatProfile", data, decision_as_of=req["decision_as_of"])


@router.get("/query-seat-reports")
def query_seat_reports(request: Request, conn=Depends(conn_dep)):
    principal(request)
    req = query_params("querySeatReports", request)
    limit, cur = _page(req)
    qh = _qhash("querySeatReports", req)
    after = _dec("querySeatReports", qh, cur)
    t = _typed(req)
    from datetime import date
    reports, nxt = _guard(lambda: calendar_service.query_seat_reports(conn, UUID(req["snapshot_id"]), date.fromisoformat(req["trade_date"]), t["decision_as_of"], limit, UUID(after) if after else None))
    return _ok("querySeatReports", {"reports": reports, "page": _token("querySeatReports", qh, nxt)}, decision_as_of=t["decision_as_of"])


@router.get("/query-community-opinions")
def query_community_opinions(request: Request, conn=Depends(conn_dep)):
    principal(request)
    req = query_params("queryCommunityOpinions", request)
    limit, cur = _page(req)
    qh = _qhash("queryCommunityOpinions", req)
    after = _dec("queryCommunityOpinions", qh, cur)
    t = {**_typed(req), "release_id": UUID(req["release_id"])}
    rows, nxt = _guard(lambda: calendar_service.query_community_opinions(conn, t, limit, None if after is None else (_ts(after[0]), after[1])))
    return _ok("queryCommunityOpinions", {"opinions": rows, "page": _token("queryCommunityOpinions", qh, nxt)}, release_id=t["release_id"])


# ================================================================ PEX12 portfolio
@router.get("/list-authorized-accounts")
def list_authorized_accounts(request: Request, conn=Depends(conn_dep)):
    pid = principal(request)
    req = query_params("listAuthorizedAccounts", request)
    limit, cur = _page(req)
    qh = _qhash("listAuthorizedAccounts", req)
    after = _dec("listAuthorizedAccounts", qh, cur)
    rows, nxt = _guard(lambda: portfolio_service.list_authorized_accounts(conn, pid, limit, UUID(after) if after else None))
    return _ok("listAuthorizedAccounts", {"accounts": rows, "page": _token("listAuthorizedAccounts", qh, nxt)})


@router.post("/import-broker-fills")
def import_broker_fills(request: Request, body: dict = Body(...), key: str = Header(..., alias="Idempotency-Key"), conn=Depends(conn_dep)):
    return _command("importBrokerFills", request, body, key, conn, lambda c, pid, req, k: portfolio_service.import_broker_fills(c, pid, req))


@router.get("/query-account-orders")
def query_account_orders(request: Request, conn=Depends(conn_dep)):
    pid = principal(request)
    req = query_params("queryAccountOrders", request)
    limit, cur = _page(req)
    qh = _qhash("queryAccountOrders", req)
    offset = int(_dec("queryAccountOrders", qh, cur) or 0)
    t = _typed(req)
    t["account_id"] = UUID(req["account_id"])
    rows, cov, nxt = _guard(lambda: portfolio_service.query_account_orders(conn, pid, t, limit, offset))
    return _ok("queryAccountOrders", {"orders": rows, "coverage": cov, "page": _token("queryAccountOrders", qh, nxt)}, coverage=cov, quality="partial")


@router.get("/query-account-performance")
def query_account_performance(request: Request, conn=Depends(conn_dep)):
    pid = principal(request)
    t = _typed(query_params("queryAccountPerformance", request))
    t["account_id"] = UUID(t["account_id"]) if isinstance(t["account_id"], str) else t["account_id"]
    data = _guard(lambda: {"performance": portfolio_service.query_account_performance(conn, pid, t)})
    perf = data["performance"]
    missing = any(m["value"] is None for m in perf["metrics"])
    return _ok("queryAccountPerformance", data, coverage=perf["coverage"], quality="partial" if missing else "valid")


@router.get("/query-opportunities")
def query_opportunities(request: Request, conn=Depends(conn_dep)):
    pid = principal(request)
    req = query_params("queryOpportunities", request)
    limit, cur = _page(req)
    qh = _qhash("queryOpportunities", req)
    offset = int(_dec("queryOpportunities", qh, cur) or 0)
    t = _typed(req)
    art, nxt = _guard(lambda: portfolio_service.query_opportunities(conn, pid, UUID(req["account_id"]) if req.get("account_id") else None,
                                                                    UUID(req["experiment_id"]) if req.get("experiment_id") else None, t["as_of"], limit, offset))
    return _ok("queryOpportunities", {"opportunity_artifact": art, "page": _token("queryOpportunities", qh, nxt)})


# ================================================================ PEX13 alerts
@router.post("/save-alert-rule")
def save_alert_rule(request: Request, body: dict = Body(...), key: str = Header(..., alias="Idempotency-Key"), conn=Depends(conn_dep)):
    return _command("saveAlertRule", request, body, key, conn, lambda c, pid, req, k: {"rule": alert_service.save_rule(c, pid, _raw_rule(body, req))})


def _raw_rule(body: dict, typed: dict) -> dict:
    return {"rule_id": typed["rule_id"], "expected_rule_version": typed["expected_rule_version"], "body": body["body"]}


@router.get("/list-user-alerts")
def list_user_alerts(request: Request, conn=Depends(conn_dep)):
    pid = principal(request)
    req = query_params("listUserAlerts", request)
    limit, cur = _page(req)
    qh = _qhash("listUserAlerts", req)
    after = _dec("listUserAlerts", qh, cur)
    t = _typed(req)
    rows, nxt = _guard(lambda: alert_service.list_alerts(conn, pid, t["window"], limit, None if after is None else (_ts(after[0]), UUID(after[1]))))
    return _ok("listUserAlerts", {"alerts": rows, "page": _token("listUserAlerts", qh, nxt)})


# ================================================================ PEX14 workspace
@router.post("/delete-workspace-item")
def delete_workspace_item(request: Request, body: dict = Body(...), key: str = Header(..., alias="Idempotency-Key"), conn=Depends(conn_dep)):
    return _command("deleteWorkspaceItem", request, body, key, conn, lambda c, pid, req, k: {"tombstone": workspace_service.delete_item(c, pid, req)})


@router.post("/schedule-workspace-export", status_code=202)
def schedule_workspace_export(request: Request, body: dict = Body(...), key: str = Header(..., alias="Idempotency-Key"), conn=Depends(conn_dep)):
    return _command("scheduleWorkspaceExport", request, body, key, conn, lambda c, pid, req, k: {"receipt": workspace_service.schedule_export(c, pid, _fix_refs(req))}, 202)


def _fix_refs(req: dict) -> dict:
    """Snapshot refs / workspace objects keep their id strings for hashing; services read ``snapshot_id`` as text or UUID alike."""
    return {**req, "input_snapshot_refs": [{**r, "snapshot_id": UUID(str(r["snapshot_id"]))} for r in req["input_snapshot_refs"]],
            "workspace_objects": [{**o, "object_id": UUID(str(o["object_id"]))} for o in req["workspace_objects"]]}


@router.get("/query-workspace-revision")
def query_workspace_revision(request: Request, conn=Depends(conn_dep)):
    pid = principal(request)
    req = query_params("queryWorkspaceRevision", request)
    data = _guard(lambda: workspace_service.query_revision(conn, pid, req["object_kind"], UUID(req["object_id"]), req["object_version"]))
    return _ok("queryWorkspaceRevision", data)


@router.get("/query-export-artifact")
def query_export_artifact(request: Request, conn=Depends(conn_dep)):
    pid = principal(request)
    req = query_params("queryExportArtifact", request)
    data = _guard(lambda: workspace_service.query_export(conn, pid, UUID(req["export_id"])))
    return _ok("queryExportArtifact", data)


@router.get("/query-workspace-index")
def query_workspace_index(request: Request, conn=Depends(conn_dep)):
    pid = principal(request)
    req = query_params("queryWorkspaceIndex", request)
    limit, cur = _page(req)
    qh = _qhash("queryWorkspaceIndex", req)
    offset = int(_dec("queryWorkspaceIndex", qh, cur) or 0)
    out, more = _guard(lambda: workspace_service.query_index(conn, pid, req["kind"], limit, offset))
    return _ok("queryWorkspaceIndex", {**out, "page": _token("queryWorkspaceIndex", qh, offset + limit if more else None)})


# ================================================================ PEX15 data health
@router.post("/run-registered-job", status_code=202)
def run_registered_job(request: Request, body: dict = Body(...), key: str = Header(..., alias="Idempotency-Key"), conn=Depends(conn_dep)):
    return _command("runRegisteredJob", request, body, key, conn, lambda c, pid, req, k: {"receipt": ops_service.run_registered_job(c, pid, req, k)}, 202)


@router.get("/query-dataset-status")
def query_dataset_status(request: Request, conn=Depends(conn_dep)):
    principal(request)
    req = query_params("queryDatasetStatus", request)
    limit, cur = _page(req)
    qh = _qhash("queryDatasetStatus", req)
    offset = int(_dec("queryDatasetStatus", qh, cur) or 0)
    rows, more = ops_service.query_dataset_status(conn, req["dataset_id"], limit, offset)
    return _ok("queryDatasetStatus", {"datasets": rows, "page": _token("queryDatasetStatus", qh, offset + limit if more else None)})


@router.get("/query-job-runs")
def query_job_runs(request: Request, conn=Depends(conn_dep)):
    pid = principal(request)
    req = query_params("queryJobRuns", request)
    limit, cur = _page(req)
    qh = _qhash("queryJobRuns", req)
    after = _dec("queryJobRuns", qh, cur)
    t = _typed(req)
    rows, nxt = ops_service.query_job_runs(conn, pid, UUID(req["job_id"]) if req.get("job_id") else None, t["window"], limit, None if after is None else (_ts(after[0]), UUID(after[1])))
    return _ok("queryJobRuns", {"jobs": rows, "page": _token("queryJobRuns", qh, nxt)})


# ================================================================ supplementary (non-contract) reads
def _raw(data: Any):
    from fastapi.responses import JSONResponse
    return JSONResponse(contracts.to_wire({"meta": meta(), "data": data}))


@router.get("/c/artifact")
def c_artifact(ref: str, request: Request, conn=Depends(conn_dep)):
    pid = principal(request)
    a = ops_service.get_artifact(conn, ref, pid)
    if a is None:
        raise ApiError(404, "NOT_FOUND", "产物不存在")
    return _raw(a)


@router.get("/c/alert-rules")
def c_alert_rules(request: Request, conn=Depends(conn_dep)):
    return _raw(alert_service.rules_wire(conn, principal(request)))


@router.get("/c/calendar-links")
def c_calendar_links(calendar_event_id: UUID, request: Request, decision_as_of: str | None = None, conn=Depends(conn_dep)):
    principal(request)
    at = _ts(decision_as_of) if decision_as_of else ops_service.now_utc()
    return _raw({"links": calendar_service.event_links(conn, calendar_event_id, at), "decision_as_of": at})


@router.get("/c/forward-books")
def c_forward_books(request: Request, conn=Depends(conn_dep)):
    return _raw(portfolio_service.forward_books_wire(conn, principal(request)))


@router.get("/c/job-definitions")
def c_job_definitions(request: Request, conn=Depends(conn_dep)):
    pid = principal(request)
    if not ops_service.is_ops(pid):
        raise ApiError(403, "FORBIDDEN", "无运行权限（ops:read）")
    return _raw({"definitions": ops_service.definitions_wire(conn)})


@router.get("/c/readmodel-context")
def c_readmodel_context(account_id: UUID, request: Request, conn=Depends(conn_dep)):
    pid = principal(request)
    return _raw({"contexts": _guard(lambda: portfolio_service.readmodel_contexts(conn, pid, account_id))})


@router.get("/c/calendar-names")
def c_calendar_names(ids: str, request: Request, conn=Depends(conn_dep)):
    """Names of calendar events (the typed CalendarRevision carries no name)."""
    principal(request)
    wanted = [UUID(x) for x in ids.split(",") if x][:200]
    rows = conn.execute("SELECT DISTINCT ON (calendar_event_id) calendar_event_id, name, category, region, source_id, notes FROM calendar.event_revision"
                        " WHERE calendar_event_id = ANY(%s) ORDER BY calendar_event_id, revision DESC", (wanted,)).fetchall()
    return _raw({"names": {str(r["calendar_event_id"]): {"name": r["name"], "category": r["category"], "region": r["region"], "source_id": r["source_id"]} for r in rows}})
