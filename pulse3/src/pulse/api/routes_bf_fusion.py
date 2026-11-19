"""Backfill lane ``fusion`` reads: the frozen static40 score, its two forward books and the forward experiments (legacy triage batch 3).

Contract: ``backfill_fusion.schema.json`` (FusionStatus, FusionScores, FusionBooks, FusionExperiments); every response is validated by
``respond_backfill`` before it leaves. Routers translate HTTP only; the numbers come from ``application.bf_fusion_read``.
GET /v1/x/bf/fusion/status
GET /v1/x/bf/fusion/scores?date=YYYY-MM-DD&limit=40
GET /v1/x/bf/fusion/books
GET /v1/x/bf/fusion/experiments
There are no writes: the daily step is the CLI ``python -m pulse.workers.fusion daily``.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request

from pulse import config
from pulse.adapters.lake import LakeStore
from pulse.api.auth import principal
from pulse.api.deps import conn_dep
from pulse.api.envelope import ApiError, meta, respond_backfill
from pulse.application import bf_fusion_read as R

router = APIRouter(prefix="/v1/x/bf/fusion")
LANE = "fusion"
MAX_LIMIT = 200


def _lake() -> LakeStore:
    return LakeStore(config.load().lake_root)


def _no_unknown(request: Request, allowed: set[str]) -> None:
    unknown = sorted(set(request.query_params) - allowed)
    if unknown:
        raise ApiError(400, "INVALID_CONTRACT", "未知参数: " + ", ".join(unknown),
                       field_errors=[{"path": "/" + u, "code": "unknown_parameter", "message": "不在请求合同中"} for u in unknown])


@router.get("/status", operation_id="getFusionStatus", summary="State of the frozen static40 score and its books", openapi_extra={"x-auth": "session"})
def status(request: Request, conn=Depends(conn_dep)):
    principal(request)
    _no_unknown(request, set())
    return respond_backfill(LANE, "FusionStatus", R.read_status(conn, _lake()), meta(quality_status="partial"))


@router.get("/scores", operation_id="getFusionScores", summary="Top of the frozen score on one day", openapi_extra={"x-auth": "session"})
def scores(request: Request, limit: int = 40, conn=Depends(conn_dep)):
    principal(request)
    _no_unknown(request, {"date", "limit"})
    if not 1 <= limit <= MAX_LIMIT:
        raise ApiError(400, "INVALID_CONTRACT", f"limit 必须在 1 到 {MAX_LIMIT} 之间")
    day = None
    raw = request.query_params.get("date")
    if raw:
        try:
            day = date.fromisoformat(raw)
        except ValueError as e:
            raise ApiError(400, "INVALID_CONTRACT", "date 必须是 YYYY-MM-DD") from e
    return respond_backfill(LANE, "FusionScores", R.read_scores(conn, _lake(), day, limit), meta(quality_status="partial"))


@router.get("/books", operation_id="getFusionBooks", summary="Frozen main book and 10-day shadow: decisions and NAV by day", openapi_extra={"x-auth": "session"})
def books(request: Request, conn=Depends(conn_dep)):
    principal(request)
    _no_unknown(request, set())
    return respond_backfill(LANE, "FusionBooks", R.read_books(conn, _lake()), meta(quality_status="partial"))


@router.get("/experiments", operation_id="getFusionExperiments", summary="Forward monitor, X20 basket and OS20 records", openapi_extra={"x-auth": "session"})
def experiments(request: Request, conn=Depends(conn_dep)):
    principal(request)
    _no_unknown(request, set())
    return respond_backfill(LANE, "FusionExperiments", R.read_experiments(conn, _lake()), meta(quality_status="partial"))
