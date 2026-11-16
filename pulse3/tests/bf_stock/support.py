"""Seed helpers for the stock backfill tests: bars with chosen closes, a circulating-cap dataset in a real lake manifest."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from api_seed import raw_coverage, seed_manifest, weekdays  # noqa: F401  (re-exported for the tests)
from pulse.domain.market.calendar import session_close_utc, session_open_utc

UTC = timezone.utc
CAP_DATASET = "instrument_circulating_cap_legacy"


def seed_closes(conn, iid: UUID, snap: UUID, days: list[date], closes: list[str], *, amounts: list[str | None] | None = None,
                volumes: list[int | None] | None = None, late: dict[int, timedelta] | None = None) -> None:
    """Raw daily bars with the given closes (open = close, high = close + 0.1, low = close - 0.1).

    ``late[i]``: the bar of day i is recorded that long AFTER its normal availability (a late-arriving version)."""
    for i, (d, c) in enumerate(zip(days, closes)):
        if c is None:
            o = h = l = cl = None
        else:
            cl = Decimal(c)
            o, h, l = cl, cl + Decimal("0.1"), cl - Decimal("0.1")
        amount = None if amounts is None else amounts[i]
        vol = (1000 * (i + 1)) if volumes is None else volumes[i]
        avail = session_close_utc(d) + timedelta(hours=2) + (late or {}).get(i, timedelta(0))
        full = cl is not None and amount is not None and vol is not None
        conn.execute(
            "INSERT INTO market.bar_projection(trade_date,instrument_id,timeframe,session_scope,bar_open_time,bar_close_time,snapshot_id,currency,"
            "open_price_raw,high_price_raw,low_price_raw,close_price_raw,volume_shares,turnover_amount_cny,is_final,available_at,quality_status)"
            " VALUES (%s,%s,'1d','regular_continuous_v1',%s,%s,%s,'CNY',%s,%s,%s,%s,%s,%s,true,%s,%s)",
            (d, iid, session_open_utc(d), session_close_utc(d), snap, o, h, l, cl, vol, amount, avail, "valid" if full else "partial"))
    conn.commit()


def cutoff_after(day: date, hours: int = 3) -> datetime:
    return session_close_utc(day) + timedelta(hours=hours)


def seed_cap(conn, lake_root: Path, rows: list[dict], *, ready_at: datetime, version: int = 1) -> UUID:
    """Publish ``instrument_circulating_cap_legacy`` as a READY manifest whose frozen lake manifest holds ``rows``.

    row: instrument_id, as_of (YYYY-MM-DD), float_cap_cny, available_at."""
    import polars as pl

    schema = {"instrument_id": pl.String, "as_of": pl.String, "float_cap_cny": pl.Decimal(38, 2), "total_cap_cny": pl.Decimal(38, 2),
              "available_at": pl.Datetime("us", "UTC")}
    frame = pl.DataFrame([{**{"total_cap_cny": None}, **r} for r in rows], schema=schema)
    (lake_root / "objects").mkdir(parents=True, exist_ok=True)
    (lake_root / "manifests").mkdir(parents=True, exist_ok=True)
    data_path = lake_root / "objects" / f"{uuid4().hex}.parquet"
    frame.write_parquet(data_path)
    raw = data_path.read_bytes()
    doc = json.dumps({"dataset_id": CAP_DATASET, "members": [{"kind": "data", "path": f"objects/{data_path.name}",
                                                              "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}], "meta": {}},
                     sort_keys=True).encode()
    digest = hashlib.sha256(doc).hexdigest()
    (lake_root / "manifests" / f"{digest}.json").write_bytes(doc)
    batch, snap = uuid4(), uuid4()
    conn.execute("INSERT INTO ingest.batch(batch_id,source_id,endpoint_id,source_schema_version,mapping_version,partition_key,batch_state,"
                 "started_at) VALUES (%s,'apisrc','quote','s1','m1','p','INGESTING',clock_timestamp())", (batch,))
    conn.execute(
        "INSERT INTO market.dataset_manifest(snapshot_id,dataset_id,manifest_version,batch_id,manifest_state,storage_uri,content_hash,"
        "contract_id,schema_version,row_count,coverage_document,created_at,ready_at) VALUES (%s,%s,%s,%s,'READY','file:///x',%s,'api.test','1.0.0',%s,%s,"
        "clock_timestamp(),%s)", (snap, CAP_DATASET, version, batch, digest, frame.height, Jsonb({"dataset": CAP_DATASET}), ready_at))
    conn.commit()
    return snap



def publish_release(conn, *, snapshot: UUID, as_of: datetime, slot: str = "main", policy: str = "observed-actual-available-at-v1") -> UUID:
    """A PUBLISHED release (required ranking + plans) in ``slot`` with a registered availability policy, so the bars handler accepts it."""
    from api_seed import sha
    from pulse.adapters.postgres.release import PgAnalysisRelease
    from w02_helpers import h, ready_all

    rel = PgAnalysisRelease(conn)
    rel.ensure_slot(slot)
    rid = uuid4()
    rel.create_release(release_id=rid, decision_as_of=as_of, availability_policy_version=policy, universe_policy_version="u1",
                       market_rules_version="r1", required_components=["ranking", "plans"])
    stage = rel.begin_stage(stage_id=uuid4(), slot_key=slot, release_id=rid, input_context_hash=h(str(rid)))
    ready_all(rel, rid, ["plans"])
    rel.mark_ready(rid, "ranking", artifact_ref="file:///r", artifact_hash=sha("r"), input_version_hash=sha("ri"), snapshot_id=snapshot)
    rel.publish(stage.stage_id, producer_version="t")
    conn.commit()
    return rid
