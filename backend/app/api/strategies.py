"""Strategy catalogue: screening presets plus their backtest signal family."""
from fastapi import APIRouter

from contracts.schema import StrategyCatalog
from ..research.strategies import catalog

router = APIRouter(tags=["research"])


@router.get("/strategies", response_model=StrategyCatalog)
def strategies() -> StrategyCatalog:
    return catalog()
