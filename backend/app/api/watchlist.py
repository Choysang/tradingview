"""Watchlist persistence. Order is preserved so the sidebar stays stable."""
from fastapi import APIRouter

from contracts.schema import WatchlistRequest, WatchlistResponse
from ..core.storage import storage

router = APIRouter(tags=["watchlist"])


@router.get("/watchlist", response_model=WatchlistResponse)
def read_watchlist() -> WatchlistResponse:
    codes = storage.load_watchlist()
    return WatchlistResponse(codes=codes, updated_at=storage.watchlist_updated_at())


@router.put("/watchlist", response_model=WatchlistResponse)
def write_watchlist(payload: WatchlistRequest) -> WatchlistResponse:
    codes, stamp = storage.save_watchlist(payload.codes)
    return WatchlistResponse(codes=codes, updated_at=stamp)
