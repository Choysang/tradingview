"""Operational surface: which providers exist, and how complete the snapshot is."""
from fastapi import APIRouter

from contracts.schema import ProvidersResponse, QualityResponse
from ..core.engine import engine

router = APIRouter(tags=["ops"])


@router.get("/providers", response_model=ProvidersResponse)
def providers() -> ProvidersResponse:
    return ProvidersResponse(providers=engine().providers())


@router.get("/quality", response_model=QualityResponse)
def quality() -> QualityResponse:
    return engine().quality()
