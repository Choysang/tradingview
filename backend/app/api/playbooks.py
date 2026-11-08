"""Playbook board, evaluation and custom runs."""
from fastapi import APIRouter, HTTPException

from contracts.schema import (
    ExitLabReport, PlaybookBoard, PlaybookEvaluation, PlaybookExitStudy, PlaybookRunRequest, RotationReport,
)
from ..playbooks import evaluate, exits, rotation

router = APIRouter(tags=["playbooks"])


@router.get("/playbooks", response_model=PlaybookBoard)
def playbook_board() -> PlaybookBoard:
    return evaluate.board()


@router.get("/playbooks/rotation", response_model=RotationReport)
def playbook_rotation() -> RotationReport:
    """Walk-forward election of the playbooks that work now, and whether following it pays."""
    return rotation.report()


@router.get("/playbooks/exits", response_model=ExitLabReport)
def playbook_exit_lab() -> ExitLabReport:
    """Every playbook's sell-rule comparison: chosen in-sample, checked out-of-sample."""
    return exits.report()


@router.get("/playbooks/{playbook_id}/exits", response_model=PlaybookExitStudy)
def playbook_exits(playbook_id: str) -> PlaybookExitStudy:
    try:
        return exits.playbook_study(playbook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown playbook: {playbook_id}") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.get("/playbooks/{playbook_id}", response_model=PlaybookEvaluation)
def playbook_detail(playbook_id: str) -> PlaybookEvaluation:
    try:
        return evaluate.evaluate(playbook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown playbook: {playbook_id}") from None


@router.post("/playbooks/{playbook_id}/run", response_model=PlaybookEvaluation)
def playbook_run(playbook_id: str, request: PlaybookRunRequest) -> PlaybookEvaluation:
    """Custom parameters, exits or sizing; the result is computed fresh and never cached."""
    try:
        return evaluate.evaluate(playbook_id, request)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown playbook: {playbook_id}") from None
