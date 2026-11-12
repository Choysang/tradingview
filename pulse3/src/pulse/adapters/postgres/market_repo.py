"""Market persistence: bar projection, dataset manifests (staged -> READY), features, reconciliation.

Why: implements the PG half of the publish protocol (database_design.md section 5,
RECON §4): after the lake wrote an immutable member manifest, PG records
``COMMITTED_UNPUBLISHED`` + dependencies, validates, and only then flips to
``READY`` in the same transaction as the outbox event. A READY manifest is
immutable (DB trigger); corrections are new snapshots.

Invariants: publish refuses when a required dependency role is missing or an input
snapshot is not READY; the lake manifest hash must equal ``content_hash``; GC is
told about every manifest that is READY, COMMITTED_UNPUBLISHED or INGESTING (so a
crash between steps never orphans-then-deletes data); feature results use a
lease epoch fence (older workers cannot overwrite).
Not covered: partition creation policy (migration 0005 owns the hook), lake I/O.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Iterable, Sequence

import psycopg
from psycopg.types.json import Jsonb

from pulse.adapters.postgres.outbox import PgOutbox
from pulse.application.ports.delivery import OutboxEvent
from pulse.domain.features.key import FeatureKey, key_hash  # noqa: F401 (key_hash re-exported)
from pulse.domain.market.reconcile import ALIGNMENT_POLICY_VERSION, TOLERANCE_POLICY_VERSION, FieldResult

PRODUCER = "data-plane/0.1"
EVENT_NS = uuid.UUID("c3a1f0d2-8b64-4e57-92a3-1d5e7f9b0a46")


def ensure_partitions(conn: psycopg.Connection, start: date, through: date) -> int:
    return conn.execute("SELECT market.ensure_bar_projection_partitions(%s, %s) AS n", (start, through)).fetchone()["n"]


BAR_COLUMNS = ("trade_date, instrument_id, timeframe, session_scope, bar_open_time, bar_close_time, snapshot_id, currency,"
               " open_price_raw, high_price_raw, low_price_raw, close_price_raw, volume_shares, turnover_amount_cny,"
               " exchange_reference_price_raw, is_final, available_at, quality_status, observation_id")


def insert_bars(conn: psycopg.Connection, rows: Sequence[dict]) -> int:
    if not rows:
        return 0
    n = 0
    with conn.cursor() as cur:
        for i in range(0, len(rows), 5000):
            cur.executemany(
                f"INSERT INTO market.bar_projection({BAR_COLUMNS}) VALUES (%(trade_date)s,%(instrument_id)s,%(timeframe)s,"
                "%(session_scope)s,%(bar_open_time)s,%(bar_close_time)s,%(snapshot_id)s,'CNY',%(open)s,%(high)s,%(low)s,%(close)s,"
                "%(volume_shares)s,%(turnover_cny)s,%(exchange_reference_price)s,%(is_final)s,%(available_at)s,%(quality_status)s,"
                "%(observation_id)s) ON CONFLICT DO NOTHING", rows[i:i + 5000])
            n += max(cur.rowcount, 0)
    return n


def read_bars(conn: psycopg.Connection, instrument_id: uuid.UUID, timeframe: str, start: date, end: date,
              snapshot_id: uuid.UUID | None = None, session_scope: str = "regular_continuous_v1") -> list[dict]:
    """Window read; the date range is mandatory so partition pruning applies."""
    sql = ("SELECT * FROM market.bar_projection WHERE instrument_id=%s AND timeframe=%s AND session_scope=%s"
           " AND trade_date BETWEEN %s AND %s")
    args: list = [instrument_id, timeframe, session_scope, start, end]
    if snapshot_id is not None:
        sql += " AND snapshot_id=%s"
        args.append(snapshot_id)
    return conn.execute(sql + " ORDER BY bar_open_time, available_at", args).fetchall()


# ---- manifests ---------------------------------------------------------------------------------
def next_manifest_version(conn: psycopg.Connection, dataset_id: str) -> int:
    # Serialize concurrent publishers of one dataset so manifest_version stays gap-free per dataset.
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 7))", (f"manifest:{dataset_id}",))
    return conn.execute("SELECT coalesce(max(manifest_version),0)+1 AS v FROM market.dataset_manifest WHERE dataset_id=%s",
                        (dataset_id,)).fetchone()["v"]


def stage_manifest(conn: psycopg.Connection, *, snapshot_id: uuid.UUID, dataset_id: str, batch_id: uuid.UUID,
                   lake_manifest_sha: str, storage_uri: str, contract_id: str, schema_version: str, row_count: int,
                   available_watermark: datetime | None, coverage_document: dict, now: datetime,
                   dependencies: Iterable[tuple[uuid.UUID, str]] = ()) -> int:
    version = next_manifest_version(conn, dataset_id)
    conn.execute(
        "INSERT INTO market.dataset_manifest(snapshot_id, dataset_id, manifest_version, batch_id, manifest_state, storage_uri,"
        " content_hash, contract_id, schema_version, row_count, available_watermark, coverage_document, created_at)"
        " VALUES (%s,%s,%s,%s,'COMMITTED_UNPUBLISHED',%s,%s,%s,%s,%s,%s,%s,%s)",
        (snapshot_id, dataset_id, version, batch_id, storage_uri, lake_manifest_sha, contract_id, schema_version, row_count,
         available_watermark, Jsonb(coverage_document), now))
    for input_id, role in dependencies:
        conn.execute("INSERT INTO market.dataset_dependency(snapshot_id, input_snapshot_id, dependency_role) VALUES (%s,%s,%s)",
                     (snapshot_id, input_id, role))
    return version


def publish_manifest(conn: psycopg.Connection, snapshot_id: uuid.UUID, now: datetime, *,
                     required_dependency_roles: Iterable[str] = (), expected_lake_sha: str | None = None,
                     expected_row_count: int | None = None) -> dict:
    """COMMITTED_UNPUBLISHED -> READY plus outbox event in ONE transaction. Idempotent for READY."""
    with conn.transaction():
        row = conn.execute("SELECT * FROM market.dataset_manifest WHERE snapshot_id=%s FOR UPDATE", (snapshot_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown snapshot {snapshot_id}")
        if row["manifest_state"] == "READY":
            return row
        if row["manifest_state"] != "COMMITTED_UNPUBLISHED":
            raise ValueError(f"snapshot is {row['manifest_state']}, cannot publish")
        if expected_lake_sha is not None and row["content_hash"] != expected_lake_sha:
            raise ValueError("manifest content_hash differs from the lake manifest")
        if expected_row_count is not None and row["row_count"] != expected_row_count:
            raise ValueError("row_count differs from validated lake rows")
        roles = {r["dependency_role"] for r in conn.execute(
            "SELECT dependency_role FROM market.dataset_dependency WHERE snapshot_id=%s", (snapshot_id,)).fetchall()}
        missing = sorted(set(required_dependency_roles) - roles)
        if missing:
            raise ValueError("missing_required_dependency_roles:" + ",".join(missing))
        unready = conn.execute(
            "SELECT count(*) AS n FROM market.dataset_dependency d JOIN market.dataset_manifest m ON m.snapshot_id=d.input_snapshot_id"
            " WHERE d.snapshot_id=%s AND m.manifest_state<>'READY'", (snapshot_id,)).fetchone()["n"]
        if unready:
            raise ValueError("dependency_snapshot_not_ready")
        updated = conn.execute("UPDATE market.dataset_manifest SET manifest_state='READY', ready_at=%s WHERE snapshot_id=%s RETURNING *",
                               (now, snapshot_id)).fetchone()
        ref = {"snapshot_id": str(snapshot_id), "dataset_id": updated["dataset_id"], "manifest_version": updated["manifest_version"],
               "content_hash": updated["content_hash"]}
        PgOutbox(conn).append(OutboxEvent(
            event_id=uuid.uuid5(EVENT_NS, f"dataset_snapshot_ready:{snapshot_id}"), event_type="market.dataset_snapshot_ready",
            schema_version="3.0.0", aggregate_type="dataset_snapshot", aggregate_id=updated["dataset_id"],
            aggregate_version=updated["manifest_version"], occurred_at=now, producer_version=PRODUCER, payload=ref, recorded_at=now))
        return updated


def fail_manifest(conn: psycopg.Connection, snapshot_id: uuid.UUID) -> None:
    conn.execute("UPDATE market.dataset_manifest SET manifest_state='FAILED' WHERE snapshot_id=%s AND manifest_state<>'READY'", (snapshot_id,))


def referenced_manifest_hashes(conn: psycopg.Connection) -> set[str]:
    """Lake manifest hashes GC must keep: everything that is not FAILED."""
    return {r["content_hash"] for r in conn.execute(
        "SELECT content_hash FROM market.dataset_manifest WHERE manifest_state IN ('READY','COMMITTED_UNPUBLISHED','INGESTING')"
        " AND content_hash IS NOT NULL").fetchall()}


def referenced_feature_hashes(conn: psycopg.Connection) -> set[str]:
    """Object hashes of READY / RUNNING feature artifacts (result_hash == lake object sha256)."""
    return {r["result_hash"] for r in conn.execute(
        "SELECT result_hash FROM market.feature_result WHERE result_state='READY' AND result_hash IS NOT NULL").fetchall()}


def get_manifest(conn: psycopg.Connection, snapshot_id: uuid.UUID) -> dict | None:
    return conn.execute("SELECT * FROM market.dataset_manifest WHERE snapshot_id=%s", (snapshot_id,)).fetchone()


def latest_ready(conn: psycopg.Connection, dataset_id: str) -> dict | None:
    return conn.execute("SELECT * FROM market.dataset_manifest WHERE dataset_id=%s AND manifest_state='READY'"
                        " ORDER BY manifest_version DESC LIMIT 1", (dataset_id,)).fetchone()


def snapshot_ref(row: dict) -> dict:
    """Contract ``SnapshotRef`` wire form."""
    return {"snapshot_id": str(row["snapshot_id"]), "dataset_id": row["dataset_id"], "manifest_version": row["manifest_version"],
            "content_hash": row["content_hash"]}


# ---- features ----------------------------------------------------------------------------------
def begin_feature(conn: psycopg.Connection, key: FeatureKey, lease_epoch: int) -> bool:
    """Claim a RUNNING row; returns False when a READY result or a newer epoch already exists."""
    h = key_hash(key)
    row = conn.execute(
        "INSERT INTO market.feature_result(feature_key_hash, feature_id, definition_version, full_key, snapshot_id,"
        " decision_as_of, availability_policy_version, result_state, lease_epoch) VALUES (%s,%s,%s,%s,%s,%s,%s,'RUNNING',%s)"
        " ON CONFLICT (feature_key_hash) DO UPDATE SET result_state='RUNNING', lease_epoch=EXCLUDED.lease_epoch"
        " WHERE market.feature_result.result_state IN ('FAILED','RUNNING') AND market.feature_result.lease_epoch < EXCLUDED.lease_epoch"
        " RETURNING feature_key_hash",
        (h, key.feature_id, key.definition_version, Jsonb(key.to_wire()), uuid.UUID(key.snapshot_id), key.decision_as_of,
         key.availability_policy_version, lease_epoch)).fetchone()
    return row is not None


def finish_feature(conn: psycopg.Connection, key: FeatureKey, lease_epoch: int, *, ok: bool, result_uri: str | None = None,
                   result_hash: str | None = None, computed_at: datetime | None = None) -> bool:
    """Fenced completion: only the current lease epoch may write the result."""
    state = "READY" if ok else "FAILED"
    row = conn.execute(
        "UPDATE market.feature_result SET result_state=%s, result_uri=%s, result_hash=%s, computed_at=%s"
        " WHERE feature_key_hash=%s AND lease_epoch=%s AND result_state='RUNNING' RETURNING feature_key_hash",
        (state, result_uri, result_hash, computed_at, key_hash(key), lease_epoch)).fetchone()
    return row is not None


def get_feature(conn: psycopg.Connection, key: FeatureKey) -> dict | None:
    return conn.execute("SELECT * FROM market.feature_result WHERE feature_key_hash=%s", (key_hash(key),)).fetchone()


# ---- reconciliation ----------------------------------------------------------------------------
def save_reconciliation(conn: psycopg.Connection, result: FieldResult, *, members: Sequence[tuple[uuid.UUID, str]],
                        decided_at: datetime, chosen: uuid.UUID | None = None, extra_trace: dict | None = None) -> uuid.UUID:
    rid = uuid.uuid4()
    trace = {**result.trace, "reasons": list(result.reasons), "origin_independence": "unconfirmed", **(extra_trace or {})}
    conn.execute(
        "INSERT INTO ops.reconciliation_result(reconciliation_id, reconciliation_status, field_path, alignment_policy_version,"
        " tolerance_policy_version, chosen_observation_id, normalization_trace, independent_origin_count, decided_at)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (rid, result.status, result.field_path, ALIGNMENT_POLICY_VERSION, TOLERANCE_POLICY_VERSION, chosen, Jsonb(trace),
         1, decided_at))
    for obs, role in members:
        conn.execute("INSERT INTO ops.reconciliation_member(reconciliation_id, observation_id, comparison_role) VALUES (%s,%s,%s)"
                     " ON CONFLICT DO NOTHING", (rid, obs, role))
    return rid
