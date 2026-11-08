"""Market structure endpoints: overview aggregates and single-stock detail."""
from fastapi import APIRouter, HTTPException, Query

from contracts.schema import AnalogForecast, MacroDashboardSummary, StockDetailResponse
from ..core.engine import engine
from ..research.indicators import compute
from ..research.patterns import match

router = APIRouter(tags=["market"])


@router.get("/market/overview", response_model=MacroDashboardSummary)
def overview() -> MacroDashboardSummary:
    return engine().overview()


@router.get("/stocks/{code}", response_model=StockDetailResponse)
def stock_detail(code: str) -> StockDetailResponse:
    if not (len(code) == 6 and code.isdigit()):
        raise HTTPException(status_code=422, detail="股票代码必须是 6 位数字")
    market = engine()
    record = market.stock(code)
    if record is None:
        raise HTTPException(status_code=404, detail=f"当前快照中没有 {code}")
    anchor = market.price_of(code)
    bars, history_provenance = (
        market.history.synthetic(code, anchor)
        if market.provenance().mode == "SIM"
        else market.history.bars(code, anchor)
    )
    # Real bars let a live snapshot show the 60-day high and yearly limit-up
    # count the quote cannot carry; only derived fields are touched.
    if history_provenance.mode == "LIVE":
        from ..research.metrics import derive

        record = record.model_copy(update=derive(code, bars, record.price))
    return StockDetailResponse(
        provenance=market.provenance(),
        history_provenance=history_provenance,
        stock=record,
        bars=bars,
        observations=market.observations(record),
        indicators=compute(bars),
        patterns=match(code, record.name, bars),
    )


@router.get("/stocks/{code}/analogs", response_model=AnalogForecast)
def stock_analogs(code: str, horizon: int = Query(20, ge=5, le=60)) -> AnalogForecast:
    """What happened after historically similar setups (nearest-neighbour distribution)."""
    if not (len(code) == 6 and code.isdigit()):
        raise HTTPException(status_code=422, detail="股票代码必须是 6 位数字")
    from ..research.analogs import forecast

    try:
        return forecast(code, horizon)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
