"""User-asset operations: getLayout, saveLayout, listWatchlists, saveWatchlist, saveAnnotation, listAnnotations.

Writes are idempotent by (principal, operation, Idempotency-Key) through
ops.command_receipt; versions are optimistic (409 VERSION_CONFLICT).
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, Request

from pulse import contracts
from pulse.adapters.postgres.commands import PgCommandStore
from pulse.adapters.postgres.ui_repo import NotFound, PgUiRepo, VersionConflict
from pulse.api.auth import principal
from pulse.api.deps import conn_dep
from pulse.api.envelope import ApiError, meta, respond
from pulse.application.bootstrap import SCHEMA_ID
from pulse.application.ports.errors import Conflict

router = APIRouter(prefix="/v1")
TTL = timedelta(days=7)


def _command(conn, pid: UUID, op: str, key: str, body: dict, handler) -> Any:
    try:
        res = PgCommandStore(conn).execute(
            principal_id=pid, operation_id=op, idempotency_key=key, request=body,
            response_contract_id=SCHEMA_ID, response_schema_version=contracts.CONTRACT_VERSION,
            handler=lambda c: handler(), ttl=TTL)
    except Conflict as e:
        raise ApiError(409, "IDEMPOTENCY_CONFLICT", str(e)) from e
    except VersionConflict as e:
        raise ApiError(409, "VERSION_CONFLICT", str(e)) from e
    return res.response


@router.get("/workspace-layouts/{layout_id}")
def get_layout(layout_id: UUID, request: Request, conn=Depends(conn_dep)):
    try:
        data = PgUiRepo(conn).get_layout(principal(request), layout_id)
    except NotFound as e:
        raise ApiError(404, "NOT_FOUND", "布局不存在") from e
    return respond("getLayout", data, meta())


@router.put("/workspace-layouts/{layout_id}")
def save_layout(layout_id: UUID, request: Request, body: dict = Body(...),
                idempotency_key: str = Header(..., alias="Idempotency-Key"), conn=Depends(conn_dep)):
    _validate("LayoutUpdate", body)
    pid = principal(request)
    data = _command(conn, pid, "saveLayout", idempotency_key, body,
                    lambda: PgUiRepo(conn).save_layout(pid, layout_id, body))
    return respond("saveLayout", data, meta())


@router.get("/watchlists")
def list_watchlists(request: Request, conn=Depends(conn_dep)):
    return respond("listWatchlists", {"items": PgUiRepo(conn).list_watchlists(principal(request))}, meta())


@router.put("/watchlists/{watchlist_id}")
def save_watchlist(watchlist_id: UUID, request: Request, body: dict = Body(...),
                   idempotency_key: str = Header(..., alias="Idempotency-Key"), conn=Depends(conn_dep)):
    _validate("WatchlistUpdate", body)
    pid = principal(request)
    data = _command(conn, pid, "saveWatchlist", idempotency_key, body,
                    lambda: PgUiRepo(conn).save_watchlist(pid, watchlist_id, body))
    return respond("saveWatchlist", data, meta())


@router.put("/annotations/{annotation_id}")
def save_annotation(annotation_id: UUID, request: Request, body: dict = Body(...),
                    idempotency_key: str = Header(..., alias="Idempotency-Key"), conn=Depends(conn_dep)):
    _validate("AnnotationUpdate", body)
    pid = principal(request)
    data = _command(conn, pid, "saveAnnotation", idempotency_key, body,
                    lambda: PgUiRepo(conn).save_annotation(pid, annotation_id, body))
    return respond("saveAnnotation", data, meta())


@router.get("/annotations")
def list_annotations(request: Request, instrument_id: UUID | None = None, watchlist_id: UUID | None = None,
                     cursor: str | None = None, conn=Depends(conn_dep)):
    items = PgUiRepo(conn).list_annotations(principal(request), instrument_id, watchlist_id)
    return respond("listAnnotations", {"items": items}, meta())


def _validate(defn: str, body: dict) -> None:
    try:
        contracts.check(defn, body)
    except contracts.ContractViolation as e:
        raise ApiError(400, "CONTRACT_VIOLATION", "请求不符合合同",
                       field_errors=[{"path": err.split(":")[0] or "/", "code": "invalid", "message": err}
                                     for err in e.errors]) from e
