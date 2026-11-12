"""Pure tests: feeds, origin independence, claim rules, verification, impact mapping (no DB, no network).

Spec: FINAL §5/§5.1, sources_sectors §5.2; acceptance SS-A01,A02,A05,A06,A10; fixtures.json origin_evidence.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from pulse.domain.evidence.announcements import NotData, SchemaChanged
from pulse.domain.intelligence import claims, feeds, impact, origin, schemas, text, verification
from pulse.domain.intelligence.origin import OriginItem
from pulse.domain.intelligence.verification import EvidenceItem

from .helpers import SPEC_PKG, recorded

UTC = timezone.utc


# ---- feeds -------------------------------------------------------------------------------------------------------------
def test_recorded_eastmoney_page_parses_with_cst_to_utc_and_cursor():
    p = feeds.parse_eastmoney_news(recorded("eastmoney_news_p1.json"))
    assert len(p.records) == 50 and p.next_cursor and not p.empty_success
    r = p.records[0]
    assert r.published_at.tzinfo is not None and r.published_time_precision == "second"
    # showTime is Asia/Shanghai: UTC = CST - 8h
    assert r.published_at.hour == (int(r.raw["showTime"][11:13]) - 8) % 24


def test_recorded_cls_page_parses_title_from_bracket_and_stock_refs():
    p = feeds.parse_cls_roll(recorded("cls_roll_p1.json"))
    assert len(p.records) == 50
    assert all(r.title for r in p.records)
    with_refs = [r for r in p.records if r.stock_refs]
    assert with_refs and feeds.stock_code_from_ref(with_refs[0].stock_refs[0]) is not None


@pytest.mark.parametrize("ref,code", [("1.605136", "605136"), ("0.300302", "300302"), ("sz300302", "300302"), ("sh600000", "600000"),
                                      ("1.000001", None), ("90.BK0800", None), ("105.MSFT", None), ("0.161631", None)])
def test_stock_ref_resolution_never_takes_indexes_or_foreign_tickers(ref, code):
    assert feeds.stock_code_from_ref(ref) == code


def test_login_page_and_schema_drift_are_not_data():  # SS-A02, DQ-SOURCE-01
    with pytest.raises(NotData):
        feeds.parse_eastmoney_news(b"<html><title>login</title></html>")
    with pytest.raises(SchemaChanged):
        feeds.parse_eastmoney_news(json.dumps({"code": "1", "data": {"other": []}}).encode())
    with pytest.raises(SchemaChanged):
        feeds.parse_cls_roll(json.dumps({"errno": 0, "data": {"x": []}}).encode())
    with pytest.raises(NotData):
        feeds.parse_guba_list(b"<html>captcha</html>")


def test_valid_empty_list_is_success_empty_not_an_error():  # SS-A01
    p = feeds.parse_eastmoney_news(json.dumps({"code": "1", "data": {"fastNewsList": [], "sortEnd": None}}).encode())
    assert p.empty_success and p.records == ()


def test_lhb_no_data_body_is_success_empty_but_vendor_error_is_not():  # DQ-SOURCE-09
    assert feeds.parse_lhb_list(recorded("lhb_daily_list_empty_20261108.json")).empty_success
    with pytest.raises(SchemaChanged):
        feeds.parse_lhb_list(json.dumps({"success": False, "code": 9501, "message": "报表配置不存在", "result": None}).encode())


def test_fundflow_columns_main_equals_large_plus_super_large():
    p = feeds.parse_fundflow(recorded("fundflow_000001.json"))
    assert p.code == "000001" and len(p.days) == 10
    for d in p.days:
        v = d.values
        assert v["main_net"] == v["large_net"] + v["super_large_net"]  # verifies the f52..f56 column order


# ---- text / clustering ----------------------------------------------------------------------------------------------------
def test_numeric_tokens_keep_units_and_ignore_bare_integers():
    t = text.numeric_tokens("收到1090万元政府补助，占比13.63%，2025年")
    assert {"value": "10900000", "unit": "CNY"} == {k: t[0][k] for k in ("value", "unit")}
    assert t[1]["unit"] == "percent" and len(t) == 2


def test_near_duplicate_titles_cluster_but_unrelated_do_not():
    a = text.sketch("丽人丽妆：收到1090万元政府补助 公司近日收到政府补助金额1090万元")
    b = text.sketch("丽人丽妆收到1090万元政府补助 公司近日收到政府补助1090万元")
    c = text.sketch("俄称打击乌多地区目标 乌称多座变电站遭袭")
    assert text.similarity(a, b) >= claims.SIM_THRESHOLD > text.similarity(a, c)


# ---- origin independence (SS-A10, fixtures.json) -------------------------------------------------------------------------
def test_fixture_origin_evidence_counts_two_independent_origins():
    fx = json.loads((SPEC_PKG / "sources_sectors" / "fixtures.json").read_text(encoding="utf-8"))
    items = [OriginItem(e["origin_group"], e["status"]) for e in fx["origin_evidence"]]
    assert origin.summarize(items).independent_origins == fx["expected_independent_origins"] == 2
    s = origin.summarize(items)
    assert s.unknown_origin_documents == 1 and s.republications_merged == 2


def test_three_reposts_of_one_wire_item_add_no_vote_and_unknown_adds_none():
    items = [OriginItem("chain:新华社:d:x", "republication", attribution_publisher="新华社") for _ in range(3)] + [OriginItem(None, "unknown")]
    s = origin.summarize(items)
    assert s.independent_origins == 0 and s.attributed_chains == ("新华社",)


def test_withdrawn_document_stops_counting():
    items = [OriginItem("o1", "confirmed_original", "withdrawn"), OriginItem("o2", "confirmed_independent_report")]
    assert origin.summarize(items).independent_origins == 1


def test_attribution_detection_and_own_dispatch():
    a = origin.detect_attribution("【俄称打击乌多地区目标】据新华社，俄罗斯国防部11日发布通报说")
    assert a and a.publisher == "新华社" and a.kind == "wire"
    assert origin.detect_attribution("财联社11月11日电，美股核电股Oklo", own_publisher="财联社") is None
    dispatch = origin.detect_attribution("【A50夜盘收涨】财联社11月12日电，富时中国A50指数期货夜盘收涨0.07%。")
    assert dispatch and dispatch.publisher == "财联社"  # an aggregator relaying a CLS dispatch is a republication of that chain
    assert origin.detect_attribution("丽人丽妆(605136.SH)公告称，公司近日收到政府补助").kind == "issuer_disclosure"
    assert origin.detect_attribution("据悉，某公司将重组").kind == "unnamed"
    g, st = origin.document_origin(source_id="cls", title="t", day="2026-11-11", own_dispatch=True, attribution=None)
    assert st == "confirmed_independent_report" and g.startswith("own:")
    assert origin.document_origin(source_id="em", title="t", day="d", own_dispatch=False, attribution=None) == (None, "unknown")


# ---- verification ---------------------------------------------------------------------------------------------------------
def _ev(i, group, status, relation="supports", official=False, docstatus="published", pub=None):
    return EvidenceItem(f"r{i}", f"d{i}", "s", OriginItem(group, status, docstatus, pub, relation), official)


def test_verification_states():
    one = [_ev(1, "o1", "confirmed_independent_report")]
    two = one + [_ev(2, "o2", "confirmed_independent_report")]
    assert verification.verify(one, "independently_reported").confirmation == "insufficient"
    assert verification.verify(one, "independently_reported").claim_status == "unverified"
    assert verification.verify(two, "independently_reported").confirmation == "supported"
    assert verification.verify([_ev(1, "issuer:x", "confirmed_original", official=True)], "attributed_statement").confirmation == "supported"
    assert verification.verify(two + [_ev(3, "o3", "confirmed_independent_report", "refutes")], "independently_reported").confirmation == "disputed"
    assert verification.verify(one + [_ev(3, "o3", "confirmed_independent_report", "refutes")], "independently_reported").confirmation == "contradicted"
    assert verification.verify([_ev(3, "o3", "confirmed_independent_report", "refutes")], "independently_reported").confirmation == "contradicted"
    assert verification.verify(two, "forecast").claim_status == "not_falsifiable_yet"


def test_reposts_cannot_buy_support_and_withdrawal_revokes_it():
    reposts = [_ev(i, f"chain:{i}", "republication", pub="新华社") for i in range(5)]
    assert verification.verify(reposts, "attributed_statement").confirmation == "insufficient"
    two = [_ev(1, "o1", "confirmed_independent_report"), _ev(2, "o2", "confirmed_independent_report", docstatus="withdrawn")]
    assert verification.verify(two, "independently_reported").confirmation == "insufficient"


# ---- claim rules ------------------------------------------------------------------------------------------------------------
def test_categories_are_taxonomy_ids():
    tax = json.loads((SPEC_PKG / "sources_sectors" / "taxonomy.json").read_text(encoding="utf-8"))
    assert set(claims.CATEGORY_IDS) == {c["category_id"] for c in tax["event_categories"]}


def test_recorded_page_extracts_enums_and_spans_only_and_subjects_come_from_vendor_refs():
    p = feeds.parse_eastmoney_news(recorded("eastmoney_news_p1.json"))
    n_subject = 0
    for rec in p.records:
        for c in claims.extract_rules(rec):
            claims.check_candidate(rec, c)
            schemas.validate("pulse.intel.claim_extraction", "1", {"extractions": [{"document_ref": "x", "claims": [c.to_structured()]}]})
            n_subject += bool(c.subject_refs)
            assert all(r in rec.stock_refs for r in c.subject_refs)
            if c.category and c.category.startswith("company."):
                assert c.subject_refs  # company category never without a named subject
    assert n_subject > 0


def test_sector_keyword_alone_never_creates_a_subject():
    rec = feeds.NewsRecord("s", "1", "半导体板块大涨 多家芯片公司受益", "半导体设备订单增长", None, "unknown", (), False, {})
    (c,) = claims.extract_rules(rec)
    assert c.subject_refs == () and not (c.category or "").startswith("company.")
    v = claims.derive(rec, c)
    assert v.subject_key is None and "半导体" in v.sector_hints


def test_candidate_with_foreign_subject_or_bad_span_is_rejected():
    rec = feeds.NewsRecord("s", "1", "标题", "正文", None, "unknown", ("1.605136",), False, {})
    bad_subject = claims.Candidate("company.contract_order", "independently_reported", "+", ("0.000001",), "none", False, (claims.Span("title", 0, 2),))
    with pytest.raises(claims.CandidateError):
        claims.check_candidate(rec, bad_subject)
    bad_span = claims.Candidate(None, "independently_reported", "none", (), "none", False, (claims.Span("title", 0, 99),))
    with pytest.raises(claims.CandidateError):
        claims.check_candidate(rec, bad_span)
    no_subject_company = claims.Candidate("company.contract_order", "independently_reported", "+", (), "none", False, (claims.Span("title", 0, 1),))
    with pytest.raises(claims.CandidateError):
        claims.check_candidate(rec, no_subject_company)


def test_assertion_types():
    def one(title, body="", own=False):
        return claims.extract_rules(feeds.NewsRecord("s", "1", title, body, None, "unknown", (), own, {}))[0]

    assert one("传闻某公司将被收购").assertion_type == "rumor"
    assert one("预计明年销量增长").assertion_type == "forecast"
    assert one("美联储官员表示", "据路透报道，美联储官员表示将暂停加息").assertion_type == "attributed_statement"
    assert one("央行开展逆回购", "财联社11月11日电，央行开展逆回购", own=True).assertion_type == "independently_reported"
    assert one("某公司辟谣：公司从未涉及此事").denial


# ---- impact mapping -----------------------------------------------------------------------------------------------------------
def test_impact_mapping_named_subject_has_horizon_falsifier_and_uncalibrated_prior():
    m = impact.map_impact(claim_id="c", claim_revision_id="cr", category="company.contract_order", headline="h", polarity_hint="+", subject_code="605136",
                          subject_instrument_id="00000000-0000-4000-8000-000000000001", sector_hints=[], event_time=None)
    schemas.validate("pulse.intel.impact_mapping", "1", m)
    assert m["exposure_status"] == "named_subject" and len(m["exposed_instruments"]) == 1
    assert m["business_variables"][0]["direction_prior"] == "+" and m["business_variables"][0]["direction_status"] == "uncalibrated_prior"
    assert m["calibration"] == {"status": "uncalibrated", "probability": None}
    assert m["falsifier"]["observable"] and m["horizon"]["to"] > 0 and m["competing_explanations"]


def test_impact_mapping_sector_keyword_has_no_instruments():
    m = impact.map_impact(claim_id="c", claim_revision_id="cr", category="industry.technology_capacity", headline="h", polarity_hint="+", subject_code=None,
                          subject_instrument_id=None, sector_hints=["半导体"], event_time=None)
    assert m["exposed_instruments"] == [] and m["exposure_status"] == "sector_hypothesis_unmapped"
    assert m["sector_hints"][0]["status"] == "keyword_hit_not_a_mapping"


def test_impact_schema_rejects_a_probability():
    m = impact.map_impact(claim_id="c", claim_revision_id="cr", category="policy.monetary", headline="h", polarity_hint="-", subject_code=None,
                          subject_instrument_id=None, sector_hints=[], event_time=None)
    m["calibration"]["probability"] = 0.7
    with pytest.raises(schemas.SchemaViolation):
        schemas.validate("pulse.intel.impact_mapping", "1", m)
