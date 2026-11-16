"""Stock page backfill reads (docs/LEGACY_TRIAGE.md rows 41, 159, 43): ``/v1/x/bf/stock/indicators`` and ``/v1/x/bf/stock/stats``.

Why: chart overlays (MA/EMA/BOLL on price, MACD/KDJ/RSI sub-panes) and the stock statistics card are computed on the server
from the same bars the chart shows; the browser only draws. Both reads are pinned to a release (``release_id`` or the current
release of slot ``main``) and use its ``decision_as_of`` / availability policy as the cutoff.
Invariants
* The bars are produced by the ``getStockBars`` handler itself (same snapshot choice as the stock page, same PIT filter
  ``available_at <= decision_as_of``, same window hash); this module never reads a bar the chart could not see.
* Payloads leave through ``respond_backfill`` (contract ``backfill_stock.schema.json``); a violation is a bug, not a pass.
* Missing stays missing: warm-up values and unavailable statistics are ``null`` with a reason.
Not covered: intraday statistics (no intraday records exist), writes.
"""
from __future__ import annotations

import json
from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, Depends

from pulse import config
from pulse.api import routes_market as M
from pulse.api.deps import conn_dep
from pulse.api.envelope import ApiError, respond_backfill
from pulse.application import bf_stock_service as S
from pulse.application import bindings as B

router = APIRouter(prefix="/v1/x/bf/stock")
ADJUSTMENTS = ("raw", "forward_adjusted", "backward_adjusted")


def _release(conn, release_id: UUID | None):
    if release_id is None:
        slot = conn.execute("SELECT current_release_id FROM decision.release_slot WHERE slot_key=%s", (B.SLOT_KEY,)).fetchone()
        if slot is None or slot["current_release_id"] is None:
            raise ApiError(404, "NOT_FOUND", "还没有已发布的版本")
        release_id = slot["current_release_id"]
    return M._public_release(conn, release_id)


def _projection_hints(conn, instrument_id: UUID, release_id: UUID) -> tuple[list[dict], str]:
    """(chart_window_refs, session policy) the workspace of this release declares for the instrument."""
    row = conn.execute(
        "SELECT projection_payload FROM decision.workspace_projection WHERE instrument_id=%s AND release_id=%s"
        " AND view_kind='public_research' AND scope_key='public' ORDER BY projection_version DESC LIMIT 1", (instrument_id, release_id)).fetchone()
    payload = (row or {}).get("projection_payload") or {}
    feats = payload.get("features") or []
    session = ((feats[0].get("key") or {}).get("session_policy_version") if feats else None) or B.SESSION_POLICY
    return list(payload.get("chart_window_refs") or []), session


def _read_window(conn, view, instrument_id: UUID, snapshot_id: str, timeframe: str, mode: str, session: str, start) -> tuple[dict, dict]:
    """(window data, envelope meta) of the bars handler for this window, as the chart would request it."""
    resp = M.get_stock_bars(
        instrument_id=instrument_id, snapshot_id=UUID(snapshot_id), decision_as_of=view.decision_as_of,
        availability_policy_version=view.availability_policy_version, timeframe=timeframe, session_policy_version=session,
        adjustment_mode=mode, start=start, end=view.decision_as_of, adjustment_version=None, conn=conn)
    body = json.loads(resp.body)
    return body["data"], body["meta"]


def _bad(field: str, code: str, message: str) -> ApiError:
    return M._bad_request(field, code, message)


@router.get("/indicators")
def get_indicators(instrument_id: UUID, timeframe: str = "1d", adjustment: str = "raw", release_id: UUID | None = None,
                   set: str | None = None, conn=Depends(conn_dep)):  # noqa: A002 - the query parameter is named ``set``
    if timeframe not in S.LOOKBACK_DAYS:
        raise _bad("timeframe", "invalid_enum", "timeframe 必须是 " + " / ".join(S.LOOKBACK_DAYS))
    if adjustment not in ADJUSTMENTS:
        raise _bad("adjustment", "invalid_enum", "adjustment 必须是 " + " / ".join(ADJUSTMENTS))
    try:
        names = S.parse_set(set)
    except ValueError as e:
        raise _bad("set", "invalid_enum", f"未知指标集 {e}；可选 " + ",".join(S.INDICATOR_SETS)) from e
    view = _release(conn, release_id)
    M._instrument(conn, instrument_id)
    chart_refs, session = _projection_hints(conn, instrument_id, view.release_id)
    snap = S.pick_snapshot(chart_refs, M._snapshot_refs(conn, view), str(instrument_id), timeframe, adjustment)
    if snap is None:
        raise ApiError(409, "SNAPSHOT_UNAVAILABLE", "该发布没有声明可读取K线的行情快照")
    start = S.window_start(view.decision_as_of, timeframe)
    window, bmeta = _read_window(conn, view, instrument_id, snap, timeframe, adjustment, session, start)
    data = {
        "instrument_id": instrument_id, "release_id": view.release_id, "decision_as_of": view.decision_as_of,
        "availability_policy_version": view.availability_policy_version, "timeframe": timeframe, "adjustment_mode": adjustment,
        "adjustment_version": window["adjustment_version"], "snapshot_id": window["snapshot_id"], "window_hash": window["window_hash"],
        "window_start": window["start"], "window_end": window["end"], "window_complete": window["window_complete"],
        "numeric_policy": S.NUMERIC_POLICY, "requested": names, "bars": S.indicator_bars(window),
        "series": S.compute_indicators(window, names)}
    m = M._release_meta(conn, view, quality_status=bmeta["quality_status"], warnings=bmeta["warnings"], coverage=bmeta["coverage"])
    m["input_snapshot_refs"] = bmeta["input_snapshot_refs"]
    return respond_backfill("stock", "StockIndicators", data, m)


@router.get("/stats")
def get_stats(instrument_id: UUID, release_id: UUID | None = None, conn=Depends(conn_dep)):
    view = _release(conn, release_id)
    M._instrument(conn, instrument_id)
    cutoff, policy = view.decision_as_of, view.availability_policy_version
    chart_refs, session = _projection_hints(conn, instrument_id, view.release_id)
    refs = M._snapshot_refs(conn, view)
    notes: list[str] = []
    windows: dict[str, tuple[dict | None, dict | None]] = {}

    def window_for(mode: str, lookback_days: int) -> tuple[dict | None, dict | None]:
        snap = S.pick_snapshot(chart_refs, refs, str(instrument_id), "1d", mode)
        if snap is None:
            notes.append(f"{mode}: 发布没有声明可读取日线的行情快照")
            return None, None
        try:
            data, bmeta = _read_window(conn, view, instrument_id, snap, "1d", mode, session, cutoff - timedelta(days=lookback_days))
        except ApiError as e:
            if e.status != 409:
                raise
            notes.append(f"{mode}: {e.message}")
            return None, None
        return data, bmeta

    price_basis, price_window, price_meta = "raw", None, None
    for mode in ("forward_adjusted", "raw"):          # the ranking features run on adjusted ratios; raw is the fallback
        price_window, price_meta = window_for(mode, S.STATS_LOOKBACK_DAYS)
        if price_window is not None:
            price_basis = mode
            break
    if price_basis == "raw" and price_window is not None:
        raw_window, raw_meta = price_window, price_meta
    else:
        raw_window, raw_meta = window_for("raw", 15)
    miss = "；".join(notes) if notes else None
    cap, cap_reason, cap_man = S.load_cap_fact(conn, config.load().lake_root, instrument_id, cutoff, policy)
    inp = S.StatsInput(
        price_basis=price_basis, price_window=price_window, price_missing_reason=miss and f"读取日线失败：{miss}",
        raw_window=raw_window, raw_missing_reason=miss and f"读取原始价日线失败：{miss}", cap=cap, cap_missing_reason=cap_reason,
        listed_on=S.load_listing(conn, instrument_id), board_code=S.load_board_code(conn, instrument_id, cutoff, policy), decision_as_of=cutoff)
    last = (price_window or {}).get("bars") or []
    data = {
        "instrument_id": instrument_id, "release_id": view.release_id, "decision_as_of": cutoff, "availability_policy_version": policy,
        "price_basis": price_basis, "bars_snapshot_id": (price_window or {}).get("snapshot_id"),
        "last_trade_date": last[-1]["trade_date"] if last else None, "fields": S.build_stats(inp)}
    metas = [x for x in (price_meta, raw_meta if raw_meta is not price_meta else None) if x]
    refs_out = {r["snapshot_id"]: r for x in metas for r in x["input_snapshot_refs"]}
    if cap_man is not None:
        ref = M.market_repo.snapshot_ref(cap_man)
        refs_out[ref["snapshot_id"]] = ref
    daily = [f for f in data["fields"] if f["key"] not in S.INTRADAY_KEYS]       # intraday fields never exist yet; not a coverage gap
    observed = sum(1 for f in daily if f["status"] != "unavailable")
    warnings = [w for x in metas for w in x["warnings"]]
    if observed < len(daily):
        warnings.append(M._warn("STATS_PARTIAL", "个股统计有字段缺失，缺失原因写在各字段里", "stats"))
    quality = "valid" if all(x["quality_status"] == "valid" for x in metas) and metas and cap is not None else "partial"
    m = M._release_meta(conn, view, quality_status=quality, warnings=warnings,
                        coverage=M._cov("stock-stats-v1", len(daily), observed, notes))
    m["input_snapshot_refs"] = list(refs_out.values())
    return respond_backfill("stock", "StockStats", data, m)
