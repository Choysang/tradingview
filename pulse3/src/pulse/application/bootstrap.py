"""Seed the contract registry: core.contract_version + core.api_binding for all 31 operations.

Why: ops.command_receipt and other rows reference the registered response
contract; FINAL_SYSTEM_SPEC §11 requires each operation to be registered with
its application service and read model. Idempotent: existing rows are kept, a
different schema hash for the same version is an error.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Jsonb

from pulse import contracts

SCHEMA_ID = "urn:pulse:contracts:v3"


def seed_registry(conn: psycopg.Connection) -> int:
    doc = contracts.v3_schema()
    canon = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    digest = hashlib.sha256(canon).hexdigest()
    with conn.transaction():
        row = conn.execute("select schema_hash from core.contract_version where contract_id=%s and schema_version=%s",
                           (SCHEMA_ID, contracts.CONTRACT_VERSION)).fetchone()
        if row is None:
            conn.execute(
                "insert into core.contract_version(contract_id, schema_version, schema_document, schema_hash,"
                " compatibility_policy, created_at) values (%s,%s,%s,%s,%s,%s)",
                (SCHEMA_ID, contracts.CONTRACT_VERSION, Jsonb(doc), digest, "strict-v3", datetime.now(timezone.utc)))
        elif (row["schema_hash"] if isinstance(row, dict) else row[0]) != digest:
            raise RuntimeError("registered v3 contract differs from the shipped contract")
        n = 0
        for op in contracts.api_bindings()["operations"]:
            conn.execute(
                "insert into core.api_binding(operation_id, http_method, path_template, response_contract_id,"
                " response_schema_version, application_service, read_model, freshness_policy_version)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s) on conflict (operation_id) do nothing",
                (op["operation_id"], op["method"], op["path"], SCHEMA_ID, contracts.CONTRACT_VERSION,
                 op["application_service"], ",".join(op.get("tables") or []) or "none", op.get("consistency") or "n/a"))
            n += 1
        n += _seed_extension(conn)
    return n


def _seed_extension(conn: psycopg.Connection) -> int:
    """Register the product-extension contract and its 41 operations (registration gate of the PEX cards)."""
    import json as _json

    from pulse.contracts import EXT_ID, _load

    doc = _load("extension.schema.json")
    version = doc["x-contract-version"]
    digest = hashlib.sha256(_json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                            .encode("utf-8")).hexdigest()
    row = conn.execute("select schema_hash from core.contract_version where contract_id=%s and schema_version=%s",
                       (EXT_ID, version)).fetchone()
    if row is None:
        conn.execute(
            "insert into core.contract_version(contract_id, schema_version, schema_document, schema_hash,"
            " compatibility_policy, created_at) values (%s,%s,%s,%s,%s,%s)",
            (EXT_ID, version, Jsonb(doc), digest, "additive-extension-of-v3", datetime.now(timezone.utc)))
    elif (row["schema_hash"] if isinstance(row, dict) else row[0]) != digest:
        raise RuntimeError(f"extension contract {version} changed without a new version")
    n = 0
    for path, methods in _load("extension.openapi.json")["paths"].items():
        for method, op in methods.items():
            conn.execute(
                "insert into core.api_binding(operation_id, http_method, path_template, response_contract_id,"
                " response_schema_version, application_service, read_model, freshness_policy_version)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s) on conflict (operation_id) do nothing",
                (op["operationId"], method.upper(), path, EXT_ID, version, op["x-card"], op["x-operation-kind"],
                 "ext-v1"))
            n += 1
    return n


RESEARCH_EXPERIMENT_JOB = "3a6c0f7e-2d1b-4e8a-9c55-7b0e4f1a2c10"


def seed_jobs(conn: psycopg.Connection) -> None:
    """Register job definitions used by API commands (research experiment runs)."""
    from uuid import UUID

    from pulse.adapters.postgres.jobs import PgJobStore
    from pulse.application.ports.jobs import JobDefinition

    PgJobStore(conn).register_definition(JobDefinition(
        job_id=UUID(RESEARCH_EXPERIMENT_JOB), job_version=1, application_operation="ResearchService.run_experiment",
        schedule_document={"trigger": "command", "timezone": "Asia/Shanghai"}, parameter_contract_id=SCHEMA_ID,
        parameter_schema_version=contracts.CONTRACT_VERSION, enabled=True, concurrency_key="research",
        max_concurrency=1, timeout_seconds=3600, retry_policy={"max_attempts": 1}))
