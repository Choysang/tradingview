"""Fill-market lane reads (docs/LEGACY_TRIAGE.md #32/#90/#155 crowd and linkage, #43 stock stats additions, #114/#115/#174 calendar store).
Contract: ``backfill_fillm.schema.json``.

``GET /v1/x/bf/fillm/crowd?release_id=``            industry diffusion, institutional net buy, name clusters (unavailable, with the reason).
``GET /v1/x/bf/fillm/stock-stats-plus?instrument_id=&release_id=``  volume ratio, circulating cap, seal success rate (with its session coverage).
``GET /v1/x/bf/fillm/calendar-store``               status of the imported old calendar store and the function the forward-log rule reads.
Release-dependent reads serve only READY / SUPERSEDED releases; routers translate HTTP only.
"""
from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from pulse import config
from pulse.adapters.lake import LakeStore
from pulse.api import routes_bf_stock as ST
from pulse.api import routes_market as M
from pulse.api.auth import principal
from pulse.api.deps import conn_dep
from pulse.api.envelope import ApiError, meta, respond_backfill
from pulse.api.routes_ext_a import public_release, release_meta
from pulse.application import bf_fillm_calendar as CAL
from pulse.application import bf_fillm_crowd as CROWD
from pulse.application import bf_fillm_stock as FS
from pulse.application import bf_stock_service as S

router = APIRouter(prefix="/v1/x/bf/fillm")
LANE = "fillm"
RAW_LOOKBACK_DAYS = 15


def _coverage(expected: int | None, observed: int, reasons: list[str]) -> dict:
    return {"policy_version": "bf-fillm-v1", "expected_count": expected, "observed_count": observed,
            "missing_count": None if expected is None else max(expected - observed, 0), "unverifiable_reasons": reasons}


@router.get("/crowd")
def crowd(release_id: UUID, request: Request, taxonomy_id: str | None = None, taxonomy_version: int | None = None, conn=Depends(conn_dep)):
    principal(request)
    row = public_release(conn, release_id)
    data = CROWD.snapshot(conn, LakeStore(config.load().lake_root), row, taxonomy_id, taxonomy_version)
    inds = data["industries"]
    reasons = ["membership_snapshot_not_a_history", "name_clusters_unavailable"]
    if data["institutions"]["status"] != "ok":
        reasons.append("institutional_net_" + data["institutions"]["status"])
    return respond_backfill(LANE, "CrowdSnapshot", data, release_meta(conn, row, coverage=_coverage(None, len(inds), reasons)))


@router.get("/stock-stats-plus")
def stock_stats_plus(instrument_id: UUID, release_id: UUID, request: Request, conn=Depends(conn_dep)):
    principal(request)
    view = ST._release(conn, release_id)
    M._instrument(conn, instrument_id)
    cutoff, policy = view.decision_as_of, view.availability_policy_version
    chart_refs, session = ST._projection_hints(conn, instrument_id, view.release_id)
    refs = M._snapshot_refs(conn, view)
    notes: list[str] = []
    window = bmeta = None
    snap = S.pick_snapshot(chart_refs, refs, str(instrument_id), "1d", "raw")
    if snap is None:
        notes.append("发布没有声明可读取日线的行情快照")
    else:
        try:
            window, bmeta = ST._read_window(conn, view, instrument_id, snap, "1d", "raw", session, cutoff - timedelta(days=RAW_LOOKBACK_DAYS))
        except ApiError as e:
            if e.status != 409:
                raise
            notes.append(e.message)
    lake_root = config.load().lake_root
    cap, cap_reason, cap_man = S.load_cap_fact(conn, lake_root, instrument_id, cutoff, policy)
    why = "；".join(notes) if notes else None
    fields = FS.stats_plus(conn, LakeStore(lake_root), instrument_id, cutoff, window, why and f"读取原始价日线失败：{why}", cap, cap_reason)
    data = {"instrument_id": instrument_id, "release_id": view.release_id, "decision_as_of": cutoff, "fields": fields}
    observed = sum(1 for f in fields if f["status"] != "unavailable")
    warnings = [] if bmeta is None else list(bmeta["warnings"])
    if observed < len(fields):
        warnings.append(M._warn("STATS_PARTIAL", "个股统计有字段缺失，缺失原因写在各字段里", "stats"))
    m = M._release_meta(conn, view, quality_status="valid" if observed == len(fields) else "partial", warnings=warnings,
                        coverage=M._cov("stock-stats-plus-v1", len(fields), observed, notes))
    refs_out = {} if bmeta is None else {r["snapshot_id"]: r for r in bmeta["input_snapshot_refs"]}
    if cap_man is not None:
        ref = M.market_repo.snapshot_ref(cap_man)
        refs_out[ref["snapshot_id"]] = ref
    m["input_snapshot_refs"] = list(refs_out.values())
    return respond_backfill(LANE, "StockStatsPlus", data, m)


@router.get("/calendar-store")
def calendar_store(request: Request, conn=Depends(conn_dep)):
    principal(request)
    data = CAL.status(conn, LakeStore(config.load().lake_root))
    return respond_backfill(LANE, "CalendarStoreStatus", data,
                            meta(coverage=_coverage(None, data["events"], [] if data["present"] else ["calendar_store_not_imported"])))
