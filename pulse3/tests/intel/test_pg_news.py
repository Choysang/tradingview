"""News -> document revisions -> claims -> events on real PostgreSQL, from recorded raw feeds (no network).

Spec: FINAL §5/§5.1/§12, sources_sectors §4/§5, SS-A01,A02,A04,A05,A06,A10; gates SS-G01,G02,G10,G12.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from pulse import contracts
from pulse.adapters.sources import cninfo
from pulse.adapters.sources.cls_news import ClsNewsAdapter
from pulse.adapters.sources.eastmoney_news import EastmoneyNewsAdapter
from pulse.adapters.sources.http import SourceBlocked
from pulse.adapters.postgres import intelligence_repo as repo

from . import helpers as h

UTC = timezone.utc
pytestmark = pytest.mark.pg
NEW = "pulse.intel.news_claim@1"


def _client(clock, **kw):
    return h.client(now=clock, transport_=kw.get("transport_"))


def _count(conn, sql, *a):
    return conn.execute(sql, a).fetchone()["n"]


def _ingest_em(svc, clock, body=None, pages=1):
    tr = h.transport(overrides={"getFastNewsList": (200, body)} if body else None)
    return svc.ingest_news(EastmoneyNewsAdapter(_client(clock, transport_=tr)), "eastmoney", pages=pages, page_size=50)


def test_recorded_feeds_become_raw_objects_and_document_revisions_with_original_ids(svc, conn, lake, clock):
    s1 = _ingest_em(svc, clock)
    s2 = svc.ingest_news(ClsNewsAdapter(_client(clock)), "cls", pages=1, page_size=50)
    assert s1.counts["documents_published"] == 50 and s2.counts["documents_published"] == 50
    raws = conn.execute("SELECT * FROM ingest.raw_object WHERE endpoint_id IN ('fast_news_list','roll_list_web')").fetchall()
    assert len(raws) == 2
    for r in raws:  # bytes kept exactly, hash verified
        assert hashlib.sha256(lake.read_object(r["content_hash"])).hexdigest() == r["content_hash"]
    em = json.loads(h.recorded("eastmoney_news_p1.json"))["data"]["fastNewsList"]
    doc = conn.execute("SELECT * FROM intelligence.document_revision WHERE source_id='eastmoney_news_724' AND source_record_id=%s", (em[0]["code"],)).fetchone()
    assert doc["revision"] == 1 and doc["document_status"] == "published" and doc["title"] == em[0]["title"] and doc["published_time_precision"] == "second"
    assert doc["available_at"] >= doc["first_seen_at"] and doc["published_at"].tzinfo is not None
    wire = {"document_revision_id": str(doc["document_revision_id"]), "document_id": str(doc["document_id"]), "source_id": doc["source_id"],
            "revision": doc["revision"], "title": doc["title"], "origin_group": doc["origin_group"], "origin_status": doc["origin_status"],
            "published_at": doc["published_at"].isoformat().replace("+00:00", "Z"), "published_time_precision": doc["published_time_precision"],
            "first_seen_at": doc["first_seen_at"].isoformat().replace("+00:00", "Z"), "available_at": doc["available_at"].isoformat().replace("+00:00", "Z"),
            "quality_status": doc["quality_status"], "document_status": doc["document_status"], "raw_object_id": str(doc["raw_object_id"]),
            "field_quality": doc["field_quality"]}
    contracts.check("EvidenceDocument", wire)
    health = conn.execute("SELECT runtime_state, last_new_record_at FROM ingest.source_health WHERE source_id='eastmoney_news_724'").fetchone()
    assert health["runtime_state"] == "healthy" and health["last_new_record_at"] is not None
    assert conn.execute("SELECT batch_state FROM ingest.batch WHERE source_id='cls_telegraph'").fetchone()["batch_state"] == "NORMALIZED"
    # origin: only an outlet's own dispatch is a confirmed origin; attributed items are republications; the rest unknown
    st = {r["origin_status"]: r["n"] for r in conn.execute("SELECT origin_status, count(*) AS n FROM intelligence.document_revision GROUP BY 1").fetchall()}
    assert st.get("republication", 0) > 0 and st.get("unknown", 0) > 0
    assert _count(conn, "SELECT count(*) AS n FROM intelligence.document_revision WHERE origin_status='unknown' AND origin_group IS NOT NULL") == 0


def test_same_delivery_twice_adds_no_document_and_a_changed_item_appends_a_revision(svc, conn, clock):  # SS-A05, SS-A06
    _ingest_em(svc, clock)
    again = _ingest_em(svc, clock)
    assert again.counts.get("documents_unchanged") == 50 and "documents_published" not in again.counts
    assert _count(conn, "SELECT count(*) AS n FROM intelligence.document_revision WHERE source_id='eastmoney_news_724'") == 50
    page = json.loads(h.recorded("eastmoney_news_p1.json"))
    page["data"]["fastNewsList"][5]["title"] += "（更正）"
    corrected = _ingest_em(svc, clock, json.dumps(page, ensure_ascii=False).encode())
    assert corrected.counts["documents_corrected"] == 1 and corrected.counts["documents_unchanged"] == 49
    code = page["data"]["fastNewsList"][5]["code"]
    revs = conn.execute("SELECT revision, document_status FROM intelligence.document_revision WHERE source_record_id=%s ORDER BY revision", (code,)).fetchall()
    assert [(r["revision"], r["document_status"]) for r in revs] == [(1, "published"), (2, "corrected")]


def test_login_page_and_block_do_not_produce_documents(svc, conn, clock):  # SS-A02, SS-A04
    s = svc.ingest_news(EastmoneyNewsAdapter(_client(clock, transport_=h.transport(overrides={"getFastNewsList": (200, b"<html>login captcha</html>")}))),
                        "eastmoney", pages=1)
    assert s.counts.get("pages_auth_required") == 1
    assert _count(conn, "SELECT count(*) AS n FROM intelligence.document_revision") == 0
    st = conn.execute("SELECT runtime_state FROM ingest.source_health WHERE source_id='eastmoney_news_724'").fetchone()["runtime_state"]
    assert st == "auth_required"
    assert _count(conn, "SELECT count(*) AS n FROM ingest.raw_object WHERE source_id='eastmoney_news_724'") == 1  # raw kept
    calls: list = []
    blocked = h.transport(overrides={"getFastNewsList": (429, b"slow down")}, calls=calls)
    with pytest.raises(SourceBlocked):
        svc.ingest_news(EastmoneyNewsAdapter(_client(clock, transport_=blocked)), "eastmoney", pages=3)
    assert len(calls) == 1  # stopped at the first block
    row = conn.execute("SELECT runtime_state, last_error_code FROM ingest.source_health WHERE source_id='eastmoney_news_724'").fetchone()
    assert row["runtime_state"] == "unavailable" and row["last_error_code"].startswith("blocked")


def test_success_empty_page_is_healthy_and_adds_nothing(svc, conn, clock):  # SS-A01
    s = _ingest_em(svc, clock, h.em_page([], None))
    assert not s.counts.get("documents_published")
    assert conn.execute("SELECT runtime_state FROM ingest.source_health WHERE source_id='eastmoney_news_724'").fetchone()["runtime_state"] == "healthy"


def test_claims_from_recorded_feeds_have_evidence_events_and_honest_status(svc, conn, clock):
    _ingest_em(svc, clock)
    svc.ingest_news(ClsNewsAdapter(_client(clock)), "cls", pages=1, page_size=50)
    s = svc.process_claims()
    assert s.counts["documents_to_process"] == 100 and s.counts.get("candidates_rejected", 0) == 0
    assert s.counts["model_runs_new"] == 2
    assert _count(conn, "SELECT count(*) AS n FROM intelligence.document_revision d WHERE NOT EXISTS (SELECT 1 FROM intelligence.claim_evidence e WHERE e.document_revision_id=d.document_revision_id)") == 0
    assert _count(conn, "SELECT count(*) AS n FROM analysis.model_run WHERE run_state='succeeded'") == 2
    # nothing is "supported" on reposts/unknown origin alone; no probability anywhere
    sup = conn.execute("SELECT count(*) AS n FROM intelligence.claim_revision WHERE claim_status='supported' AND claim_schema_version=%s", (NEW,)).fetchone()["n"]
    assert sup == 0
    ev = conn.execute("SELECT * FROM intelligence.event_revision").fetchall()
    assert ev
    for e in ev:
        p = e["analysis_payload"]
        assert p["calibration"]["probability"] is None and all(b["direction_status"] == "uncalibrated_prior" for b in p["business_variables"])
        if p["exposure_status"] != "named_subject":
            assert p["exposed_instruments"] == []
        latest_claim_at = conn.execute("SELECT max(c.available_at) AS a FROM intelligence.event_claim ec JOIN intelligence.claim_revision c USING (claim_revision_id) WHERE ec.event_revision_id=%s", (e["event_revision_id"],)).fetchone()["a"]
        assert e["available_at"] >= latest_claim_at
    # SS-G01: a claim is never available before its evidence
    bad = _count(conn, "SELECT count(*) AS n FROM intelligence.claim_revision c JOIN intelligence.claim_evidence e USING (claim_revision_id) WHERE c.available_at < e.available_at")
    assert bad == 0
    # second pass: nothing left, model not called again
    s2 = svc.process_claims()
    assert s2.counts["documents_to_process"] == 0
    # EvidenceClaim wire check
    c = conn.execute("SELECT * FROM intelligence.claim_revision WHERE claim_schema_version=%s ORDER BY claim_id LIMIT 1", (NEW,)).fetchone()
    evs = repo.claim_evidence(conn, c["claim_revision_id"])
    wire = {"claim_revision_id": str(c["claim_revision_id"]), "claim_id": str(c["claim_id"]), "revision": c["revision"], "subject_kind": c["subject_kind"],
            "subject_key": c["subject_key"], "predicate": c["predicate"], "object_payload": c["object_payload"], "claim_schema_version": c["claim_schema_version"],
            "assertion_type": c["assertion_type"], "claim_status": c["claim_status"], "verification_policy_version": c["verification_policy_version"],
            "available_at": c["available_at"].isoformat().replace("+00:00", "Z"), "quality_status": c["quality_status"],
            "evidence": [{"document_revision_id": str(e["document_revision_id"]), "evidence_ordinal": e["evidence_ordinal"], "relation": e["relation"],
                          "evidence_span": e["evidence_span"], "extraction_version": e["extraction_version"],
                          "available_at": e["available_at"].isoformat().replace("+00:00", "Z")} for e in evs]}
    contracts.check("EvidenceClaim", wire)


NOW_CST = "2026-11-11 19:00:00"
FACT_TITLE = "丽人丽妆：收到1090万元政府补助"
FACT_BODY = "【丽人丽妆：收到1090万元政府补助】丽人丽妆(605136.SH)公告称，公司近日收到政府补助金额1090万元，补助类型为与收益相关。"


def _cninfo_for(svc, clock, ms=1794394200000):  # 2026-11-11 18:30 CST
    tr = httpx.MockTransport(lambda req: httpx.Response(200, content=h.cninfo_page("1225000001", "605136", "关于收到政府补助的公告", ms),
                                                        headers={"content-type": "application/json"}))
    svc.data.bootstrap()
    return svc.data.ingest_announcements(cninfo.CninfoAdapter(h.client(now=clock, transport_=tr)), days=1)


def test_news_about_an_issuer_is_supported_by_its_own_official_disclosure_only(svc, conn, clock):
    _cninfo_for(svc, clock)
    _ingest_em(svc, clock, h.em_page([h.em_item("E1", FACT_TITLE, FACT_BODY, NOW_CST, ["1.605136"])]))
    s = svc.process_claims()
    assert s.counts["official_links_attached"] == 1 and s.counts["claim_status_supported"] == 1
    c = conn.execute("SELECT * FROM intelligence.claim_revision WHERE claim_schema_version=%s", (NEW,)).fetchone()
    assert c["claim_status"] == "supported" and c["subject_key"] == "605136" and c["assertion_type"] == "attributed_statement"
    assert c["object_payload"]["verification"]["official_support"] is True and c["object_payload"]["verification"]["independent_support_origins"] >= 1
    rows = repo.claim_evidence(conn, c["claim_revision_id"])
    assert {r["relation"] for r in rows} == {"supports"} and len(rows) == 2
    ev = conn.execute("SELECT * FROM intelligence.event_revision").fetchone()
    assert ev["event_category"] == "company.capital_action" and ev["analysis_payload"]["exposure_status"] == "named_subject"
    assert ev["analysis_payload"]["exposed_instruments"][0]["code"] == "605136"


def test_without_an_official_document_the_same_news_stays_insufficient(svc, conn, clock):
    _ingest_em(svc, clock, h.em_page([h.em_item("E1", FACT_TITLE, FACT_BODY, NOW_CST, ["1.605136"])]))
    svc.process_claims()
    c = conn.execute("SELECT * FROM intelligence.claim_revision WHERE claim_schema_version=%s", (NEW,)).fetchone()
    assert c["claim_status"] == "unverified" and c["object_payload"]["verification"]["confirmation"] == "insufficient"
    assert c["object_payload"]["verification"]["attributed_chains"] == ["issuer_disclosure"]


def test_reposts_join_one_claim_and_add_no_independent_origin(svc, conn, clock):  # SS-A10
    items = [h.em_item(f"R{i}", f"据新华社报道，{t}", f"据新华社，俄罗斯国防部11日发布通报说，继续使用无人机打击为乌克兰军工企业运转提供支持的能源基础设施{x}", f"2026-11-11 1{i}:00:00")
             for i, (t, x) in enumerate([("俄称打击乌多地区目标", "。"), ("俄称打击乌多地区目标 乌称多座变电站遭袭", "。"), ("俄称打击乌多地区目标", "，")])]
    _ingest_em(svc, clock, h.em_page(items))
    svc.process_claims()
    rows = conn.execute("SELECT * FROM intelligence.claim_revision WHERE claim_schema_version=%s ORDER BY revision ASC", (NEW,)).fetchall()
    top = {r["claim_id"]: r for r in rows}
    assert len(top) == 1
    only = next(iter(top.values()))
    assert only["revision"] == 3 and only["claim_status"] == "unverified"
    assert len(repo.claim_evidence(conn, only["claim_revision_id"])) == 3
    v = only["object_payload"]["verification"]
    assert v["independent_support_origins"] == 0 and v["attributed_chains"] == ["新华社"]


def test_withdrawal_recomputes_the_claim_and_its_event_without_the_withdrawn_evidence(svc, conn, clock):  # SS-G12
    _cninfo_for(svc, clock)
    _ingest_em(svc, clock, h.em_page([h.em_item("E1", FACT_TITLE, FACT_BODY, NOW_CST, ["1.605136"])]))
    svc.process_claims()
    raw = conn.execute("SELECT raw_object_id FROM intelligence.document_revision WHERE source_id='cninfo_announcements'").fetchone()["raw_object_id"]
    clock.advance(minutes=10)
    # the issuer withdraws the announcement (source evidence is the later raw page; the service never infers withdrawal)
    cn = conn.execute("SELECT source_record_id, raw_object_id FROM intelligence.document_revision WHERE source_id='cninfo_announcements'").fetchone()
    s = svc.withdraw_document("cninfo_announcements", cn["source_record_id"], raw_object_id=raw, reason="source_reported_withdrawn")
    assert s.counts["claims_recomputed"] >= 1
    revs = conn.execute("SELECT revision, claim_status, quality_status FROM intelligence.claim_revision WHERE claim_schema_version=%s ORDER BY revision", (NEW,)).fetchall()
    assert [r["claim_status"] for r in revs] == ["supported", "unverified"] and revs[-1]["quality_status"] == "partial"
    assert _count(conn, "SELECT count(*) AS n FROM intelligence.event_revision") == 2
    old = conn.execute("SELECT claim_status FROM intelligence.claim_revision WHERE claim_schema_version=%s AND revision=1", (NEW,)).fetchone()
    assert old["claim_status"] == "supported"  # history stays replayable


def test_correction_replaces_the_old_revision_in_the_claim_without_double_counting(svc, conn, clock):  # SS-A06
    _ingest_em(svc, clock, h.em_page([h.em_item("E1", FACT_TITLE, FACT_BODY, NOW_CST, ["1.605136"])]))
    svc.process_claims()
    clock.advance(minutes=5)
    _ingest_em(svc, clock, h.em_page([h.em_item("E1", FACT_TITLE, FACT_BODY.replace("1090", "1190"), NOW_CST, ["1.605136"])]))
    svc.process_claims()
    claims = conn.execute("SELECT DISTINCT ON (claim_id) * FROM intelligence.claim_revision WHERE claim_schema_version=%s ORDER BY claim_id, revision DESC", (NEW,)).fetchall()
    cited = {tuple(sorted(conn.execute("SELECT d.revision FROM intelligence.claim_evidence e JOIN intelligence.document_revision d USING (document_revision_id) WHERE e.claim_revision_id=%s",
                                       (c["claim_revision_id"],)).fetchone().values())) for c in claims}
    assert (1,) not in cited and (2,) in cited


def test_two_dispatches_of_one_outlet_on_the_same_fact_are_one_origin(svc, conn, clock):  # SS-A10: same chain, one vote
    def cls_item(i, ctime):
        return {"id": i, "ctime": ctime, "title": "", "content": "财联社11月11日电，富时中国A50指数期货夜盘收涨0.07%。", "stock_list": [], "status": 1, "type": -1, "level": "C"}

    body = json.dumps({"errno": 0, "data": {"roll_data": [cls_item(1, 1794390000), cls_item(2, 1794390600)]}}).encode()
    svc.ingest_news(ClsNewsAdapter(_client(clock, transport_=h.transport(overrides={"cls.cn/v1/roll": (200, body)}))), "cls", pages=1)
    svc.process_claims()
    c = conn.execute("SELECT DISTINCT ON (claim_id) * FROM intelligence.claim_revision WHERE claim_schema_version=%s ORDER BY claim_id, revision DESC", (NEW,)).fetchall()
    assert len(c) == 1 and c[0]["revision"] == 2
    v = c[0]["object_payload"]["verification"]
    assert v["independent_support_origins"] == 1 and c[0]["claim_status"] == "unverified"
