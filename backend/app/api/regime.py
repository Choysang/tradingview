"""Market regime history: trend, liquidity, sentiment and style."""
from fastapi import APIRouter, HTTPException, Query

from contracts.schema import RegimeHistoryResponse
from ..playbooks.regime import regime_history
from ..warehouse.store import active

router = APIRouter(tags=["regime"])


@router.get("/regime", response_model=RegimeHistoryResponse)
def regime(days: int = Query(500, ge=60, le=2000)) -> RegimeHistoryResponse:
    """Daily trend / liquidity / sentiment-cycle / style labels with the rules that produced them."""
    try:
        return regime_history(active(), days)
    except ValueError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
