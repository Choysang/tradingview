"""Crowd snapshot: industries and name clusters moving as a pack, Dragon-Tiger seats."""
from fastapi import APIRouter

from contracts.schema import CrowdSnapshot
from ..playbooks import crowd_view

router = APIRouter(tags=["crowd"])


@router.get("/crowd", response_model=CrowdSnapshot)
def crowd_snapshot() -> CrowdSnapshot:
    return crowd_view.snapshot()
