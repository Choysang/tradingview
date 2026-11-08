"""Operational endpoints: health, data-mode switch, machine-readable manifest.

The manifest is what an external agent reads first. It is generated from the
capability registry, which also feeds the MCP tools and /llms.txt.
"""
from fastapi import APIRouter

from contracts.schema import (
    AgentManifest,
    DataProvenance,
    HealthResponse,
    ModeSwitchRequest,
)
from ..agent.capabilities import CAPABILITIES
from ..config import settings
from ..core.engine import engine

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", version=settings.version, provenance=engine().provenance())


@router.post("/admin/mode", response_model=DataProvenance)
def switch_mode(payload: ModeSwitchRequest) -> DataProvenance:
    return engine().set_mode(payload.mode)


@router.get("/agent/manifest", response_model=AgentManifest)
def manifest() -> AgentManifest:
    return AgentManifest(version=settings.version, tools=[capability.definition() for capability in CAPABILITIES])
