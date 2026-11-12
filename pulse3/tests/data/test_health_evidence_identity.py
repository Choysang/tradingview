"""M1-E04 source health, official announcement normalization (M1-E01/E02), instrument identity."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pulse.domain.evidence import announcements as ann
from pulse.domain.evidence.health import HealthPolicy, HealthState, apply_attempt, evaluate_freshness, half_open
from pulse.domain.instruments import identity as ident

from .helpers import recorded

T0 = datetime(2026, 11, 11, 3, 0, tzinfo=timezone.utc)


def sec(n: float) -> timedelta:
    return timedelta(seconds=n)


# ---- health ----------------------------------------------------------------------------------------
def test_http_200_with_old_content_is_contact_not_recovery():
    s = HealthState(runtime_state="healthy")
    s = apply_attempt(s, "transport_error", T0, error_code="timeout")
    s = apply_attempt(s, "transport_error", T0 + sec(60), error_code="timeout")
    s = apply_attempt(s, "transport_error", T0 + sec(200), error_code="timeout")
    assert s.runtime_state == "unavailable" and s.consecutive_failures == 3
    old = T0 - timedelta(hours=3)
    s = apply_attempt(s, "success_data", T0 + sec(900), new_records=0, newest_content_at=old)
    assert s.freshness_state == "stale" and s.runtime_state == "degraded"  # reachable again, still not recovered
    assert s.last_new_record_at is None and s.last_transport_ok_at == T0 + sec(900)
    fresh = apply_attempt(s, "success_data", T0 + sec(1000), new_records=5, newest_content_at=T0 + sec(990))
    assert fresh.runtime_state == "healthy" and fresh.freshness_state == "current" and fresh.last_new_record_at == T0 + sec(1000)


def test_quiet_news_and_closed_market_are_not_failures():
    s = HealthState(runtime_state="healthy")
    s = apply_attempt(s, "success_empty", T0, newest_content_at=T0 - timedelta(hours=5), content_expected_now=False)
    assert s.freshness_state == "not_expected" and s.runtime_state == "healthy"
    s = apply_attempt(s, "not_modified", T0 + sec(60), newest_content_at=None, content_expected_now=True)
    assert s.freshness_state == "unknown" and s.runtime_state == "healthy" and s.last_new_record_at is None


def test_login_page_is_not_data_and_schema_change_pauses_canonical():
    s = apply_attempt(HealthState(runtime_state="healthy", last_transport_ok_at=T0), "auth_required", T0 + sec(10), error_code="login_page")
    assert s.runtime_state == "auth_required" and s.last_transport_ok_at == T0  # contact time unchanged
    s = apply_attempt(HealthState(runtime_state="healthy"), "schema_changed", T0, error_code="missing_key:announcements")
    assert s.runtime_state == "degraded" and s.last_error_code == "missing_key:announcements" and s.next_attempt_at > T0


def test_block_opens_the_circuit_and_http_200_does_not_close_it_early():
    s = apply_attempt(HealthState(runtime_state="healthy"), "blocked", T0, error_code="http_403")
    assert s.runtime_state == "unavailable" and s.circuit == "open" and s.last_error_code == "blocked:http_403"
    assert s.next_attempt_at == T0 + sec(HealthPolicy().blocked_cooldown_seconds)
    with pytest.raises(ValueError, match="circuit_open"):
        apply_attempt(s, "success_data", T0 + sec(60), new_records=1, newest_content_at=T0)
    probe = half_open(s, s.next_attempt_at)
    assert probe.circuit == "half_open"
    ok = apply_attempt(probe, "success_data", s.next_attempt_at, new_records=1, newest_content_at=s.next_attempt_at - sec(5))
    assert ok.circuit == "closed" and ok.runtime_state == "healthy"


def test_disabled_and_unconfigured_are_not_claimed_healthy_and_clocks_are_checked():
    assert apply_attempt(HealthState(enabled=False), "success_data", T0).runtime_state == "paused"
    assert apply_attempt(HealthState(configured=False), "success_data", T0).runtime_state == "unconfigured"
    with pytest.raises(ValueError, match="future"):
        apply_attempt(HealthState(runtime_state="healthy"), "success_data", T0, newest_content_at=T0 + sec(1))
    assert evaluate_freshness(T0 - sec(100), T0, stale_after_seconds=300) == "current"
    assert evaluate_freshness(T0 - sec(100), T0, stale_after_seconds=60) == "stale"
    assert evaluate_freshness(T0 - sec(100), T0, stale_after_seconds=60, required_after=T0 - sec(200)) == "current"


# ---- announcements ----------------------------------------------------------------------------------
def test_recorded_cninfo_pages_normalize_with_original_id_title_time_and_url():
    p1, p2 = ann.parse_response(recorded("cninfo_page1.json")), ann.parse_response(recorded("cninfo_page2.json"))
    assert p1.has_more and len(p1.records) == 30 and p1.total_records > 30
    ids = [r["announcementId"] for r in p1.records + p2.records]
    assert len(set(ids)) == len(ids)  # pages do not overlap
    rec = p1.records[0]
    d = ann.normalize_record(rec)
    assert d.source_record_id == str(rec["announcementId"]) and d.url.startswith("http://static.cninfo.com.cn/")
    assert d.sec_code == rec["secCode"] and d.revision == 1 and d.document_status == "published"
    assert d.published_at is not None and d.published_at.tzinfo is not None
    assert d.document_id == ann.document_uuid(d.source_record_id)


def test_midnight_time_is_date_precision_not_invented_seconds():
    _, prec = ann.publish_time(1794326400000)  # 2026-11-11 00:00:00 CST
    assert prec == "date"
    _, prec = ann.publish_time(1794367951000)
    assert prec == "second"
    assert ann.publish_time(None) == (None, "unknown")


def test_same_announcement_on_other_portals_is_one_origin_but_issuers_differ():
    base = {"announcementId": "1", "secCode": "600000", "announcementTitle": "浦发银行关于回购股份的公告", "announcementTime": 1794367951000, "adjunctUrl": "finalpage/a.PDF"}
    mirror = {**base, "announcementId": "portal-77", "announcementTitle": "浦发银行 关于回购股份的公告"}
    other_issuer = {**base, "secCode": "600001"}
    a, b, c = (ann.normalize_record(x) for x in (base, mirror, other_issuer))
    assert a.origin_group == b.origin_group != c.origin_group


def test_revision_withdrawal_and_relation_hints():
    rec = {"announcementId": "9", "secCode": "000001", "announcementTitle": "平安银行关于2025年度利润分配方案的更正公告", "announcementTime": 1794367951000, "adjunctUrl": "x.PDF", "adjunctSize": 10}
    first = ann.normalize_record(rec)
    assert [h["relation"] for h in first.hints] == ["corrects"] and first.hints[0]["resolved_document_id"] is None
    assert first.category == "company.capital_action"
    assert ann.next_revision(None, first) == (1, "published")
    assert ann.next_revision(first, ann.normalize_record(dict(rec))) is None  # repeat poll: no new business conclusion
    changed = ann.normalize_record({**rec, "adjunctSize": 11})
    assert ann.next_revision(first, changed) == (2, "corrected")
    assert ann.detect_deleted({"1", "2", "3"}, {"1", "3"}, window_fully_paged=True) == {"2"}
    assert ann.detect_deleted({"1", "2", "3"}, {"1"}, window_fully_paged=False) == set()  # a partial listing proves nothing
    assert ann.event_category("某公司关于召开董事会的通知") is None  # unclassified stays unclassified


def test_login_page_and_schema_drift_are_not_data():
    with pytest.raises(ann.NotData):
        ann.parse_response("<html><title>登录</title></html>".encode())
    with pytest.raises(ann.NotData):
        ann.parse_response(b"")
    with pytest.raises(ann.SchemaChanged):
        ann.parse_response(b'{"totalAnnouncement": 3}')
    with pytest.raises(ann.SchemaChanged, match="missing_record_keys"):
        ann.parse_response(b'{"announcements":[{"announcementId":"1"}]}')
    empty = ann.parse_response(b'{"announcements":null,"hasMore":false,"totalAnnouncement":0}')
    assert empty.empty_success and empty.records == ()


def test_date_windows_are_non_overlapping_cst_days():
    w = ann.date_windows(datetime(2026, 11, 8, 20, tzinfo=timezone.utc), datetime(2026, 11, 11, 6, tzinfo=timezone.utc))
    assert w == [("2026-11-09", "2026-11-09"), ("2026-11-10", "2026-11-10"), ("2026-11-11", "2026-11-11")]


# ---- identity --------------------------------------------------------------------------------------
def test_instrument_uuid_is_stable_and_symbols_are_aliases():
    a = ident.instrument_uuid("XSHE", "000001")
    assert str(a) == "260eb48a-a8aa-5638-a898-ea93c1e9d5b5"  # frozen forever: changing the namespace re-identifies everything
    assert a != ident.instrument_uuid("XSHG", "000001") != ident.index_uuid("XSHG", "000001")  # stock 000001.SZ vs index 000001.SH
    assert ident.parse_symbol("600000.SH") == ("600000", "XSHG") and ident.parse_symbol("sz000001") == ("000001", "XSHE")
    with pytest.raises(ValueError):
        ident.parse_symbol("600000")
    with pytest.raises(ValueError):
        ident.instrument_uuid("XSHE", "1")  # leading zeros are part of the identity


@pytest.mark.parametrize("code,venue,board", [("600519", "XSHG", "main"), ("601318", "XSHG", "main"), ("688981", "XSHG", "star"),
                                              ("000001", "XSHE", "main"), ("002415", "XSHE", "main"), ("300750", "XSHE", "chinext"),
                                              ("920001", "XBSE", "bse"), ("430047", "XBSE", "bse")])
def test_board_classification(code, venue, board):
    assert ident.venue_for_code(code) == venue and ident.board_for(venue, code) == board


def test_risk_warning_parse_uses_the_dated_name_only():
    assert ident.parse_display_name("*ST爱迪") == ("*ST爱迪", True)
    assert ident.parse_display_name("ＳＴ达华") == ("ST达华", True)
    assert ident.parse_display_name("万  科Ａ") == ("万科A", False)
    assert ident.parse_display_name("STAR市场")[1] is False  # ST must not be followed by a Latin letter
    rec = ident.InstrumentRecord.build("000413", "ST旭电")
    assert rec.is_risk_warning and rec.board_code == "main" and rec.symbols["sina"] == "sz000413"
    assert ident.is_main_board_universe("600000") and not ident.is_main_board_universe("300750")
