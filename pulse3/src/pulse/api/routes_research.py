"""Strategy registry and research operations: listStrategies, listPapers, createExperiment, getEvaluation, getLeague.

createExperiment only enqueues a job (JobReceipt); results arrive through the
research worker and the same evaluation contract. League rank never sets weights.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, Request

from pulse import contracts
from pulse.adapters.postgres import research_repo
from pulse.adapters.postgres.commands import PgCommandStore
from pulse.api.auth import principal
from pulse.api.deps import conn_dep
from pulse.api.envelope import ApiError, meta, respond
from pulse.application.bootstrap import SCHEMA_ID
from pulse.application.ports.errors import Conflict

router = APIRouter(prefix="/v1")
RESEARCH_JOB = UUID("3a6c0f7e-2d1b-4e8a-9c55-7b0e4f1a2c10")


@router.get("/strategies")
def list_strategies(role: str | None = None, horizon_id: str | None = None, method_family: str | None = None,
                    lifecycle: str | None = None, cursor: str | None = None, conn=Depends(conn_dep)):
    sql = "select * from strategy.definition where true"
    args: list[Any] = []
    for col, val in (("role", role), ("horizon_id", horizon_id), ("method_family", method_family)):
        if val:
            sql += f" and {col}=%s"
            args.append(val)
    defs = conn.execute(sql + " order by display_name, strategy_version", args).fetchall()
    inst, tags = defaultdict(list), defaultdict(list)
    for i in conn.execute("select * from strategy.instance order by created_at"):
        inst[(i["strategy_id"], i["strategy_version"])].append(i)
    for t in conn.execute("select * from strategy.tag_assignment"):
        tags[(t["strategy_id"], t["strategy_version"])].append(t)
    items = []
    for d in defs:
        key = (d["strategy_id"], d["strategy_version"])
        insts = [i for i in inst[key] if not lifecycle or i["lifecycle"] == lifecycle]
        if lifecycle and not insts:
            continue
        items.append({
            "strategy_id": d["strategy_id"], "strategy_version": d["strategy_version"],
            "display_name": d["display_name"], "role": d["role"], "method_family": d["method_family"],
            "forecast_target": d["forecast_target"], "horizon_id": d["horizon_id"],
            "definition_hash": d["definition_hash"], "specification": d["specification"],
            "tags": [{"taxonomy_id": t["taxonomy_id"], "taxonomy_version": t["taxonomy_version"],
                      "node_id": t["node_id"]} for t in tags[key]],
            "instances": [{k: i[k] for k in ("instance_id", "strategy_id", "strategy_version", "parameter_hash",
                                              "universe_policy_version", "lifecycle", "implementation_status",
                                              "reproduction_status", "health")} for i in insts],
        })
    return respond("listStrategies", {"items": items}, meta())


@router.get("/research/papers")
def list_papers(publication_year: int | None = None, publication_status: str | None = None,
                read_depth: str | None = None, cursor: str | None = None, conn=Depends(conn_dep)):
    sql = ("select distinct on (paper_id) paper_id, paper_revision, canonical_id, title, primary_url, venue,"
           " publication_year, publication_status, read_depth, method_card, available_at"
           " from research.paper_revision where true")
    args: list[Any] = []
    for col, val in (("publication_year", publication_year), ("publication_status", publication_status),
                     ("read_depth", read_depth)):
        if val is not None:
            sql += f" and {col}=%s"
            args.append(val)
    rows = conn.execute(sql + " order by paper_id, paper_revision desc", args).fetchall()
    return respond("listPapers", {"items": [dict(r) for r in sorted(rows, key=lambda r: r["title"])]}, meta())


@router.post("/experiments", status_code=202)
def create_experiment(request: Request, body: dict = Body(...), idempotency_key: str = Header(..., alias="Idempotency-Key"),
                      conn=Depends(conn_dep)):
    pid = principal(request)                      # authenticate first: an anonymous caller learns nothing about the schema
    try:
        contracts.check("ExperimentRequest", body)
    except contracts.ContractViolation as e:
        raise ApiError(400, "CONTRACT_VIOLATION", "请求不符合合同",
                       field_errors=[{"path": "/", "code": "invalid", "message": m} for m in e.errors]) from e

    def handler() -> dict:
        from pulse.adapters.postgres.jobs import PgJobStore

        run_id = uuid.uuid5(RESEARCH_JOB, f"{pid}:{idempotency_key}")
        store = PgJobStore(conn)
        store.enqueue(job_run_id=run_id, job_id=RESEARCH_JOB, job_version=1, idempotency_key=f"exp:{run_id}",
                      **_enqueue_extra(body))
        return store.receipt(run_id)

    try:
        res = PgCommandStore(conn).execute(
            principal_id=pid, operation_id="createExperiment", idempotency_key=idempotency_key, request=body,
            response_contract_id=SCHEMA_ID, response_schema_version=contracts.CONTRACT_VERSION,
            handler=lambda c: handler(), ttl=timedelta(days=7))
    except Conflict as e:
        raise ApiError(409, "IDEMPOTENCY_CONFLICT", str(e)) from e
    return respond("createExperiment", res.response, meta(), status=202)


def _enqueue_extra(body: dict) -> dict:
    """PgJobStore.enqueue arguments beyond identity: request hash, schedule and the resource (strategy instance)."""
    from pulse.adapters.postgres._canon import sha256_of

    return {"parameter_hash": sha256_of(body), "scheduled_for": datetime.now(timezone.utc),
            "resource_type": "strategy_instance", "resource_id": UUID(body["instance_id"])}


@router.get("/experiments/{experiment_id}")
def get_evaluation(experiment_id: UUID, request: Request, conn=Depends(conn_dep)):
    principal(request)
    data = research_repo.load_evaluation_wire(conn, experiment_id)
    if data is None:
        raise ApiError(404, "NOT_FOUND", "实验不存在")
    return respond("getEvaluation", data, meta(decision_as_of=None))


@router.get("/leagues/{league_id}/versions/{league_version}")
def get_league(league_id: UUID, league_version: int, request: Request, conn=Depends(conn_dep)):
    principal(request)
    try:
        data, gaps = research_repo.load_league(conn, league_id, league_version)
    except KeyError as e:
        raise ApiError(404, "NOT_FOUND", "联赛不存在") from e
    warnings = []
    if gaps:   # contract gap: the League outcome enum has no slot for these two settled results
        kinds = sorted({g["db_outcome"] for g in gaps})
        warnings.append({"code": "LEAGUE_OUTCOME_MAPPED", "affected_component": "league", "evidence_ref": ",".join(str(g["pairing_id"]) for g in gaps),
                         "message": f"{len(gaps)} 个配对的结果 ({', '.join(kinds)}) 不在 League 合同枚举内，已映射为最接近的合同值，原值保留在 reason_ref 前缀"})
    return respond("getLeague", data, meta(warnings=warnings))
