"""Seed helpers for the W02 database tests (real PostgreSQL, no mocks).

Every helper inserts the minimum rows the package DDL demands and uses fresh UUIDs, so tests can share
the session database without interfering.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

UTC = timezone.utc


def now() -> datetime:
    return datetime.now(UTC)


def h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def seed_contract(conn: psycopg.Connection, contract_id: str = "test.contract", version: str = "1.0.0") -> tuple[str, str]:
    conn.execute(
        "INSERT INTO core.contract_version(contract_id, schema_version, schema_document, schema_hash,"
        " compatibility_policy, created_at) VALUES (%s,%s,%s,%s,'strict',clock_timestamp()) ON CONFLICT DO NOTHING",
        (contract_id, version, Jsonb({"type": "object"}), h(contract_id + version)))
    return contract_id, version


def seed_api_binding(conn: psycopg.Connection, operation_id: str) -> str:
    cid, ver = seed_contract(conn)
    conn.execute(
        "INSERT INTO core.api_binding(operation_id, http_method, path_template, response_contract_id,"
        " response_schema_version, application_service, read_model, freshness_policy_version)"
        " VALUES (%s,'POST',%s,%s,%s,'svc','rm','fp1') ON CONFLICT DO NOTHING",
        (operation_id, f"/v1/test/{operation_id}", cid, ver))
    return operation_id


def seed_manifest(conn: psycopg.Connection, state: str = "READY", dataset_id: str | None = None) -> UUID:
    cid, ver = seed_contract(conn)
    conn.execute("INSERT INTO core.source(source_id, display_name, provider_uri, created_at)"
                 " VALUES ('w02src','w02','http://x',clock_timestamp()) ON CONFLICT DO NOTHING")
    conn.execute(
        "INSERT INTO core.source_endpoint(source_id, endpoint_id, source_schema_version, mapping_version,"
        " contract_id, schema_version, mapping_hash, enabled, transport_kind, stale_after_seconds, policy_document,"
        " created_at) VALUES ('w02src','ep','s1','m1',%s,%s,%s,true,'poll',60,'{}',clock_timestamp())"
        " ON CONFLICT DO NOTHING", (cid, ver, h("m")))
    batch, snap = uuid4(), uuid4()
    conn.execute(
        "INSERT INTO ingest.batch(batch_id, source_id, endpoint_id, source_schema_version, mapping_version,"
        " partition_key, batch_state, started_at) VALUES (%s,'w02src','ep','s1','m1','p','INGESTING',clock_timestamp())",
        (batch,))
    ready = state == "READY"
    conn.execute(
        "INSERT INTO market.dataset_manifest(snapshot_id, dataset_id, manifest_version, batch_id, manifest_state,"
        " storage_uri, content_hash, contract_id, schema_version, row_count, coverage_document, created_at, ready_at)"
        " VALUES (%s,%s,1,%s,%s,%s,%s,%s,%s,%s,'{}',clock_timestamp(),%s)",
        (snap, dataset_id or f"ds_{snap.hex[:8]}", batch, state, "file:///x" if ready else None,
         h(str(snap)) if ready else None, cid, ver, 0 if ready else None, now() if ready else None))
    return snap


def new_release(rel, *, required=("ranking", "plans"), optional=None, as_of=None) -> UUID:
    rid = uuid4()
    rel.create_release(
        release_id=rid, decision_as_of=as_of or now(), availability_policy_version="observed.v1",
        universe_policy_version="u1", market_rules_version="r1", required_components=list(required),
        optional_components=optional)
    return rid


def ready_all(rel, rid: UUID, keys, **kw) -> None:
    for k in keys:
        rel.mark_ready(rid, k, artifact_ref=f"file:///{k}", artifact_hash=h(k), input_version_hash=h("in" + k), **kw)


def staged(rel, slot: str, *, required=("ranking", "plans"), optional=None, as_of=None):
    """Create slot + BUILDING release + stage; returns (release_id, stage_ref)."""
    rel.ensure_slot(slot)
    rid = new_release(rel, required=required, optional=optional, as_of=as_of)
    stage = rel.begin_stage(stage_id=uuid4(), slot_key=slot, release_id=rid, input_context_hash=h(str(rid)))
    return rid, stage
