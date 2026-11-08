"""Market event tape: seals, breaks and fast moves derived from snapshot diffs."""
from typing import Annotated

from fastapi import APIRouter, Query

from contracts.schema import MarketEventsResponse
from ..core.engine import engine

router = APIRouter(tags=["events"])


@router.get("/market/events", response_model=MarketEventsResponse)
def market_events(
    since: Annotated[
        int | None, Query(ge=0, description="Last sequence already seen; only newer events are returned.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=300)] = 100,
) -> MarketEventsResponse:
    # Annotated (not `= Query(...)`) so the capability registry can call this directly.
    return engine().events(since, limit)
