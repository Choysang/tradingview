"""Live lane reads (docs/LEGACY_TRIAGE.md #5/#124). Contract: ``backfill_live.schema.json``.

``GET /v1/x/bf/live/seal-clock?session_date=``  first-seal histogram and cumulative curve of one session (latest when omitted).
``GET /v1/x/bf/live/seal-facts?instrument_id=&session_date=``  first/last seal, broken count, close seal amount of one stock.
Both serve the READY ``intraday_seal_facts`` dataset built from the imported recorder snapshots; every figure is estimated from
60 s snapshots and says so. A session without published facts is a 404 for the clock and ``status: no_dataset`` for the stock read.
"""
from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from pulse import config
from pulse.adapters.lake import LakeStore
from pulse.api.auth import principal
from pulse.api.deps import conn_dep
from pulse.api.envelope import ApiError, meta, respond_backfill
from pulse.application.bf_live_service import LiveService

router = APIRouter(prefix="/v1/x/bf/live")
LANE = "live"
WARNING = {"code": "ESTIMATED_FROM_60S_SNAPSHOTS", "message": "封板时间等数字由约 60 秒一次的快照估算，不是逐笔数据",
           "affected_component": "intraday_seal_facts", "evidence_ref": None}


def _svc(conn) -> LiveService:
    return LiveService(conn, LakeStore(config.load().lake_root))


def _coverage(observed: int) -> dict:
    return {"policy_version": "bf-live-v1", "expected_count": None, "observed_count": observed, "missing_count": None,
            "unverifiable_reasons": ["estimated_from_60s_snapshots"]}


def _as_of(data: dict):
    src = data.get("source")
    return src["available_at"] if src else None


def _pin(conn, release_id: UUID | None, session_date: date | None):
    """A release pins the read: its session and its decision_as_of cutoff (only READY/SUPERSEDED releases)."""
    if release_id is None:
        return session_date, None
    from pulse.adapters.lake import LakeStore
    from pulse.api.routes_decision import _release_meta
    from pulse.application.market_today_service import release_session_date
    from pulse import config

    m = _release_meta(conn, release_id)
    sess = session_date or release_session_date(conn, LakeStore(config.load().lake_root), release_id)
    return sess, m["decision_as_of"]


@router.get("/seal-clock")
def seal_clock(request: Request, session_date: date | None = None, bucket_minutes: int = 5, release_id: UUID | None = None,
               conn=Depends(conn_dep)):
    principal(request)
    if bucket_minutes not in (1, 2, 3, 5, 10, 15, 30):
        raise ApiError(400, "INVALID_CONTRACT", "bucket_minutes 必须能整除 60 且不超过 30")
    session_date, cutoff = _pin(conn, release_id, session_date)
    data = _svc(conn).seal_clock(session_date, bucket_minutes, cutoff)
    if data is None:
        raise ApiError(404, "NOT_FOUND", "该交易日没有封板事实（尚未导入盘中记录）")
    return respond_backfill(LANE, "SealClock", data, meta(decision_as_of=_as_of(data), quality_status="partial", coverage=_coverage(data["instruments_observed"]),
                                                          warnings=[WARNING]))


@router.get("/seal-facts")
def seal_facts(instrument_id: UUID, request: Request, session_date: date | None = None, release_id: UUID | None = None,
               conn=Depends(conn_dep)):
    principal(request)
    session_date, cutoff = _pin(conn, release_id, session_date)
    data = _svc(conn).seal_fact(instrument_id, session_date, cutoff)
    ok = data["status"] == "ok"
    return respond_backfill(LANE, "SealFacts", data, meta(decision_as_of=_as_of(data), quality_status="partial" if ok else "valid",
                                                          coverage=_coverage(1 if ok else 0), warnings=[WARNING] if ok else None))
