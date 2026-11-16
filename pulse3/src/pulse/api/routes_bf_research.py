"""Backfill lane ``research`` reads: playbook replay (equity + trades + regime attribution), playbook x regime heatmap, experiment archive.

Contract: ``backfill_research.schema.json`` (definitions PlaybookReplay, RegimeHeatmap, ExperimentArchive); every response leaves through
``respond_backfill`` and is validated first. Routers translate HTTP only; the logic is in ``application.bf_research_service``.
GET /v1/x/bf/research/playbook-replay?catalog_id=PB:<id>|experiment_id=<uuid>[&trades_limit=50]
GET /v1/x/bf/research/regime-heatmap
GET /v1/x/bf/research/experiments?instance_id=&purpose=&status=&limit=50&cursor=
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request

from pulse.api.auth import principal
from pulse.api.deps import conn_dep
from pulse.api.envelope import ApiError, meta, respond_backfill
from pulse.application import bf_research_service as S

router = APIRouter(prefix="/v1/x/bf/research")
LANE = "research"
MAX_TRADES = 500


def _uuid(raw: str | None, name: str) -> UUID | None:
    if raw is None or raw == "":
        return None
    try:
        return UUID(raw)
    except ValueError as e:
        raise ApiError(400, "INVALID_CONTRACT", f"{name} 不是合法的 UUID") from e


def _warn(code: str, message: str, ref: str | None = None) -> dict:
    return {"code": code, "message": message, "affected_component": "bf_research", "evidence_ref": ref}


@router.get("/playbook-replay")
def playbook_replay(request: Request, catalog_id: str | None = None, experiment_id: str | None = None, trades_limit: int = 50, conn=Depends(conn_dep)):
    principal(request)
    eid = _uuid(experiment_id, "experiment_id")
    if (eid is None) == (not catalog_id):
        raise ApiError(400, "INVALID_CONTRACT", "catalog_id 与 experiment_id 必须且只能给一个")
    if not 1 <= trades_limit <= MAX_TRADES:
        raise ApiError(400, "INVALID_CONTRACT", f"trades_limit 必须在 1 到 {MAX_TRADES} 之间")
    stored = S.load_replay(conn, experiment_id=eid) if eid is not None else S.load_replay(conn, catalog_id=catalog_id)
    if stored is None:
        raise ApiError(404, "NOT_FOUND", "还没有这个战法的回放结果：运行 ext-b-run 后才有净值曲线和成交明细")
    data = S.replay_view(stored, trades_limit)
    warnings = []
    if not data["attribution"]["regime"]["available"]:
        warnings.append(_warn("REGIME_UNAVAILABLE", f"该实验运行时没有可用的市场环境（{data['attribution']['regime']['reason']}），不做环境归因", data["experiment"]["artifact_ref"]))
    return respond_backfill(LANE, "PlaybookReplay", data, meta(quality_status="partial", warnings=warnings))


@router.get("/regime-heatmap")
def regime_heatmap(request: Request, conn=Depends(conn_dep)):
    principal(request)
    data = S.heatmap_view(conn)
    return respond_backfill(LANE, "RegimeHeatmap", data, meta(quality_status="partial"))


@router.get("/experiments")
def experiments(request: Request, instance_id: str | None = None, purpose: str | None = None, status: str | None = None, limit: int = 50,
                cursor: str | None = None, conn=Depends(conn_dep)):
    principal(request)
    try:
        data = S.archive(conn, instance_id=_uuid(instance_id, "instance_id"), purpose=purpose or None, status=status or None, limit=limit, cursor=cursor or None)
    except S.ResearchReadError as e:
        raise ApiError(e.status, e.code, e.message) from e
    return respond_backfill(LANE, "ExperimentArchive", data, meta(next_cursor=data["page"]["next_cursor"]))
