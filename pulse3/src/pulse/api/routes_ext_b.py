"""Extension group B routes: screening (PEX10), analogs + interval statistics (PEX04), tactic replay (PEX06), strategy registry (PEX11).

Contract operations (extension contract ``urn:pulse:contracts:ext:v1``, paths /v1/x/<kebab-op>): parseScreenText, runScreen,
saveScreen, queryScreenRun, listSavedScreens, queryAnalogCases, queryIntervalStatistics, scheduleTacticReplay,
registerStrategyDraft, queryStrategyVersion. GET queries take the ``<op>Request`` fields; object fields (page, window) are JSON
text or dotted keys (``page.limit=50``). POST commands take the request body plus an ``Idempotency-Key`` header (it must equal
``idempotency_key`` in the body) and are replay-safe through ops.command_receipt. Every response leaves through ``respond_ext``.
Supplementary reads (not contract operations, proposed additions): ``/v1/x/b/analog-key`` (the query key a client must send to
queryAnalogCases) and ``/v1/x/b/screen-run-csv`` (CSV of a whole screen run). Routers translate HTTP only.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Header, Request
from fastapi.responses import Response

from pulse import config, contracts
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres.commands import PgCommandStore
from pulse.api import deps
from pulse.api.auth import principal
from pulse.api.deps import conn_dep
from pulse.api.envelope import ApiError, meta, respond_ext
from pulse.application import bindings as B
from pulse.application import playbook_service as PS
from pulse.application import screen_service as S
from pulse.application.ports.errors import Conflict

router = APIRouter(prefix="/v1/x")
TTL = timedelta(days=7)


def lake() -> LakeStore:
    return LakeStore(config.load().lake_root)


def _ext_version() -> str:
    return contracts._load("extension.schema.json")["x-contract-version"]


# ---- request handling -----------------------------------------------------------------------------------------
def _coerce(raw: Any) -> Any:
    if isinstance(raw, str) and raw[:1] in ("{", "["):
        try:
            return json.loads(raw)
        except ValueError as e:
            raise ApiError(400, "INVALID_CONTRACT", "对象参数不是合法 JSON") from e
    return raw


def _num(raw: str) -> Any:
    return int(raw) if raw.lstrip("-").isdigit() else raw


def params_of(op: str, request: Request) -> dict[str, Any]:
    """Query string -> ``<op>Request`` (checked against the extension contract). Dotted keys build objects."""
    props = contracts._load("extension.schema.json")["$defs"][f"{op}Request"]["properties"]
    out: dict[str, Any] = {}
    unknown = []
    for k in request.query_params:
        head, _, tail = k.partition(".")
        if head not in props:
            unknown.append(k)
            continue
        vals = request.query_params.getlist(k)
        if not tail:
            raw = vals[0]
            out[head] = _coerce(raw) if raw != "" else None
            if out[head] is None:
                del out[head]
        else:
            obj = out.setdefault(head, {})
            if vals[0] != "":
                obj[tail] = _num(vals[0]) if tail == "limit" else vals[0]
    if unknown:
        raise ApiError(400, "INVALID_CONTRACT", "未知参数: " + ", ".join(sorted(unknown)),
                       field_errors=[{"path": "/" + u, "code": "unknown_parameter", "message": "不在请求合同中"} for u in unknown])
    for name, spec in props.items():
        if name in out and "Integer" in str(spec) and isinstance(out[name], str):
            out[name] = _num(out[name])
    try:
        contracts.check(f"{op}Request", out, contracts.EXT_ID)
    except contracts.ContractViolation as e:
        raise ApiError(400, "INVALID_CONTRACT", "请求参数不符合合同",
                       field_errors=[{"path": x.split(":", 1)[0] or "/", "code": "invalid", "message": x.split(":", 1)[-1].strip()[:200]}
                                     for x in e.errors[:10]]) from e
    return out


def _as_api(e: Exception) -> ApiError:
    return ApiError(e.status, e.code, e.message)  # type: ignore[attr-defined]


def _command(conn, op: str, request: Request, body: dict, header_key: str, handler) -> dict:
    """Authenticate, check the body against the contract, then run ``handler(pid)`` once per (principal, op, key)."""
    pid = principal(request)
    try:
        contracts.check(f"{op}Request", body, contracts.EXT_ID)
    except contracts.ContractViolation as e:
        raise ApiError(400, "INVALID_CONTRACT", "请求不符合合同",
                       field_errors=[{"path": x.split(":", 1)[0] or "/", "code": "invalid", "message": x.split(":", 1)[-1].strip()[:200]} for x in e.errors[:10]]) from e
    if header_key != body["idempotency_key"]:
        raise ApiError(400, "INVALID_CONTRACT", "Idempotency-Key 头必须等于请求体 idempotency_key")
    try:
        res = PgCommandStore(conn).execute(
            principal_id=pid, operation_id=op, idempotency_key=header_key, request=body, response_contract_id=contracts.EXT_ID,
            response_schema_version=_ext_version(), handler=lambda c: handler(pid), ttl=TTL)
    except Conflict as e:
        raise ApiError(409, "IDEMPOTENCY_CONFLICT", str(e)) from e
    except (S.ScreenError, PS.StrategyError) as e:
        raise _as_api(e) from e
    return res.response


# ---- PEX10 ------------------------------------------------------------------------------------------------------
@router.get("/parse-screen-text")
def parse_screen_text(request: Request):
    principal(request)
    q = params_of("parseScreenText", request)
    try:
        data = S.parse_text(q["text"], q["field_catalog_version"], q["locale"])
    except S.ScreenError as e:
        raise _as_api(e) from e
    return respond_ext("parseScreenText", data, meta())


def _run_pending(max_runs: int = 3) -> None:
    """Background executor for queued screen runs (the worker CLI does the same work out of process)."""
    with deps.pool().connection() as conn:
        for _ in range(max_runs):
            if S.execute_screen_run(conn, lake()) is None:
                break


@router.post("/run-screen")
def run_screen(request: Request, tasks: BackgroundTasks, body: dict = Body(...), idempotency_key: str = Header(..., alias="Idempotency-Key"),
               conn=Depends(conn_dep)):
    now = datetime.now(timezone.utc)
    data = _command(conn, "runScreen", request, body, idempotency_key, lambda pid: S.create_run(conn, pid, body, idempotency_key, now))
    tasks.add_task(_run_pending)
    return respond_ext("runScreen", data, meta())


@router.post("/save-screen")
def save_screen(request: Request, body: dict = Body(...), idempotency_key: str = Header(..., alias="Idempotency-Key"), conn=Depends(conn_dep)):
    now = datetime.now(timezone.utc)
    data = _command(conn, "saveScreen", request, body, idempotency_key, lambda pid: S.save_screen(conn, pid, body, now))
    return respond_ext("saveScreen", data, meta())


def _run_meta(run: dict) -> dict:
    doc = run["input_document"]
    cov = {"policy_version": "screen-run-v1", "expected_count": run["evaluated_count"], "observed_count": run["true_count"] or 0,
           "missing_count": run["unknown_count"], "unverifiable_reasons": [f"unknown_condition_rows:{run['unknown_count']}"] if run["unknown_count"] else []}
    warn = [{"code": "SCREEN_COUNTS", "message": f"真 {run['true_count']} · 未知 {run['unknown_count']} · 假 {run['false_count']} · 共评估 {run['evaluated_count']}",
             "affected_component": "screen_run", "evidence_ref": str(run["screen_run_id"])},
            {"code": "PRICE_BASIS", "message": S.PRICE_BASIS_NOTE, "affected_component": "research_bars", "evidence_ref": None}]
    return meta(decision_as_of=datetime.fromisoformat(doc["decision_as_of"].replace("Z", "+00:00")), availability_policy_version=B.AVAILABILITY_POLICY,
                input_snapshot_refs=doc["input_snapshot_refs"], quality_status="partial", coverage=cov, warnings=warn)


@router.get("/query-screen-run")
def query_screen_run(request: Request, conn=Depends(conn_dep)):
    pid = principal(request)
    q = params_of("queryScreenRun", request)
    try:
        run, rows, page = S.query_run(conn, pid, UUID(q["screen_run_id"]), q["page"]["limit"], q["page"].get("cursor"))
    except S.ScreenError as e:
        raise _as_api(e) from e
    data = {"rows": rows, "page": page, "capability_gaps": run["capability_gaps"]}
    m = _run_meta(run)
    m["next_cursor"] = page["next_cursor"]
    return respond_ext("queryScreenRun", data, m)


@router.get("/list-saved-screens")
def list_saved_screens(request: Request, conn=Depends(conn_dep)):
    pid = principal(request)
    q = params_of("listSavedScreens", request)
    try:
        items, page = S.list_saved(conn, pid, q["page"]["limit"], q["page"].get("cursor"))
    except S.ScreenError as e:
        raise _as_api(e) from e
    return respond_ext("listSavedScreens", {"screens": items, "page": page}, meta(next_cursor=page["next_cursor"]))


@router.get("/b/screen-run-csv")
def screen_run_csv(screen_run_id: UUID, request: Request, conn=Depends(conn_dep)):
    """CSV of the WHOLE frozen run (true and unknown rows, with the supporting values); owner only."""
    pid = principal(request)
    try:
        run = S.get_run(conn, pid, screen_run_id)
    except S.ScreenError as e:
        raise _as_api(e) from e
    if run["state"] != "ready":
        raise ApiError(409, "RESULT_NOT_READY", "筛选尚未完成")
    metas, _ = B.load_instrument_meta(conn, run["completed_at"])
    rows = conn.execute("SELECT * FROM research.screen_run_row WHERE screen_run_id=%s ORDER BY sort_position", (screen_run_id,)).fetchall()
    fields = list(dict.fromkeys(f["field"] for r in rows for f in r["features"]))
    labels = {f.id: f.label for f in S.K.FIELDS}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["序号", "代码", "名称", "条件结果"] + [f"{labels.get(f, f)}" for f in fields])
    for r in rows:
        m = metas.get(r["instrument_id"])
        cells = {f["field"]: f["value"] for f in r["features"]}
        w.writerow([r["sort_position"], m.code if m and m.code else "", m.display_name if m else "",
                    {"true": "满足", "unknown": "未知", "false": "不满足"}[r["condition_value"]]] + [("" if cells.get(f) is None else cells[f]) for f in fields])
    body = "﻿" + buf.getvalue()
    return Response(body.encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="screen-{str(screen_run_id)[:8]}.csv"'})


@router.get("/b/context")
def screen_context(request: Request, conn=Depends(conn_dep)):
    """Inputs the web forms need: the newest research snapshot ref, default decision_as_of, universe policies, playbook instances
    usable as screen signals, and the tactic-replay context. Supplementary read (proposed addition), nothing is written."""
    principal(request)
    man = B._ready_manifest(conn, B.RESEARCH_DATASET, None)
    if man is None:
        raise ApiError(409, "RESULT_NOT_READY", "还没有就绪的研究行情快照")
    ref = {"snapshot_id": str(man["snapshot_id"]), "dataset_id": man["dataset_id"], "manifest_version": man["manifest_version"], "content_hash": man["content_hash"]}
    sig = conn.execute(
        "SELECT i.instance_id, i.strategy_id, i.strategy_version, d.display_name, d.specification->'playbook'->>'id' AS playbook_id FROM strategy.instance i"
        " JOIN strategy.definition d USING (strategy_id, strategy_version) WHERE d.specification ? 'playbook' ORDER BY d.display_name").fetchall()
    return {"data": contracts.to_wire({
        "input_snapshot_refs": [ref], "decision_as_of": max(man["ready_at"], man["available_watermark"] or man["ready_at"]),
        "universe_policy_versions": list(S.UNIVERSES), "field_catalog_version": S.K.CATALOG_VERSION,
        "playbook_instances": [{"instance_id": r["instance_id"], "strategy_id": r["strategy_id"], "strategy_version": r["strategy_version"],
                                "display_name": r["display_name"], "playbook_id": r["playbook_id"]} for r in sig],
        "replay": PS.replay_context(conn)})}


@router.get("/b/instrument-labels")
def instrument_labels(request: Request, ids: str = "", codes: str = "", conn=Depends(conn_dep)):
    """id <-> code/name for result tables and pickers (the typed rows carry only instrument ids)."""
    principal(request)
    idl = [x for x in ids.split(",") if x][:300]
    codel = [x for x in codes.split(",") if x][:50]
    rows = conn.execute(
        "SELECT i.instrument_id, a.source_symbol AS code,"
        " (SELECT s.display_name FROM core.instrument_state s WHERE s.instrument_id=i.instrument_id ORDER BY s.effective_from DESC, s.state_version DESC LIMIT 1) AS name,"
        " (SELECT s.is_risk_warning FROM core.instrument_state s WHERE s.instrument_id=i.instrument_id ORDER BY s.effective_from DESC, s.state_version DESC LIMIT 1) AS st"
        " FROM core.instrument i JOIN core.instrument_alias a ON a.instrument_id=i.instrument_id AND a.source_id='legacy_warehouse'"
        " WHERE i.instrument_id = ANY(%s::uuid[]) OR a.source_symbol = ANY(%s)", (idl, codel)).fetchall()
    return {"data": [{"instrument_id": str(r["instrument_id"]), "code": r["code"], "name": r["name"], "is_risk_warning": r["st"]} for r in rows]}


# ---- PEX04 ------------------------------------------------------------------------------------------------------
@router.get("/b/analog-key")
def analog_key(instrument_id: UUID, release_id: UUID, request: Request, conn=Depends(conn_dep)):
    principal(request)
    try:
        rel = PS.release_row(conn, release_id)
        key, man = PS.analog_query_key(conn, instrument_id, rel)
    except PS.StrategyError as e:
        raise _as_api(e) from e
    return {"feature_key_hash": key, "selection_policy_versions": list(PS.AN.POLICIES), "default_horizon_sessions": 20,
            "decision_as_of": contracts.utc_z(rel["decision_as_of"])}


@router.get("/query-analog-cases")
def query_analog_cases(request: Request, conn=Depends(conn_dep)):
    principal(request)
    q = params_of("queryAnalogCases", request)
    try:
        data, ctx = PS.query_analog(conn, lake(), UUID(q["instrument_id"]), UUID(q["release_id"]), q["feature_key_hash"],
                                    q["selection_policy_version"], q["page"]["limit"], q["page"].get("cursor"))
    except PS.StrategyError as e:
        raise _as_api(e) from e
    rel = ctx["release"]
    m = meta(release_id=rel["release_id"], decision_as_of=rel["decision_as_of"], availability_policy_version=B.AVAILABILITY_POLICY,
             input_snapshot_refs=[{"snapshot_id": str(ctx["manifest"]["snapshot_id"]), "dataset_id": ctx["manifest"]["dataset_id"],
                                   "manifest_version": ctx["manifest"]["manifest_version"], "content_hash": ctx["manifest"]["content_hash"]}],
             quality_status="partial", next_cursor=data["page"]["next_cursor"],
             warnings=[{"code": "EMPIRICAL_DISTRIBUTION", "message": "这是历史相似情景的经验分布，不是校准概率；价格为前复权研究序列", "affected_component": "analog", "evidence_ref": None}])
    return respond_ext("queryAnalogCases", data, m)


@router.get("/query-interval-statistics")
def query_interval_statistics(request: Request, conn=Depends(conn_dep)):
    principal(request)
    q = params_of("queryIntervalStatistics", request)
    try:
        metrics, man = PS.interval_statistics(conn, lake(), UUID(q["instrument_id"]), UUID(q["snapshot_id"]), q["window"], q["adjustment_mode"],
                                              q["metric_policy_version"])
    except PS.StrategyError as e:
        raise _as_api(e) from e
    ref = {"snapshot_id": str(man["snapshot_id"]), "dataset_id": man["dataset_id"], "manifest_version": man["manifest_version"], "content_hash": man["content_hash"]}
    return respond_ext("queryIntervalStatistics", {"metrics": metrics, "exploratory": True, "input_snapshot": ref},
                       meta(input_snapshot_refs=[ref], quality_status="partial",
                            warnings=[{"code": "EXPLORATORY", "message": "区间由用户事后选择：仅作描述，不计入任何前向战绩", "affected_component": "interval", "evidence_ref": None}]))


# ---- PEX06 ------------------------------------------------------------------------------------------------------
@router.post("/schedule-tactic-replay")
def schedule_tactic_replay(request: Request, body: dict = Body(...), idempotency_key: str = Header(..., alias="Idempotency-Key"), conn=Depends(conn_dep)):
    now = datetime.now(timezone.utc)
    data = _command(conn, "scheduleTacticReplay", request, body, idempotency_key,
                    lambda pid: PS.schedule_tactic_replay(conn, pid, body, idempotency_key, now))
    return respond_ext("scheduleTacticReplay", data, meta())


# ---- PEX11 ------------------------------------------------------------------------------------------------------
@router.post("/register-strategy-draft")
def register_strategy_draft(request: Request, body: dict = Body(...), idempotency_key: str = Header(..., alias="Idempotency-Key"), conn=Depends(conn_dep)):
    now = datetime.now(timezone.utc)
    warnings: list[dict] = []

    def handler(pid: UUID) -> dict:
        data, w = PS.register_strategy_draft(conn, pid, body, now)
        warnings.extend(w)
        return data

    data = _command(conn, "registerStrategyDraft", request, body, idempotency_key, handler)
    return respond_ext("registerStrategyDraft", data, meta(warnings=warnings))


@router.get("/query-strategy-version")
def query_strategy_version(request: Request, conn=Depends(conn_dep)):
    pid = principal(request)
    q = params_of("queryStrategyVersion", request)
    try:
        data = PS.query_strategy_version(conn, pid, UUID(q["strategy_id"]), q["strategy_version"])
    except PS.StrategyError as e:
        raise _as_api(e) from e
    return respond_ext("queryStrategyVersion", data, meta())
