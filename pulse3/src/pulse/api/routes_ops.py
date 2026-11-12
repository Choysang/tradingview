"""Release, update and job operations: getCurrentRelease, getRelease, pollUpdates, getJobRun.

Releases are read only through the slot pointer or an explicit release_id;
only READY or previously published SUPERSEDED releases are public.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request

from pulse import contracts
from pulse.adapters.postgres.jobs import PgJobStore
from pulse.adapters.postgres.outbox import PgOutbox, stream_name_for, to_update_page
from pulse.adapters.postgres.release import PgAnalysisRelease
from pulse.api.auth import principal
from pulse.api.deps import conn_dep
from pulse.api.envelope import ApiError, meta, respond
from pulse.application.ports.errors import CursorInvalid, NotFound

router = APIRouter(prefix="/v1")
PUBLIC_STATES = {"READY", "SUPERSEDED"}


@router.get("/releases/current")
def get_current_release(slot_key: str, conn=Depends(conn_dep)):
    row = conn.execute(
        "select slot_key, current_release_id, pointer_generation, issued_stage_generation,"
        " published_stage_generation, updated_at from decision.release_slot where slot_key=%s", (slot_key,)).fetchone()
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"发布槽 {slot_key} 不存在")
    return respond("getCurrentRelease", dict(row), meta(release_id=row["current_release_id"]))


def release_wire(view) -> dict:
    return {
        "release_id": view.release_id, "release_state": view.release_state, "decision_as_of": view.decision_as_of,
        "availability_policy_version": view.availability_policy_version,
        "universe_policy_version": view.universe_policy_version, "market_rules_version": view.market_rules_version,
        "required_components": list(view.required_components), "release_hash": view.release_hash,
        "published_at": view.published_at,
        "components": [{
            "component_key": c.component_key, "component_state": c.component_state,
            "artifact": {"ref": c.artifact_ref, "content_hash": c.artifact_hash or c.input_version_hash,
                         "contract_id": "urn:pulse:contracts:v3", "schema_version": contracts.CONTRACT_VERSION},
            "snapshot_id": c.snapshot_id, "input_version_hash": c.input_version_hash,
            "computed_at": c.computed_at, "valid_until": c.valid_until} for c in view.components],
    }


@router.get("/releases/{release_id}")
def get_release(release_id: UUID, conn=Depends(conn_dep)):
    try:
        view = PgAnalysisRelease(conn).get_release(release_id)
    except NotFound as e:
        raise ApiError(404, "NOT_FOUND", "发布不存在") from e
    if view.release_state not in PUBLIC_STATES:
        raise ApiError(404, "NOT_FOUND", "发布尚未公开")
    return respond("getRelease", release_wire(view),
                   meta(release_id=view.release_id, decision_as_of=view.decision_as_of,
                        availability_policy_version=view.availability_policy_version,
                        freshness_state="current" if view.release_state == "READY" else "stale"))


@router.get("/updates")
def poll_updates(slot_key: str, cursor: str | None = None, conn=Depends(conn_dep)):
    try:
        result = PgOutbox(conn).poll(stream_name_for("release_slot", slot_key), cursor)
    except CursorInvalid as e:
        raise ApiError(400, "CURSOR_INVALID", str(e)) from e
    return respond("pollUpdates", to_update_page(result), meta())


@router.get("/jobs/{job_run_id}")
def get_job_run(job_run_id: UUID, request: Request, conn=Depends(conn_dep)):
    pid = principal(request)
    store = PgJobStore(conn)
    try:
        data = store.job_run_wire(job_run_id)
    except NotFound as e:
        raise ApiError(404, "NOT_FOUND", "作业不存在") from e
    initiators = store.initiators(job_run_id)
    if initiators and pid not in initiators:      # a job started by a command belongs to the principal that issued it
        raise ApiError(404, "NOT_FOUND", "作业不存在")
    return respond("getJobRun", data, meta())
