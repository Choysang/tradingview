"""Evidence persistence: document revisions, official-disclosure claims, events (intelligence.*).

Why: an announcement is stored as an append-only ``document_revision`` (published ->
corrected -> deleted_at_source) bound to its raw object, plus one
``official_disclosure`` claim ("issuer disclosed document X") supported by that exact
document revision, plus - only when the title maps to a taxonomy category - an
``event_revision`` linked through ``event_claim`` as ``core_fact``. Correction /
withdrawal relations found in titles are stored as separate, UNRESOLVED
``announcement.<relation>`` claims (target hint text, no guessed link).

Invariants: ids are deterministic (uuid5) so replays are idempotent; revisions only
append; ``available_at`` is the platform normalization time (>= first_seen_at);
an official claim confirms the publication, not the truth of the content
(``claim_status='supported'`` only for the publication predicate).
Not covered: claim truth verification, entity extraction beyond the issuer code.
"""
from __future__ import annotations

import uuid
from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb

from pulse.domain.evidence.announcements import SOURCE_ID, AnnouncementDoc

NS = uuid.UUID("f1e2d3c4-b5a6-4978-8a9b-0c1d2e3f4a5b")
TAXONOMY_VERSION = "1"
VERIFICATION_POLICY = "official-self-disclosure-v1"
CLAIM_SCHEMA = "announcement-claim-v1"


def _id(*parts: object) -> uuid.UUID:
    return uuid.uuid5(NS, "|".join(str(p) for p in parts))


def latest_revision(conn: psycopg.Connection, source_record_id: str) -> dict | None:
    return conn.execute(
        "SELECT * FROM intelligence.document_revision WHERE source_id=%s AND source_record_id=%s ORDER BY revision DESC LIMIT 1",
        (SOURCE_ID, source_record_id)).fetchone()


def window_ids(conn: psycopg.Connection, start: datetime, end: datetime) -> set[str]:
    return {r["source_record_id"] for r in conn.execute(
        "SELECT DISTINCT d.source_record_id FROM intelligence.document_revision d WHERE d.source_id=%s"
        " AND d.published_at >= %s AND d.published_at < %s AND d.document_status <> 'deleted_at_source'"
        " AND NOT EXISTS (SELECT 1 FROM intelligence.document_revision n WHERE n.document_id=d.document_id AND n.revision>d.revision"
        " AND n.document_status='deleted_at_source')", (SOURCE_ID, start, end)).fetchall()}


def insert_document(conn: psycopg.Connection, doc: AnnouncementDoc, *, raw_object_id: uuid.UUID, revision: int, status: str,
                    first_seen_at: datetime, available_at: datetime, now: datetime) -> uuid.UUID:
    rid = _id("doc", doc.source_record_id, revision)
    fq = {**doc.field_quality}
    for v in fq.values():
        v["raw_object_id"] = str(raw_object_id)
    conn.execute(
        "INSERT INTO intelligence.document_revision(document_revision_id, document_id, source_id, source_record_id, revision,"
        " raw_object_id, document_status, origin_group, origin_status, original_publisher_source_id, title, language_code,"
        " published_at, published_time_precision, event_time, first_seen_at, available_at, recorded_at, quality_status, field_quality)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'confirmed_original',NULL,%s,'zh',%s,%s,NULL,%s,%s,%s,%s,%s)"
        " ON CONFLICT (source_id, source_record_id, revision) DO NOTHING",
        (rid, doc.document_id, SOURCE_ID, doc.source_record_id, revision, raw_object_id, status, doc.origin_group, doc.title,
         doc.published_at, doc.published_time_precision, first_seen_at, available_at, now,
         "valid" if doc.published_at else "partial", Jsonb(fq)))
    return rid


def insert_publication_claim(conn: psycopg.Connection, doc: AnnouncementDoc, document_revision_id: uuid.UUID, revision: int,
                             available_at: datetime, now: datetime) -> uuid.UUID:
    claim_id = _id("claim", doc.source_record_id)
    crid = _id("claim_rev", doc.source_record_id, revision)
    conn.execute(
        "INSERT INTO intelligence.claim_revision(claim_revision_id, claim_id, revision, subject_kind, subject_key, predicate,"
        " object_payload, claim_schema_version, assertion_type, claim_status, verification_policy_version, model_version,"
        " event_time, available_at, recorded_at, quality_status) VALUES (%s,%s,%s,'issuer',%s,'announcement.published',%s,%s,"
        "'official_disclosure',%s,%s,NULL,%s,%s,%s,'valid') ON CONFLICT (claim_id, revision) DO NOTHING",
        (crid, claim_id, revision, doc.sec_code, Jsonb({"source_record_id": doc.source_record_id, "title": doc.title, "url": doc.url,
                                                         "document_type": doc.doc_type, "category": doc.category}),
         CLAIM_SCHEMA, "supported", VERIFICATION_POLICY,
         doc.published_at, available_at, now))
    conn.execute(
        "INSERT INTO intelligence.claim_evidence(claim_revision_id, document_revision_id, evidence_ordinal, relation, evidence_span,"
        " extraction_version, available_at, recorded_at) VALUES (%s,%s,1,'supports',%s,'cninfo-field-map-v1',%s,%s)"
        " ON CONFLICT DO NOTHING", (crid, document_revision_id, Jsonb({"field_path": "announcementTitle"}), available_at, now))
    return crid


def insert_relation_claims(conn: psycopg.Connection, doc: AnnouncementDoc, document_revision_id: uuid.UUID, revision: int,
                           available_at: datetime, now: datetime) -> list[uuid.UUID]:
    out = []
    for i, hint in enumerate(doc.hints, start=1):
        claim_id = _id("rel_claim", doc.source_record_id, hint["relation"])
        crid = _id("rel_claim_rev", doc.source_record_id, hint["relation"], revision)
        conn.execute(
            "INSERT INTO intelligence.claim_revision(claim_revision_id, claim_id, revision, subject_kind, subject_key, predicate,"
            " object_payload, claim_schema_version, assertion_type, claim_status, verification_policy_version, model_version,"
            " event_time, available_at, recorded_at, quality_status) VALUES (%s,%s,%s,'issuer',%s,%s,%s,%s,"
            "'official_disclosure','unverified',%s,NULL,%s,%s,%s,'partial') ON CONFLICT (claim_id, revision) DO NOTHING",
            (crid, claim_id, revision, doc.sec_code, f"announcement.{hint['relation']}",
             Jsonb({"source_record_id": doc.source_record_id, "target_title_hint": hint["target_title_hint"],
                    "resolved_document_id": hint["resolved_document_id"], "resolution": "unresolved_title_hint_only"}),
             CLAIM_SCHEMA, VERIFICATION_POLICY, doc.published_at, available_at, now))
        conn.execute(
            "INSERT INTO intelligence.claim_evidence(claim_revision_id, document_revision_id, evidence_ordinal, relation, evidence_span,"
            " extraction_version, available_at, recorded_at) VALUES (%s,%s,%s,'context',%s,'cninfo-title-keyword-v1',%s,%s)"
            " ON CONFLICT DO NOTHING", (crid, document_revision_id, i, Jsonb({"field_path": "announcementTitle"}), available_at, now))
        out.append(crid)
    return out


def insert_event(conn: psycopg.Connection, doc: AnnouncementDoc, publication_claim_revision: uuid.UUID, revision: int,
                 available_at: datetime, now: datetime) -> uuid.UUID | None:
    if doc.category is None:
        return None
    event_id = _id("event", doc.source_record_id)
    erid = _id("event_rev", doc.source_record_id, revision)
    conn.execute(
        "INSERT INTO intelligence.event_revision(event_revision_id, event_id, revision, event_category, taxonomy_version, event_time,"
        " event_time_end, analysis_payload, analysis_schema_version, analysis_model_version, available_at, recorded_at, quality_status)"
        " VALUES (%s,%s,%s,%s,%s,%s,NULL,%s,'announcement-event-v1','keyword-category-v1',%s,%s,'partial')"
        " ON CONFLICT (event_id, revision) DO NOTHING",
        (erid, event_id, revision, doc.category, TAXONOMY_VERSION, doc.published_at,
         Jsonb({"issuer": doc.sec_code, "title": doc.title, "classification": "title_keyword_candidate_not_verified"}),
         available_at, now))
    conn.execute("INSERT INTO intelligence.event_claim(event_revision_id, claim_revision_id, relation) VALUES (%s,%s,'core_fact')"
                 " ON CONFLICT DO NOTHING", (erid, publication_claim_revision))
    return erid


def append_deleted_revision(conn: psycopg.Connection, prev: dict, *, raw_object_id: uuid.UUID, now: datetime) -> uuid.UUID:
    """Append ``deleted_at_source`` after the id vanished from a FULLY paged window (caller proves that)."""
    revision = prev["revision"] + 1
    rid = _id("doc", prev["source_record_id"], revision)
    fq = dict(prev["field_quality"])
    fq["deleted_at_source"] = {"status": "observed", "reason": "absent_from_fully_paged_listing", "raw_object_id": str(raw_object_id),
                               "value_origin": "primary_source"}
    conn.execute(
        "INSERT INTO intelligence.document_revision(document_revision_id, document_id, source_id, source_record_id, revision,"
        " raw_object_id, document_status, origin_group, origin_status, original_publisher_source_id, title, language_code,"
        " published_at, published_time_precision, event_time, first_seen_at, available_at, recorded_at, quality_status, field_quality)"
        " VALUES (%s,%s,%s,%s,%s,%s,'deleted_at_source',%s,%s,NULL,%s,%s,%s,%s,NULL,%s,%s,%s,'disputed',%s)"
        " ON CONFLICT (source_id, source_record_id, revision) DO NOTHING",
        (rid, prev["document_id"], prev["source_id"], prev["source_record_id"], revision, raw_object_id, prev["origin_group"],
         prev["origin_status"], prev["title"], prev["language_code"], prev["published_at"], prev["published_time_precision"], now, now, now,
         Jsonb(fq)))
    return rid
