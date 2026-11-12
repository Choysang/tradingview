"""Ingest persistence: contracts, sources, endpoints, batches, raw objects, observations, cursors, health.

Why: implements the recoverable ingest protocol of database_design.md section 5:
raw object saved first -> batch member + RAW_COMMITTED in one transaction -> only
then a compare-and-swap cursor advance. Same raw bytes are reused by content hash
(a repeated pull records a new batch observation, not a new fact).

Invariants: observation ids are deterministic (uuid5 over raw object + natural key
+ revision) so a replay is idempotent; the cursor never advances before RAW_COMMITTED;
a stale ``cursor_version`` loses the CAS; health rows mirror the pure state machine.
Not covered: lake file I/O (adapters/lake), normalization (domain).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any, Iterable, Sequence

import psycopg
from psycopg.types.json import Jsonb

from pulse import contracts
from pulse.adapters.postgres._canon import sha256_of
from pulse.adapters.sources.registry import CONTRACT_VERSION, SOURCES, EndpointSpec
from pulse.domain.evidence.health import HealthState

OBS_NS = uuid.UUID("0c2f6a42-3d11-4f0e-9b57-2a1d8c6e7f90")
RAW_NS = uuid.UUID("7b3e9d10-55aa-4c21-8e0b-6d4f1a2c9e33")


def observation_id(raw_object_id: uuid.UUID, natural_key: str, revision: int) -> uuid.UUID:
    """Deterministic, entity-clustered id (RFC 4122 variant, version nibble 8 = custom).

    ``natural_key`` always starts with the entity key, so one raw object can hold many entities. The first
    8 bytes derive from the entity key alone: all revisions / bars of one instrument sit next to each other
    in the primary-key index, so bulk loads sorted by id are sequential instead of random page writes.
    """
    entity = natural_key.split("|", 1)[0]
    head = hashlib.sha256(entity.encode()).digest()[:8]
    tail = hashlib.sha256(f"{raw_object_id}|{natural_key}|{revision}".encode()).digest()[:8]
    b = bytearray(head + tail)
    b[6] = (b[6] & 0x0F) | 0x80
    b[8] = (b[8] & 0x3F) | 0x80
    return uuid.UUID(bytes=bytes(b))


def ensure_contracts(conn: psycopg.Connection, now: datetime, ids: Iterable[str] = ("CanonicalBar", "AdjustedBar", "CanonicalQuote",
                                                                                    "EvidenceDocument", "FeatureResult")) -> None:
    defs = contracts.v3_schema()["$defs"]
    for cid in ids:
        doc = defs[cid]
        conn.execute(
            "INSERT INTO core.contract_version(contract_id, schema_version, schema_document, schema_hash,"
            " compatibility_policy, created_at) VALUES (%s,%s,%s,%s,'additive-optional-v1',%s)"
            " ON CONFLICT (contract_id, schema_version) DO NOTHING",
            (cid, CONTRACT_VERSION, Jsonb(doc), sha256_of(doc), now))


def ensure_sources(conn: psycopg.Connection, now: datetime) -> None:
    for sid, (name, uri) in SOURCES.items():
        conn.execute("INSERT INTO core.source(source_id, display_name, provider_uri, created_at) VALUES (%s,%s,%s,%s)"
                     " ON CONFLICT (source_id) DO NOTHING", (sid, name, uri, now))


def ensure_endpoint(conn: psycopg.Connection, spec: EndpointSpec, now: datetime) -> None:
    from pulse.domain.market.capability import REGISTRY

    cap = REGISTRY.get(spec.capability_dataset) if spec.capability_dataset else None
    policy = {**spec.policy, "capability": cap.to_doc() if cap else None, "mapping_profile_id": (spec.profile or {}).get("profile_id")}
    row = conn.execute(
        "SELECT mapping_hash FROM core.source_endpoint WHERE source_id=%s AND endpoint_id=%s AND source_schema_version=%s AND mapping_version=%s",
        spec.key).fetchone()
    if row is not None:
        if row["mapping_hash"] != spec.mapping_hash:
            raise ValueError(f"mapping for {spec.key} changed without a new mapping_version")
        return
    conn.execute(
        "INSERT INTO core.source_endpoint(source_id, endpoint_id, source_schema_version, mapping_version, contract_id,"
        " schema_version, mapping_hash, enabled, credential_ref, transport_kind, poll_interval_seconds, stale_after_seconds,"
        " policy_document, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s,%s,%s)",
        (*spec.key, spec.contract_id, CONTRACT_VERSION, spec.mapping_hash, spec.enabled, spec.transport_kind,
         spec.poll_interval_seconds, spec.stale_after_seconds, Jsonb(policy), now))


def bootstrap(conn: psycopg.Connection, specs: Iterable[EndpointSpec], now: datetime) -> None:
    ensure_contracts(conn, now)
    ensure_sources(conn, now)
    for s in specs:
        ensure_endpoint(conn, s, now)


def create_batch(conn: psycopg.Connection, spec: EndpointSpec, partition_key: str, started_at: datetime,
                 cursor_before: Any = None, batch_id: uuid.UUID | None = None) -> uuid.UUID:
    bid = batch_id or uuid.uuid4()
    conn.execute(
        "INSERT INTO ingest.batch(batch_id, source_id, endpoint_id, source_schema_version, mapping_version, partition_key,"
        " cursor_before, batch_state, started_at) VALUES (%s,%s,%s,%s,%s,%s,%s,'INGESTING',%s)",
        (bid, *spec.key, partition_key, Jsonb(cursor_before) if cursor_before is not None else None, started_at))
    return bid


def insert_raw_object(conn: psycopg.Connection, *, source_id: str, endpoint_id: str, source_schema_version: str,
                      source_record_key: str, content_hash: str, storage_uri: str, mime_type: str,
                      published_at: datetime | None, source_time_precision: str, first_seen_at: datetime,
                      ingested_at: datetime, origin_group: str | None = None) -> tuple[uuid.UUID, bool]:
    """Return (raw_object_id, created). Same bytes under the same record key reuse the first row."""
    rid = uuid.uuid5(RAW_NS, f"{source_id}|{endpoint_id}|{source_schema_version}|{source_record_key}|{content_hash}")
    row = conn.execute(
        "INSERT INTO ingest.raw_object(raw_object_id, source_id, endpoint_id, source_schema_version, source_record_key,"
        " content_hash, storage_uri, mime_type, origin_group, published_at, source_time_precision, first_seen_at, ingested_at)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (source_id, endpoint_id, source_schema_version, source_record_key, content_hash) DO NOTHING"
        " RETURNING raw_object_id",
        (rid, source_id, endpoint_id, source_schema_version, source_record_key, content_hash, storage_uri, mime_type,
         origin_group, published_at, source_time_precision, first_seen_at, ingested_at)).fetchone()
    if row is not None:
        return row["raw_object_id"], True
    existing = conn.execute(
        "SELECT raw_object_id FROM ingest.raw_object WHERE source_id=%s AND endpoint_id=%s AND source_schema_version=%s"
        " AND source_record_key=%s AND content_hash=%s", (source_id, endpoint_id, source_schema_version, source_record_key, content_hash)).fetchone()
    return existing["raw_object_id"], False


def add_batch_member(conn: psycopg.Connection, batch_id: uuid.UUID, raw_object_id: uuid.UUID, received_at: datetime) -> None:
    conn.execute("INSERT INTO ingest.batch_member(batch_id, raw_object_id, received_at) VALUES (%s,%s,%s)"
                 " ON CONFLICT DO NOTHING", (batch_id, raw_object_id, received_at))


def commit_raw(conn: psycopg.Connection, batch_id: uuid.UUID, now: datetime, expected_count: int | None = None) -> None:
    n = conn.execute("SELECT count(*) AS n FROM ingest.batch_member WHERE batch_id=%s", (batch_id,)).fetchone()["n"]
    conn.execute("UPDATE ingest.batch SET batch_state='RAW_COMMITTED', received_count=%s, expected_count=%s, raw_committed_at=%s"
                 " WHERE batch_id=%s AND batch_state='INGESTING'", (n, expected_count, now, batch_id))


def set_batch_state(conn: psycopg.Connection, batch_id: uuid.UUID, state: str, failure_code: str | None = None,
                    cursor_after: Any = None) -> None:
    conn.execute("UPDATE ingest.batch SET batch_state=%s, failure_code=%s, cursor_after=COALESCE(%s, cursor_after)"
                 " WHERE batch_id=%s", (state, failure_code, Jsonb(cursor_after) if cursor_after is not None else None, batch_id))


OBS_COLUMNS = ("observation_id, raw_object_id, contract_id, schema_version, mapping_version, entity_type, entity_key,"
               " natural_key, observation_revision, event_time, available_at, recorded_at, quality_status,"
               " canonical_payload, field_quality, content_hash")


_OBS_COPY_COLS = ("observation_id", "raw_object_id", "contract_id", "schema_version", "mapping_version", "entity_type", "entity_key",
                  "natural_key", "observation_revision", "event_time", "available_at", "recorded_at", "quality_status",
                  "canonical_payload", "field_quality", "content_hash")


def insert_observations(conn: psycopg.Connection, rows: Sequence[dict]) -> int:
    """Bulk idempotent insert: COPY into a staging table, then INSERT ... ORDER BY id ON CONFLICT DO NOTHING.

    Sorting by the entity-clustered primary key keeps the index writes sequential (random-UUID bulk loads
    were measured at ~70 rows/s once the index exceeded shared_buffers). Returns the number created.
    """
    if not rows:
        return 0
    cols = ", ".join(_OBS_COPY_COLS)
    with conn.transaction():
        conn.execute("CREATE TEMP TABLE obs_stage (LIKE ingest.observation INCLUDING DEFAULTS) ON COMMIT DROP")
        with conn.cursor().copy(f"COPY obs_stage ({cols}) FROM STDIN") as cp:
            for r in rows:
                cp.write_row([r[c] if c not in ("canonical_payload", "field_quality") else Jsonb(r[c]) for c in _OBS_COPY_COLS])
        cur = conn.execute(f"INSERT INTO ingest.observation({cols}) SELECT {cols} FROM obs_stage ORDER BY observation_id ON CONFLICT DO NOTHING")
        return cur.rowcount


def read_cursor(conn: psycopg.Connection, spec: EndpointSpec, partition_key: str) -> dict | None:
    return conn.execute(
        "SELECT committed_cursor, committed_batch_id, source_event_watermark, cursor_version FROM ingest.source_cursor"
        " WHERE source_id=%s AND endpoint_id=%s AND source_schema_version=%s AND mapping_version=%s AND partition_key=%s",
        (*spec.key, partition_key)).fetchone()


def advance_cursor(conn: psycopg.Connection, spec: EndpointSpec, partition_key: str, *, batch_id: uuid.UUID,
                   new_cursor: Any, watermark: datetime | None, expected_version: int, now: datetime) -> bool:
    """CAS: only a RAW_COMMITTED batch may advance; the loser of a race returns False."""
    state = conn.execute("SELECT batch_state FROM ingest.batch WHERE batch_id=%s", (batch_id,)).fetchone()
    if state is None or state["batch_state"] not in ("RAW_COMMITTED", "NORMALIZED"):
        raise ValueError("checkpoint_requires_durable_raw_commit")
    if expected_version == 0:
        row = conn.execute(
            "INSERT INTO ingest.source_cursor(source_id, endpoint_id, source_schema_version, mapping_version, partition_key,"
            " committed_cursor, committed_batch_id, source_event_watermark, cursor_version, updated_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1,%s) ON CONFLICT DO NOTHING RETURNING cursor_version",
            (*spec.key, partition_key, Jsonb(new_cursor), batch_id, watermark, now)).fetchone()
        return row is not None
    row = conn.execute(
        "UPDATE ingest.source_cursor SET committed_cursor=%s, committed_batch_id=%s, source_event_watermark=%s,"
        " cursor_version=cursor_version+1, updated_at=%s WHERE source_id=%s AND endpoint_id=%s AND source_schema_version=%s"
        " AND mapping_version=%s AND partition_key=%s AND cursor_version=%s RETURNING cursor_version",
        (Jsonb(new_cursor), batch_id, watermark, now, *spec.key, partition_key, expected_version)).fetchone()
    return row is not None


def save_health(conn: psycopg.Connection, spec: EndpointSpec, partition_key: str, st: HealthState, now: datetime) -> None:
    runtime = st.runtime_state
    conn.execute(
        "INSERT INTO ingest.source_health(source_id, endpoint_id, source_schema_version, mapping_version, partition_key,"
        " runtime_state, freshness_state, last_attempt_at, last_transport_ok_at, last_new_record_at, last_canonical_ready_at,"
        " consecutive_failures, next_attempt_at, last_error_code, updated_at)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (source_id, endpoint_id, source_schema_version, mapping_version, partition_key) DO UPDATE SET"
        " runtime_state=EXCLUDED.runtime_state, freshness_state=EXCLUDED.freshness_state,"
        " last_attempt_at=EXCLUDED.last_attempt_at, last_transport_ok_at=EXCLUDED.last_transport_ok_at,"
        " last_new_record_at=EXCLUDED.last_new_record_at, last_canonical_ready_at=EXCLUDED.last_canonical_ready_at,"
        " consecutive_failures=EXCLUDED.consecutive_failures, next_attempt_at=EXCLUDED.next_attempt_at,"
        " last_error_code=EXCLUDED.last_error_code, updated_at=EXCLUDED.updated_at",
        (*spec.key, partition_key, runtime, st.freshness_state, st.last_attempt_at, st.last_transport_ok_at,
         st.last_new_record_at, st.last_canonical_ready_at, st.consecutive_failures, st.next_attempt_at,
         st.last_error_code, now))


def load_health(conn: psycopg.Connection, spec: EndpointSpec, partition_key: str) -> HealthState:
    row = conn.execute(
        "SELECT h.*, e.enabled FROM ingest.source_health h JOIN core.source_endpoint e USING (source_id, endpoint_id,"
        " source_schema_version, mapping_version) WHERE h.source_id=%s AND h.endpoint_id=%s AND h.source_schema_version=%s"
        " AND h.mapping_version=%s AND h.partition_key=%s", (*spec.key, partition_key)).fetchone()
    if row is None:
        return HealthState(enabled=spec.enabled, configured=True, runtime_state="unconfigured")
    code = row["last_error_code"] or ""
    circuit = "open" if (row["runtime_state"] == "unavailable" and code.startswith("blocked")) else "closed"
    return HealthState(enabled=row["enabled"], configured=True, runtime_state=row["runtime_state"],
                       freshness_state=row["freshness_state"], last_attempt_at=row["last_attempt_at"],
                       last_transport_ok_at=row["last_transport_ok_at"], last_new_record_at=row["last_new_record_at"],
                       last_canonical_ready_at=row["last_canonical_ready_at"], consecutive_failures=row["consecutive_failures"],
                       next_attempt_at=row["next_attempt_at"], last_error_code=row["last_error_code"], circuit=circuit)


def record_issue(conn: psycopg.Connection, *, issue_code: str, severity: str, payload: dict, now: datetime,
                 batch_id: uuid.UUID | None = None, raw_object_id: uuid.UUID | None = None,
                 observation_id_: uuid.UUID | None = None, field_path: str | None = None) -> uuid.UUID:
    iid = uuid.uuid4()
    conn.execute(
        "INSERT INTO ops.data_issue(issue_id, batch_id, raw_object_id, observation_id, issue_code, field_path, severity,"
        " issue_payload, issue_state, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'open',%s)",
        (iid, batch_id, raw_object_id, observation_id_, issue_code, field_path, severity, Jsonb(payload), now))
    return iid


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
