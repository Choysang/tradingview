"""Direct-SQL seed helpers for the market / account API tests (real PostgreSQL, package DDL, fresh UUIDs)."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from w02_helpers import ready_all as _ready_all, staged as _staged
from psycopg.types.json import Jsonb

from pulse.adapters.postgres.release import PgAnalysisRelease
from pulse.domain.market.calendar import CST, session_close_utc, session_open_utc

UTC = timezone.utc
HASH = "b" * 64


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def seed_sources(conn) -> None:
    conn.execute("INSERT INTO core.contract_version(contract_id,schema_version,schema_document,schema_hash,compatibility_policy,created_at)"
                 " VALUES ('api.test','1.0.0',%s,%s,'strict',clock_timestamp()) ON CONFLICT DO NOTHING", (Jsonb({"type": "object"}), HASH))
    for s in ("cninfo_announcements", "legacy_warehouse", "apisrc"):
        conn.execute("INSERT INTO core.source(source_id,display_name,provider_uri,created_at) VALUES (%s,%s,'urn:x',clock_timestamp())"
                     " ON CONFLICT DO NOTHING", (s, s))
    conn.execute(
        "INSERT INTO core.source_endpoint(source_id,endpoint_id,source_schema_version,mapping_version,contract_id,schema_version,"
        "mapping_hash,enabled,transport_kind,stale_after_seconds,policy_document,created_at) VALUES"
        " ('apisrc','quote','s1','m1','api.test','1.0.0',%s,true,'poll',120,'{}',clock_timestamp()) ON CONFLICT DO NOTHING", (HASH,))
    conn.execute(
        "INSERT INTO core.source_endpoint(source_id,endpoint_id,source_schema_version,mapping_version,contract_id,schema_version,"
        "mapping_hash,enabled,transport_kind,stale_after_seconds,policy_document,created_at) VALUES"
        " ('apisrc','quiet','s1','m1','api.test','1.0.0',%s,false,'batch',60,'{}',clock_timestamp()) ON CONFLICT DO NOTHING", (HASH,))
    conn.commit()


def seed_instrument(conn, code: str, name: str = "测试股份") -> UUID:
    iid = uuid4()
    conn.execute("INSERT INTO core.instrument(instrument_id,instrument_type,venue_code,currency,exchange_timezone,created_at)"
                 " VALUES (%s,'equity','XSHG','CNY','Asia/Shanghai',clock_timestamp())", (iid,))
    conn.execute("INSERT INTO core.instrument_state(instrument_id,state_version,board_code,display_name,is_risk_warning,trading_status,"
                 "effective_from,available_at,evidence_ref) VALUES (%s,1,'main',%s,false,'active','2020-01-01Z',clock_timestamp(),'t')",
                 (iid, name))
    for src in ("cninfo_announcements", "legacy_warehouse"):
        conn.execute("INSERT INTO core.instrument_alias(alias_id,source_id,source_symbol,instrument_id,effective_from,available_at,revision,"
                     "source_anchor) VALUES (%s,%s,%s,%s,'2020-01-01Z',clock_timestamp(),1,'t')", (uuid4(), src, code, iid))
    conn.commit()
    return iid


def seed_manifest(conn, dataset_id: str, coverage: dict, state: str = "READY", content_hash: str | None = None) -> UUID:
    batch, snap = uuid4(), uuid4()
    conn.execute("INSERT INTO ingest.batch(batch_id,source_id,endpoint_id,source_schema_version,mapping_version,partition_key,batch_state,"
                 "started_at) VALUES (%s,'apisrc','quote','s1','m1','p','INGESTING',clock_timestamp())", (batch,))
    ready = state == "READY"
    conn.execute(
        "INSERT INTO market.dataset_manifest(snapshot_id,dataset_id,manifest_version,batch_id,manifest_state,storage_uri,content_hash,"
        "contract_id,schema_version,row_count,coverage_document,created_at,ready_at) VALUES (%s,%s,1,%s,%s,%s,%s,'api.test','1.0.0',%s,%s,"
        "clock_timestamp(),%s)", (snap, f"{dataset_id}_{snap.hex[:8]}", batch, state, "file:///x" if ready else None,
                                  (content_hash or sha(str(snap))) if ready else None, 0 if ready else None, Jsonb(coverage),
                                  datetime.now(UTC) if ready else None))
    conn.commit()
    return snap


RAW_CAP = {"dataset_id": "bars_raw_1d", "source_id": "apisrc", "endpoint_id": "kline", "features": ["daily_bars", "interval_volume", "raw_prices"],
           "estimated": False, "notes": [], "history_depth": None, "sampled_interval_seconds": None}
FWD_CAP = {**RAW_CAP, "dataset_id": "bars_legacy_fwd_adjusted_1d", "features": ["adjusted_prices", "daily_bars", "interval_volume"]}


def raw_coverage(first: date, last: date) -> dict:
    return {"adjustment_mode": "raw", "capability": RAW_CAP, "first_date": first.isoformat(), "last_date": last.isoformat()}


def weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def seed_daily_bars(conn, iid: UUID, snap: UUID, days: list[date], *, base: Decimal = Decimal("10.00"), available_after_close: timedelta = timedelta(hours=2),
                    turnover: bool = True) -> None:
    for i, d in enumerate(days):
        o = base + i
        avail = session_close_utc(d) + available_after_close
        conn.execute(
            "INSERT INTO market.bar_projection(trade_date,instrument_id,timeframe,session_scope,bar_open_time,bar_close_time,snapshot_id,currency,"
            "open_price_raw,high_price_raw,low_price_raw,close_price_raw,volume_shares,turnover_amount_cny,is_final,available_at,quality_status)"
            " VALUES (%s,%s,'1d','regular_continuous_v1',%s,%s,%s,'CNY',%s,%s,%s,%s,%s,%s,true,%s,%s)",
            (d, iid, session_open_utc(d), session_close_utc(d), snap, o, o + 1, o - 1, o + Decimal("0.5"), 1000 * (i + 1),
             (o * 1000 * (i + 1)) if turnover else None, avail, "valid" if turnover else "partial"))
    conn.commit()


def write_lake(root: Path, dataset: str, rows: list[dict]) -> str:
    """Immutable lake manifest + one parquet member, byte layout as LakeStore writes it; returns the manifest sha."""
    import polars as pl

    dec = pl.Decimal(38, 12)
    schema = {"instrument_id": pl.String, "trade_date": pl.Date, "timeframe": pl.String, "bar_open_time": pl.Datetime("us", "UTC"),
              "bar_close_time": pl.Datetime("us", "UTC"), "open_price": dec, "high_price": dec, "low_price": dec, "close_price": dec,
              "volume_shares": pl.Int64, "turnover_amount_cny": dec, "adjustment_mode": pl.String, "adjustment_version": pl.String,
              "is_final": pl.Boolean, "available_at": pl.Datetime("us", "UTC"), "quality_status": pl.String, "revision": pl.Int32}
    (root / "objects").mkdir(parents=True, exist_ok=True)
    (root / "manifests").mkdir(parents=True, exist_ok=True)
    member = root / "objects" / f"{uuid4().hex}.parquet"
    pl.DataFrame(rows, schema=schema).write_parquet(member)
    doc = json.dumps({"dataset_id": dataset, "members": [{"kind": "data", "path": f"objects/{member.name}", "sha256": "0" * 64, "size": 1}],
                      "meta": {}}, sort_keys=True).encode()
    digest = hashlib.sha256(doc).hexdigest()
    (root / "manifests" / f"{digest}.json").write_bytes(doc)
    return digest


def lake_row(iid: UUID, d: date, close: str, version: str = "legacy-qfq-unversioned") -> dict:
    c = Decimal(close)
    return {"instrument_id": str(iid), "trade_date": d, "timeframe": "1d", "bar_open_time": session_open_utc(d), "bar_close_time": session_close_utc(d),
            "open_price": c - 1, "high_price": c + 1, "low_price": c - 2, "close_price": c, "volume_shares": 500, "turnover_amount_cny": None,
            "adjustment_mode": "forward_adjusted", "adjustment_version": version, "is_final": True,
            "available_at": session_close_utc(d) + timedelta(hours=2), "quality_status": "partial", "revision": 1}


def cst(d: date, hh: int, mm: int) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=CST).astimezone(UTC)


def publish(conn, *, snapshot=None, as_of=None, optional=None, missing=()):
    slot = f"mk-{uuid4().hex[:8]}"
    rel = PgAnalysisRelease(conn)
    rid, stage = _staged(rel, slot, required=("ranking", "plans"), optional=optional, as_of=as_of or datetime.now(UTC))
    _ready_all(rel, rid, ["plans"])
    rel.mark_ready(rid, "ranking", artifact_ref="file:///r", artifact_hash=sha("r"), input_version_hash=sha("ri"), snapshot_id=snapshot)
    for key in missing:
        rel.put_component(rid, key, component_state="MISSING", artifact_ref="missing", input_version_hash=sha(key))
    rel.publish(stage.stage_id, producer_version="t")
    conn.commit()
    return rid


def projection(conn, iid, rid, *, view="public_research", scope="public", payload=None):
    conn.execute("INSERT INTO decision.workspace_projection(instrument_id,release_id,view_kind,scope_key,projection_version,projection_payload,"
                 "projection_hash,generated_at) VALUES (%s,%s,%s,%s,1,%s,%s,clock_timestamp())",
                 (iid, rid, view, scope, Jsonb(payload or {"features": [], "plan_refs": []}), sha(f"{iid}{rid}")))
    conn.commit()


def seed_quote_observation(conn, iid, *, event_time, last="11.710", bid="11.700", ask="0.000"):
    raw = uuid4()
    conn.execute("INSERT INTO core.contract_version(contract_id,schema_version,schema_document,schema_hash,compatibility_policy,created_at)"
                 " VALUES ('CanonicalQuote','3.0.0',%s,%s,'strict',clock_timestamp()) ON CONFLICT DO NOTHING", (Jsonb({"type": "object"}), HASH))
    conn.execute("INSERT INTO ingest.raw_object(raw_object_id,source_id,endpoint_id,source_schema_version,source_record_key,content_hash,"
                 "storage_uri,mime_type,source_time_precision,first_seen_at,ingested_at) VALUES (%s,'apisrc','quote','s1',%s,%s,'file:///q',"
                 "'text/plain','second',%s,%s)", (raw, str(raw), sha(str(raw)), event_time, event_time))
    oid = uuid4()
    payload = {"trade_date": event_time.date().isoformat(), "last_price_raw": last, "bid_price_raw": bid, "ask_price_raw": ask,
               "bid_volume_shares": 300, "ask_volume_shares": 0}
    fq = {"*": {"status": "observed", "reason": None, "raw_object_id": str(raw), "value_origin": "primary_source"}}
    conn.execute("INSERT INTO ingest.observation(observation_id,raw_object_id,contract_id,schema_version,mapping_version,entity_type,entity_key,"
                 "natural_key,observation_revision,event_time,available_at,recorded_at,quality_status,canonical_payload,field_quality,content_hash)"
                 " VALUES (%s,%s,'CanonicalQuote','3.0.0','m1','instrument',%s,%s,1,%s,%s,%s,'valid',%s,%s,%s)",
                 (oid, raw, str(iid), f"{iid}|quote|{event_time.isoformat()}", event_time, event_time, event_time, Jsonb(payload), Jsonb(fq), sha(str(oid))))
    conn.commit()
    return oid


