"""Board lane reads (docs/LEGACY_TRIAGE.md #38/#39, #30, #50). Contract: ``backfill_board.schema.json``.

``GET /v1/x/bf/board/watch-cards``  release-pinned cards for a batch of instruments (price, change, sparkline, amount, rank).
``GET /v1/x/bf/board/theme-map``    per-sector amount estimate and average change of the release's membership snapshot.
``GET /v1/x/bf/board/instrument-search``  code prefix / name contains, for the command palette.
Release-dependent reads serve only READY / SUPERSEDED releases (404 otherwise); routers translate HTTP only.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request

from pulse import config
from pulse.adapters.lake import LakeStore
from pulse.api.auth import principal
from pulse.api.deps import conn_dep
from pulse.api.envelope import ApiError, meta, respond_backfill
from pulse.api.routes_ext_a import public_release, release_meta
from pulse.application import bf_board_service as S

router = APIRouter(prefix="/v1/x/bf/board")
LANE = "board"


def _bad(e: S.BoardError) -> ApiError:
    return ApiError(e.status, e.code, e.message)


def _coverage(expected: int, observed: int, reasons: list[str]) -> dict:
    return {"policy_version": "bf-board-v1", "expected_count": expected, "observed_count": observed,
            "missing_count": max(expected - observed, 0), "unverifiable_reasons": reasons}


@router.get("/watch-cards")
def watch_cards(release_id: UUID, ids: str, request: Request, horizon_id: str = "short_term", forecast_target: str = "excess_return_5d",
                conn=Depends(conn_dep)):
    principal(request)
    row = public_release(conn, release_id)
    raw = [x for x in ids.split(",") if x]
    try:
        idl = [str(UUID(x)) for x in raw]
    except ValueError as e:
        raise ApiError(400, "INVALID_CONTRACT", "ids 含非法编号") from e
    try:
        data = S.watch_cards(conn, row, idl, horizon_id, forecast_target)
    except S.BoardError as e:
        raise _bad(e) from e
    cards = data["cards"]
    got = sum(1 for c in cards if c["last_close"] is not None)
    reasons = sorted({str(c["pct_missing_reason"]) for c in cards if c["pct_missing_reason"]})
    return respond_backfill(LANE, "WatchCards", data, release_meta(conn, row, coverage=_coverage(len(cards), got, reasons)))


@router.get("/theme-map")
def theme_map(release_id: UUID, taxonomy_id: str, taxonomy_version: int, request: Request, conn=Depends(conn_dep)):
    principal(request)
    row = public_release(conn, release_id)
    try:
        data = S.theme_map(conn, LakeStore(config.load().lake_root), row, taxonomy_id, taxonomy_version)
    except S.BoardError as e:
        raise _bad(e) from e
    nodes = data["nodes"]
    got = sum(1 for n in nodes if n["amount_estimate_cny"] is not None)
    return respond_backfill(LANE, "ThemeMap", data, release_meta(conn, row, coverage=_coverage(len(nodes), got, ["amount_is_estimate"])))


@router.get("/instrument-search")
def instrument_search(q: str, request: Request, limit: int = 12, conn=Depends(conn_dep)):
    principal(request)
    if len(q) > 40:
        raise ApiError(400, "INVALID_CONTRACT", "搜索词过长")
    data = S.search_instruments(conn, q, max(1, min(limit, S.SEARCH_LIMIT)))
    return respond_backfill(LANE, "InstrumentSearch", data, meta(coverage=_coverage(len(data["items"]), len(data["items"]), [])))
