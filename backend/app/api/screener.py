"""Screener endpoint. Filtering is one Polars pass over the in-memory frame."""
from fastapi import APIRouter

from contracts.schema import (
    ParseRequest,
    ParseResponse,
    ScreenerFilterRequest,
    ScreenerQueryResponse,
)
from ..core.engine import engine
from ..query.nlq import parse

router = APIRouter(tags=["screener"])


@router.post("/screener/query", response_model=ScreenerQueryResponse)
def query(filters: ScreenerFilterRequest) -> ScreenerQueryResponse:
    return engine().query(filters)


@router.post("/screener/parse", response_model=ParseResponse)
def parse_query(request: ParseRequest) -> ParseResponse:
    """Natural-language text to filters. Deterministic: no model is involved."""
    return parse(request.text, engine().theme_names())
