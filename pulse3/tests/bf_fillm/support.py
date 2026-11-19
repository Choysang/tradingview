"""Seed helpers of the fill-market tests: a frame published as a READY dataset manifest in a real lake, plus the bar helpers of bf_stock."""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bf_stock.support import cutoff_after, publish_release, seed_closes  # noqa: E402,F401
from api_seed import raw_coverage, seed_instrument, seed_manifest, weekdays  # noqa: E402,F401


def seed_frame(conn, lake_root: Path, dataset_id: str, frame, *, ready_at: datetime, version: int | None = None, coverage: dict | None = None) -> UUID:
    """Publish ``frame`` (polars) as a READY manifest of ``dataset_id`` whose frozen lake manifest holds it. Returns the snapshot id."""
    (lake_root / "objects").mkdir(parents=True, exist_ok=True)
    (lake_root / "manifests").mkdir(parents=True, exist_ok=True)
    data_path = lake_root / "objects" / f"{uuid4().hex}.parquet"
    frame.write_parquet(data_path)
    raw = data_path.read_bytes()
    doc = json.dumps({"dataset_id": dataset_id, "members": [{"kind": "data", "path": f"objects/{data_path.name}", "sha256": hashlib.sha256(raw).hexdigest(),
                                                              "size": len(raw)}], "meta": {}}, sort_keys=True).encode()
    digest = hashlib.sha256(doc).hexdigest()
    (lake_root / "manifests" / f"{digest}.json").write_bytes(doc)
    if version is None:                  # versions of one dataset are unique and grow: the newest READY version of a session is read
        version = conn.execute("SELECT coalesce(max(manifest_version), 0) + 1 AS v FROM market.dataset_manifest WHERE dataset_id=%s", (dataset_id,)).fetchone()["v"]
    batch, snap = uuid4(), uuid4()
    conn.execute("INSERT INTO ingest.batch(batch_id,source_id,endpoint_id,source_schema_version,mapping_version,partition_key,batch_state,"
                 "started_at) VALUES (%s,'apisrc','quote','s1','m1','p','INGESTING',clock_timestamp())", (batch,))
    conn.execute(
        "INSERT INTO market.dataset_manifest(snapshot_id,dataset_id,manifest_version,batch_id,manifest_state,storage_uri,content_hash,"
        "contract_id,schema_version,row_count,coverage_document,created_at,ready_at) VALUES (%s,%s,%s,%s,'READY','file:///x',%s,'api.test','1.0.0',%s,%s,"
        "clock_timestamp(),%s)", (snap, dataset_id, version, batch, digest, frame.height, Jsonb(coverage or {"dataset": dataset_id}), ready_at))
    conn.commit()
    return snap


def seed_equity(conn, code: str, name: str = "测试股份") -> UUID:
    """A main-board equity whose state was known long before any test cutoff (``seed_instrument`` stamps the real clock)."""
    iid = seed_instrument(conn, code, name)
    conn.execute("UPDATE core.instrument_state SET available_at='2020-01-02Z' WHERE instrument_id=%s", (iid,))
    conn.execute("UPDATE core.instrument_alias SET available_at='2020-01-02Z' WHERE instrument_id=%s", (iid,))
    conn.commit()
    return iid
