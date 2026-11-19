"""Batch 6 reads (E093-E096): historical results per segment, forward progress, the auction field check. Contract: ``backfill_batch6.schema.json``.

``GET /v1/x/bf/batch6/overview``   E093 / E094 results (PRE / DEV, fill rate, matched excess, block bootstrap p, Holm), E095 / E096 forward progress toward 40 signal days.
Every experiment is a shadow record (no order, no ledger, no rule change); the daily step is the worker ``pulse.workers.batch6 forward``; these routes only read.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from pulse import config
from pulse.adapters.lake import LakeStore
from pulse.api.auth import principal
from pulse.api.deps import conn_dep
from pulse.api.envelope import meta, respond_backfill
from pulse.application import bf_batch6_reads as R

router = APIRouter(prefix="/v1/x/bf/batch6")
LANE = "batch6"
WARNING = {"code": "SHADOW_RESEARCH", "message": "影子研究：不下单、不入账、不改任何规则；前向实验在 40 个信号日之前只是进度", "affected_component": "batch6", "evidence_ref": None}


@router.get("/overview", operation_id="getBatch6Overview", summary="Batch 6 historical results and forward progress", openapi_extra={"x-auth": "session"})
def overview(request: Request, conn=Depends(conn_dep)):
    principal(request)
    ov = R.overview(conn, LakeStore(config.load().lake_root))
    data = R.wire(ov)
    observed = len(ov["sessions_decided"])
    return respond_backfill(LANE, "Overview", data, meta(quality_status="partial", coverage={
        "policy_version": "bf-batch6-v1", "expected_count": None, "observed_count": observed, "missing_count": None,
        "unverifiable_reasons": ["forward_experiments_in_progress", "daily_bars_since_2025_10", "turnover_and_float_cap_are_estimates"]}, warnings=[WARNING]))
