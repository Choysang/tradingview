"""Extension group A routes: market today, ranking history / track record, sector details, sentiment, SSE transport.

Contract operations (extension contract ``urn:pulse:contracts:ext:v1``, paths /v1/x/<kebab-op>): queryMarketToday (PEX01),
queryRankingHistory + queryRankingTrackRecord (PEX02), querySectorDetails (PEX03), queryMarketSentiment (PEX09),
streamRegisteredUpdates (PEX16). Query parameters are the ``<op>Request`` fields; object / array fields are JSON text.
Every response leaves through ``respond_ext`` (validated against ``<op>Response``).
Supplementary reads (registered in migration 0020, proposed contract additions): ``/v1/x/a/artifact`` resolves the
``feature:`` / ``outcome:`` references that the typed responses carry; ``/v1/x/a/market-tape`` and
``/v1/x/a/market-tape/stream`` serve the event tape (a separate log from the strict 3.0.0 update stream, because a new
event type must not be sent through ``streamRegisteredUpdates`` before it is registered, card PEX16).
Routers only translate HTTP <-> ``market_today_service``; no market logic lives here.
Streams: ``id`` is the opaque outbox cursor (epoch + position); ``Last-Event-ID`` resumes; a cursor that is missing, expired
or from another epoch yields one ``reset`` event (``resync_required: true``) and the stream continues from the head.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from pulse import config, contracts
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import decision_repo, market_repo
from pulse.adapters.postgres.outbox import PgOutbox, decode_cursor, encode_cursor, stream_name_for, to_update_page
from pulse.api import deps
from pulse.api.auth import principal
from pulse.api.deps import conn_dep
from pulse.api.envelope import ApiError, meta, respond_ext
from pulse.application import bindings as B
from pulse.application import market_today_service as MT
from pulse.application.ports.delivery import PollResult
from pulse.application.ports.errors import CursorInvalid
from pulse.domain.strategy.canonical import canonical_hash

router = APIRouter(prefix="/v1/x")
PUBLIC_STATES = ("READY", "SUPERSEDED")
UPDATE_EVENT_TYPES = ("decision.published", "account.updated", "source.health.changed", "actionability.ready")
DISPLAY_LABELS = ("SHORT", "SWING", "LONG")
TAPE_SLOT = "main"
KEEPALIVE_SECONDS = 15.0


def lake() -> LakeStore:
    return LakeStore(config.load().lake_root)


# --------------------------------------------------------------------------------------------------------
# request handling
# --------------------------------------------------------------------------------------------------------
def _coerce(raw: Any) -> Any:
    if isinstance(raw, list):
        return [_coerce(x) for x in raw]
    if isinstance(raw, str) and raw[:1] in ("{", "["):
        try:
            return json.loads(raw)
        except ValueError as e:
            raise ApiError(400, "INVALID_CONTRACT", "对象参数不是合法 JSON") from e
    return raw


def params_of(op: str, request: Request) -> dict[str, Any]:
    """Query string -> ``<op>Request`` (checked against the extension contract)."""
    props = contracts._load("extension.schema.json")["$defs"][f"{op}Request"]["properties"]
    q = request.query_params
    unknown = sorted(set(q) - set(props))
    if unknown:
        raise ApiError(400, "INVALID_CONTRACT", "未知参数: " + ", ".join(unknown),
                       field_errors=[{"path": "/" + u, "code": "unknown_parameter", "message": "不在请求合同中"} for u in unknown])
    out: dict[str, Any] = {}
    for name in props:
        vals = q.getlist(name)
        if not vals or vals == [""]:
            continue
        if len(vals) > 1 or name.endswith("_ids"):             # repeated parameter or one JSON array: both give one flat list
            out[name] = [x for v in vals for x in (lambda c: c if isinstance(c, list) else [c])(_coerce(v))]
        else:
            out[name] = _coerce(vals[0])
    try:
        contracts.check(f"{op}Request", out, contracts.EXT_ID)
    except contracts.ContractViolation as e:
        raise ApiError(400, "INVALID_CONTRACT", "请求参数不符合合同",
                       field_errors=[{"path": x.split(":", 1)[0] or "/", "code": "invalid", "message": x.split(":", 1)[-1].strip()[:200]}
                                     for x in e.errors[:10]]) from e
    return out


def public_release(conn, release_id: uuid.UUID | str) -> dict:
    row = MT.release_row(conn, uuid.UUID(str(release_id)))
    if row is None or row["release_state"] not in PUBLIC_STATES:
        raise ApiError(404, "NOT_FOUND", "发布不存在或尚未公开")
    return row


def release_meta(conn, row: dict, *, quality: str = "valid", coverage: dict | None = None, warnings: list | None = None,
                 refs: list | None = None, next_cursor: str | None = None, freshness: str | None = None) -> dict:
    now = datetime.now(timezone.utc)
    expired = bool(conn.execute("SELECT 1 FROM decision.release_component WHERE release_id=%s AND component_state='READY'"
                                " AND valid_until IS NOT NULL AND valid_until < %s LIMIT 1", (row["release_id"], now)).fetchone())
    fresh = freshness or MT.freshness_of(row["release_state"], row["decision_as_of"], now, expired_components=expired)
    warns = list(warnings or [])
    if fresh == "stale":
        warns.append({"code": "STALE_BASELINE", "message": "该发布包不是当前分析：已被取代、已过期或数据过旧，页面不得当作新分析展示",
                      "affected_component": "release", "evidence_ref": f"release:{row['release_id']}"})
    return meta(release_id=row["release_id"], decision_as_of=row["decision_as_of"], availability_policy_version=row["availability_policy_version"],
                input_snapshot_refs=refs or [], quality_status=quality, freshness_state=fresh, coverage=coverage, warnings=warns,
                next_cursor=next_cursor)


def snap_ref(man: dict) -> dict:
    return {"snapshot_id": man["snapshot_id"], "dataset_id": man["dataset_id"], "manifest_version": man["manifest_version"],
            "content_hash": man["content_hash"]}


def release_wire(conn, row: dict) -> dict:
    from pulse.adapters.postgres.release import PgAnalysisRelease
    from pulse.api.routes_ops import release_wire as rw

    return rw(PgAnalysisRelease(conn).get_release(row["release_id"]))


# --------------------------------------------------------------------------------------------------------
# PEX01 queryMarketToday
# --------------------------------------------------------------------------------------------------------
@router.get("/query-market-today")
def query_market_today(request: Request, conn=Depends(conn_dep)):
    p = params_of("queryMarketToday", request)
    row = public_release(conn, p["release_id"])
    account_id = p.get("account_id")
    pid = principal(request) if account_id else None        # an account overlay requires an authenticated principal
    lk = lake()
    got = MT.market_today(conn, lk, row["release_id"])
    overlays: list[dict] = []
    q = market_repo.latest_ready(conn, "quotes_l1")
    if q is not None:
        overlays.append({"kind": "quote", "as_of": q["ready_at"],
                         "artifact": {"ref": f"snapshot:quotes_l1:{q['manifest_version']}", "content_hash": q["content_hash"],
                                      "contract_id": q["contract_id"], "schema_version": q["schema_version"]},
                         "scope": {"scope_kind": "market", "scope_key": MT.MARKET_SCOPE[1], "membership_snapshot_id": None}})
    if account_id:
        head = conn.execute("SELECT current_sequence FROM execution.account_head WHERE account_id=%s", (account_id,)).fetchone()
        if head is None:
            raise ApiError(404, "NOT_FOUND", "账户不存在")
        overlays.append({"kind": "account", "as_of": datetime.now(timezone.utc),
                         "artifact": {"ref": f"account:{account_id}:v{head['current_sequence']}",
                                      "content_hash": canonical_hash({"account_id": str(account_id), "account_version": head["current_sequence"],
                                                                      "principal": str(pid)}),
                                      "contract_id": "AccountProjection", "schema_version": contracts.CONTRACT_VERSION},
                         "scope": {"scope_kind": "account", "scope_key": str(account_id), "membership_snapshot_id": None}})
    sent = MT.sentiment_for(conn, lk, row["release_id"], *MT.MARKET_SCOPE)
    warnings = []
    if got["snapshot"] is None:
        warnings.append({"code": "COMPONENT_NOT_IN_RELEASE", "message": "该发布包没有市场状态组件，不显示市场状态",
                         "affected_component": MT.COMPONENT_KEY, "evidence_ref": None})
    m = release_meta(conn, row, quality=got["quality"], coverage=None if sent is None else sent["coverage"], warnings=warnings,
                     refs=[] if got["snapshot"] is None else [snap_ref(got["snapshot"])])
    data = {"release": release_wire(conn, row), "state": got["state"], "overlays": overlays, "capability_gaps": got["capability_gaps"]}
    headers = ({"Cache-Control": "private, no-store", "Vary": "Cookie"} if account_id else {"Cache-Control": "public, max-age=10", "Vary": "Cookie"})
    resp = respond_ext("queryMarketToday", data, m)
    resp.headers.update(headers)
    return resp


# --------------------------------------------------------------------------------------------------------
# PEX09 queryMarketSentiment
# --------------------------------------------------------------------------------------------------------
@router.get("/query-market-sentiment")
def query_market_sentiment(request: Request, conn=Depends(conn_dep)):
    p = params_of("queryMarketSentiment", request)
    row = public_release(conn, p["release_id"])
    sc = p["scope"]
    if sc["scope_kind"] not in ("market", "sector"):
        if sc["scope_kind"] in ("watchlist", "account"):
            principal(request)
        raise ApiError(422, "UNSUPPORTED_CONTEXT_SHAPE", "情绪面板仅支持 market 与 sector 范围")
    key = MT.MARKET_SCOPE[1] if sc["scope_kind"] == "market" and sc["scope_key"] in ("main-board", "market") else sc["scope_key"]
    got = MT.sentiment_for(conn, lake(), row["release_id"], sc["scope_kind"], key)
    if got is None:
        raise ApiError(404, "NOT_FOUND", "该发布包没有这个范围的情绪面板")
    m = release_meta(conn, row, quality="partial" if any(f["result"]["quality_status"] != "valid" for f in got["features"]) else "valid",
                     coverage=got["coverage"], refs=[snap_ref(got["snapshot"])])
    return respond_ext("queryMarketSentiment", {"features": got["features"], "coverage": got["coverage"], "artifact": got["artifact"]}, m)


# --------------------------------------------------------------------------------------------------------
# PEX03 querySectorDetails
# --------------------------------------------------------------------------------------------------------
@router.get("/query-sector-details")
def query_sector_details(request: Request, conn=Depends(conn_dep)):
    p = params_of("querySectorDetails", request)
    row = public_release(conn, p["release_id"])
    node = p["sector"]
    tax, ver, nid = node["taxonomy_id"], node["taxonomy_version"], node["node_id"]
    if not conn.execute("SELECT 1 FROM core.taxonomy_node WHERE taxonomy_id=%s AND taxonomy_version=%s AND node_id=%s", (tax, ver, nid)).fetchone():
        raise ApiError(404, "NOT_FOUND", "板块不存在于该体系版本")
    lk = lake()
    members, man = MT.membership_of(conn, lk, row["release_id"], tax, nid)
    horizon, target = p["horizon_id"], p["forecast_target"]
    run = conn.execute("SELECT ranking_id, ranking_policy_version, membership_snapshot_id FROM decision.ranking_run WHERE release_id=%s"
                       " AND ranking_scope='sector' AND scope_key=%s AND horizon_id=%s AND forecast_target=%s",
                       (row["release_id"], f"{tax}/{nid}/{horizon}", horizon, target)).fetchone()
    reasons: list[str] = []
    if run is not None:
        ranking = decision_repo.load_ranking_wire(conn, run["ranking_id"])
    else:
        skipped = (MT.component_summary(conn, lk, row["release_id"], f"ranking:sector:{tax}:{horizon}") or {}).get("skipped", {})
        reasons.append("SECTOR_RANKING_NOT_PRODUCED:" + str(skipped.get(nid, "no ranking run for this node, horizon and target")).replace(" ", "_")[:80])
        ranking = contracts.to_wire({
            "ranking_id": MT.aid("empty-sector-ranking", row["release_id"], tax, nid, horizon, target), "release_id": row["release_id"],
            "ranking_scope": "sector", "scope_key": f"{tax}/{nid}/{horizon}", "horizon_id": horizon, "forecast_target": target,
            "ranking_policy_version": "not-ranked:insufficient-included-members", "taxonomy_node": node,
            "membership_snapshot_id": None if man is None else man["snapshot_id"], "entries": []})
        if man is None:
            raise ApiError(404, "NOT_FOUND", "该发布包没有板块成员快照")
    data = {"sector": node, "members": [{"instrument_id": i, "role_feature": None} for i in members], "ranking": ranking,
            "candidate_pools": MT.pool_for_node(conn, row, tax, ver, nid), "related_event_artifacts": []}
    cov = {"policy_version": "sector-details-v1", "expected_count": len(members), "observed_count": len(ranking["entries"]),
           "missing_count": max(len(members) - len(ranking["entries"]), 0), "unverifiable_reasons": reasons}
    warns = [{"code": "ROLE_NOT_AVAILABLE", "message": "龙头/中军角色没有算法输出，保持未知", "affected_component": "members", "evidence_ref": None}]
    m = release_meta(conn, row, quality="partial" if reasons else "valid", coverage=cov, warnings=warns,
                     refs=[] if man is None else [snap_ref(man)])
    return respond_ext("querySectorDetails", data, m)


# --------------------------------------------------------------------------------------------------------
# PEX02 queryRankingHistory / queryRankingTrackRecord
# --------------------------------------------------------------------------------------------------------
@router.get("/query-ranking-history")
def query_ranking_history(request: Request, conn=Depends(conn_dep)):
    p = params_of("queryRankingHistory", request)
    sc, win, page = p["scope"], p["window"], p["page"]
    if p["horizon_id"] in DISPLAY_LABELS:
        raise ApiError(400, "INVALID_CONTRACT", "SHORT/SWING/LONG 只是展示标签，请用 horizon_id（如 5td）和 forecast_target 过滤",
                       field_errors=[{"path": "/horizon_id", "code": "display_label", "message": "展示标签不是过滤条件"}])
    start, end = (datetime.fromisoformat(win[k].replace("Z", "+00:00")) for k in ("start", "end"))
    if end <= start:
        raise ApiError(400, "INVALID_CONTRACT", "window.end 必须晚于 window.start")
    if not 1 <= page["limit"] <= 200:
        raise ApiError(400, "INVALID_CONTRACT", "page.limit 必须在 1..200")
    if sc["scope_kind"] in ("watchlist", "account"):
        principal(request)
        data = {"entries": [], "page": {"next_cursor": None, "snapshot_hash": canonical_hash(["private-empty", sc["scope_key"]])}}
        return respond_ext("queryRankingHistory", data, meta())
    if sc["scope_kind"] not in ("market", "sector"):
        raise ApiError(422, "UNSUPPORTED_CONTEXT_SHAPE", "榜单历史仅支持 market 与 sector 范围")
    try:
        data = MT.ranking_history(conn, lake(), scope_kind=sc["scope_kind"], scope_key=sc["scope_key"], start=start, end=end,
                                  horizon_id=p["horizon_id"], target=p["forecast_target"], limit=page["limit"], cursor=page.get("cursor"))
    except MT.CursorExpired as e:
        raise ApiError(409, "CURSOR_EXPIRED", "分页游标已失效，请重新查询") from e
    return respond_ext("queryRankingHistory", data, meta(next_cursor=data["page"]["next_cursor"]))


@router.get("/query-ranking-track-record")
def query_ranking_track_record(request: Request, conn=Depends(conn_dep)):
    p = params_of("queryRankingTrackRecord", request)
    ids = list(dict.fromkeys(uuid.UUID(i) for i in p["ranking_ids"]))
    if not ids or len(ids) > 200:
        raise ApiError(400, "INVALID_CONTRACT", "ranking_ids 需要 1..200 个榜单")
    if uuid.UUID(p["evaluation_context_id"]) != MT.OUTCOME_CONTEXT_ID:
        raise ApiError(422, "UNSUPPORTED_CONTEXT_SHAPE", "不支持这个评价方法；当前只有前向结果方法 " + MT.outcomes.OUTCOME_VERSION)
    runs = conn.execute("SELECT ranking_id, ranking_scope FROM decision.ranking_run WHERE ranking_id = ANY(%s)", (ids,)).fetchall()
    if len(runs) != len(ids):
        raise ApiError(404, "NOT_FOUND", "有榜单不存在")
    if any(r["ranking_scope"] in ("watchlist", "account") for r in runs):
        principal(request)                                   # a private scope needs its own authorization
    as_of = datetime.fromisoformat(p["outcome_as_of"].replace("Z", "+00:00"))
    refs = MT.latest_outcome_refs(conn, lake(), ids, outcome_as_of=as_of)
    outcomes = [refs[i] for i in ids if i in refs]
    pending = len(ids) - len(outcomes)
    cov = {"policy_version": MT.outcomes.OUTCOME_VERSION, "expected_count": len(ids), "observed_count": len(outcomes), "missing_count": pending,
           "unverifiable_reasons": ["pending:horizon_not_reached_or_not_evaluated"] if pending else []}
    return respond_ext("queryRankingTrackRecord", {"outcomes": outcomes, "pending_count": pending},
                       meta(coverage=cov, quality_status="valid" if not pending else "partial"))


# --------------------------------------------------------------------------------------------------------
# artifact resolver
# --------------------------------------------------------------------------------------------------------
@router.get("/a/artifact")
def read_artifact(ref: str, conn=Depends(conn_dep)):
    try:
        art = MT.resolve_artifact(conn, lake(), ref)
    except (LookupError, ValueError) as e:
        raise ApiError(404, "NOT_FOUND", "产物不存在") from e
    body = contracts.to_wire({"meta": meta(), "data": art})
    contracts.check("ResponseMeta", body["meta"])
    return JSONResponse(body, headers={"Cache-Control": "public, max-age=3600, immutable", "ETag": '"' + art["content_hash"] + '"'})


# --------------------------------------------------------------------------------------------------------
# SSE
# --------------------------------------------------------------------------------------------------------
def sse(data: Any, *, event: str | None = None, id_: str | None = None) -> str:
    head = ([f"id: {id_}"] if id_ else []) + ([f"event: {event}"] if event else [])
    return "\n".join(head + ["data: " + json.dumps(contracts.to_wire(data), ensure_ascii=False, separators=(",", ":"))]) + "\n\n"


def _settings() -> tuple[float, float]:
    return float(os.environ.get("PULSE_SSE_MAX_SECONDS", "300")), float(os.environ.get("PULSE_SSE_POLL_SECONDS", "1.0"))


def _cursor_arg(request: Request, query_cursor: str | None) -> str | None:
    return request.headers.get("last-event-id") or query_cursor or None       # a reconnecting browser's header wins


def stream_events(stream_name: str, cursor: str | None, render, *, event: str) -> Iterator[str]:
    """Poll the shared outbox log and write SSE frames. ``render(delivered) -> payload | None`` (None = not for this stream)."""
    max_seconds, poll_seconds = _settings()
    start = last_ping = time.monotonic()
    yield "retry: 3000\n\n"
    cur, seen = cursor, {}
    while time.monotonic() - start < max_seconds:
        with deps.pool().connection() as c:
            res = PgOutbox(c).poll(stream_name, cur)
        if res.reset_required:
            reason = "NO_CURSOR" if cur is None else "CURSOR_EXPIRED"
            yield f": reason={reason}\n"                    # strict data object below; the reason travels as an SSE comment
            yield sse({"events": [], "resync_required": True}, event="reset", id_=res.resume_cursor)
            cur = res.resume_cursor
        else:
            for e in res.events:
                payload = render(e, res)
                prior = seen.setdefault(str(e.event_id), e.payload_hash)
                if prior != e.payload_hash:
                    yield sse({"error": "EVENT_ID_PAYLOAD_CONFLICT", "event_id": str(e.event_id)}, event="error")
                    return
                if payload is not None:
                    yield sse(payload, event=event, id_=encode_cursor(stream_name, res.stream_epoch, e.stream_sequence))
            cur = res.resume_cursor
            if res.events:
                last_ping = time.monotonic()
        if time.monotonic() - last_ping >= KEEPALIVE_SECONDS:
            yield ": keepalive\n\n"
            last_ping = time.monotonic()
        time.sleep(poll_seconds)


def _sse_response(gen: Iterator[str]) -> StreamingResponse:
    return StreamingResponse(gen, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


def _preflight(stream_name: str, cursor: str | None) -> None:
    if cursor:
        try:
            scope, _, _ = decode_cursor(cursor)
        except CursorInvalid as e:
            raise ApiError(400, "CURSOR_INVALID", str(e)) from e
        if scope != stream_name:
            raise ApiError(400, "CURSOR_INVALID", "游标属于另一个订阅范围")


@router.get("/stream-registered-updates")
def stream_registered_updates(request: Request):
    """PEX16: the pollUpdates log as Server-Sent Events. Phase 1 carries only the four registered 3.0.0 event types."""
    p = params_of("streamRegisteredUpdates", request)
    sc = p["scope"]
    versions = p["accepted_schema_versions"]
    if not any(str(v).split(".")[0] == contracts.CONTRACT_VERSION.split(".")[0] for v in versions):
        raise ApiError(406, "SCHEMA_NOT_ACCEPTED", "客户端不接受 3.x 事件合同；不同 major 不混发")
    if sc["scope_kind"] != "market":
        if sc["scope_kind"] in ("account", "watchlist"):
            principal(request)
        raise ApiError(422, "UNSUPPORTED_CONTEXT_SHAPE", "第一阶段仅支持 market 范围（发布槽）的更新流")
    stream = stream_name_for("release_slot", sc["scope_key"])
    cursor = _cursor_arg(request, p.get("cursor"))
    _preflight(stream, cursor)

    def render(e, res: PollResult):
        if e.event_type not in UPDATE_EVENT_TYPES or e.schema_version != contracts.CONTRACT_VERSION:
            return None                                            # unregistered types never ride this stream
        note = to_update_page(PollResult(res.stream_name, res.stream_epoch, (e,), res.resume_cursor, False))["events"][0]
        env = {"cursor": encode_cursor(stream, res.stream_epoch, e.stream_sequence), "scope": sc, "notification": note,
               "release_id": note["payload"].get("release_id")}
        contracts.check("StreamEnvelope", contracts.to_wire(env), contracts.EXT_ID)
        return env

    return _sse_response(stream_events(stream, cursor, render, event="update"))


# --------------------------------------------------------------------------------------------------------
# event tape (separate log; payload contract pulse.market_tape)
# --------------------------------------------------------------------------------------------------------
def _tape_stream() -> str:
    return stream_name_for(MT.TAPE_AGGREGATE, TAPE_SLOT)


def _tape_item(e) -> dict:
    MT.validate_tape_payload(dict(e.payload))
    return {"event_id": str(e.event_id), "recorded_at": contracts.utc_z(e.recorded_at), **dict(e.payload)}


@router.get("/a/market-tape")
def market_tape(cursor: str | None = None, recent: int = 100, conn=Depends(conn_dep)):
    """Poll: with a cursor, events after it; without one, the newest ``recent`` events and the head cursor (reset_required: true)."""
    stream = _tape_stream()
    _preflight(stream, cursor)
    ob = PgOutbox(conn)
    if cursor is None:
        recent = max(1, min(recent, 300))
        rows = conn.execute(
            "SELECT o.event_id, o.event_type, o.schema_version, o.aggregate_type, o.aggregate_id, o.aggregate_version, o.event_ordinal,"
            " o.occurred_at, o.recorded_at, o.payload, o.payload_hash, p.stream_name, p.stream_epoch, p.stream_sequence"
            " FROM ops.outbox_position p JOIN ops.outbox o ON o.event_id=p.event_id WHERE p.stream_name=%s"
            " AND p.stream_epoch=(SELECT stream_epoch FROM ops.outbox_stream WHERE stream_name=%s)"
            " ORDER BY p.stream_sequence DESC LIMIT %s", (stream, stream, recent)).fetchall()
        from pulse.application.ports.delivery import DeliveredEvent
        events = [DeliveredEvent(**r) for r in reversed(rows)]
        head = ob.poll(stream, None)
        out = {"events": [_tape_item(e) for e in events], "resume_cursor": head.resume_cursor, "reset_required": True, "stream_epoch": head.stream_epoch}
    else:
        res = ob.poll(stream, cursor)
        out = {"events": [_tape_item(e) for e in res.events], "resume_cursor": res.resume_cursor, "reset_required": res.reset_required,
               "stream_epoch": res.stream_epoch}
    m = meta()
    contracts.check("ResponseMeta", m)
    return JSONResponse({"meta": m, "data": out})


@router.get("/a/market-tape/stream")
def market_tape_stream(request: Request, cursor: str | None = None):
    stream = _tape_stream()
    cur = _cursor_arg(request, cursor)
    _preflight(stream, cur)

    def render(e, res):
        return _tape_item(e) if e.event_type == "market.tape.event" else None

    return _sse_response(stream_events(stream, cur, render, event="tape"))
