"""Forward-log reads (docs/LEGACY_TRIAGE.md batch 3, lane fwdlog). Contract: ``backfill_fwdlog.schema.json``.

``GET /v1/x/bf/fwdlog/experiments``                          the forward experiments with progress toward their pre-registered decision day, metrics (provisional until then), pending days.
``GET /v1/x/bf/fwdlog/experiments/{key}/rows``                the stored daily rows of E077 / E078 / E089 / PAPER (paper: one row per session with its rule tables).
``GET /v1/x/bf/fwdlog/paper/{session_date}/fills?kind=``      the shadow buys (B1 / B3, three queue scenarios) or the sell rules of one session, filterable by rule and code.
Every experiment is a shadow record (nothing booked); rows come from append-only datasets, a decided day is never rewritten. The daily step is the worker
``pulse.workers.fwdlog run``; these routes only read.
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request

from pulse import config
from pulse.adapters.lake import LakeStore
from pulse.api.auth import principal
from pulse.api.deps import conn_dep
from pulse.api.envelope import ApiError, meta, respond_backfill
from pulse.application import bf_fwdlog_reads as R
from pulse.application import bf_fwdlog_registry as REG
from pulse.application.bf_fwdlog_service import FwdlogService

router = APIRouter(prefix="/v1/x/bf/fwdlog")
LANE = "fwdlog"
KEYS = {e.key for e in REG.EXPERIMENTS}
WARNING = {"code": "SHADOW_FORWARD_LOG", "message": "前向影子记录：不下单、不入账、不改任何规则；决策日之前的指标只是进度", "affected_component": "fwdlog", "evidence_ref": None}


def _svc(conn) -> FwdlogService:
    return FwdlogService(conn, LakeStore(config.load().lake_root))


def _coverage(observed: int) -> dict:
    return {"policy_version": "bf-fwdlog-v1", "expected_count": None, "observed_count": observed, "missing_count": None,
            "unverifiable_reasons": ["forward_log_in_progress", "recordings_are_60s_sampled_snapshots"]}


def _pending(svc: FwdlogService) -> dict:
    """Recorded sessions without a row, per experiment (the reason says what the daily step is waiting for)."""
    out: dict[str, list[dict]] = {}
    sessions = svc.recorded_sessions(date(2100, 1, 1))
    for key, ds in (("E077", "E077"), ("E078", "E078"), ("PAPER", "PAPER_DAYS")):
        done = svc.decided(R.DS[ds])
        out[key] = [{"session_date": s, "reason": "recorded, not decided yet: the daily step has not run since the recording was imported, or it waits for data (see worker output)"}
                    for s in sessions if s.isoformat() not in done]
    return out


@router.get("/experiments")
def experiments(request: Request, conn=Depends(conn_dep)):
    principal(request)
    svc = _svc(conn)
    data = R.cards(svc, pending=_pending(svc))
    return respond_backfill(LANE, "ExperimentsData", data, meta(quality_status="partial", coverage=_coverage(sum(c["sessions_decided"] for c in data["experiments"])), warnings=[WARNING]))


@router.get("/experiments/{key}/rows")
def rows(key: str, request: Request, from_date: date | None = Query(None, alias="from"), to_date: date | None = Query(None, alias="to"),
         limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0), conn=Depends(conn_dep)):
    principal(request)
    if key not in KEYS:
        raise ApiError(404, "NOT_FOUND", "未知实验")
    data = R.rows_page(_svc(conn), key, start=from_date, end=to_date, limit=limit, offset=offset)
    return respond_backfill(LANE, "RowsData", data, meta(quality_status="partial", coverage=_coverage(data["total"]), warnings=[WARNING]))


@router.get("/paper/{session_date}/fills")
def paper_fills(session_date: date, request: Request, kind: Literal["buys", "sells"] = "sells", rule: str | None = None, code: str | None = None,
                limit: int = Query(500, ge=1, le=2000), offset: int = Query(0, ge=0), conn=Depends(conn_dep)):
    principal(request)
    data = R.paper_fills(_svc(conn), session_date, kind, rule=rule, code=code, limit=limit, offset=offset)
    if data["total"] == 0 and not _svc(conn).decided(R.DS["PAPER_DAYS"]) & {session_date.isoformat()}:
        raise ApiError(404, "NOT_FOUND", "该交易日没有纸面回放记录")
    return respond_backfill(LANE, "PaperFillsData", data, meta(quality_status="partial", coverage=_coverage(data["total"]), warnings=[WARNING]))
