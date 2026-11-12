"""Stock workbench reads: workspace, quote, bars, evidence, community, fund flow, seats, data status, app config.

Why: FINAL_SYSTEM_SPEC §4 (point-in-time market data), §5 (evidence/intelligence), §7 (data health), §11-12 (one
envelope, pinned releases). Every response goes through ``respond`` and is checked against ``<operationId>Response``.
Invariants:
* the frozen workspace comes from ``decision.workspace_projection`` of the pinned release; the live quote and the
  actionability id are separate overlays and never rewrite ``projection_hash``;
* a quote is never replaced by a daily close; an old quote is ``stale`` (derived at request time), not edited;
* bars are served from one READY snapshot, filtered by ``available_at <= decision_as_of``; adjusted modes and
  timeframes are checked against the snapshot's declared capability and refused with 409 when unavailable;
  weekly..yearly bars are aggregated by ``pulse.domain.market`` over an observed calendar and are not final until
  that calendar proves the period ended;
* intelligence lists with no rows return an empty contract-valid list, ``quality_status=partial`` and a coverage
  record that shows the gap; nothing is fabricated.
Not covered: a persisted ``bars.window`` completeness artifact (``window_complete`` is derived from declared dataset
coverage), an exchange holiday calendar, ``policy_usable_at`` for any availability policy other than the observed one,
writing ``market.quote_projection`` (read-only API role).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import uuid
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from pulse import config, contracts
from pulse.adapters.postgres import market_repo
from pulse.adapters.postgres.instrument_repo import get_instrument_wire
from pulse.adapters.postgres.release import PgAnalysisRelease
from pulse.api.auth import principal
from pulse.api.deps import conn_dep
from pulse.api.envelope import ApiError, meta, respond
from pulse.application.ports.errors import NotFound
from pulse.domain.market import builders
from pulse.domain.market.bars import SESSION_SCOPE_REGULAR, Bar, aggregate
from pulse.domain.market.calendar import CST, TradingCalendar
from pulse.domain.market.capability import (ADJUSTED_PRICES, DAILY_BARS, MINUTE_BARS_TRUE_OHLC, RAW_PRICES, Capability,
                                            Requirement, evaluate)
from pulse.domain.market.units import sha256_json

router = APIRouter(prefix="/v1")
config_router = APIRouter()

PUBLIC_STATES = ("READY", "SUPERSEDED")
VIEW_KINDS = ("public_research", "account_holding")
ADJUSTMENT_MODES = ("raw", "forward_adjusted", "backward_adjusted")
QUOTE_POLICY_VERSION = "quote-observation-latest-v1"
OBSERVED_AVAILABILITY_POLICY = "observed-actual-available-at-v1"
RECONSTRUCTED_AVAILABILITY_POLICY = "reconstructed-session-close-plus-1h-v1"
REGISTERED_AVAILABILITY_POLICIES = (OBSERVED_AVAILABILITY_POLICY, RECONSTRUCTED_AVAILABILITY_POLICY)
DEFAULT_STALE_AFTER_SECONDS = 120
PAGE = 50
TIMEFRAME_RE = re.compile(r"^[1-9][0-9]*(s|m|h|d|w|mo|q|y)$")
AGGREGATED = ("1w", "1mo", "1q", "1y")
ISSUER_ALIAS_SOURCES = ("cninfo_announcements", "legacy_warehouse")
_QUOTE_NS = uuid.UUID("5d0e1a52-3b6c-5f0e-8a7e-2c4f6e1a9d33")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _bad_request(field: str, code: str, message: str) -> ApiError:
    return ApiError(400, "BAD_REQUEST", "请求参数无效", field_errors=[{"path": f"/{field}", "code": code, "message": message}])


def _cov(policy: str, expected: int | None, observed: int, reasons: list[str] | None = None) -> dict:
    missing = None if expected is None else max(expected - observed, 0)
    return {"policy_version": policy, "expected_count": expected, "observed_count": observed, "missing_count": missing,
            "unverifiable_reasons": reasons or []}


def _warn(code: str, message: str, component: str | None = None, evidence: str | None = None) -> dict:
    return {"code": code, "message": message, "affected_component": component, "evidence_ref": evidence}


# ---- shared lookups ---------------------------------------------------------------------------------------------
def _public_release(conn, release_id: UUID):
    try:
        view = PgAnalysisRelease(conn).get_release(release_id)
    except NotFound as e:
        raise ApiError(404, "NOT_FOUND", "发布不存在") from e
    if view.release_state not in PUBLIC_STATES:
        raise ApiError(404, "NOT_FOUND", "发布尚未公开")
    return view


def _snapshot_refs(conn, view) -> list[dict]:
    ids = sorted({c.snapshot_id for c in view.components if c.snapshot_id is not None})
    if not ids:
        return []
    rows = conn.execute("SELECT * FROM market.dataset_manifest WHERE snapshot_id = ANY(%s) AND manifest_state='READY'"
                        " ORDER BY dataset_id, manifest_version", (ids,)).fetchall()
    return [market_repo.snapshot_ref(r) for r in rows]


def _release_meta(conn, view, **kw) -> dict:
    warnings = list(kw.pop("warnings", []))
    if view.release_state == "SUPERSEDED":
        warnings.append(_warn("RELEASE_SUPERSEDED", "该发布已被新发布取代，仍按其冻结内容显示", "release"))
    return meta(release_id=view.release_id, decision_as_of=view.decision_as_of,
                availability_policy_version=view.availability_policy_version,
                input_snapshot_refs=_snapshot_refs(conn, view),
                freshness_state="current" if view.release_state == "READY" else "stale", warnings=warnings, **kw)


def _instrument(conn, instrument_id: UUID) -> dict:
    try:
        return get_instrument_wire(conn, instrument_id)
    except KeyError as e:
        raise ApiError(404, "NOT_FOUND", "证券不存在") from e


def _cursor_enc(doc: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(doc, separators=(",", ":")).encode()).decode().rstrip("=")


def _cursor_dec(cursor: str, release_id: UUID) -> tuple[datetime, UUID]:
    try:
        doc = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        at, ident, rel = datetime.fromisoformat(doc["a"]), UUID(doc["i"]), doc["r"]
    except Exception as e:  # a malformed cursor is a client error, never a silent restart
        raise ApiError(400, "CURSOR_INVALID", "游标无效") from e
    if rel != str(release_id):
        raise ApiError(409, "CURSOR_RELEASE_MISMATCH", "游标属于另一个发布，需要重新加载")
    return at, ident


def _next_cursor(rows: list[dict], key: str, release_id: UUID) -> str | None:
    if len(rows) <= PAGE:
        return None
    last = rows[PAGE - 1]
    return _cursor_enc({"r": str(release_id), "a": last["available_at"].isoformat(), "i": str(last[key])})


# ---- quote ------------------------------------------------------------------------------------------------------
def _pos(value: Any) -> str | None:
    """Positive decimal string or None: a zero price in a source quote means 'no price', never a price."""
    if value is None:
        return None
    d = Decimal(str(value))
    return None if d <= 0 else contracts.decimal_str(d)


def _stale_after(conn, source_id: str, endpoint_id: str, schema_v: str, mapping_v: str) -> int:
    row = conn.execute("SELECT stale_after_seconds FROM core.source_endpoint WHERE source_id=%s AND endpoint_id=%s"
                       " AND source_schema_version=%s AND mapping_version=%s", (source_id, endpoint_id, schema_v, mapping_v)).fetchone()
    return row["stale_after_seconds"] if row else DEFAULT_STALE_AFTER_SECONDS


def _freshness(reference: datetime | None, stale_after: int, now: datetime) -> str:
    if reference is None:
        return "unknown"
    return "current" if (now - reference).total_seconds() <= stale_after else "stale"


def _quote_from_projection(conn, row: dict, policy: str, now: datetime) -> tuple[dict, str, list[dict]]:
    q = {
        "instrument_id": row["instrument_id"], "source_id": row["source_id"], "endpoint_id": row["endpoint_id"],
        "source_schema_version": row["source_schema_version"], "mapping_version": row["mapping_version"],
        "observation_id": row["observation_id"], "live_quote_version": row["live_quote_version"],
        "trade_date": row["trade_date"], "source_event_time": row["source_event_time"], "first_seen_at": row["first_seen_at"],
        "available_at": row["available_at"], "currency": "CNY", "adjustment_mode": "raw",
        "last_price_raw": _pos(row["last_price_raw"]), "bid_price_raw": _pos(row["bid_price_raw"]),
        "ask_price_raw": _pos(row["ask_price_raw"]), "bid_volume_shares": row["bid_volume_shares"],
        "ask_volume_shares": row["ask_volume_shares"], "quality_status": row["quality_status"],
        "field_quality": row["field_quality"]}
    stale_after = _stale_after(conn, row["source_id"], row["endpoint_id"], row["source_schema_version"], row["mapping_version"])
    fresh = _freshness(row["source_event_time"] or row["available_at"], stale_after, now)
    return {"quote_snapshot_id": row["quote_snapshot_id"], "quote_policy_version": policy, "quote": q,
            "freshness_state": fresh, "freshness_evaluated_at": now}, fresh, []


def _quote_from_observation(conn, instrument_id: UUID, now: datetime) -> tuple[dict, str, list[dict]] | None:
    """Latest canonical quote observation; used while no ``market.quote_projection`` row exists (read-only API role)."""
    row = conn.execute(
        "SELECT o.*, r.source_id AS raw_source_id, r.endpoint_id AS raw_endpoint_id, r.source_schema_version AS raw_schema_version,"
        " r.first_seen_at AS raw_first_seen_at FROM ingest.observation o JOIN ingest.raw_object r USING (raw_object_id)"
        " WHERE o.contract_id='CanonicalQuote' AND o.entity_type='instrument' AND o.entity_key=%s"
        " ORDER BY o.event_time DESC NULLS LAST, o.available_at DESC, o.observation_revision DESC LIMIT 1",
        (str(instrument_id),)).fetchone()
    if row is None:
        return None
    p = row["canonical_payload"]
    quality = row["quality_status"]
    first_seen = min(row["raw_first_seen_at"], row["available_at"])
    q = {
        "instrument_id": instrument_id, "source_id": row["raw_source_id"], "endpoint_id": row["raw_endpoint_id"],
        "source_schema_version": row["raw_schema_version"], "mapping_version": row["mapping_version"],
        "observation_id": row["observation_id"], "live_quote_version": row["observation_revision"],
        "trade_date": p.get("trade_date"), "source_event_time": row["event_time"], "first_seen_at": first_seen,
        "available_at": row["available_at"], "currency": "CNY", "adjustment_mode": "raw",
        "last_price_raw": _pos(p.get("last_price_raw")), "bid_price_raw": _pos(p.get("bid_price_raw")),
        "ask_price_raw": _pos(p.get("ask_price_raw")), "bid_volume_shares": p.get("bid_volume_shares"),
        "ask_volume_shares": p.get("ask_volume_shares"), "quality_status": quality, "field_quality": row["field_quality"]}
    stale_after = _stale_after(conn, q["source_id"], q["endpoint_id"], q["source_schema_version"], q["mapping_version"])
    fresh = _freshness(row["event_time"] or row["available_at"], stale_after, now)
    snapshot_id = uuid.uuid5(_QUOTE_NS, f"{row['observation_id']}:{QUOTE_POLICY_VERSION}")
    warnings = [_warn("QUOTE_PROJECTION_ABSENT",
                      "没有 market.quote_projection 行；报价取自最新规范报价观测，quote_snapshot_id 由观测派生而非数据快照", "quote",
                      str(row["observation_id"]))]
    return {"quote_snapshot_id": snapshot_id, "quote_policy_version": QUOTE_POLICY_VERSION, "quote": q,
            "freshness_state": fresh, "freshness_evaluated_at": now}, fresh, warnings


def _latest_quote(conn, instrument_id: UUID, policy: str, snapshot_id: UUID | None, now: datetime):
    """(QuoteSnapshot, freshness, warnings, input refs) or None. Raises 404/409 for an unresolvable exact snapshot."""
    sql = ("SELECT q.*, m.manifest_state, m.dataset_id, m.manifest_version, m.content_hash FROM market.quote_projection q"
           " JOIN market.dataset_manifest m ON m.snapshot_id=q.quote_snapshot_id WHERE q.instrument_id=%s AND q.quote_policy_version=%s")
    args: list[Any] = [instrument_id, policy]
    if snapshot_id is not None:
        sql += " AND q.quote_snapshot_id=%s"
        args.append(snapshot_id)
    row = conn.execute(sql + " ORDER BY q.live_quote_version DESC LIMIT 1", args).fetchone()
    if row is not None:
        if row["manifest_state"] != "READY":
            raise ApiError(409, "QUOTE_SNAPSHOT_NOT_READY", "报价快照尚未发布")
        snap, fresh, warns = _quote_from_projection(conn, row, policy, now)
        ref = {"snapshot_id": row["quote_snapshot_id"], "dataset_id": row["dataset_id"],
               "manifest_version": row["manifest_version"], "content_hash": row["content_hash"]}
        return snap, fresh, warns, [ref]
    if snapshot_id is not None:
        raise ApiError(404, "NOT_FOUND", "该报价快照不存在")
    if policy != QUOTE_POLICY_VERSION:
        return None
    got = _quote_from_observation(conn, instrument_id, now)
    return None if got is None else (*got, [])


@router.get("/stocks/{instrument_id}/quote")
def get_stock_quote(instrument_id: UUID, quote_policy_version: str, quote_snapshot_id: UUID | None = None,
                    conn=Depends(conn_dep)):
    _instrument(conn, instrument_id)
    now = _now()
    got = _latest_quote(conn, instrument_id, quote_policy_version, quote_snapshot_id, now)
    if got is None:
        raise ApiError(404, "NOT_FOUND", "没有可用的报价（不会用日线收盘价代替）")
    snap, fresh, warns, refs = got
    if fresh != "current":
        warns = [*warns, _warn("QUOTE_STALE" if fresh == "stale" else "QUOTE_TIME_UNKNOWN",
                               "报价已超过端点的新鲜度阈值" if fresh == "stale" else "报价没有可用时间，无法判断新鲜度", "quote")]
    return respond("getStockQuote", snap, meta(
        quality_status=snap["quote"]["quality_status"], freshness_state=fresh, input_snapshot_refs=refs, warnings=warns,
        coverage=_cov("quote-latest-v1", 1, 1)))


# ---- workspace --------------------------------------------------------------------------------------------------
FROZEN_KEYS = ("chart_window_refs", "features", "plan_refs", "evidence_revision_ids", "strategy_contributions")


@router.get("/stocks/{instrument_id}/workspace")
def get_stock_workspace(instrument_id: UUID, release_id: UUID, view_kind: str, request: Request,
                        account_id: UUID | None = None, conn=Depends(conn_dep)):
    if view_kind not in VIEW_KINDS:
        raise _bad_request("view_kind", "invalid_enum", f"view_kind 必须是 {' / '.join(VIEW_KINDS)}")
    scopes = ["public"]
    if view_kind == "account_holding":
        if account_id is None:
            raise _bad_request("account_id", "required", "account_holding 视图需要 account_id")
        principal(request)
        scopes = [str(account_id), f"account:{account_id}"]
    view = _public_release(conn, release_id)
    inst = _instrument(conn, instrument_id)
    row = conn.execute(
        "SELECT * FROM decision.workspace_projection WHERE instrument_id=%s AND release_id=%s AND view_kind=%s"
        " AND scope_key = ANY(%s) ORDER BY projection_version DESC LIMIT 1", (instrument_id, release_id, view_kind, scopes)).fetchone()
    if row is None:
        blocked = [c for c in view.components if c.component_key.startswith("workspace") and c.component_state != "READY"]
        if blocked:
            raise ApiError(409, "RELEASE_COMPONENT_UNAVAILABLE",
                           f"该发布的 {blocked[0].component_key} 组件状态为 {blocked[0].component_state}，没有此股票的工作台")
        raise ApiError(404, "NOT_FOUND", "该发布中没有此股票的工作台投影")
    payload = row["projection_payload"] or {}
    now = _now()
    overlay = _latest_quote(conn, instrument_id, QUOTE_POLICY_VERSION, None, now)
    action_id = None
    if view_kind == "account_holding":
        a = conn.execute("SELECT actionability_id FROM decision.actionability_snapshot WHERE account_id=%s AND release_id=%s"
                         " AND valid_until > %s ORDER BY checked_at DESC LIMIT 1", (account_id, release_id, now)).fetchone()
        action_id = a["actionability_id"] if a else None
    data = {k: payload.get(k, []) for k in FROZEN_KEYS}
    data.update({"instrument": inst, "release_id": release_id, "view_kind": view_kind, "scope_key": row["scope_key"],
                 "projection_version": row["projection_version"], "projection_hash": row["projection_hash"],
                 "generated_at": row["generated_at"], "actionability_id": action_id,
                 "quote_overlay": overlay[0] if overlay else None})
    feats = data["features"]
    ready = sum(1 for f in feats if f.get("result_state") == "READY")
    warns = []
    if overlay is None:
        warns.append(_warn("QUOTE_OVERLAY_MISSING", "没有可用实时报价，工作台只显示冻结分析", "quote_overlay"))
    elif overlay[1] != "current":
        warns.append(_warn("QUOTE_STALE", "实时报价已过期", "quote_overlay"))
    quality = "valid" if ready == len(feats) else "partial"
    return respond("getStockWorkspace", data, _release_meta(
        conn, view, quality_status=quality, warnings=warns, actionability_id=action_id,
        coverage=_cov("workspace-features-v1", len(feats), ready)))


# ---- bars -------------------------------------------------------------------------------------------------------
def _manifest_for_bars(conn, snapshot_id: UUID) -> dict:
    manifest = market_repo.get_manifest(conn, snapshot_id)
    if manifest is None:
        raise ApiError(404, "NOT_FOUND", "数据快照不存在")
    if manifest["manifest_state"] != "READY":
        raise ApiError(409, "SNAPSHOT_NOT_READY", "数据快照尚未发布")
    if not manifest["dataset_id"].startswith("bars") or "capability" not in (manifest["coverage_document"] or {}):
        raise ApiError(409, "SNAPSHOT_NOT_BARS", "该快照不是K线数据集")
    return manifest


def _check_bars_request(manifest: dict, timeframe: str, mode: str, version: str | None) -> str | None:
    """Capability and adjustment checks; returns the adjustment_version to serve (None for raw)."""
    cov = manifest["coverage_document"]
    cap = Capability.from_doc(cov["capability"])
    daily = timeframe == "1d" or timeframe in AGGREGATED
    need = {DAILY_BARS if daily else MINUTE_BARS_TRUE_OHLC, RAW_PRICES if mode == "raw" else ADJUSTED_PRICES}
    decision = evaluate(Requirement("getStockBars", frozenset(need)), cap)
    if decision.outcome == "rejected":
        raise ApiError(409, "CAPABILITY_UNAVAILABLE", "该数据快照不具备所请求的能力：" + "；".join(decision.reasons),
                       field_errors=[{"path": "/snapshot_id", "code": "capability_unavailable", "message": r} for r in decision.reasons])
    served = cov.get("adjustment_mode", "raw")
    if mode != served:
        raise ApiError(409, "ADJUSTMENT_MODE_UNAVAILABLE", f"该快照的价格口径是 {served}，不能提供 {mode}",
                       field_errors=[{"path": "/adjustment_mode", "code": "unavailable", "message": f"snapshot serves {served}"}])
    if mode == "raw":
        if version is not None:
            raise _bad_request("adjustment_version", "not_applicable", "raw 价格没有复权版本")
        return None
    have = cov.get("adjustment_version")
    if have is None:
        raise ApiError(409, "ADJUSTMENT_VERSION_UNKNOWN", "快照没有声明复权版本")
    if version is not None and version != have:
        raise ApiError(409, "ADJUSTMENT_VERSION_MISMATCH", f"快照的复权版本是 {have}，请求的是 {version}",
                       field_errors=[{"path": "/adjustment_version", "code": "mismatch", "message": f"snapshot has {have}"}])
    return have


def _bar_from_row(r: dict) -> Bar:
    return Bar(instrument_id=str(r["instrument_id"]), timeframe=r["timeframe"], session_scope=r["session_scope"],
               trade_date=r["trade_date"], bar_open_time=r["bar_open_time"], bar_close_time=r["bar_close_time"],
               open=r["open_price_raw"], high=r["high_price_raw"], low=r["low_price_raw"], close=r["close_price_raw"],
               volume_shares=r["volume_shares"], turnover_cny=r["turnover_amount_cny"], available_at=r["available_at"],
               is_final=r["is_final"], quality_status=r["quality_status"], exchange_reference_price=r["exchange_reference_price_raw"])


def _read_projection_bars(conn, instrument_id: UUID, base_tf: str, d0: date, d1: date, snapshot_id: UUID, scope: str,
                          as_of: datetime) -> tuple[list[Bar], dict[str, dict]]:
    rows = [r for r in market_repo.read_bars(conn, instrument_id, base_tf, d0, d1, snapshot_id, scope) if r["available_at"] <= as_of]
    ids = [r["observation_id"] for r in rows if r["observation_id"] is not None]
    fq = {}
    if ids:
        fq = {str(o["observation_id"]): o["field_quality"] for o in conn.execute(
            "SELECT observation_id, field_quality FROM ingest.observation WHERE observation_id = ANY(%s)", (ids,)).fetchall()}
    out, by_key = [], {}
    for r in rows:
        bar = _bar_from_row(r)
        out.append(bar)
        by_key[bar.bar_open_time.isoformat()] = fq.get(str(r["observation_id"])) or {
            "*": {"status": "missing", "reason": "observation_field_quality_unavailable", "raw_object_id": None,
                  "value_origin": "not_available"}}
    return out, by_key


def _lake_manifest(sha: str) -> list[Path]:
    root = config.load().lake_root
    data = (root / "manifests" / f"{sha}.json").read_bytes()
    if hashlib.sha256(data).hexdigest() != sha:
        raise ApiError(503, "LAKE_INTEGRITY", "湖清单摘要不一致", retryable=False)
    return [root / m["path"] for m in json.loads(data)["members"] if m["path"].endswith(".parquet")]


def _lake_daily_rows(manifest: dict, instrument_id: UUID, d0: date, d1: date) -> list[dict]:
    """Daily rows of one instrument from the frozen lake manifest of an adjusted snapshot (read-only)."""
    import polars as pl

    try:
        paths = _lake_manifest(manifest["content_hash"])
        frame = (pl.scan_parquet(paths).filter((pl.col("instrument_id") == str(instrument_id))
                                               & pl.col("trade_date").is_between(d0, d1)).sort("trade_date").collect())
    except FileNotFoundError as e:
        raise ApiError(503, "LAKE_UNAVAILABLE", "快照对应的数据湖文件不可读", retryable=True) from e
    return frame.to_dicts()


def _read_lake_bars(manifest: dict, instrument_id: UUID, d0: date, d1: date, as_of: datetime) -> tuple[list[Bar], dict[str, dict]]:
    cov = manifest["coverage_document"]
    src = cov["capability"]
    fq_all = {"*": {"status": "derived", "reason": "forward_adjusted_with_unknown_factor_series", "raw_object_id": None,
                    "source_id": src["source_id"], "endpoint_id": src["endpoint_id"], "mapping_version": None,
                    "conversion": "legacy-import", "value_origin": "primary_source", "raw_value": None, "raw_unit": None,
                    "precision_note": cov.get("adjustment_note")},
              "turnover_amount_cny": {"status": "missing", "reason": "absent_in_source", "raw_object_id": None,
                                      "value_origin": "not_available"}}
    out, by_key = [], {}
    for r in _lake_daily_rows(manifest, instrument_id, d0, d1):
        if r["available_at"] > as_of:
            continue
        bar = Bar(instrument_id=r["instrument_id"], timeframe="1d", session_scope=SESSION_SCOPE_REGULAR, trade_date=r["trade_date"],
                  bar_open_time=r["bar_open_time"], bar_close_time=r["bar_close_time"], open=r["open_price"], high=r["high_price"],
                  low=r["low_price"], close=r["close_price"], volume_shares=r["volume_shares"], turnover_cny=r["turnover_amount_cny"],
                  available_at=r["available_at"], is_final=r["is_final"], quality_status=r["quality_status"],
                  adjustment_mode=r["adjustment_mode"], adjustment_version=r["adjustment_version"])
        out.append(bar)
        by_key[bar.bar_open_time.isoformat()] = fq_all
    return out, by_key


def _period_start(d: date, tf: str) -> date:
    return TradingCalendar(sessions=(d,), known_through=d).period_key(d, tf)[0]


def _aggregate(daily: list[Bar], tf: str, decision_as_of: datetime) -> list[Bar]:
    """Weekly/monthly... bars. ``computed_at`` is the newest input ``available_at`` (bounded by decision_as_of), never the wall
    clock: the derived available_at is max(inputs, computed_at) = max(inputs), so the window is deterministic and PIT-safe."""
    if not daily:
        return []
    computed_at = min(max(b.available_at for b in daily), decision_as_of)
    cal = TradingCalendar(sessions=tuple(b.trade_date for b in daily), known_through=max(b.trade_date for b in daily))
    first = min(b.trade_date for b in daily)
    out = []
    for b in aggregate(daily, cal, tf, computed_at=computed_at):
        own = max(d.available_at for d in daily if _period_start(d.trade_date, tf) == _period_start(b.trade_date, tf))
        b = replace(b, available_at=min(own, decision_as_of))   # a period is available when its own last input is
        started_inside = _period_start(first, tf) < first and b.trade_date >= first and _period_start(b.trade_date, tf) == _period_start(first, tf)
        out.append(replace(b, is_final=False, quality_status="partial", issues=(*b.issues, "data_starts_inside_period")) if started_inside else b)
    return out


def _derived_fq(tf: str) -> dict:
    return {"*": {"status": "derived", "reason": f"aggregated_from_1d_to_{tf}", "raw_object_id": None,
                  "value_origin": "calculation"}}


def _raw_snapshot_for(conn, manifest: dict) -> UUID:
    row = conn.execute("SELECT input_snapshot_id FROM market.dataset_dependency WHERE snapshot_id=%s ORDER BY dependency_role LIMIT 1",
                       (manifest["snapshot_id"],)).fetchone()
    return row["input_snapshot_id"] if row else manifest["snapshot_id"]


def _validate_bars_params(timeframe: str, mode: str, start: datetime, end: datetime, as_of: datetime) -> None:
    errs = []
    if not TIMEFRAME_RE.match(timeframe):
        errs.append({"path": "/timeframe", "code": "invalid_pattern", "message": "timeframe 不符合合同格式"})
    if mode not in ADJUSTMENT_MODES:
        errs.append({"path": "/adjustment_mode", "code": "invalid_enum", "message": "adjustment_mode 无效"})
    if any(t.tzinfo is None for t in (start, end, as_of)):
        errs.append({"path": "/start", "code": "timezone_required", "message": "时间需要时区（UTC Z）"})
    elif end < start:
        errs.append({"path": "/end", "code": "range_invalid", "message": "end 早于 start"})
    if errs:
        raise ApiError(400, "BAD_REQUEST", "请求参数无效", field_errors=errs)


@router.get("/stocks/{instrument_id}/bars")
def get_stock_bars(instrument_id: UUID, snapshot_id: UUID, decision_as_of: datetime, availability_policy_version: str,
                   timeframe: str, session_policy_version: str, adjustment_mode: str, start: datetime, end: datetime,
                   adjustment_version: str | None = None, conn=Depends(conn_dep)):
    _validate_bars_params(timeframe, adjustment_mode, start, end, decision_as_of)
    if availability_policy_version not in REGISTERED_AVAILABILITY_POLICIES:
        raise ApiError(409, "AVAILABILITY_POLICY_UNREGISTERED", f"未注册的可得性政策 {availability_policy_version}")
    _instrument(conn, instrument_id)
    manifest = _manifest_for_bars(conn, snapshot_id)
    version = _check_bars_request(manifest, timeframe, adjustment_mode, adjustment_version)
    d0, d1 = start.astimezone(CST).date(), end.astimezone(CST).date()
    base_tf = "1d" if timeframe in AGGREGATED else timeframe
    read_from = _period_start(d0, timeframe) if timeframe in AGGREGATED else d0
    if adjustment_mode == "raw":
        bars, fq = _read_projection_bars(conn, instrument_id, base_tf, read_from, d1, snapshot_id, session_policy_version, decision_as_of)
        if not bars and market_repo.read_bars(conn, instrument_id, base_tf, read_from, d1, snapshot_id, SESSION_SCOPE_REGULAR) \
                and session_policy_version != SESSION_SCOPE_REGULAR:
            raise ApiError(409, "SESSION_POLICY_UNAVAILABLE", f"快照使用的会话口径是 {SESSION_SCOPE_REGULAR}")
    else:
        if base_tf != "1d":
            raise ApiError(409, "TIMEFRAME_UNAVAILABLE", "复权口径只提供日线及其聚合周期")
        bars, fq = _read_lake_bars(manifest, instrument_id, read_from, d1, decision_as_of)
    if timeframe in AGGREGATED:
        bars = _aggregate(bars, timeframe, decision_as_of)
        fq = {b.bar_open_time.isoformat(): _derived_fq(timeframe) for b in bars}
    bars = [b for b in bars if b.bar_open_time <= end and b.bar_close_time >= start]
    raw_snapshot = _raw_snapshot_for(conn, manifest) if adjustment_mode != "raw" else None
    wire_bars = []
    for b in bars:
        w = builders.wire_bar_payload(b, snapshot_id=str(snapshot_id), field_quality=fq.get(b.bar_open_time.isoformat(), _derived_fq(timeframe)))
        if raw_snapshot is not None:
            w["raw_snapshot_id"] = str(raw_snapshot)
        wire_bars.append(w)
    cov = manifest["coverage_document"]
    covered_from = date.fromisoformat(cov["first_date"]) if cov.get("first_date") else None
    reasons = ["exchange_calendar_not_ingested:sessions_inferred_from_data"] if base_tf == "1d" else []
    missing_sessions = any(i.startswith("missing_sessions") for b in bars for i in b.issues)
    if covered_from is None or d0 < covered_from:
        reasons.append(f"window_starts_before_dataset_coverage:{covered_from.isoformat() if covered_from else 'unknown'}")
    if not bars:
        reasons.append("no_bars_in_window")
    complete = bool(bars) and not missing_sessions and covered_from is not None and d0 >= covered_from
    warns = []
    if availability_policy_version != OBSERVED_AVAILABILITY_POLICY:
        warns.append(_warn("AVAILABILITY_POLICY_NOT_APPLIED",
                           "该可得性政策已注册但图表窗口按真实 available_at 过滤，不派生 policy_usable_at", "bars"))
    window = {"instrument_id": instrument_id, "snapshot_id": snapshot_id, "timeframe": timeframe,
              "session_policy_version": session_policy_version, "adjustment_mode": adjustment_mode, "adjustment_version": version,
              "start": start, "end": end, "window_complete": complete,
              "window_hash": sha256_json({"snapshot": str(snapshot_id), "asof": contracts.utc_z(decision_as_of), "bars": wire_bars}),
              "bars": wire_bars}
    quality = "valid" if complete and all(b.quality_status == "valid" for b in bars) else "partial"
    return respond("getStockBars", window, meta(
        decision_as_of=decision_as_of, availability_policy_version=availability_policy_version, quality_status=quality,
        input_snapshot_refs=[market_repo.snapshot_ref(manifest)], warnings=warns,
        coverage=_cov("bars-window-declared-coverage-v1", None, len(wire_bars), reasons)))


# ---- intelligence ----------------------------------------------------------------------------------------------
def _intel_meta(conn, view, kind: str, observed: int, quality: list[str], reasons: list[str] | None = None,
                next_cursor: str | None = None) -> dict:
    reasons = list(reasons or [])
    if observed == 0:
        reasons.append(f"no_{kind}_observations_as_of_release")
    worst = "valid" if observed and all(q == "valid" for q in quality) else "partial"
    return _release_meta(conn, view, quality_status=worst, next_cursor=next_cursor,
                         coverage=_cov(f"{kind}-as-of-release-v1", 1, 1 if observed else 0, reasons))


def _after(cursor: str | None, release_id: UUID, col: str, idcol: str) -> tuple[str, list]:
    if not cursor:
        return "", []
    at, ident = _cursor_dec(cursor, release_id)
    return f" AND ({col}, {idcol}) < (%s, %s)", [at, ident]


def _claim_subjects(conn, instrument_id: UUID) -> list[str]:
    rows = conn.execute("SELECT source_symbol FROM core.instrument_alias WHERE instrument_id=%s AND source_id = ANY(%s)",
                        (instrument_id, list(ISSUER_ALIAS_SOURCES))).fetchall()
    return sorted({str(instrument_id), *(r["source_symbol"] for r in rows)})


@router.get("/stocks/{instrument_id}/evidence")
def get_stock_evidence(instrument_id: UUID, release_id: UUID, cursor: str | None = None, conn=Depends(conn_dep)):
    view = _public_release(conn, release_id)
    _instrument(conn, instrument_id)
    extra, extra_args = _after(cursor, release_id, "available_at", "claim_revision_id")
    claims = conn.execute(
        "SELECT * FROM (SELECT DISTINCT ON (claim_id) * FROM intelligence.claim_revision WHERE subject_kind IN ('instrument','issuer')"
        " AND subject_key = ANY(%s) AND available_at <= %s ORDER BY claim_id, revision DESC) latest WHERE true" + extra +
        f" ORDER BY available_at DESC, claim_revision_id DESC LIMIT {PAGE + 1}",
        [_claim_subjects(conn, instrument_id), view.decision_as_of, *extra_args]).fetchall()
    nxt = _next_cursor(claims, "claim_revision_id", release_id)
    claims = claims[:PAGE]
    ids = [c["claim_revision_id"] for c in claims]
    links = conn.execute("SELECT * FROM intelligence.claim_evidence WHERE claim_revision_id = ANY(%s) AND available_at <= %s"
                         " ORDER BY claim_revision_id, document_revision_id, evidence_ordinal", (ids, view.decision_as_of)).fetchall() if ids else []
    doc_ids = sorted({link["document_revision_id"] for link in links})
    docs = conn.execute("SELECT * FROM intelligence.document_revision WHERE document_revision_id = ANY(%s) AND available_at <= %s"
                        " ORDER BY available_at DESC, document_revision_id", (doc_ids, view.decision_as_of)).fetchall() if doc_ids else []
    seen = {d["document_revision_id"] for d in docs}
    by_claim: dict[UUID, list] = {}
    for link in links:
        if link["document_revision_id"] in seen:
            by_claim.setdefault(link["claim_revision_id"], []).append({k: link[k] for k in (
                "document_revision_id", "evidence_ordinal", "relation", "evidence_span", "extraction_version", "available_at")})
    data = {"instrument_id": instrument_id, "release_id": release_id,
            "documents": [{k: d[k] for k in ("document_revision_id", "document_id", "source_id", "revision", "title", "origin_group",
                                              "origin_status", "published_at", "published_time_precision", "first_seen_at",
                                              "available_at", "quality_status", "document_status", "raw_object_id", "field_quality")}
                          for d in docs],
            "claims": [{**{k: c[k] for k in ("claim_revision_id", "claim_id", "revision", "subject_kind", "subject_key", "predicate",
                                              "object_payload", "claim_schema_version", "assertion_type", "claim_status",
                                              "verification_policy_version", "available_at", "quality_status")},
                        "evidence": by_claim.get(c["claim_revision_id"], [])} for c in claims]}
    reasons = ["claims_matched_by_issuer_code_alias"] if claims else []
    return respond("getStockEvidence", data, _intel_meta(conn, view, "evidence", len(claims), [c["quality_status"] for c in claims], reasons, nxt))


@router.get("/stocks/{instrument_id}/community")
def get_stock_community(instrument_id: UUID, release_id: UUID, cursor: str | None = None, conn=Depends(conn_dep)):
    view = _public_release(conn, release_id)
    _instrument(conn, instrument_id)
    extra, extra_args = _after(cursor, release_id, "available_at", "community_observation_id")
    rows = conn.execute(
        "SELECT * FROM intelligence.community_observation WHERE available_at <= %s AND (analysis_payload->'instrument_ids' ? %s"
        " OR analysis_payload->>'instrument_id' = %s)" + extra +
        f" ORDER BY available_at DESC, community_observation_id DESC LIMIT {PAGE + 1}",
        [view.decision_as_of, str(instrument_id), str(instrument_id), *extra_args]).fetchall()
    nxt = _next_cursor(rows, "community_observation_id", release_id)
    rows = rows[:PAGE]
    keys = ("community_observation_id", "source_id", "post_id", "post_revision", "author_pseudonym", "post_created_at", "sampled_at",
            "sampling_entry", "sampling_sort", "sampling_window_start", "sampling_window_end", "coverage_status", "like_count",
            "reply_count", "unique_participant_count", "analysis_schema_version", "analysis_payload", "available_at", "quality_status")
    data = {"instrument_id": instrument_id, "release_id": release_id, "observations": [{k: r[k] for k in keys} for r in rows]}
    reasons = ["community_rows_linked_through_analysis_payload.instrument_ids"] if rows else []
    return respond("getStockCommunity", data, _intel_meta(conn, view, "community", len(rows), [r["quality_status"] for r in rows], reasons, nxt))


@router.get("/stocks/{instrument_id}/fund-flow")
def get_stock_fund_flow(instrument_id: UUID, release_id: UUID, cursor: str | None = None, conn=Depends(conn_dep)):
    view = _public_release(conn, release_id)
    _instrument(conn, instrument_id)
    extra, extra_args = _after(cursor, release_id, "available_at", "funding_observation_id")
    rows = conn.execute(
        "SELECT * FROM (SELECT DISTINCT ON (source_id, source_record_key, metric_namespace, metric_name, methodology_version) *"
        " FROM intelligence.funding_observation WHERE instrument_id=%s AND available_at <= %s"
        " ORDER BY source_id, source_record_key, metric_namespace, metric_name, methodology_version, revision DESC) latest WHERE true" + extra +
        f" ORDER BY available_at DESC, funding_observation_id DESC LIMIT {PAGE + 1}",
        [instrument_id, view.decision_as_of, *extra_args]).fetchall()
    nxt = _next_cursor(rows, "funding_observation_id", release_id)
    rows = rows[:PAGE]
    keys = ("funding_observation_id", "source_id", "instrument_id", "market_key", "metric_namespace", "metric_name", "methodology_id",
            "methodology_version", "measurement_class", "side_convention", "trade_or_order_basis", "session_scope", "market_direction",
            "observation_window_start", "observation_window_end", "raw_value", "raw_unit", "canonical_value", "canonical_unit", "currency",
            "missing_reason", "available_at", "quality_status", "field_quality")
    data = {"instrument_id": instrument_id, "release_id": release_id, "observations": [{k: r[k] for k in keys} for r in rows]}
    return respond("getStockFundFlow", data, _intel_meta(conn, view, "fund_flow", len(rows), [r["quality_status"] for r in rows], None, nxt))


@router.get("/stocks/{instrument_id}/seats")
def get_stock_seats(instrument_id: UUID, release_id: UUID, cursor: str | None = None, conn=Depends(conn_dep)):
    view = _public_release(conn, release_id)
    _instrument(conn, instrument_id)
    extra, extra_args = _after(cursor, release_id, "available_at", "report_revision_id")
    reports = conn.execute(
        "SELECT * FROM (SELECT DISTINCT ON (source_id, source_report_id) * FROM intelligence.seat_report WHERE instrument_id=%s"
        " AND available_at <= %s ORDER BY source_id, source_report_id, revision DESC) latest WHERE true" + extra +
        f" ORDER BY available_at DESC, report_revision_id DESC LIMIT {PAGE + 1}",
        [instrument_id, view.decision_as_of, *extra_args]).fetchall()
    nxt = _next_cursor(reports, "report_revision_id", release_id)
    reports = reports[:PAGE]
    ids = [r["report_revision_id"] for r in reports]
    rows = conn.execute("SELECT * FROM intelligence.seat_report_row WHERE report_revision_id = ANY(%s) ORDER BY report_revision_id, side, rank",
                        (ids,)).fetchall() if ids else []
    by_report: dict[UUID, list] = {}
    for r in rows:
        by_report.setdefault(r["report_revision_id"], []).append({k: r[k] for k in (
            "side", "rank", "raw_seat_name", "canonical_amount_cny", "resolution_status", "seat_id", "seat_version", "resolution_evidence")})
    data = {"instrument_id": instrument_id, "release_id": release_id, "reports": [
        {**{k: r[k] for k in ("report_revision_id", "instrument_id", "source_id", "revision", "window_start", "window_end",
                              "disclosure_reason", "rule_version", "published_at", "available_at", "quality_status")},
         "rows": by_report.get(r["report_revision_id"], [])} for r in reports]}
    return respond("getStockSeats", data, _intel_meta(conn, view, "seats", len(reports), [r["quality_status"] for r in reports], None, nxt))


# ---- data status ------------------------------------------------------------------------------------------------
@router.get("/data-status")
def get_data_status(conn=Depends(conn_dep)):
    """One row per endpoint/partition. ``enabled`` is the deployment intent, ``runtime_state`` and
    ``last_transport_ok_at`` are transport facts, ``freshness_state`` is about content; a sample older than the
    endpoint's own stale threshold can no longer claim ``current`` and is reported ``unknown``."""
    now = _now()
    endpoints = conn.execute("SELECT * FROM core.source_endpoint ORDER BY source_id, endpoint_id, source_schema_version, mapping_version").fetchall()
    health = {(h["source_id"], h["endpoint_id"], h["source_schema_version"], h["mapping_version"], h["partition_key"]): h
              for h in conn.execute("SELECT * FROM ingest.source_health").fetchall()}
    cursors = {(c["source_id"], c["endpoint_id"], c["source_schema_version"], c["mapping_version"], c["partition_key"]): c
               for c in conn.execute("SELECT * FROM ingest.source_cursor").fetchall()}
    items, warns = [], []
    for e in endpoints:
        base = (e["source_id"], e["endpoint_id"], e["source_schema_version"], e["mapping_version"])
        partitions = sorted({k[4] for k in (*health, *cursors) if k[:4] == base}) or ["default"]
        for part in partitions:
            h, c = health.get((*base, part)), cursors.get((*base, part))
            state, fresh = (h["runtime_state"], h["freshness_state"]) if h else ("unconfigured", "unknown")
            if h and fresh == "current" and (now - h["updated_at"]).total_seconds() > e["stale_after_seconds"]:
                fresh = "unknown"
                warns.append(_warn("HEALTH_SAMPLE_OLD", f"{e['source_id']}/{e['endpoint_id']} 的健康采样已超过其新鲜度阈值，不再声明 current",
                                   f"{e['source_id']}/{e['endpoint_id']}"))
            items.append({
                **dict(zip(("source_id", "endpoint_id", "source_schema_version", "mapping_version"), base)), "partition_key": part,
                "enabled": e["enabled"], "runtime_state": state, "freshness_state": fresh,
                "last_attempt_at": h["last_attempt_at"] if h else None,
                "last_transport_ok_at": h["last_transport_ok_at"] if h else None,
                "last_new_record_at": (h["last_new_record_at"] if h else None) or (c["updated_at"] if c else None),
                "last_canonical_ready_at": h["last_canonical_ready_at"] if h else None,
                "consecutive_failures": h["consecutive_failures"] if h else 0,
                "next_attempt_at": h["next_attempt_at"] if h else None, "last_error_code": h["last_error_code"] if h else None})
    bad = sum(1 for i in items if i["runtime_state"] != "healthy" or i["freshness_state"] not in ("current", "not_expected"))
    return respond("getDataStatus", {"items": items}, meta(
        quality_status="valid" if items and not bad else "partial", freshness_state="current", warnings=warns,
        coverage=_cov("data-status-v1", len(items), len(items) - sum(1 for i in items if i["runtime_state"] == "unconfigured"))))


# ---- runtime config for the web app (not a /v1 operation) ---------------------------------------------------------
DEFAULT_HORIZONS = [
    {"key": "short", "label": "短线", "hint": "未来约 5 个交易日的相对机会", "horizonId": "short_term", "forecastTarget": "excess_return_5d"},
    {"key": "swing", "label": "波段", "hint": "未来约 20 个交易日的相对机会", "horizonId": "swing", "forecastTarget": "excess_return_20d"},
    {"key": "long", "label": "长期", "hint": "未来约 120 个交易日的相对机会", "horizonId": "long_term", "forecastTarget": "excess_return_120d"},
]
DEFAULT_TAXONOMIES = [{"id": "industry", "version": 1, "label": "行业"}, {"id": "concept", "version": 1, "label": "概念题材"}]
TAXONOMY_LABELS = {"industry": "行业", "concept": "概念题材"}
HORIZON_KEYS = (("short", "短线"), ("swing", "波段"), ("long", "长期"))


def _published_runs(conn, slot_key: str) -> tuple[list[dict], list[dict]]:
    """Horizons and sector taxonomies that the current release of the slot actually contains."""
    slot = conn.execute("SELECT current_release_id FROM decision.release_slot WHERE slot_key=%s", (slot_key,)).fetchone()
    if slot is None or slot["current_release_id"] is None:
        return [], []
    rid = slot["current_release_id"]
    hz = conn.execute("SELECT DISTINCT horizon_id, forecast_target FROM decision.ranking_run WHERE release_id=%s AND ranking_scope='market'", (rid,)).fetchall()
    tx = conn.execute("SELECT DISTINCT taxonomy_id, taxonomy_version FROM decision.ranking_run WHERE release_id=%s"
                      " AND ranking_scope='sector_overview' AND taxonomy_id IS NOT NULL", (rid,)).fetchall()
    return hz, tx


def build_app_config(conn) -> dict:
    """Values the contract needs as inputs but does not enumerate. Derived from the published release when present;
    PULSE_SLOT_KEY and a PULSE_APP_CONFIG_FILE JSON file override."""
    slot_key = os.environ.get("PULSE_SLOT_KEY", "main")
    hz, tx = _published_runs(conn, slot_key)

    def days(h: dict) -> int:
        m = re.search(r"(\d+)d", h["forecast_target"])
        return int(m.group(1)) if m else 10 ** 6

    horizons = DEFAULT_HORIZONS
    if hz:
        ordered = sorted(hz, key=days)[:3]
        horizons = [{"key": HORIZON_KEYS[i][0], "label": HORIZON_KEYS[i][1],
                     "hint": f"未来约 {days(h)} 个交易日的相对机会" if days(h) < 10 ** 6 else "相对机会",
                     "horizonId": h["horizon_id"], "forecastTarget": h["forecast_target"]} for i, h in enumerate(ordered)]
    taxonomies = [{"id": t["taxonomy_id"], "version": t["taxonomy_version"], "label": TAXONOMY_LABELS.get(t["taxonomy_id"], t["taxonomy_id"])}
                  for t in sorted(tx, key=lambda t: t["taxonomy_id"])] or DEFAULT_TAXONOMIES
    doc = {"slot_key": slot_key, "horizons": horizons, "taxonomies": taxonomies, "quote_policy_version": QUOTE_POLICY_VERSION,
           "session_policy_version": SESSION_SCOPE_REGULAR, "availability_policy_version": OBSERVED_AVAILABILITY_POLICY,
           "source": "published_release" if hz else "defaults"}
    path = os.environ.get("PULSE_APP_CONFIG_FILE")
    if path and Path(path).exists():
        doc.update(json.loads(Path(path).read_text(encoding="utf-8")))
    return doc


@config_router.get("/app-config.json", include_in_schema=False)
def app_config(conn=Depends(conn_dep)):
    return JSONResponse(build_app_config(conn), headers={"Cache-Control": "no-store"})

