"""Calendar, event clusters, seats and community opinions: the read side of the intelligence cards (PEX07, PEX08, PEX09; D08-D10).

Why: one place turns what the I3 pipeline already stores (documents, claims, events, seat reports, community rows) and the
versioned calendar into the typed answers of the extension contract, and keeps the calendar itself (sync, revisions, links).
Invariants:
* calendar: a revision is usable only from its ``available_at``; a changed schedule is a new revision (postponed/corrected), a
  vanished source row is not a cancellation; ``expected`` and ``actual`` are separate objects of separate revisions; event ->
  industry links are stored per revision under a rule version and resolve to stocks point in time from the stored memberships
  (``effective_from <= decision_as_of < effective_to`` and ``available_at <= decision_as_of``);
* clusters: a cluster is the claim of the I3 pipeline (reposts of one upstream are documents of ONE claim, they add no independent
  origin); a membership change appends a revision (hash-unique), never edits one; macro events without a named instrument are kept;
* seats: the directory never merges a nickname, an anonymous pool label or two seat versions; a report is read at its knowledge
  cutoff (``available_at <= decision_as_of``, newest revision then), so an after-close report is not intraday knowledge and a
  later correction is unseen; seat statistics are conditional on disclosure (entry = first session open after the report became
  available), keep the denominators, and say None (not 0) below the sample floor; same-seat buy+sell in one report is netted
  INSIDE that report only;
* community: only claims bound to ``pulse.community_forecast@1.0`` appear; an outcome is a separate immutable artifact; a forecast
  without explicit target/horizon never enters accuracy.
Not covered: community forecast extraction (needs a language model), seat intent/position inference (never produced).
"""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from statistics import median
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from pulse import contracts
from pulse.adapters.postgres import ingest_repo
from pulse.adapters.postgres._canon import sha256_of
from pulse.adapters.sources import calendar_em, corp_events
from pulse.adapters.sources.http import SourceBlocked, TransportFailure
from pulse.application import ops_service
from pulse.domain.calendar import events as CE
from pulse.domain.evidence.announcements import NotData, SchemaChanged
from pulse.domain.market_rules.calendar import WeekdayOnlyCalendar

CST = ZoneInfo("Asia/Shanghai")
ZERO_UUID = ops_service.ZERO_UUID
CLUSTER_POLICY = "claim-cluster-v1"
SEAT_POLICY = "seat-profile-v1"
MIN_SAMPLE = 10
HORIZONS = (1, 3, 5)
TAXONOMY = "legacy_boards_industry"
NEWS_TOKEN = "pulse.intel.news_claim@1"
FORECAST_TOKEN = "pulse.community_forecast@1.0"
OUTCOME_CONTRACT = "pulse.community_outcome"


class IntelError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _public_release(conn: psycopg.Connection, release_id: UUID) -> dict:
    r = conn.execute("SELECT * FROM decision.release WHERE release_id=%s", (release_id,)).fetchone()
    if r is None or r["release_state"] not in ("READY", "SUPERSEDED"):
        raise IntelError(404, "NOT_FOUND", "发布不存在")
    return r


# ---- calendar: ingest -------------------------------------------------------------------------------------------------------------
def _instrument_for_code(conn: psycopg.Connection, code: str | None) -> UUID | None:
    if not code:
        return None
    r = conn.execute("SELECT instrument_id FROM core.instrument_alias WHERE source_symbol=%s ORDER BY effective_from DESC LIMIT 1", (code,)).fetchone()
    return r["instrument_id"] if r else None


def ingest_observations(conn: psycopg.Connection, obs: list[CE.Observation], *, available_at: datetime, now: datetime | None = None,
                        raw_object_id: UUID | None = None) -> dict:
    """Append revisions for changed/new observations; write industry links; close with a snapshot. Idempotent."""
    now = now or ops_service.now_utc()
    s = {"new": 0, "revised": 0, "same": 0, "links": 0}
    seen: dict[uuid.UUID, str] = {}
    for o in obs:
        cid = o.event_id
        prev = conn.execute("SELECT * FROM calendar.event_revision WHERE calendar_event_id=%s ORDER BY revision DESC LIMIT 1", (cid,)).fetchone()
        kind, rev, status = CE.next_revision(prev, o)
        if kind == "same":
            s["same"] += 1
            continue
        actual = o.actual if o.actual is not None else (prev["actual"] if prev else None)   # a source that stops listing the value is not a retraction
        final = CE.finalize(CE.Observation(**{**o.__dict__, "actual": actual}), status)
        h = CE.content_hash(final)
        with conn.transaction():
            conn.execute(
                "INSERT INTO calendar.event_revision(calendar_event_id, revision, source_id, source_event_key, name, category, region, scheduled_at, scheduled_date,"
                " time_precision, status, expected, actual, instrument_id, raw_object_id, content_hash, available_at, recorded_at, notes)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (cid, rev, o.source_id, o.source_event_key, o.name, o.category, o.region, o.scheduled_at, o.scheduled_date, o.time_precision, status,
                 Jsonb(contracts.to_wire(o.expected)) if o.expected else None, Jsonb(contracts.to_wire(actual)) if actual else None,
                 _instrument_for_code(conn, o.instrument_code), raw_object_id, h, available_at, now, Jsonb(o.notes)))
            for board, theme in CE.links_for(o):
                node = conn.execute("SELECT taxonomy_version, node_id FROM core.taxonomy_node WHERE taxonomy_id=%s AND display_name=%s", (TAXONOMY, board)).fetchone()
                if node:
                    conn.execute("INSERT INTO calendar.event_link(calendar_event_id, revision, link_rule_version, taxonomy_id, taxonomy_version, node_id, industry_hint, theme)"
                                 " VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                                 (cid, rev, CE.LINK_RULE_VERSION, TAXONOMY, node["taxonomy_version"], node["node_id"], board, theme))
                    s["links"] += 1
        s[kind] += 1
        seen[cid] = h
    if s["new"] or s["revised"]:
        digest = sha256_of(sorted(f"{k}:{v}" for k, v in seen.items()))
        conn.execute("INSERT INTO calendar.snapshot(snapshot_id, created_at, source_id, event_count, content_hash) VALUES (%s,%s,%s,%s,%s)",
                     (uuid.uuid5(CE.CALENDAR_NS, f"snapshot|{digest}|{now.isoformat()}"), now, obs[0].source_id if obs else "mixed", s["new"] + s["revised"], digest))
    return s


def derive_company_events(conn: psycopg.Connection, now: datetime | None = None) -> dict:
    """Lock-up / suspension / meeting events whose date is explicit in an ingested announcement title."""
    now = now or ops_service.now_utc()
    rows = conn.execute(
        "SELECT DISTINCT ON (c.claim_id) c.subject_key issuer_code, c.object_payload->>'title' title, c.available_at announced_at,"
        " c.object_payload->>'source_record_id' source_record_id FROM intelligence.claim_revision c WHERE c.claim_schema_version='announcement-claim-v1'"
        " AND c.object_payload->>'title' ~ '限售|解禁|停牌|复牌|股东大会|股东会|说明会' ORDER BY c.claim_id, c.revision DESC").fetchall()
    s = {"announcements": len(rows), "date_unresolved": 0, "events": 0, "new": 0, "revised": 0, "same": 0, "links": 0}
    for r in rows:
        r = {**r, "display_name": None}
        parsed = corp_events.to_observations([r])
        s["date_unresolved"] += parsed.date_unresolved
        s["events"] += len(parsed.observations)
        if parsed.observations:
            got = ingest_observations(conn, parsed.observations, available_at=r["announced_at"], now=now)
            for k in ("new", "revised", "same", "links"):
                s[k] += got[k]
    return s


def add_rule_expiries(conn: psycopg.Connection, year: int, month: int, now: datetime | None = None) -> dict:
    from pulse.application.portfolio_service import calendar
    now = now or ops_service.now_utc()
    cal = calendar(conn)
    return ingest_observations(conn, CE.expiry_observations(year, month, cal.is_trading_day), available_at=now, now=now)


def sync(conn: psycopg.Connection, adapter, lake, *, days: int = 60, now: datetime | None = None) -> dict:
    """Pull the Eastmoney calendar (<= 1 request/s through the adapter's client) into revisions. A block stops at once."""
    from pulse.adapters.postgres import intelligence_repo as repo
    from pulse.application.intelligence_service import IntelligenceService

    now = now or ops_service.now_utc()
    svc = IntelligenceService(conn, lake, clock=lambda: now)
    with conn.transaction():
        repo.ensure_sources(conn, calendar_em.SOURCE_ROWS, now)
        schema = {"type": "object", "description": "calendar event row (see calendar.event_revision)", "x-owner": "extension group C"}
        conn.execute("INSERT INTO core.contract_version(contract_id, schema_version, schema_document, schema_hash, compatibility_policy, created_at)"
                     " VALUES (%s,%s,%s,%s,'strict-object',%s) ON CONFLICT DO NOTHING", (calendar_em.SPEC.contract_id, calendar_em.SPEC.contract_id and "3.0.0", Jsonb(schema), sha256_of(schema), now))
        ingest_repo.ensure_endpoint(conn, calendar_em.SPEC, now)
    spec, part = calendar_em.SPEC, f"next{days}d"
    batch_id, owner = svc._batch(spec, part)
    summary: dict[str, Any] = {"pages": 0, "rows": 0, "blocked": False}
    rows: list[dict] = []
    raw_first: UUID | None = None
    try:
        page, pages = 1, 1
        start = now.astimezone(CST).date()
        while page <= min(pages, calendar_em.MAX_PAGES):
            cap = svc._fetch(spec, part, lambda p=page: adapter.page(start, days, p))
            rid = svc._store(spec, batch_id, cap, owner)
            raw_first = raw_first or rid
            try:
                got, pages = calendar_em.parse_page(cap.body)
            except (NotData, SchemaChanged) as exc:
                summary["error"] = f"{type(exc).__name__}: {exc}"
                break
            rows += got
            summary["pages"] += 1
            page += 1
        with conn.transaction():
            ingest_repo.commit_raw(conn, batch_id, now)
        if "error" not in summary:
            summary["rows"] = len(rows)
            summary.update(ingest_observations(conn, calendar_em.to_observations(rows), available_at=now, now=now, raw_object_id=raw_first))
            svc._finish_batch(spec, part, batch_id, {"last_sync": now.isoformat()}, now)
    except SourceBlocked:
        summary["blocked"] = True
        svc._blocked(batch_id, owner, ops_service_summary(), "eastmoney_calendar")
        raise
    except TransportFailure as exc:
        summary["transport_error"] = str(exc)[:120]
    finally:
        lake.release(owner)
    return summary


def ops_service_summary():
    from pulse.application.data_service import Summary
    return Summary("calendar")


# ---- calendar: reads --------------------------------------------------------------------------------------------------------------
def _wire_event(r: dict) -> dict:
    def meas(m: dict | None) -> dict | None:
        return None if m is None else {"value": m.get("value"), "text_value": m.get("text_value"), "unit": m.get("unit"),
                                       "reference_period": m.get("reference_period", ""), "source_revision_id": m.get("source_revision_id") or str(r["raw_object_id"] or ZERO_UUID)}
    return {"calendar_event_id": r["calendar_event_id"], "revision": r["revision"], "scheduled_at": r["scheduled_at"], "scheduled_date": r["scheduled_date"],
            "time_precision": r["time_precision"], "status": r["status"], "expected": meas(r["expected"]), "actual": meas(r["actual"]), "available_at": r["available_at"],
            "name": r["name"]}


def query_calendar_events(conn: psycopg.Connection, snapshot_id: UUID, decision_as_of: datetime, window: dict, limit: int, offset: int) -> tuple[list[dict], bool]:
    cutoff = None
    if snapshot_id != ZERO_UUID:
        s = conn.execute("SELECT created_at FROM calendar.snapshot WHERE snapshot_id=%s", (snapshot_id,)).fetchone()
        if s is None:
            raise IntelError(404, "NOT_FOUND", "日历快照不存在")
        cutoff = s["created_at"]
    # the LATEST visible revision decides whether an event is in the window (an old date must not hide a postponement)
    rows = conn.execute(
        "SELECT * FROM calendar.event_revision WHERE available_at <= %s AND (%s::timestamptz IS NULL OR recorded_at <= %s)"
        " AND calendar_event_id IN (SELECT calendar_event_id FROM calendar.event_revision WHERE available_at <= %s AND (%s::timestamptz IS NULL OR recorded_at <= %s)"
        " AND COALESCE(scheduled_at, scheduled_date::timestamp AT TIME ZONE 'Asia/Shanghai') >= %s"
        " AND COALESCE(scheduled_at, scheduled_date::timestamp AT TIME ZONE 'Asia/Shanghai') < %s)",
        (decision_as_of, cutoff, cutoff, decision_as_of, cutoff, cutoff, window["start"], window["end"])).fetchall()

    def when(r: dict) -> datetime:
        return r["scheduled_at"] or datetime.combine(r["scheduled_date"], time(0, 0), CST)
    latest = [r for r in CE.visible(rows, decision_as_of) if window["start"] <= when(r) < window["end"]]
    page = latest[offset:offset + limit]
    return [_wire_event(r) for r in page], offset + limit < len(latest)


def event_links(conn: psycopg.Connection, calendar_event_id: UUID, decision_as_of: datetime) -> list[dict]:
    rev = conn.execute("SELECT max(revision) r FROM calendar.event_revision WHERE calendar_event_id=%s AND available_at <= %s", (calendar_event_id, decision_as_of)).fetchone()["r"]
    if rev is None:
        return []
    out = []
    for l in conn.execute("SELECT * FROM calendar.event_link WHERE calendar_event_id=%s AND revision=%s ORDER BY node_id", (calendar_event_id, rev)).fetchall():
        stocks = conn.execute(
            "SELECT DISTINCT t.instrument_id, s.display_name, (SELECT source_symbol FROM core.instrument_alias a WHERE a.instrument_id=t.instrument_id ORDER BY source_symbol DESC LIMIT 1) code"
            " FROM core.instrument_tag t JOIN LATERAL (SELECT display_name FROM core.instrument_state x WHERE x.instrument_id=t.instrument_id ORDER BY state_version DESC LIMIT 1) s ON true"
            " WHERE t.taxonomy_id=%s AND t.taxonomy_version=%s AND t.node_id=%s AND t.effective_from <= %s AND (t.effective_to IS NULL OR t.effective_to > %s)"
            " AND t.available_at <= %s ORDER BY code LIMIT 200",
            (l["taxonomy_id"], l["taxonomy_version"], l["node_id"], decision_as_of, decision_as_of, decision_as_of)).fetchall()
        out.append({"industry_node_id": l["node_id"], "industry_name": l["industry_hint"], "link_rule_version": l["link_rule_version"],
                    "instruments": [{"instrument_id": x["instrument_id"], "code": x["code"], "name": x["display_name"]} for x in stocks]})
    return out


# ---- clusters (queryEventClusters, D08) -------------------------------------------------------------------------------------------
def _instrument_filter(conn: psycopg.Connection, scope: dict) -> tuple[str, str | None]:
    kind, key = scope["scope_kind"], scope["scope_key"]
    if kind == "market":
        return "market", None
    if kind in ("instrument", "sector"):
        return kind, key
    raise IntelError(400, "INVALID_CONTRACT", f"不支持的范围 {kind}（market / instrument / sector）")


def materialize_clusters(conn: psycopg.Connection, decision_as_of: datetime, since: datetime, until: datetime, now: datetime | None = None) -> int:
    """Append a cluster revision for every claim whose membership differs from its latest stored revision. Returns appended count."""
    now = now or ops_service.now_utc()
    claims = conn.execute("SELECT DISTINCT ON (claim_id) * FROM intelligence.claim_revision WHERE claim_schema_version=%s AND available_at <= %s AND available_at >= %s AND available_at < %s"
                          " ORDER BY claim_id, revision DESC", (NEWS_TOKEN, decision_as_of, since, until)).fetchall()
    n = 0
    for c in claims:
        docs = [r["document_revision_id"] for r in conn.execute("SELECT document_revision_id FROM intelligence.claim_evidence WHERE claim_revision_id=%s ORDER BY evidence_ordinal", (c["claim_revision_id"],))]
        evs = [r["event_revision_id"] for r in conn.execute("SELECT event_revision_id FROM intelligence.event_claim WHERE claim_revision_id=%s ORDER BY event_revision_id", (c["claim_revision_id"],))]
        inst = sorted({UUID(i["instrument_id"]) for r in conn.execute(
            "SELECT e.analysis_payload->'exposed_instruments' ex FROM intelligence.event_claim ec JOIN intelligence.event_revision e USING (event_revision_id) WHERE ec.claim_revision_id=%s", (c["claim_revision_id"],))
            for i in (r["ex"] or [])}, key=str)
        mh = sha256_of({"claims": [str(c["claim_revision_id"])], "docs": sorted(map(str, docs)), "events": sorted(map(str, evs))})
        last = conn.execute("SELECT revision, membership_hash FROM intelligence.cluster_revision WHERE cluster_id=%s ORDER BY revision DESC LIMIT 1", (c["claim_id"],)).fetchone()
        if last and last["membership_hash"] == mh:
            continue
        origins = (c["object_payload"].get("verification") or {}).get("independent_support_origins", 0)
        conn.execute("INSERT INTO intelligence.cluster_revision(cluster_id, revision, membership_hash, claim_revision_ids, document_revision_ids, event_revision_ids,"
                     " instrument_ids, independent_origin_count, available_at, clustering_policy_version, recorded_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                     (c["claim_id"], (last["revision"] + 1) if last else 1, mh, [c["claim_revision_id"]], docs, evs, inst, origins, c["available_at"], CLUSTER_POLICY, now))
        n += 1
    return n


def query_event_clusters(conn: psycopg.Connection, req: dict, limit: int, after: tuple | None) -> tuple[list[dict], tuple | None]:
    rel = _public_release(conn, req["release_id"])
    kind, key = _instrument_filter(conn, req["scope"])
    win = req["window"]
    hi = min(win["end"], rel["decision_as_of"] + timedelta(microseconds=1))
    if req.get("cluster_id") is not None:
        if req.get("cluster_revision") is None:
            raise IntelError(400, "INVALID_CONTRACT", "cluster_id 与 cluster_revision 必须成对")
        rows = conn.execute("SELECT * FROM intelligence.cluster_revision WHERE cluster_id=%s AND revision=%s AND available_at <= %s",
                            (req["cluster_id"], req["cluster_revision"], rel["decision_as_of"])).fetchall()
        return [_wire_cluster(r) for r in rows], None
    materialize_clusters(conn, rel["decision_as_of"], win["start"], hi)
    sql = ("SELECT DISTINCT ON (c.cluster_id) c.* FROM intelligence.cluster_revision c WHERE c.available_at <= %s AND c.available_at >= %s AND c.available_at < %s")
    args: list[Any] = [rel["decision_as_of"], win["start"], hi]
    if kind == "instrument":
        sql += " AND %s = ANY(c.instrument_ids)"
        args.append(UUID(key))
    sql += " ORDER BY c.cluster_id, c.revision DESC"
    rows = conn.execute(sql, args).fetchall()
    if kind == "sector":
        node = conn.execute("SELECT display_name FROM core.taxonomy_node WHERE taxonomy_id=%s AND node_id=%s", (TAXONOMY, key)).fetchone()
        if node is None:
            raise IntelError(404, "NOT_FOUND", "板块不存在")
        from pulse.domain.alerts.tiers import industries_for
        keep = []
        for r in rows:
            head = conn.execute("SELECT object_payload->>'headline' h FROM intelligence.claim_revision WHERE claim_revision_id = ANY(%s) LIMIT 1", (r["claim_revision_ids"],)).fetchone()
            if head and node["display_name"] in industries_for(head["h"] or ""):
                keep.append(r)
        rows = keep
    rows.sort(key=lambda r: (r["available_at"], str(r["cluster_id"])), reverse=True)
    if after is not None:
        rows = [r for r in rows if (r["available_at"], str(r["cluster_id"])) < (after[0], after[1])]
    more = len(rows) > limit
    rows = rows[:limit]
    return [_wire_cluster(r) for r in rows], ((rows[-1]["available_at"], str(rows[-1]["cluster_id"])) if more and rows else None)


def _wire_cluster(r: dict) -> dict:
    return {"cluster_id": r["cluster_id"], "revision": r["revision"], "claim_revision_ids": r["claim_revision_ids"], "document_revision_ids": r["document_revision_ids"],
            "event_revision_ids": r["event_revision_ids"], "available_at": r["available_at"], "clustering_policy_version": r["clustering_policy_version"]}


# ---- seats (PEX08, D10) ------------------------------------------------------------------------------------------------------------
def _cutoff(conn: psycopg.Connection, snapshot_id: UUID) -> datetime:
    if snapshot_id == ZERO_UUID:
        return ops_service.now_utc()
    m = conn.execute("SELECT ready_at FROM market.dataset_manifest WHERE snapshot_id=%s AND manifest_state='READY'", (snapshot_id,)).fetchone()
    if m is None:
        raise IntelError(404, "NOT_FOUND", "目录快照不存在或未就绪")
    return m["ready_at"]


def query_seat_directory(conn: psycopg.Connection, snapshot_id: UUID, name_query: str, limit: int, offset: int) -> tuple[list[dict], bool]:
    cutoff = _cutoff(conn, snapshot_id)
    q = name_query.strip()
    if not q:
        raise IntelError(400, "INVALID_CONTRACT", "name_query 不能为空")
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    out: list[dict] = []
    for r in conn.execute("SELECT * FROM intelligence.seat_entity_version WHERE available_at <= %s AND (canonical_name ILIKE %s OR broker_legal_name ILIKE %s OR alias_evidence::text ILIKE %s)"
                          " ORDER BY canonical_name, seat_id, seat_version", (cutoff, like, like, like)):
        out.append({"seat_id": r["seat_id"], "seat_version": r["seat_version"], "display_name": r["canonical_name"], "resolution_status": "resolved", "report_local_ref": None})
    for r in conn.execute(
            "SELECT DISTINCT ON (r.raw_seat_name, r.resolution_status) r.raw_seat_name, r.resolution_status, r.report_revision_id, r.side, r.rank FROM intelligence.seat_report_row r"
            " JOIN intelligence.seat_report p USING (report_revision_id) WHERE p.available_at <= %s AND r.resolution_status <> 'resolved' AND r.raw_seat_name ILIKE %s"
            " ORDER BY r.raw_seat_name, r.resolution_status, p.available_at DESC LIMIT 200", (cutoff, like)):
        out.append({"seat_id": None, "seat_version": None, "display_name": r["raw_seat_name"], "resolution_status": r["resolution_status"],
                    "report_local_ref": {"report_revision_id": r["report_revision_id"], "side": r["side"], "rank": r["rank"]}})
    page = out[offset:offset + limit]
    return page, offset + limit < len(out)


def query_seat_reports(conn: psycopg.Connection, snapshot_id: UUID, trade_date: date, decision_as_of: datetime, limit: int, after: UUID | None) -> tuple[list[dict], UUID | None]:
    cutoff = min(_cutoff(conn, snapshot_id), decision_as_of) if snapshot_id != ZERO_UUID else decision_as_of
    reps = conn.execute(
        "SELECT * FROM (SELECT DISTINCT ON (source_id, source_report_id) * FROM intelligence.seat_report WHERE window_end=%s AND available_at <= %s"
        " ORDER BY source_id, source_report_id, revision DESC) latest ORDER BY report_revision_id", (trade_date, cutoff)).fetchall()
    if after is not None:
        reps = [r for r in reps if str(r["report_revision_id"]) > str(after)]
    more = len(reps) > limit
    reps = reps[:limit]
    ids = [r["report_revision_id"] for r in reps]
    rows = conn.execute("SELECT * FROM intelligence.seat_report_row WHERE report_revision_id = ANY(%s) ORDER BY report_revision_id, side, rank", (ids,)).fetchall() if ids else []
    by: dict[UUID, list] = {}
    for r in rows:
        by.setdefault(r["report_revision_id"], []).append({k: r[k] for k in ("side", "rank", "raw_seat_name", "canonical_amount_cny", "resolution_status", "seat_id", "seat_version", "resolution_evidence")})
    out = [{**{k: r[k] for k in ("report_revision_id", "instrument_id", "source_id", "revision", "window_start", "window_end", "disclosure_reason", "rule_version",
                                  "published_at", "available_at", "quality_status")}, "rows": by.get(r["report_revision_id"], [])} for r in reps]
    return out, (ids[-1] if more else None)


def _opens_after(conn: psycopg.Connection, iid: UUID, after: datetime, n: int, as_of: datetime) -> list[Decimal]:
    """Raw opens of the first ``n`` sessions that open after ``after`` and were known by ``as_of`` (one query per report)."""
    return [r["open_price_raw"] for r in conn.execute(
        "SELECT open_price_raw FROM market.bar_projection WHERE instrument_id=%s AND timeframe='1d' AND open_price_raw IS NOT NULL AND bar_open_time > %s"
        " AND available_at <= %s ORDER BY bar_open_time LIMIT %s", (iid, after, as_of, n)).fetchall()]


def query_seat_profile(conn: psycopg.Connection, principal: UUID | None, req: dict) -> dict:
    if req["policy_version"] != SEAT_POLICY:
        raise IntelError(400, "INVALID_CONTRACT", f"未登记的统计政策 {req['policy_version']}（可用：{SEAT_POLICY}）")
    seat = conn.execute("SELECT * FROM intelligence.seat_entity_version WHERE seat_id=%s AND seat_version=%s AND available_at <= %s",
                        (req["seat_id"], req["seat_version"], req["decision_as_of"])).fetchone()
    if seat is None:
        raise IntelError(404, "NOT_FOUND", "席位实体不存在或在截止时点尚不可得")
    win, asof = req["window"], req["decision_as_of"]
    existing = conn.execute("SELECT * FROM intelligence.seat_profile WHERE seat_id=%s AND seat_version=%s AND window_start=%s AND window_end=%s AND decision_as_of=%s AND policy_version=%s",
                            (req["seat_id"], req["seat_version"], win["start"], win["end"], asof, SEAT_POLICY)).fetchone()
    if existing:
        a = conn.execute("SELECT ref, content_hash, contract_id, schema_version FROM ops.ext_artifact WHERE ref=%s", (existing["artifact_ref"],)).fetchone()
        return {"profile": dict(a), "sample_count": existing["sample_count"], "limitations": existing["limitations"]}
    reps = conn.execute(
        "SELECT p.*, r.side, r.canonical_amount_cny FROM (SELECT DISTINCT ON (source_id, source_report_id) * FROM intelligence.seat_report WHERE window_end >= %s AND window_end < %s"
        " AND available_at <= %s ORDER BY source_id, source_report_id, revision DESC) p JOIN intelligence.seat_report_row r ON r.report_revision_id=p.report_revision_id"
        " JOIN intelligence.seat_entity_version e ON e.seat_id=r.seat_id AND e.seat_version=r.seat_version AND e.available_at <= %s"
        " WHERE r.seat_id=%s AND r.resolution_status='resolved'",
        (win["start"].astimezone(CST).date(), win["end"].astimezone(CST).date(), asof, asof, req["seat_id"])).fetchall()
    per: dict[UUID, dict] = {}
    for r in reps:                                   # same seat on both sides of ONE report: both kept, net derived inside the report only
        d = per.setdefault(r["report_revision_id"], {"rep": r, "buy": Decimal(0), "sell": Decimal(0)})
        d[r["side"]] += r["canonical_amount_cny"]
    stats: dict[str, dict[int, list[Decimal]]] = {"single_day": {n: [] for n in HORIZONS}, "multi_day": {n: [] for n in HORIZONS}}
    missing = {n: 0 for n in HORIZONS}
    gross_buy = gross_sell = Decimal(0)
    for d in per.values():
        gross_buy += d["buy"]
        gross_sell += d["sell"]
        net = d["buy"] - d["sell"]
        rep = d["rep"]
        if net <= 0:
            continue
        layer = "single_day" if rep["window_start"] == rep["window_end"] else "multi_day"
        opens = _opens_after(conn, rep["instrument_id"], rep["available_at"], 1 + max(HORIZONS), asof)
        entry = opens[0] if opens else None
        for n in HORIZONS:
            exit_ = opens[n] if len(opens) > n else None
            if entry is None or exit_ is None or entry == 0:
                missing[n] += 1
            else:
                stats[layer][n].append(exit_ / entry - 1)
    metrics = [{"name": "report_count", "definition_version": SEAT_POLICY, "value": Decimal(len(per)), "unit": "count", "sample_count": len(per), "missing_reason": None},
               {"name": "gross_buy_cny", "definition_version": SEAT_POLICY, "value": gross_buy if per else None, "unit": "CNY", "sample_count": len(per),
                "missing_reason": None if per else "no_reports_in_window"},
               {"name": "gross_sell_cny", "definition_version": SEAT_POLICY, "value": gross_sell if per else None, "unit": "CNY", "sample_count": len(per),
                "missing_reason": None if per else "no_reports_in_window"}]
    for layer, byn in stats.items():
        for n, vals in byn.items():
            ok = len(vals) >= MIN_SAMPLE
            metrics.append({"name": f"net_buy_forward_{n}d_mean_return.{layer}", "definition_version": SEAT_POLICY, "value": (sum(vals) / len(vals)) if ok else None, "unit": "ratio",
                            "sample_count": len(vals), "missing_reason": None if ok else "insufficient_sample"})
            metrics.append({"name": f"net_buy_forward_{n}d_median_return.{layer}", "definition_version": SEAT_POLICY, "value": median(vals) if ok else None, "unit": "ratio",
                            "sample_count": len(vals), "missing_reason": None if ok else "insufficient_sample"})
    limitations = ["披露只含每侧前列席位，不是完整成交", "只统计披露可得之后的表现（入场=披露可得后的首个开盘）", "使用原始未复权价格，除权窗口未剔除",
                   f"样本不足 {MIN_SAMPLE} 个时不给统计值", "不推断持仓、意图或后续行为；地域标签不构成规则", "同报告同席位买卖双侧各自保留，只在该报告内净额",
                   *(["有 %d 个报告缺少后续行情，已从相应统计剔除" % max(missing.values())] if any(missing.values()) else [])]
    body = {"seat": {"seat_id": seat["seat_id"], "seat_version": seat["seat_version"], "display_name": seat["canonical_name"], "resolution_status": "resolved"},
            "window": win, "decision_as_of": asof, "policy_version": SEAT_POLICY, "metrics": metrics, "report_revision_ids": sorted(per, key=str), "sample_count": len(per),
            "limitations": limitations}
    art = ops_service.put_artifact(conn, kind="seat-profile", contract_id="pulse.seat_profile", schema_version="0.1", body=contracts.to_wire(body))
    conn.execute("INSERT INTO intelligence.seat_profile(seat_id, seat_version, window_start, window_end, decision_as_of, policy_version, artifact_ref, sample_count, limitations,"
                 " report_revision_ids) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                 (req["seat_id"], req["seat_version"], win["start"], win["end"], asof, SEAT_POLICY, art["ref"], len(per), Jsonb(limitations), sorted(per, key=str)))
    return {"profile": art, "sample_count": len(per), "limitations": limitations}


# ---- community opinions (PEX09) -----------------------------------------------------------------------------------------------------
def query_community_opinions(conn: psycopg.Connection, req: dict, limit: int, after: tuple | None) -> tuple[list[dict], tuple | None]:
    rel = _public_release(conn, req["release_id"])
    kind, key = _instrument_filter(conn, req["scope"])
    sql = ("SELECT DISTINCT ON (c.claim_id) c.* FROM intelligence.claim_revision c WHERE c.claim_schema_version=%s AND c.available_at <= %s")
    args: list[Any] = [FORECAST_TOKEN, rel["decision_as_of"]]
    if kind == "instrument":
        sql += " AND c.subject_key=%s"
        args.append(key)
    rows = conn.execute(sql + " ORDER BY c.claim_id, c.revision DESC", args).fetchall()
    rows.sort(key=lambda r: (r["available_at"], str(r["claim_revision_id"])))
    if after is not None:
        rows = [r for r in rows if (r["available_at"], str(r["claim_revision_id"])) > (after[0], after[1])]
    more = len(rows) > limit
    rows = rows[:limit]
    out = []
    for r in rows:
        ev = conn.execute("SELECT document_revision_id FROM intelligence.claim_evidence WHERE claim_revision_id=%s ORDER BY evidence_ordinal LIMIT 1", (r["claim_revision_id"],)).fetchone()
        o = conn.execute("SELECT body FROM ops.ext_artifact WHERE contract_id=%s AND body->>'claim_revision_id'=%s AND body->>'computed_at' <= %s"
                         " AND (body->>'label_available_at' IS NULL OR body->>'label_available_at' <= %s) ORDER BY created_at DESC LIMIT 1",
                         (OUTCOME_CONTRACT, str(r["claim_revision_id"]), contracts.utc_z(req["outcome_as_of"]), contracts.utc_z(req["outcome_as_of"]))).fetchone()
        out.append({"claim_revision_id": r["claim_revision_id"], "document_revision_id": ev["document_revision_id"] if ev else ZERO_UUID,
                    "forecast": r["object_payload"], "outcome": o["body"] if o else None})
    return out, ((rows[-1]["available_at"], str(rows[-1]["claim_revision_id"])) if more and rows else None)


@ops_service.register_handler("CalendarService.sync")
def _job_sync(conn, run, args) -> dict:
    from pulse import config
    from pulse.adapters.lake import LakeStore
    from pulse.adapters.sources.http import RateLimitedClient
    client = RateLimitedClient(min_interval_seconds=1.0)
    try:
        lake = LakeStore(config.load().lake_root)
        s = sync(conn, calendar_em.CalendarAdapter(client), lake, days=int(args["params"].get("days", 60)))
    finally:
        client.close()
    s["company_events"] = derive_company_events(conn)
    return s
