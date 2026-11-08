"""Daily picks, report archive and track record."""
from fastapi import APIRouter, HTTPException

from contracts.schema import DailyReport, ReportIndex, TrackRecordResponse
from ..playbooks import picks

router = APIRouter(tags=["picks"])


@router.get("/picks/today", response_model=DailyReport)
def picks_today() -> DailyReport:
    return picks.generate_report()


@router.post("/picks/generate", response_model=DailyReport)
def picks_generate() -> DailyReport:
    return picks.generate_report(force=True)


# Fixed paths first: "/picks/{date}" would otherwise swallow them.
@router.get("/picks/history", response_model=ReportIndex)
def picks_history() -> ReportIndex:
    return picks.report_index()


@router.get("/picks/track-record", response_model=TrackRecordResponse)
def picks_track_record() -> TrackRecordResponse:
    return picks.track_record()


@router.get("/picks/{trade_date}", response_model=DailyReport)
def picks_by_date(trade_date: str) -> DailyReport:
    found = picks.report(trade_date)
    if found is None:
        raise HTTPException(status_code=404, detail=f"no report for {trade_date}")
    return found
