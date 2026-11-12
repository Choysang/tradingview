"""Direct-SQL seeding for the group-C tests (real PostgreSQL, package DDL): instruments, raw bars, a published release with a
market ranking and a plan, claims with impact events, seat reports, ledger accounts. Every helper uses fresh UUIDs, so tests
may share one migrated database."""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "db"))

from w02_helpers import staged, ready_all  # noqa: E402

from pulse.adapters.postgres.release import PgAnalysisRelease  # noqa: E402
from pulse.domain.market.calendar import CST, session_close_utc, session_open_utc  # noqa: E402

UTC = timezone.utc
HASH = "c" * 64
HERE = Path(__file__).resolve().parent


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def seed_sources(conn) -> None:
    conn.execute("INSERT INTO core.contract_version(contract_id,schema_version,schema_document,schema_hash,compatibility_policy,created_at)"
                 " VALUES ('c.test','1.0.0',%s,%s,'strict',clock_timestamp()) ON CONFLICT DO NOTHING", (Jsonb({"type": "object"}), HASH))
    for s in ("csrc", "cninfo_announcements", "eastmoney_calendar"):
        conn.execute("INSERT INTO core.source(source_id,display_name,provider_uri,created_at) VALUES (%s,%s,'urn:x',clock_timestamp()) ON CONFLICT DO NOTHING", (s, s))


def seed_instrument(conn, code: str, name: str = "测试股份", board: str = "main") -> UUID:
    iid = uuid4()
    code = f"{code}-{iid.hex[:6]}"                      # tests share one database: aliases must stay unique
    conn.execute("INSERT INTO core.instrument(instrument_id,instrument_type,venue_code,currency,exchange_timezone,created_at)"
                 " VALUES (%s,'equity','XSHG','CNY','Asia/Shanghai',clock_timestamp())", (iid,))
    conn.execute("INSERT INTO core.instrument_state(instrument_id,state_version,board_code,display_name,is_risk_warning,trading_status,"
                 "effective_from,available_at,evidence_ref) VALUES (%s,1,%s,%s,false,'active','2020-01-01Z',clock_timestamp(),'t')", (iid, board, name))
    conn.execute("INSERT INTO core.instrument_alias(alias_id,source_id,source_symbol,instrument_id,effective_from,available_at,revision,source_anchor)"
                 " VALUES (%s,'csrc',%s,%s,'2020-01-01Z',clock_timestamp(),1,'t')", (uuid4(), code, iid))
    return iid


def seed_manifest(conn, dataset_id: str = "bars_raw_1d", state: str = "READY") -> UUID:
    batch, snap = uuid4(), uuid4()
    conn.execute("INSERT INTO core.source_endpoint(source_id,endpoint_id,source_schema_version,mapping_version,contract_id,schema_version,mapping_hash,enabled,"
                 "transport_kind,stale_after_seconds,policy_document,created_at) VALUES ('csrc','bars','s1','m1','c.test','1.0.0',%s,true,'poll',120,'{}',clock_timestamp())"
                 " ON CONFLICT DO NOTHING", (HASH,))
    conn.execute("INSERT INTO ingest.batch(batch_id,source_id,endpoint_id,source_schema_version,mapping_version,partition_key,batch_state,started_at)"
                 " VALUES (%s,'csrc','bars','s1','m1','p','INGESTING',clock_timestamp())", (batch,))
    ready = state == "READY"
    conn.execute("INSERT INTO market.dataset_manifest(snapshot_id,dataset_id,manifest_version,batch_id,manifest_state,storage_uri,content_hash,contract_id,schema_version,"
                 "row_count,coverage_document,created_at,ready_at,available_watermark) VALUES (%s,%s,(SELECT coalesce(max(manifest_version),0)+1 FROM market.dataset_manifest WHERE dataset_id=%s),"
                 "%s,%s,%s,%s,'c.test','1.0.0',%s,'{}',clock_timestamp(),%s,%s)",
                 (snap, dataset_id, dataset_id, batch, state, "file:///x" if ready else None, sha(str(snap)) if ready else None, 0 if ready else None,
                  datetime.now(UTC) if ready else None, datetime.now(UTC) if ready else None))
    return snap


def seed_bars(conn, iid: UUID, snap: UUID, days: list[date], *, base: Decimal = Decimal("10.00"), step: Decimal = Decimal("1"), volume: int = 100000) -> None:
    for i, d in enumerate(days):
        o = base + step * i
        prev = (base + step * (i - 1)) + Decimal("0.5") if i else None
        conn.execute(
            "INSERT INTO market.bar_projection(trade_date,instrument_id,timeframe,session_scope,bar_open_time,bar_close_time,snapshot_id,currency,open_price_raw,high_price_raw,"
            "low_price_raw,close_price_raw,volume_shares,turnover_amount_cny,exchange_reference_price_raw,is_final,available_at,quality_status)"
            " VALUES (%s,%s,'1d','regular_continuous_v1',%s,%s,%s,'CNY',%s,%s,%s,%s,%s,NULL,%s,true,%s,'partial')",
            (d, iid, session_open_utc(d), session_close_utc(d), snap, o, o + 1, o - 1, o + Decimal("0.5"), volume, prev, session_close_utc(d) + timedelta(hours=2)))


def publish(conn, *, slot: str | None = None, as_of: datetime | None = None, ranking: list | None = None) -> UUID:
    slot = slot or f"c-{uuid4().hex[:8]}"
    rel = PgAnalysisRelease(conn)
    rid, stage = staged(rel, slot, required=("ranking", "plans"), as_of=as_of or datetime.now(UTC))
    ready_all(rel, rid, ["plans", "ranking"])
    if ranking:
        seed_ranking(conn, rid, ranking)                       # a published ranking is immutable: write it before the pointer moves
    rel.publish(stage.stage_id, producer_version="t")
    return rid


def seed_ranking(conn, release_id: UUID, instruments: list[UUID], horizon: str = "5td") -> UUID:
    ranking_id = uuid4()
    conn.execute("INSERT INTO decision.ranking_run(ranking_id,release_id,ranking_scope,scope_key,horizon_id,forecast_target,ranking_policy_version,created_at)"
                 " VALUES (%s,%s,'market',%s,%s,'t','p1',clock_timestamp())", (ranking_id, release_id, f"market/{horizon}", horizon))
    for i, iid in enumerate(instruments, 1):
        conn.execute("INSERT INTO decision.ranking_entry(ranking_id,instrument_id,alpha_rank,relative_rank_score,action_status,explanation_ref) VALUES (%s,%s,%s,%s,'watch','e')",
                     (ranking_id, iid, i, Decimal(100 - i)))
    return ranking_id


def make_plan(instrument_id: UUID, release_id: UUID, *, plan_id: UUID | None = None, version: int = 1) -> dict:
    p = json.loads((HERE / "plan_sample.json").read_text(encoding="utf-8"))
    pid = str(plan_id or uuid4())
    text = json.dumps(p).replace(p["instrument_id"], str(instrument_id)).replace(p["plan_id"], pid)
    p = json.loads(text)
    p.update({"plan_version": version, "based_on_release_id": str(release_id), "account_id": None, "expected_account_version": None, "supersedes": None,
              "available_at": datetime.now(UTC).isoformat().replace("+00:00", "Z")})
    return p


def insert_plan(conn, plan: dict) -> None:
    from pulse.adapters.postgres import decision_repo
    decision_repo.append_plan_version(conn, plan)


def raw_object(conn, source: str = "csrc", payload: str = "x") -> UUID:
    rid = uuid4()
    conn.execute("INSERT INTO ingest.raw_object(raw_object_id,source_id,endpoint_id,source_schema_version,source_record_key,content_hash,storage_uri,mime_type,source_time_precision,"
                 "first_seen_at,ingested_at) VALUES (%s,%s,'bars','s1',%s,%s,'file:///r','text/plain','second',clock_timestamp(),clock_timestamp())",
                 (rid, source, str(rid), sha(payload + str(rid))))
    return rid


def add_claim(conn, *, headline: str, category: str, polarity: str, instruments: list[UUID], available_at: datetime, published_at: datetime | None = None,
              status: str = "unverified", sector_hints: list[str] | None = None, claim_id: UUID | None = None, revision: int = 1) -> dict:
    """A news claim with one document, one impact event naming ``instruments`` (the I3 shapes, written directly)."""
    cid = claim_id or uuid4()
    raw = raw_object(conn, "cninfo_announcements", headline)
    doc_id = uuid4()
    pub = published_at or available_at
    conn.execute("INSERT INTO intelligence.document_revision(document_revision_id,document_id,source_id,source_record_id,revision,raw_object_id,document_status,origin_group,origin_status,"
                 "title,published_at,published_time_precision,first_seen_at,available_at,recorded_at,quality_status,field_quality) VALUES (%s,%s,'cninfo_announcements',%s,1,%s,'published','g','unknown',"
                 "%s,%s,'second',%s,%s,%s,'valid','{}')", (doc_id, uuid4(), str(doc_id), raw, headline, pub, available_at, available_at, available_at))
    crid = uuid4()
    payload = {"headline": headline, "category": category, "polarity_hint": polarity, "sector_hints": sector_hints or [], "cluster": {"seed": str(cid), "policy": "claim-cluster-v1"},
               "verification": {"confirmation": "insufficient", "independent_support_origins": 0}}
    conn.execute("INSERT INTO intelligence.claim_revision(claim_revision_id,claim_id,revision,subject_kind,subject_key,predicate,object_payload,claim_schema_version,assertion_type,claim_status,"
                 "verification_policy_version,available_at,recorded_at,quality_status,event_time) VALUES (%s,%s,%s,'event',NULL,'news.fact_reported',%s,'pulse.intel.news_claim@1',"
                 "'independently_reported',%s,'multi-origin-v1',%s,%s,'valid',%s)", (crid, cid, revision, Jsonb(payload), status, available_at, available_at, pub))
    conn.execute("INSERT INTO intelligence.claim_evidence(claim_revision_id,document_revision_id,evidence_ordinal,relation,evidence_span,extraction_version,available_at,recorded_at)"
                 " VALUES (%s,%s,1,'supports',%s,'t',%s,%s)", (crid, doc_id, Jsonb({"field": "title", "start": 0, "end": 1}), available_at, available_at))
    erid = uuid4()
    impact = {"exposure_status": "named_subject" if instruments else "none", "exposed_instruments": [{"instrument_id": str(i), "code": None, "exposure_basis": "named"} for i in instruments],
              "sector_hints": [{"hint": h, "status": "keyword_hit_not_a_mapping"} for h in (sector_hints or [])]}
    conn.execute("INSERT INTO intelligence.event_revision(event_revision_id,event_id,revision,event_category,taxonomy_version,analysis_payload,analysis_schema_version,analysis_model_version,"
                 "available_at,recorded_at,quality_status) VALUES (%s,%s,1,%s,'t',%s,'impact-mapping-v1','rules',%s,%s,'valid')", (erid, uuid4(), category, Jsonb(impact), available_at, available_at))
    conn.execute("INSERT INTO intelligence.event_claim(event_revision_id,claim_revision_id,relation) VALUES (%s,%s,'core_fact')", (erid, crid))
    return {"claim_id": cid, "claim_revision_id": crid, "document_revision_id": doc_id, "event_revision_id": erid}


def open_account(conn, account_id: UUID | None = None, cash: str = "1000000", day: date = date(2026, 10, 1), positions: dict | None = None) -> UUID:
    from pulse.application.account_service import open_paper_account
    aid = account_id or uuid4()
    open_paper_account(conn, trading_day=day, account_id=aid, initial_cash=cash, positions=positions)
    return aid


def seat_entity(conn, name: str = "华泰证券股份有限公司上海分公司", city: str = "SH") -> UUID:
    seat = uuid4()
    raw = raw_object(conn, "csrc", name)
    conn.execute("INSERT INTO intelligence.seat_entity_version(seat_id,seat_version,entity_kind,canonical_name,broker_legal_name,city_code,effective_from,evidence_raw_object_id,alias_evidence,available_at,recorded_at)"
                 " VALUES (%s,1,'broker_branch',%s,%s,%s,'2020-01-01Z',%s,'[]',%s,%s)", (seat, name, name, city, raw, datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)))
    return seat


def seat_report(conn, instrument: UUID, day: date, rows: list[tuple[str, int, str, Decimal, UUID | None]], *, available_at: datetime, revision: int = 1,
                start: date | None = None, source_report_id: str | None = None) -> UUID:
    raw = raw_object(conn, "csrc", str(day))
    rep = uuid4()
    conn.execute("INSERT INTO intelligence.seat_report(report_revision_id,source_id,raw_object_id,source_report_id,revision,exchange_code,instrument_id,window_start,window_end,disclosure_reason,"
                 "rule_version,trade_scope,available_at,recorded_at,quality_status) VALUES (%s,'csrc',%s,%s,%s,'XSHG',%s,%s,%s,'日涨幅偏离值达7%%','r1','unknown',%s,%s,'valid')",
                 (rep, raw, source_report_id or str(rep), revision, instrument, start or day, day, available_at, available_at))
    for side, rank, name, amount, seat in rows:
        status = "resolved" if seat else ("report_local_label" if name.startswith("机构专用") else "unresolved")
        conn.execute("INSERT INTO intelligence.seat_report_row(report_revision_id,side,rank,raw_seat_name,raw_amount,raw_unit,canonical_amount_cny,resolution_status,seat_id,seat_version,resolution_evidence)"
                     " VALUES (%s,%s,%s,%s,%s,'CNY',%s,%s,%s,%s,'{}')", (rep, side, rank, name, amount, amount, status, seat, 1 if seat else None))
    return rep


def t(y: int, m: int, d: int, hh: int = 12, mm: int = 0) -> datetime:
    return datetime.combine(date(y, m, d), time(hh, mm), tzinfo=CST).astimezone(UTC)
