"""Intelligence persistence: news documents, claims, events, community, seats, funding, model policy/run (intelligence.*, analysis.*).

Why: SQL for the I3 package kept in one repository. Facts are append-only revisions with deterministic uuid5 ids
so a replay is idempotent; ``available_at`` is always the actual completion time (>= first_seen_at).

Invariants: documents/claims/events/reports only append revisions; a claim revision carries ALL its evidence
links (state at that revision), so older revisions stay replayable; seat names are stored verbatim and the
entity is attached only when the resolver said ``resolved``; funding rows never write 0 for unknown; model runs
are inserted ``running`` then finished exactly once. Payload schemas are validated by the caller
(``domain.intelligence.schemas``) before insert.
Not covered: transaction boundaries (callers use short explicit transactions), HTTP, lake IO.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, Sequence

import psycopg
from psycopg.types.json import Jsonb

from pulse.adapters.postgres import ingest_repo
from pulse.adapters.postgres._canon import sha256_of
from pulse.domain.intelligence import schemas
from pulse.domain.intelligence.seats import SeatReportRec, norm_amount

NS = uuid.UUID("2d8f4a61-b3c7-4e90-a512-6f0e9d1c7b38")


def _id(*parts: object) -> uuid.UUID:
    return uuid.uuid5(NS, "|".join(str(p) for p in parts))


# ---- bootstrap ---------------------------------------------------------------------------------------------
def ensure_sources(conn: psycopg.Connection, rows: dict[str, tuple[str, str]], now: datetime) -> None:
    for sid, (name, uri) in rows.items():
        conn.execute("INSERT INTO core.source(source_id, display_name, provider_uri, created_at) VALUES (%s,%s,%s,%s) ON CONFLICT (source_id) DO NOTHING",
                     (sid, name, uri, now))


def ensure_intel_contracts(conn: psycopg.Connection, now: datetime) -> None:
    """Register the package contracts used here plus the named JSON Schemas of the intelligence payloads."""
    ingest_repo.ensure_contracts(conn, now, ("EvidenceDocument", "CommunityObservation", "SeatReport", "FundFlowObservation"))
    for (cid, ver), doc in schemas.SCHEMAS.items():
        conn.execute(
            "INSERT INTO core.contract_version(contract_id, schema_version, schema_document, schema_hash, compatibility_policy, created_at)"
            " VALUES (%s,%s,%s,%s,'additive-optional-v1',%s) ON CONFLICT (contract_id, schema_version) DO NOTHING", (cid, ver, Jsonb(doc), sha256_of(doc), now))
    from pulse import contracts
    from pulse.domain.intelligence import forecast

    sup = contracts._load("supplemental.schema.json")["$defs"]["CommunityForecastPayload"]
    conn.execute(
        "INSERT INTO core.contract_version(contract_id, schema_version, schema_document, schema_hash, compatibility_policy, created_at)"
        " VALUES (%s,%s,%s,%s,'additive-optional-v1',%s) ON CONFLICT (contract_id, schema_version) DO NOTHING",
        (forecast.CONTRACT_ID, forecast.SCHEMA_VERSION, Jsonb(sup), sha256_of(sup), now))


# ---- news documents ------------------------------------------------------------------------------------------
def news_document_id(source_id: str, source_record_id: str) -> uuid.UUID:
    return _id("news_doc", source_id, source_record_id)


def latest_document(conn: psycopg.Connection, source_id: str, source_record_id: str) -> dict | None:
    return conn.execute("SELECT * FROM intelligence.document_revision WHERE source_id=%s AND source_record_id=%s ORDER BY revision DESC LIMIT 1",
                        (source_id, source_record_id)).fetchone()


def insert_news_document(conn: psycopg.Connection, *, source_id: str, source_record_id: str, revision: int, raw_object_id: uuid.UUID, status: str,
                         origin_group: str | None, origin_status: str, title: str | None, published_at: datetime | None, precision: str,
                         first_seen_at: datetime, available_at: datetime, now: datetime, field_quality: dict, quality_status: str = "valid",
                         document_id: uuid.UUID | None = None) -> uuid.UUID:
    rid = _id("news_doc_rev", source_id, source_record_id, revision)
    conn.execute(
        "INSERT INTO intelligence.document_revision(document_revision_id, document_id, source_id, source_record_id, revision, raw_object_id,"
        " document_status, origin_group, origin_status, original_publisher_source_id, title, language_code, published_at, published_time_precision,"
        " event_time, first_seen_at, available_at, recorded_at, quality_status, field_quality)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s,'zh',%s,%s,NULL,%s,%s,%s,%s,%s)"
        " ON CONFLICT (source_id, source_record_id, revision) DO NOTHING",
        (rid, document_id or news_document_id(source_id, source_record_id), source_id, source_record_id, revision, raw_object_id, status, origin_group, origin_status,
         title, published_at, precision, first_seen_at, available_at, now, quality_status, Jsonb(field_quality)))
    return rid


def unprocessed_documents(conn: psycopg.Connection, source_ids: Sequence[str], limit: int) -> list[dict]:
    """Latest revisions of news documents that no claim revision cites yet (a superseded revision is covered by its successor)."""
    return conn.execute(
        "SELECT d.* FROM intelligence.document_revision d WHERE d.source_id = ANY(%s)"
        " AND d.revision = (SELECT max(x.revision) FROM intelligence.document_revision x WHERE x.document_id=d.document_id)"
        " AND NOT EXISTS (SELECT 1 FROM intelligence.claim_evidence ce WHERE ce.document_revision_id=d.document_revision_id)"
        " ORDER BY d.available_at, d.document_revision_id LIMIT %s", (list(source_ids), limit)).fetchall()


def document_revision_by_id(conn: psycopg.Connection, document_revision_id: uuid.UUID) -> dict | None:
    return conn.execute("SELECT * FROM intelligence.document_revision WHERE document_revision_id=%s", (document_revision_id,)).fetchone()


def latest_revisions_of(conn: psycopg.Connection, document_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, dict]:
    rows = conn.execute(
        "SELECT DISTINCT ON (document_id) * FROM intelligence.document_revision WHERE document_id = ANY(%s) ORDER BY document_id, revision DESC",
        (list(document_ids),)).fetchall()
    return {r["document_id"]: r for r in rows}


def issuer_disclosures(conn: psycopg.Connection, issuer_code: str, start: datetime, end: datetime) -> list[dict]:
    """Official cninfo publication claims of one issuer in [start, end): the primary documents a news item may rest on."""
    return conn.execute(
        "SELECT DISTINCT ON (d.document_id) d.*, c.object_payload AS claim_payload FROM intelligence.claim_revision c"
        " JOIN intelligence.claim_evidence ce ON ce.claim_revision_id=c.claim_revision_id"
        " JOIN intelligence.document_revision d ON d.document_revision_id=ce.document_revision_id"
        " WHERE c.subject_kind='issuer' AND c.subject_key=%s AND c.predicate='announcement.published' AND d.source_id='cninfo_announcements'"
        " AND d.published_at >= %s AND d.published_at < %s AND d.document_status NOT IN ('withdrawn','deleted_at_source')"
        " ORDER BY d.document_id, d.revision DESC", (issuer_code, start, end)).fetchall()


# ---- claims ----------------------------------------------------------------------------------------------------
def recent_news_claims(conn: psycopg.Connection, token: str, since: datetime) -> list[dict]:
    return conn.execute(
        "SELECT DISTINCT ON (claim_id) * FROM intelligence.claim_revision WHERE claim_schema_version=%s AND event_time >= %s"
        " ORDER BY claim_id, revision DESC", (token, since)).fetchall()


def latest_claim(conn: psycopg.Connection, claim_id: uuid.UUID) -> dict | None:
    return conn.execute("SELECT * FROM intelligence.claim_revision WHERE claim_id=%s ORDER BY revision DESC LIMIT 1", (claim_id,)).fetchone()


def claim_evidence(conn: psycopg.Connection, claim_revision_id: uuid.UUID) -> list[dict]:
    return conn.execute("SELECT * FROM intelligence.claim_evidence WHERE claim_revision_id=%s ORDER BY document_revision_id, evidence_ordinal",
                        (claim_revision_id,)).fetchall()


def claim_revision_id(claim_id: uuid.UUID, revision: int) -> uuid.UUID:
    return _id("claim_rev", claim_id, revision)


def insert_claim_revision(conn: psycopg.Connection, *, claim_id: uuid.UUID, revision: int, subject_kind: str, subject_key: str | None, predicate: str,
                          payload: dict, token: str, assertion_type: str, status: str, policy_version: str, model_version: str | None,
                          event_time: datetime | None, available_at: datetime, now: datetime, quality: str) -> uuid.UUID:
    crid = claim_revision_id(claim_id, revision)
    conn.execute(
        "INSERT INTO intelligence.claim_revision(claim_revision_id, claim_id, revision, subject_kind, subject_key, predicate, object_payload,"
        " claim_schema_version, assertion_type, claim_status, verification_policy_version, model_version, event_time, available_at, recorded_at,"
        " quality_status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (claim_id, revision) DO NOTHING",
        (crid, claim_id, revision, subject_kind, subject_key, predicate, Jsonb(payload), token, assertion_type, status, policy_version, model_version,
         event_time, available_at, now, quality))
    return crid


def insert_claim_evidence(conn: psycopg.Connection, claim_rev: uuid.UUID, document_revision_id: uuid.UUID, ordinal: int, relation: str, span: dict,
                          extraction_version: str, available_at: datetime, now: datetime) -> None:
    conn.execute(
        "INSERT INTO intelligence.claim_evidence(claim_revision_id, document_revision_id, evidence_ordinal, relation, evidence_span, extraction_version,"
        " available_at, recorded_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
        (claim_rev, document_revision_id, ordinal, relation, Jsonb(span), extraction_version, available_at, now))


def claims_citing_documents(conn: psycopg.Connection, document_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
    """claim_ids whose LATEST revision cites any revision of the given logical documents."""
    rows = conn.execute(
        "SELECT DISTINCT c.claim_id FROM intelligence.claim_evidence ce"
        " JOIN intelligence.document_revision d ON d.document_revision_id=ce.document_revision_id"
        " JOIN intelligence.claim_revision c ON c.claim_revision_id=ce.claim_revision_id WHERE d.document_id = ANY(%s)", (list(document_ids),)).fetchall()
    return [r["claim_id"] for r in rows]


def insert_event_revision(conn: psycopg.Connection, *, claim_id: uuid.UUID, revision: int, category: str, event_time: datetime | None, payload: dict,
                          model_version: str, available_at: datetime, now: datetime, quality: str, taxonomy_version: str = "1") -> uuid.UUID:
    event_id = _id("event", claim_id)
    erid = _id("event_rev", claim_id, revision)
    conn.execute(
        "INSERT INTO intelligence.event_revision(event_revision_id, event_id, revision, event_category, taxonomy_version, event_time, event_time_end,"
        " analysis_payload, analysis_schema_version, analysis_model_version, available_at, recorded_at, quality_status)"
        " VALUES (%s,%s,%s,%s,%s,%s,NULL,%s,'pulse.intel.impact_mapping@1',%s,%s,%s,%s) ON CONFLICT (event_id, revision) DO NOTHING",
        (erid, event_id, revision, category, taxonomy_version, event_time, Jsonb(payload), model_version, available_at, now, quality))
    return erid


def insert_event_claim(conn: psycopg.Connection, event_revision_id: uuid.UUID, claim_rev: uuid.UUID, relation: str = "core_fact") -> None:
    conn.execute("INSERT INTO intelligence.event_claim(event_revision_id, claim_revision_id, relation) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                 (event_revision_id, claim_rev, relation))


def latest_event(conn: psycopg.Connection, claim_id: uuid.UUID) -> dict | None:
    return conn.execute("SELECT * FROM intelligence.event_revision WHERE event_id=%s ORDER BY revision DESC LIMIT 1", (_id("event", claim_id),)).fetchone()


def instrument_exists(conn: psycopg.Connection, instrument_id: uuid.UUID) -> bool:
    return conn.execute("SELECT 1 FROM core.instrument WHERE instrument_id=%s", (instrument_id,)).fetchone() is not None


# ---- community ----------------------------------------------------------------------------------------------------
def latest_post(conn: psycopg.Connection, source_id: str, post_id: str) -> dict | None:
    return conn.execute(
        "SELECT * FROM intelligence.community_observation WHERE source_id=%s AND post_id=%s ORDER BY post_revision DESC, sampled_at DESC LIMIT 1",
        (source_id, post_id)).fetchone()


def insert_community_observation(conn: psycopg.Connection, *, source_id: str, raw_object_id: uuid.UUID, post_id: str, post_revision: int,
                                 author: str | None, created_at: datetime | None, sampled_at: datetime, entry: str, sort: str, window_start: datetime,
                                 window_end: datetime, coverage: str, like_count: int | None, reply_count: int | None, unique_participants: int | None,
                                 analysis: dict, model_version: str, available_at: datetime, now: datetime, quality: str) -> tuple[uuid.UUID, bool]:
    oid = _id("community", source_id, post_id, post_revision, sampled_at.isoformat(), model_version)
    row = conn.execute(
        "INSERT INTO intelligence.community_observation(community_observation_id, source_id, raw_object_id, post_id, post_revision, author_pseudonym,"
        " post_created_at, post_edited_at, sampled_at, sampling_entry, sampling_sort, sampling_window_start, sampling_window_end, coverage_status,"
        " like_count, reply_count, unique_participant_count, analysis_payload, analysis_schema_version, analysis_model_version, available_at,"
        " recorded_at, quality_status) VALUES (%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pulse.intel.community_post@1',%s,%s,%s,%s)"
        " ON CONFLICT (source_id, post_id, post_revision, sampled_at, analysis_model_version) DO NOTHING RETURNING community_observation_id",
        (oid, source_id, raw_object_id, post_id, post_revision, author, created_at, sampled_at, entry, sort, window_start, window_end, coverage,
         like_count, reply_count, unique_participants, Jsonb(analysis), model_version, available_at, now, quality)).fetchone()
    return oid, row is not None


def community_rows(conn: psycopg.Connection, source_id: str, code: str, sampled_at: datetime) -> list[dict]:
    return conn.execute(
        "SELECT * FROM intelligence.community_observation WHERE source_id=%s AND sampled_at=%s AND analysis_payload->>'instrument_code'=%s ORDER BY post_created_at DESC",
        (source_id, sampled_at, code)).fetchall()


# ---- seats ----------------------------------------------------------------------------------------------------------
def upsert_seat_entity(conn: psycopg.Connection, *, seat_id: uuid.UUID, canonical_name: str, broker_legal_name: str, city: str, effective_from: datetime,
                       evidence_raw_object_id: uuid.UUID, alias_evidence: list, available_at: datetime, now: datetime) -> bool:
    row = conn.execute(
        "INSERT INTO intelligence.seat_entity_version(seat_id, seat_version, entity_kind, canonical_name, broker_legal_name, city_code, effective_from,"
        " effective_to, evidence_raw_object_id, alias_evidence, available_at, recorded_at) VALUES (%s,1,'broker_branch',%s,%s,%s,%s,NULL,%s,%s,%s,%s)"
        " ON CONFLICT (seat_id, seat_version) DO NOTHING RETURNING seat_id",
        (seat_id, canonical_name, broker_legal_name, city, effective_from, evidence_raw_object_id, Jsonb(alias_evidence), available_at, now)).fetchone()
    return row is not None


def existing_report_ids(conn: psycopg.Connection, source_id: str) -> dict[str, int]:
    rows = conn.execute("SELECT source_report_id, max(revision) AS r FROM intelligence.seat_report WHERE source_id=%s GROUP BY 1", (source_id,)).fetchall()
    return {r["source_report_id"]: r["r"] for r in rows}


def report_content_hash(conn: psycopg.Connection, source_id: str, source_report_id: str, revision: int) -> str | None:
    import hashlib

    rep = conn.execute("SELECT * FROM intelligence.seat_report WHERE source_id=%s AND source_report_id=%s AND revision=%s",
                       (source_id, source_report_id, revision)).fetchone()
    if rep is None:
        return None
    rows = conn.execute("SELECT side, rank, raw_seat_name, canonical_amount_cny FROM intelligence.seat_report_row WHERE report_revision_id=%s ORDER BY side, rank",
                        (rep["report_revision_id"],)).fetchall()
    parts = [rep["window_start"].isoformat(), rep["window_end"].isoformat(), rep["disclosure_reason"]]
    parts += [f"{r['side']}|{r['rank']}|{r['raw_seat_name']}|{norm_amount(r['canonical_amount_cny'])}" for r in rows]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def report_revision_id(source_id: str, source_report_id: str, revision: int) -> uuid.UUID:
    return _id("seat_report", source_id, source_report_id, revision)


def _dec_text(d: Decimal) -> str:
    return format(d, "f")


def insert_seat_report(conn: psycopg.Connection, rep: SeatReportRec, *, revision: int, raw_object_id: uuid.UUID, instrument_id: uuid.UUID,
                       seat_versions: dict[uuid.UUID, int], published_at: datetime | None, now: datetime) -> uuid.UUID:
    """One report + its rows (COPY). ``seat_versions`` = resolved seat_id -> version that exists in seat_entity_version."""
    rid = report_revision_id(rep.source_id, rep.source_report_id, revision)
    avail = rep.available_at or now
    conn.execute(
        "INSERT INTO intelligence.seat_report(report_revision_id, source_id, raw_object_id, source_report_id, revision, exchange_code, instrument_id,"
        " window_start, window_end, disclosure_reason, rule_version, trade_scope, published_at, available_at, recorded_at, quality_status)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (rid, rep.source_id, raw_object_id, rep.source_report_id, revision, rep.exchange_code, instrument_id, rep.window_start, rep.window_end,
         rep.disclosure_reason, rep.rule_version, rep.trade_scope, published_at, avail, now, rep.quality_status))
    if rep.rows:
        with conn.cursor().copy("COPY intelligence.seat_report_row(report_revision_id, side, rank, raw_seat_name, raw_amount, raw_unit, canonical_amount_cny,"
                                " resolution_status, seat_id, seat_version, resolution_evidence) FROM STDIN") as cp:
            for r in rep.rows:
                res = r.resolution
                sid = res.seat_id if res.status == "resolved" else None
                ver = seat_versions.get(sid) if sid else None
                if sid is not None and ver is None:
                    raise ValueError("resolved seat without a stored entity version")
                ev = res.evidence(r.raw_seat_name, source_amounts=r.source_amounts, source_seat_code=r.source_seat_code)
                cp.write_row([rid, r.side, r.rank, r.raw_seat_name, _dec_text(r.raw_amount), r.raw_unit, _dec_text(r.canonical_amount_cny), res.status, sid, ver,
                              Jsonb(ev)])
    return rid


def seat_reports_for(conn: psycopg.Connection, instrument_id: uuid.UUID, decision_as_of: datetime) -> list[dict]:
    return conn.execute(
        "SELECT DISTINCT ON (source_id, source_report_id) * FROM intelligence.seat_report WHERE instrument_id=%s AND available_at <= %s"
        " ORDER BY source_id, source_report_id, revision DESC", (instrument_id, decision_as_of)).fetchall()


def seat_rows_as_of(conn: psycopg.Connection, instrument_id: uuid.UUID, as_of: datetime) -> list[dict]:
    """Seat rows of reports available at ``as_of``. SS-G08: a row counts as ``resolved`` only if its entity version was also
    available at ``as_of``; a name resolved by a later resolver run is ``unresolved`` for earlier research snapshots."""
    return conn.execute(
        "SELECT rep.source_id, rep.source_report_id, rep.window_start, rep.window_end, r.side, r.rank, r.raw_seat_name, r.canonical_amount_cny,"
        " CASE WHEN r.seat_id IS NOT NULL AND (e.available_at IS NULL OR e.available_at > %s) THEN 'unresolved' ELSE r.resolution_status END AS resolution_status_as_of,"
        " r.seat_id FROM (SELECT DISTINCT ON (source_id, source_report_id) * FROM intelligence.seat_report WHERE instrument_id=%s AND available_at <= %s"
        " ORDER BY source_id, source_report_id, revision DESC) rep JOIN intelligence.seat_report_row r USING (report_revision_id)"
        " LEFT JOIN intelligence.seat_entity_version e ON e.seat_id=r.seat_id AND e.seat_version=r.seat_version ORDER BY rep.window_end, r.side, r.rank",
        (as_of, instrument_id, as_of)).fetchall()


# ---- funding --------------------------------------------------------------------------------------------------------
def latest_funding(conn: psycopg.Connection, source_id: str, record_key: str, namespace: str, name: str, methodology_version: str) -> dict | None:
    return conn.execute(
        "SELECT * FROM intelligence.funding_observation WHERE source_id=%s AND source_record_key=%s AND metric_namespace=%s AND metric_name=%s"
        " AND methodology_version=%s ORDER BY revision DESC LIMIT 1", (source_id, record_key, namespace, name, methodology_version)).fetchone()


def insert_funding(conn: psycopg.Connection, *, source_id: str, raw_object_id: uuid.UUID, record_key: str, revision: int, instrument_id: uuid.UUID,
                   namespace: str, name: str, methodology_id: str, methodology_version: str, measurement_class: str, side_convention: str,
                   basis: str, session_scope: str, direction: str, window_start: datetime, window_end: datetime, raw_value: str | None, raw_unit: str,
                   canonical_value: Decimal | None, canonical_unit: str, currency: str | None, missing_reason: str | None, available_at: datetime,
                   now: datetime, quality: str, field_quality: dict) -> bool:
    fid = _id("funding", source_id, record_key, namespace, name, methodology_version, revision)
    row = conn.execute(
        "INSERT INTO intelligence.funding_observation(funding_observation_id, source_id, raw_object_id, source_record_key, revision, instrument_id, market_key,"
        " metric_namespace, metric_name, methodology_id, methodology_version, measurement_class, side_convention, trade_or_order_basis, session_scope,"
        " market_direction, observation_window_start, observation_window_end, period_end, raw_value, raw_unit, canonical_value, canonical_unit, currency,"
        " missing_reason, published_at, available_at, recorded_at, quality_status, field_quality)"
        " VALUES (%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s,%s)"
        " ON CONFLICT (source_id, source_record_key, metric_namespace, metric_name, methodology_version, revision) DO NOTHING RETURNING funding_observation_id",
        (fid, source_id, raw_object_id, record_key, revision, instrument_id, namespace, name, methodology_id, methodology_version, measurement_class,
         side_convention, basis, session_scope, direction, window_start, window_end, raw_value, raw_unit, canonical_value, canonical_unit, currency,
         missing_reason, available_at, now, quality, Jsonb(field_quality))).fetchone()
    return row is not None


# ---- model policy / run ---------------------------------------------------------------------------------------------
def ensure_model_policy(conn: psycopg.Connection, p, now: datetime) -> None:
    doc = schemas.SCHEMAS[(p.response_contract_id, p.response_schema_version)]
    conn.execute(
        "INSERT INTO core.contract_version(contract_id, schema_version, schema_document, schema_hash, compatibility_policy, created_at)"
        " VALUES (%s,%s,%s,%s,'additive-optional-v1',%s) ON CONFLICT (contract_id, schema_version) DO NOTHING",
        (p.response_contract_id, p.response_schema_version, Jsonb(doc), sha256_of(doc), now))
    conn.execute(
        "INSERT INTO analysis.model_policy(model_policy_id, model_policy_version, task_kind, provider_id, model_locator, response_contract_id,"
        " response_schema_version, prompt_digest, retrieval_policy_version, capability_requirements, timeout_seconds, token_budget, rollout_state,"
        " evaluation_experiment_id, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s) ON CONFLICT DO NOTHING",
        (p.model_policy_id, p.version, p.task_kind, p.provider_id, Jsonb(p.model_locator), p.response_contract_id, p.response_schema_version, p.prompt_digest,
         p.retrieval_policy_version, Jsonb(p.capability_requirements), p.timeout_seconds, p.token_budget, p.rollout_state, now))


def find_succeeded_run(conn: psycopg.Connection, policy_id: str, version: int, context_hash: str) -> dict | None:
    return conn.execute(
        "SELECT * FROM analysis.model_run WHERE model_policy_id=%s AND model_policy_version=%s AND input_context_hash=%s AND run_state='succeeded'"
        " ORDER BY completed_at DESC LIMIT 1", (policy_id, version, context_hash)).fetchone()


def insert_model_run(conn: psycopg.Connection, *, run_id: uuid.UUID, policy_id: str, version: int, context_hash: str, manifest: list[dict],
                     analysis_as_of: datetime, started_at: datetime) -> None:
    conn.execute(
        "INSERT INTO analysis.model_run(model_run_id, model_policy_id, model_policy_version, stage_id, input_context_hash, input_manifest, analysis_as_of,"
        " run_state, publication_eligibility, started_at, completed_at, output_ref, output_hash, usage_document, error_code)"
        " VALUES (%s,%s,%s,NULL,%s,%s,%s,'running','unchecked',%s,NULL,NULL,NULL,%s,NULL)",
        (run_id, policy_id, version, context_hash, Jsonb({"entries": manifest}), analysis_as_of, started_at, Jsonb({})))


def finish_model_run(conn: psycopg.Connection, run_id: uuid.UUID, *, state: str, completed_at: datetime, output_ref: str | None, output_hash: str | None,
                     usage: dict, error_code: str | None) -> None:
    conn.execute(
        "UPDATE analysis.model_run SET run_state=%s, completed_at=%s, output_ref=%s, output_hash=%s, usage_document=%s, error_code=%s"
        " WHERE model_run_id=%s AND run_state='running'", (state, completed_at, output_ref, output_hash, Jsonb(usage), error_code, run_id))


def get_model_run(conn: psycopg.Connection, run_id: uuid.UUID) -> dict | None:
    return conn.execute("SELECT * FROM analysis.model_run WHERE model_run_id=%s", (run_id,)).fetchone()


# ---- seat style hypotheses (research registry) -----------------------------------------------------------------------
def register_seat_hypothesis(conn: psycopg.Connection, h, available_at: datetime) -> uuid.UUID:
    """Pre-register a SeatStyleHypothesis in ``research.hypothesis`` (immutable; same spec = same id)."""
    from pulse.domain.strategy.canonical import canonical_hash

    hid = h.hypothesis_id()
    falsification = {"rule": "conditional outcome interval includes zero or reverses sign out of sample", "min_sample": h.min_sample}
    spec_hash = canonical_hash([h.claim, h.key_kind, h.key, h.conditions, h.horizon_sessions, h.outcome, falsification])
    conn.execute(
        "INSERT INTO research.hypothesis(hypothesis_id, hypothesis_version, paper_id, paper_revision, strategy_id, strategy_version, claim, mechanism,"
        " forecast_target, horizon_id, input_requirements, falsification_conditions, specification_hash, available_at)"
        " VALUES (%s,%s,NULL,NULL,NULL,NULL,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
        (hid, h.version, h.claim, "disclosed seat activity conditions later price path (hypothesis, not a rule)", h.outcome, f"{h.horizon_sessions}_sessions",
         Jsonb(h.to_input_requirements()), Jsonb(falsification), spec_hash, available_at))
    return hid
