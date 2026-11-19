"""Calendar store import and read (legacy rows #114 / #115 / #174): the old ``events.jsonl`` as the declared dataset ``legacy_calendar_events``.

Why: the forward-log M3 catalyst rule reads the old calendar store from the old install path (``bf_fwdlog_inputs.read_events``). With this
import the same events live in pulse3 as a READY dataset version, point in time, and the rule can read pulse3 data instead.
The function the forward-log lane should call is :func:`read_calendar_events` (same return shape as ``bf_fwdlog_inputs.read_events``).
Invariants:
* the source file is only read; the import is idempotent (a file with the same sha256 as the newest version publishes nothing);
* every revision is stored; at read time an event is its newest revision whose ``first_seen_at`` is at or before the cutoff, and only
  versions that were READY at the cutoff are visible (point in time: the store content is available from the import time);
* a missing dataset reads as no events with ``present: false``, never as an empty calendar that looks complete.
Not covered: native syncing (``calendar_service``), writing back to the old store.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import polars as pl
import psycopg
from psycopg.types.json import Jsonb

from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import ingest_repo
from pulse.adapters.postgres._canon import sha256_of
from pulse.adapters.sources import legacy_calendar as LC
from pulse.adapters.sources import registry
from pulse.application.data_service import DataService, Summary
from pulse.domain.fwdlog import board as BD

UTC = timezone.utc
DATASET = "legacy_calendar_events"
CONTRACT_ID = "pulse.bf_fillm.calendar_event"
CONTRACT_VERSION = "3.0.0"
READER = "pulse.application.bf_fillm_calendar.read_calendar_events(conn, lake, cutoff) -> (list[BD.CalEvent], dict)"


def _z(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def latest_ready(conn: psycopg.Connection, cutoff: datetime | None = None) -> dict | None:
    sql, args = "SELECT * FROM market.dataset_manifest WHERE dataset_id=%s AND manifest_state='READY'", [DATASET]
    if cutoff is not None:
        sql += " AND ready_at <= %s"
        args.append(cutoff)
    return conn.execute(sql + " ORDER BY manifest_version DESC LIMIT 1", args).fetchone()


def import_store(conn: psycopg.Connection, lake: LakeStore, path: Path, *, clock: Callable[[], datetime] | None = None) -> Summary:
    """Import the old store file as one new dataset version (nothing when the bytes are unchanged)."""
    s = Summary(f"legacy-calendar-store {path.name}")
    clock = clock or (lambda: datetime.now(UTC))
    path = Path(path)
    if not path.exists():
        s.notes.append(f"{path} does not exist; nothing imported")
        return s
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    data = DataService(conn, lake, clock=clock)
    data.bootstrap()
    now = clock()
    with conn.transaction():
        schema = {"description": "One revision of one calendar event of the old event calendar store (read-only import).", "type": "object"}
        conn.execute(
            "INSERT INTO core.contract_version(contract_id, schema_version, schema_document, schema_hash, compatibility_policy, created_at)"
            " VALUES (%s,%s,%s,%s,'additive-optional-v1',%s) ON CONFLICT (contract_id, schema_version) DO NOTHING",
            (CONTRACT_ID, CONTRACT_VERSION, Jsonb(schema), sha256_of(schema), now))
    cur = latest_ready(conn)
    if cur is not None and cur["coverage_document"].get("source_sha256") == digest:
        s.add("unchanged")
        return s
    try:
        df = LC.parse_store(raw)
    except LC.StoreUnreadable as exc:
        s.add("unreadable")
        s.notes.append(f"{exc}; nothing imported")
        return s
    spec = registry.spec(LC.SOURCE_ID, LC.ENDPOINT_ID)
    batch_id = ingest_repo.create_batch(conn, spec, f"calendar:{digest[:12]}", now)
    owner = f"job:{batch_id}"
    try:
        obj = lake.put_bytes(raw, owner=owner, kind="raw", suffix=".jsonl")
        with conn.transaction():
            rid, _ = ingest_repo.insert_raw_object(
                conn, source_id=spec.source_id, endpoint_id=spec.endpoint_id, source_schema_version=spec.source_schema_version,
                source_record_key=path.name, content_hash=obj.sha256, storage_uri=obj.rel_path, mime_type="application/x-ndjson", published_at=None,
                source_time_precision="unknown", first_seen_at=now, ingested_at=now)
            ingest_repo.add_batch_member(conn, batch_id, rid, now)
            ingest_repo.commit_raw(conn, batch_id, clock())
        ref = lake.put_parquet(df, owner=owner)
        manifest = lake.commit_manifest(DATASET, [ref], owner=owner, meta={"row_count": df.height, "kind": "table"})
        with conn.transaction():
            ingest_repo.set_batch_state(conn, batch_id, "NORMALIZED")
        events = df["event_id"].n_unique()
        coverage = {"dataset": DATASET, "source_sha256": digest, "source_name": path.name, "events": events, "revisions": df.height, "imported_at": _z(now),
                    "available_at_policy": "import time (the store content is not available to pulse3 earlier)",
                    "latest_first_seen_at": _z(df["first_seen_at"].max()), "reader": READER,
                    "includes": "research seeds, Eastmoney calendar rows and rule-computed expiries of the old store, every revision"}
        pub = data._stage_and_publish(DATASET, uuid.uuid4(), batch_id, manifest, CONTRACT_ID, coverage, owner, df.height, now, (), ())
    except Exception:
        with conn.transaction():
            ingest_repo.set_batch_state(conn, batch_id, "FAILED", failure_code="import_error")
        lake.release(owner)
        raise
    s.counts.update({"events": events, "revisions": df.height, "manifest_version": pub["manifest_version"]})
    return s


def read_calendar_events(conn: psycopg.Connection, lake: LakeStore, cutoff: datetime | None = None) -> tuple[list[BD.CalEvent], dict]:
    """Latest revision of every event that was visible at ``cutoff`` (None = now), as the forward-log rule expects (``BD.CalEvent``)."""
    man = latest_ready(conn, cutoff)
    if man is None:
        return [], {"present": False, "events": 0, "snapshot_id": None, "source_sha256": None, "latest_first_seen_at": None, "reader": READER}
    df = lake.read_snapshot_frames(man["content_hash"])
    if cutoff is not None:
        df = df.filter(pl.col("first_seen_at") <= cutoff)
    df = df.sort(["event_id", "revision"]).unique("event_id", keep="last")
    out = []
    for r in df.iter_rows(named=True):
        out.append(BD.CalEvent(r["event_id"], r["name"], r["category"], r["status"], r["event_date"], r["first_seen_at"], r["event_end_date"], r["event_at"],
                               r["event_timezone"], r["result_known_at"], frozenset(json.loads(r["instruments"] or "[]"))))
    return out, {"present": True, "events": len(out), "snapshot_id": man["snapshot_id"], "source_sha256": man["coverage_document"].get("source_sha256"),
                 "latest_first_seen_at": max((e.first_seen_at for e in out), default=None), "reader": READER}


def status(conn: psycopg.Connection, lake: LakeStore, cutoff: datetime | None = None, sample: int = 5) -> dict:
    man = latest_ready(conn, cutoff)
    if man is None:
        return {"present": False, "snapshot_id": None, "events": 0, "revisions": 0, "source_sha256": None, "imported_at": None, "latest_first_seen_at": None,
                "reader": READER, "sample": []}
    events, info = read_calendar_events(conn, lake, cutoff)
    cov = man["coverage_document"]
    top = sorted(events, key=lambda e: e.first_seen_at, reverse=True)[:sample]
    return {"present": True, "snapshot_id": man["snapshot_id"], "events": len(events), "revisions": int(cov.get("revisions") or 0),
            "source_sha256": cov.get("source_sha256"), "imported_at": man["ready_at"], "latest_first_seen_at": info["latest_first_seen_at"], "reader": READER,
            "sample": [{"event_id": e.event_id, "name": e.name, "category": e.category, "status": e.status, "event_date": e.event_date,
                        "event_end_date": e.event_end_date, "first_seen_at": e.first_seen_at} for e in top]}
