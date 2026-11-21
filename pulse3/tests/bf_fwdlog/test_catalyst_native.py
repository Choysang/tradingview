"""M3 catalyst source switch and the native calendar reader (#174): mapping to CalEvent, point in time, no silent fallback, comparison with the legacy dataset.
SYNTHETIC events: proves wiring and invariants."""
from __future__ import annotations

import hashlib
import uuid
from datetime import date, datetime, timezone

import pytest

from pulse.application import bf_fwdlog_catalyst as CAT
from pulse.domain.fwdlog import board as BD

UTC = timezone.utc
T0 = datetime(2026, 11, 1, 2, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 10, 2, 0, tzinfo=UTC)


def ev(name, category="company_lockup", d=date(2026, 11, 12), stocks=(), eid=None, status="scheduled"):
    return BD.CalEvent(eid or name, name, category, status, d, T0, None, None, None, None, frozenset(stocks))


# ---- switch ------------------------------------------------------------------------------------------------------------------------
def test_source_mode_defaults_to_legacy_and_rejects_an_unknown_value():
    assert CAT.source_mode({}) == "legacy" and CAT.source_mode({CAT.ENV: ""}) == "legacy"
    assert CAT.source_mode({CAT.ENV: "native"}) == "native" and CAT.source_mode({CAT.ENV: " Legacy "}) == "legacy"
    with pytest.raises(ValueError, match="PULSE_CATALYST_SOURCE"):
        CAT.source_mode({CAT.ENV: "nativ"})


# ---- comparison ----------------------------------------------------------------------------------------------------------------------
def test_compare_counts_by_category_and_names_the_one_sided_events():
    legacy = [ev("a"), ev("b", "policy_meeting"), ev("c", stocks={"600001"}), ev("d", "macro_release")]
    native = [ev("a", eid="x1"), ev("c", stocks={"600001", "600002"}, eid="x2"), ev("e", "conference")]
    out = CAT.compare(legacy, native)
    assert (out["legacy_events"], out["native_events"], out["matched"]) == (4, 3, 2)
    assert out["by_category"]["company_lockup"] == {"legacy": 2, "native": 2}
    assert out["by_category"]["policy_meeting"] == {"legacy": 1, "native": 0} and out["by_category"]["conference"] == {"legacy": 0, "native": 1}
    assert out["only_legacy"] == 2 and {r["name"] for r in out["only_legacy_sample"]} == {"b", "d"}
    assert out["only_native"] == 1 and out["only_native_sample"][0]["name"] == "e"
    assert out["matched_with_different_stocks"] == 1 and out["different_stocks_sample"][0]["legacy_stocks"] == 1 and out["different_stocks_sample"][0]["native_stocks"] == 2


def test_compare_ignores_whitespace_in_names_and_two_empty_sides_agree():
    assert CAT.compare([ev("x  y")], [ev("x y")])["matched"] == 1
    out = CAT.compare([], [])
    assert out["matched"] == out["only_legacy"] == out["only_native"] == 0 and out["by_category"] == {}


# ---- native reader (PostgreSQL) -----------------------------------------------------------------------------------------------------
def put_rev(conn, cid, rev, name, status, *, date_=None, at=None, avail=T0, actual=None, iid=None, key=None, category="company_lockup"):
    from psycopg.types.json import Jsonb

    precision = "minute" if at is not None else "date"
    h = hashlib.sha256(f"{cid}{rev}{status}".encode()).hexdigest()
    conn.execute(
        "INSERT INTO calendar.event_revision(calendar_event_id, revision, source_id, source_event_key, name, category, region, scheduled_at, scheduled_date, time_precision, status,"
        " actual, instrument_id, content_hash, available_at) VALUES (%s,%s,'test',%s,%s,%s,'CN',%s,%s,%s,%s,%s,%s,%s,%s)",
        (cid, rev, key or f"k-{cid}", name, category, at, date_, precision, status, Jsonb(actual) if actual else None, iid, h, avail))


@pytest.fixture
def world(conn):
    conn.execute("SET session_replication_role = replica")           # instruments, aliases and taxonomy nodes of this test are bare rows
    own, member = uuid.uuid4(), uuid.uuid4()
    for iid, code in ((own, "600001"), (member, "600002")):
        conn.execute("INSERT INTO core.instrument_alias(alias_id, source_id, source_symbol, instrument_id, effective_from, available_at, revision, source_anchor)"
                     " VALUES (%s,'t',%s,%s,%s,%s,1,'a')", (uuid.uuid4(), code, iid, T0, T0))
    conn.execute("INSERT INTO core.instrument_tag(assignment_id, instrument_id, taxonomy_id, taxonomy_version, node_id, effective_from, available_at, assertion_kind, evidence_ref)"
                 " VALUES (%s,%s,'tx',1,'n1',%s,%s,'observed','e')", (uuid.uuid4(), member, T0, T0))
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    put_rev(conn, a, 1, "lockup A", "scheduled", date_=date(2026, 11, 12), iid=own)
    put_rev(conn, a, 2, "lockup A", "occurred", date_=date(2026, 11, 12), iid=own, avail=T1, actual={"v": 1})
    put_rev(conn, b, 1, "meeting B", "scheduled", at=datetime(2026, 11, 13, 1, 30, tzinfo=UTC), category="policy_meeting")
    conn.execute("INSERT INTO calendar.event_link(calendar_event_id, revision, link_rule_version, taxonomy_id, taxonomy_version, node_id, industry_hint)"
                 " VALUES (%s,1,'r1','tx',1,'n1','hint')", (b,))
    put_rev(conn, c, 1, "later C", "scheduled", date_=date(2026, 11, 20), avail=T1)
    return {"a": a, "b": b, "c": c}


def test_native_events_map_to_calevent_and_hide_revisions_after_the_cutoff(conn, world):
    mid = datetime(2026, 11, 5, tzinfo=UTC)
    early, info = CAT.read_native_events(conn, mid)
    by = {e.name: e for e in early}
    assert set(by) == {"lockup A", "meeting B"}, "event C was first available after the cutoff"
    a = by["lockup A"]
    assert a.status == "scheduled" and a.result_known_at is None and a.first_seen_at == T0 and a.event_date == date(2026, 11, 12) and a.instruments == {"600001"}
    b = by["meeting B"]
    assert b.event_at == datetime(2026, 11, 13, 1, 30, tzinfo=UTC) and b.event_date == date(2026, 11, 13) and b.event_timezone == "Asia/Shanghai"
    assert b.instruments == {"600002"}, "stocks of the industry link, resolved at the cutoff"
    assert info["path_present"] is True and info["events"] == 2 and len(info["sha256"]) == 64 and info["source"] == "pulse3_calendar_native"

    late, _ = CAT.read_native_events(conn, datetime(2026, 11, 15, tzinfo=UTC))
    la = {e.name: e for e in late}
    assert set(la) == {"lockup A", "meeting B", "later C"}
    assert la["lockup A"].status == "occurred" and la["lockup A"].result_known_at == T1 and la["lockup A"].first_seen_at == T1


def test_an_empty_native_calendar_is_reported_not_papered_over(conn):
    events, info = CAT.read_native_events(conn, datetime(2026, 11, 15, tzinfo=UTC))
    assert events == [] and info["path_present"] is False and info["events"] == 0 and info["sha256"] is None and "no event visible" in info["note"]


def test_the_catalyst_rule_gives_the_same_answer_on_native_events(conn, world):
    events, _ = CAT.read_native_events(conn, datetime(2026, 11, 15, tzinfo=UTC))
    before = datetime(2026, 11, 11, 3, 0, tzinfo=UTC)
    assert BD.catalyst_pending(events, "600002", before) is True, "the meeting of 11-13 is still ahead"
    assert BD.catalyst_pending(events, "999999", before) is None, "no linked event: unknown, not false"
    assert BD.catalyst_pending(events, "600001", datetime(2026, 11, 14, 3, 0, tzinfo=UTC)) is False, "the lock-up of 11-12 has landed"


# ---- service wiring -----------------------------------------------------------------------------------------------------------------
def service(conn, lake, tmp_path):
    from pulse.adapters.sources.legacy_recordings import LegacyRecordings
    from pulse.application.bf_fwdlog_service import FwdlogService

    return FwdlogService(conn, lake, LegacyRecordings(tmp_path), clock=lambda: datetime(2026, 11, 15, tzinfo=UTC))


def test_the_service_reads_the_native_calendar_only_when_asked_and_never_falls_back(conn, lake, tmp_path, world, monkeypatch):
    svc = service(conn, lake, tmp_path)
    monkeypatch.setenv(CAT.ENV, "native")
    events, info = svc._events()
    assert info["source"] == "pulse3_calendar_native" and info["events"] == len(events) == 3 and info["path_present"] is True
    monkeypatch.setenv(CAT.ENV, "nope")
    with pytest.raises(ValueError):
        svc._events()


def test_an_empty_native_calendar_does_not_fall_back_to_the_legacy_store(conn, lake, tmp_path, monkeypatch):
    monkeypatch.setenv(CAT.ENV, "native")
    monkeypatch.setenv("PULSE_FWDLOG_EVENTS", str(tmp_path / "events.jsonl"))
    (tmp_path / "events.jsonl").write_text('{"event_id":"e","revision":1,"name":"n","category":"conference","status":"scheduled","event_date":"2026-11-12","first_seen_at":"2026-11-01T00:00:00+08:00","instruments":["600001"]}\n',
                                           encoding="utf-8")
    events, info = service(conn, lake, tmp_path)._events()
    assert events == [] and info["path_present"] is False and info["source"] == "pulse3_calendar_native"
    monkeypatch.setenv(CAT.ENV, "legacy")
    events, info = service(conn, lake, tmp_path)._events()
    assert [e.event_id for e in events] == ["e"] and info["source"] == "file", "default behaviour unchanged: the legacy store is read"


def test_compare_stores_reports_an_absent_side(conn, lake, world):
    out = CAT.compare_stores(conn, lake, datetime(2026, 11, 15, tzinfo=UTC))
    assert out["native_present"] is True and out["legacy_present"] is False and out["native_events"] == 3 and out["legacy_events"] == 0
    assert out["only_native"] == 3 and any("legacy_calendar_events dataset is absent" in n for n in out["notes"])
