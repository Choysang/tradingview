"""Ranking, sector, candidate pool, trade plan and actionability operations.

Every list is pinned: the cursor carries ranking_id/actionability_id and the
last rank, so page 2 never comes from a different (re-sorted) run
(FINAL_SYSTEM_SPEC §6, §11).
"""
from __future__ import annotations

import base64
import os
import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from pulse.adapters.postgres import decision_repo as repo
from pulse.adapters.postgres.release import PgAnalysisRelease
from pulse.api.auth import principal
from pulse.api.deps import conn_dep
from pulse.api.envelope import ApiError, meta, respond
from pulse.application.ports.errors import NotFound

router = APIRouter(prefix="/v1")
PAGE = 50


def _enc(doc: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(doc, separators=(",", ":")).encode()).decode().rstrip("=")


def _dec(cursor: str) -> dict:
    try:
        return json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except Exception as e:  # malformed cursor is a client error, never a silent restart
        raise ApiError(400, "CURSOR_INVALID", "游标无效") from e


def _release_meta(conn, release_id: UUID, **kw) -> dict:
    try:
        view = PgAnalysisRelease(conn).get_release(release_id)
    except NotFound as e:
        raise ApiError(404, "NOT_FOUND", "发布不存在") from e
    if view.release_state not in ("READY", "SUPERSEDED"):
        raise ApiError(404, "NOT_FOUND", "发布尚未公开")
    return meta(release_id=release_id, decision_as_of=view.decision_as_of,
                availability_policy_version=view.availability_policy_version,
                freshness_state="current" if view.release_state == "READY" else "stale", **kw)


def _ranking_page(conn, ranking_id: UUID, cursor: str | None, release_id: UUID) -> tuple[dict, str | None]:
    after = 0
    if cursor:
        doc = _dec(cursor)
        if doc.get("r") != str(ranking_id):
            raise ApiError(409, "CURSOR_RELEASE_MISMATCH", "游标属于另一个排序，需要重新加载")
        after = int(doc["a"])
    data = repo.load_ranking_wire(conn, ranking_id, after_alpha_rank=after, limit=PAGE + 1)
    nxt = None
    if len(data["entries"]) > PAGE:
        data["entries"] = data["entries"][:PAGE]
        nxt = _enc({"r": str(ranking_id), "rel": str(release_id), "a": data["entries"][-1]["alpha_rank"]})
    return data, nxt


def _find_run(conn, release_id: UUID, scope: str, horizon_id: str, target: str, **extra: Any) -> UUID:
    sql = ("select ranking_id from decision.ranking_run where release_id=%s and ranking_scope=%s"
           " and horizon_id=%s and forecast_target=%s")
    args: list[Any] = [release_id, scope, horizon_id, target]
    for k, v in extra.items():
        sql += f" and {k}=%s"
        args.append(v)
    row = conn.execute(sql + " order by created_at desc limit 1", args).fetchone()
    if row is None:
        raise ApiError(404, "NOT_FOUND", "该发布中没有这个排序")
    return row["ranking_id"]


@router.get("/rankings")
def get_market_ranking(release_id: UUID, horizon_id: str, forecast_target: str, cursor: str | None = None,
                       conn=Depends(conn_dep)):
    m = _release_meta(conn, release_id)
    rid = _find_run(conn, release_id, "market", horizon_id, forecast_target)
    data, nxt = _ranking_page(conn, rid, cursor, release_id)
    m["next_cursor"] = nxt
    return respond("getMarketRanking", data, m)


@router.get("/sectors/rankings")
def get_sector_overview(release_id: UUID, taxonomy_id: str, taxonomy_version: int, rank_kind: str,
                        horizon_id: str, forecast_target: str, cursor: str | None = None, conn=Depends(conn_dep)):
    m = _release_meta(conn, release_id)
    rid = _find_run(conn, release_id, "sector_overview", horizon_id, forecast_target,
                    taxonomy_id=taxonomy_id, taxonomy_version=taxonomy_version)
    data = repo.load_sector_overview_wire(conn, rid)
    data["entries"] = [e for e in data["entries"] if e["rank_kind"] == rank_kind]
    return respond("getSectorOverview", data, m)


@router.get("/sectors/{node_id}/rankings")
def get_sector_ranking(node_id: str, release_id: UUID, taxonomy_id: str, taxonomy_version: int, horizon_id: str,
                       forecast_target: str, cursor: str | None = None, conn=Depends(conn_dep)):
    m = _release_meta(conn, release_id)
    rid = _find_run(conn, release_id, "sector", horizon_id, forecast_target, taxonomy_id=taxonomy_id,
                    taxonomy_version=taxonomy_version, node_id=node_id)
    data, nxt = _ranking_page(conn, rid, cursor, release_id)
    m["next_cursor"] = nxt
    return respond("getSectorRanking", data, m)


@router.get("/candidate-pools/{pool_id}")
def get_candidate_pool(pool_id: UUID, release_id: UUID, cursor: str | None = None, conn=Depends(conn_dep)):
    m = _release_meta(conn, release_id)
    try:
        data = repo.load_pool_wire(conn, pool_id, release_id)
    except KeyError as e:
        raise ApiError(404, "NOT_FOUND", "候选池不存在") from e
    return respond("getCandidatePool", data, m)


def _viewer(request: Request) -> UUID | None:
    """Optional session (getTradePlan security is [{}, sessionAuth]); an invalid cookie is still 403."""
    if request.cookies.get("pulse_session") or os.environ.get("PULSE_DEV_PRINCIPAL"):
        return principal(request)
    return None


_VISIBLE = ("(m.origin IS NULL OR m.origin='system' OR m.principal_id = %(viewer)s)")


@router.get("/plans/{plan_id}/versions/{plan_version}")
def get_trade_plan(plan_id: UUID, plan_version: int, request: Request, conn=Depends(conn_dep)):
    """Public release plans for everyone; a user-edited version only for the principal who made it."""
    viewer = _viewer(request)
    args = {"plan_id": plan_id, "plan_version": plan_version, "viewer": viewer}
    meta_join = (" left join decision.plan_revision_metadata m on m.plan_id=t.plan_id and m.plan_version=t.plan_version"
                 if _has_plan_metadata(conn) else " left join (select null::uuid plan_id, null::int plan_version,"
                 " null::text origin, null::uuid principal_id) m on false")
    row = conn.execute("select t.plan_payload, t.based_on_release_id, r.release_state from decision.trade_plan t"
                       " join decision.release r on r.release_id=t.based_on_release_id" + meta_join +
                       " where t.plan_id=%(plan_id)s and t.plan_version=%(plan_version)s and " + _VISIBLE, args).fetchone()
    if row is None or row["release_state"] not in ("READY", "SUPERSEDED"):      # a failed/unpublished build is not served
        raise ApiError(404, "NOT_FOUND", "计划版本不存在")
    latest = conn.execute("select max(t.plan_version) v from decision.trade_plan t join decision.release r"
                          " on r.release_id=t.based_on_release_id" + meta_join +
                          " where t.plan_id=%(plan_id)s and r.release_state in ('READY','SUPERSEDED') and " + _VISIBLE,
                          args).fetchone()
    warnings = []
    if latest["v"] > plan_version:
        warnings.append({"code": "PLAN_SUPERSEDED", "message": f"已有更新的计划版本 {latest['v']}",
                         "affected_component": "plan", "evidence_ref": None})
    return respond("getTradePlan", row["plan_payload"],
                   meta(release_id=row["based_on_release_id"], warnings=warnings,
                        freshness_state="stale" if warnings else "current"))


def _has_plan_metadata(conn) -> bool:
    return conn.execute("select to_regclass('decision.plan_revision_metadata') is not null as ok").fetchone()["ok"]


@router.get("/accounts/{account_id}/actionability/{actionability_id}")
def get_account_actionability(account_id: UUID, actionability_id: UUID, request: Request,
                              cursor: str | None = None, conn=Depends(conn_dep)):
    principal(request)
    data = repo.load_actionability(conn, actionability_id)
    if data is None or str(data.get("account_id")) != str(account_id):
        raise ApiError(404, "NOT_FOUND", "可行动快照不存在")
    return respond("getAccountActionability", data,
                   meta(release_id=data["release_id"], actionability_id=actionability_id))
