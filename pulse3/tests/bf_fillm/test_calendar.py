"""Exchange holiday schedule (frozen data, no dates from memory) and the legacy calendar store as a declared dataset on real PostgreSQL.
The store lines are SYNTHETIC; the equivalence test with the real old store runs when the private copy is present."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from pulse import contracts
from pulse.application import bf_fillm_calendar as CAL
from pulse.domain.market_rules import forward_days
from pulse.domain.market_rules import holidays as H
from pulse.domain.portfolio.forward_book import HybridCalendar

UTC = timezone.utc


# ---- schedule rules (pure) ---------------------------------------------------------------------------------------------------
def test_shipped_years_are_missing_with_a_reason_and_no_dates():
    for year in (2026, 2027):
        s = H.OFFICIAL_SCHEDULES[year]
        assert s.status == H.MISSING and not s.closed and s.missing_reason
    assert "c_20251222_10802507" in H.OFFICIAL_SCHEDULES[2026].missing_reason
    assert H.is_open(date(2026, 12, 24)) is None, "a weekday of a year without a complete schedule is unknown, not open"
    assert H.is_open(date(2026, 12, 26)) is False, "a Saturday is closed whatever the schedule says"


def test_a_complete_schedule_needs_its_citation_and_weekday_dates_of_its_year():
    ok = dict(year=2026, status=H.COMPLETE, closed=frozenset({date(2026, 6, 19)}), notice_title="t", issuer="SSE", published_on=date(2025, 12, 22), source_ref="u")
    H.YearSchedule(**ok)
    for bad in ({"notice_title": None}, {"issuer": None}, {"published_on": None}, {"source_ref": None}, {"closed": frozenset()},
                {"closed": frozenset({date(2026, 6, 20)})}, {"closed": frozenset({date(2025, 6, 19)})}):
        with pytest.raises(ValueError):
            H.YearSchedule(**{**ok, **bad})
    with pytest.raises(ValueError):
        H.YearSchedule(2027, H.MISSING, closed=frozenset({date(2027, 1, 4)}), missing_reason="x")
    with pytest.raises(ValueError):
        H.YearSchedule(2027, H.MISSING)


def synthetic(year: int, *closed: date) -> dict[int, H.YearSchedule]:
    """TEST DATA, not an official notice: it only exercises the rules."""
    return {year: H.YearSchedule(year, H.COMPLETE, frozenset(closed), "synthetic test notice", "test", date(year - 1, 12, 1), "test://notice")}


def test_tail_skips_complete_closures_and_reports_assumed_for_unknown_years():
    sch = synthetic(2026, date(2026, 11, 20))                                   # a Friday
    tail, assumed = H.weekday_tail(date(2026, 11, 17), 6, sch)                  # Tue 11-17 .. Mon 11-23
    assert tail == [date(2026, 11, 18), date(2026, 11, 19), date(2026, 11, 23)] and assumed is False
    tail2, assumed2 = H.weekday_tail(date(2026, 12, 29), 5, sch)                # crosses into 2027, which the schedule does not know
    assert date(2027, 1, 1) in tail2 and assumed2 is True


def test_forward_days_keeps_the_weekday_rule_and_says_it_assumed_while_the_data_are_missing():
    tail, assumed = forward_days(date(2026, 11, 17), 7)
    assert tail == [date(2026, 11, 18), date(2026, 11, 19), date(2026, 11, 20), date(2026, 11, 23), date(2026, 11, 24)] and assumed is True


def test_hybrid_calendar_uses_the_schedule_after_the_last_observed_day(monkeypatch):
    cal = HybridCalendar([date(2026, 11, 16), date(2026, 11, 17)])
    assert cal.next_trading_day(date(2026, 11, 19)) == date(2026, 11, 20)
    monkeypatch.setitem(H.OFFICIAL_SCHEDULES, 2026, synthetic(2026, date(2026, 11, 20))[2026])
    assert cal.is_trading_day(date(2026, 11, 20)) is False
    assert cal.next_trading_day(date(2026, 11, 19)) == date(2026, 11, 23)
    assert cal.is_trading_day(date(2026, 11, 17)) is True, "an observed day is never overridden by the schedule"
    assert cal.trading_days_between(date(2026, 11, 17), date(2026, 11, 24)) == 4


# ---- the old calendar store as a dataset ---------------------------------------------------------------------------------------
def line(event_id: str, revision: int, first_seen: str, **kw) -> str:
    e = {"schema_version": "1.0.0", "event_id": event_id, "revision": revision, "supersedes": None, "name": kw.get("name", event_id), "category": "market_rule",
         "region": "CN", "status": kw.get("status", "official_scheduled"), "event_date": kw.get("event_date", "2026-11-20"), "event_end_date": None, "event_at": None,
         "event_timezone": "Asia/Shanghai", "precision": "date", "milestones": [], "source_url": "x", "source_announcement_date": None, "schedule_known_at": None,
         "first_seen_at": first_seen, "result_known_at": None, "hypothesis_registered_at": None, "themes": [], "instruments": kw.get("instruments", []), "notes": ""}
    return json.dumps(e, ensure_ascii=False)


@pytest.fixture()
def store(tmp_path):
    p = tmp_path / "events.jsonl"
    p.write_text("\n".join([line("A", 0, "2026-11-02T11:57:21.853219Z"), line("A", 1, "2026-11-09T01:00:00Z", status="postponed", event_date="2026-11-25"),
                            line("B", 0, "2026-11-05T03:00:00", instruments=["600000"])]) + "\n", encoding="utf-8")   # B: naive time = Asia/Shanghai
    return p


@pytest.mark.pg
def test_import_is_idempotent_versioned_and_read_point_in_time(acon, flake, store):
    t0 = datetime(2026, 11, 12, 1, tzinfo=UTC)
    s = CAL.import_store(acon, flake, store, clock=lambda: t0)
    assert s.counts["events"] == 2 and s.counts["revisions"] == 3
    again = CAL.import_store(acon, flake, store, clock=lambda: t0 + timedelta(hours=1))
    assert again.counts.get("unchanged") == 1
    assert acon.execute("select count(*) n from market.dataset_manifest where dataset_id=%s", (CAL.DATASET,)).fetchone()["n"] == 1
    events, info = CAL.read_calendar_events(acon, flake, t0 + timedelta(minutes=1))
    got = {e.event_id: e for e in events}
    assert got["A"].event_date == date(2026, 11, 25) and got["A"].status == "postponed", "the newest revision seen by the cutoff wins"
    assert got["B"].first_seen_at == datetime(2026, 11, 4, 19, 0, tzinfo=UTC), "a naive store time is Asia/Shanghai"
    assert got["B"].instruments == frozenset({"600000"}) and info["present"] is True and info["events"] == 2
    # before the import nothing is available to pulse3 (the store content is available from the import time)
    none, none_info = CAL.read_calendar_events(acon, flake, t0 - timedelta(minutes=1))
    assert none == [] and none_info["present"] is False
    # a changed file is a new version; the old version stays readable at its own cutoff
    store.write_text(store.read_text(encoding="utf-8") + line("C", 0, "2026-11-13T00:00:00Z") + "\n", encoding="utf-8")
    CAL.import_store(acon, flake, store, clock=lambda: t0 + timedelta(days=2))
    assert {e.event_id for e in CAL.read_calendar_events(acon, flake, t0 + timedelta(days=1))[0]} == {"A", "B"}
    assert {e.event_id for e in CAL.read_calendar_events(acon, flake, t0 + timedelta(days=3))[0]} == {"A", "B", "C"}


@pytest.mark.pg
def test_a_revision_first_seen_after_the_cutoff_is_not_visible(acon, flake, store):
    t0 = datetime(2026, 11, 5, tzinfo=UTC)                                    # revision 1 of A is first seen on 11-09, after this import and cutoff
    CAL.import_store(acon, flake, store, clock=lambda: t0)
    events, _ = CAL.read_calendar_events(acon, flake, t0 + timedelta(hours=1))
    a = [e for e in events if e.event_id == "A"][0]
    assert a.event_date == date(2026, 11, 20) and a.status == "official_scheduled", "revision 1 was not known yet"
    assert {e.event_id for e in events} == {"A", "B"}
    later, _ = CAL.read_calendar_events(acon, flake, datetime(2026, 11, 10, tzinfo=UTC))
    assert [e for e in later if e.event_id == "A"][0].event_date == date(2026, 11, 25)


@pytest.mark.pg
def test_a_half_understood_file_imports_nothing_and_a_missing_file_says_so(acon, flake, tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text(line("A", 0, "2026-11-02T00:00:00Z") + '\n{"event_id": "B"}\n', encoding="utf-8")
    s = CAL.import_store(acon, flake, bad)
    assert s.counts.get("unreadable") == 1 and CAL.latest_ready(acon) is None
    assert "does not exist" in CAL.import_store(acon, flake, tmp_path / "none.jsonl").notes[0]
    events, info = CAL.read_calendar_events(acon, flake)
    assert events == [] and info["present"] is False, "no dataset must read as 'not imported', not as an empty calendar"


@pytest.mark.pg
def test_calendar_store_status_route_is_contract_valid(fclient, fresh_dsn, store):
    from pulse import config
    from pulse.adapters.lake import LakeStore
    from pulse.adapters.postgres import db

    r0 = fclient.get("/v1/x/bf/fillm/calendar-store")
    assert r0.status_code == 200 and r0.json()["data"]["present"] is False and r0.json()["meta"]["coverage"]["unverifiable_reasons"] == ["calendar_store_not_imported"]
    conn = db.connect(fresh_dsn, autocommit=True)
    try:
        CAL.import_store(conn, LakeStore(config.load().lake_root), store)
    finally:
        conn.close()
    d = fclient.get("/v1/x/bf/fillm/calendar-store").json()["data"]
    contracts.check("CalendarStoreStatus", d, contracts.backfill_id("fillm"))
    assert d["present"] is True and d["events"] == 2 and d["revisions"] == 3 and "read_calendar_events" in d["reader"]
    assert len(d["source_sha256"]) == 64 and {x["event_id"] for x in d["sample"]} == {"A", "B"}


REAL = Path(__file__).resolve().parents[3] / ".work" / "fwdlog" / "aux" / "events.jsonl"


@pytest.mark.pg
@pytest.mark.skipif(not REAL.exists(), reason="the private copy of the old calendar store is not on this machine")
def test_the_import_reads_back_exactly_what_the_forward_log_reads_from_the_old_file(acon, flake):
    from pulse.application import bf_fwdlog_inputs as FI

    CAL.import_store(acon, flake, REAL, clock=lambda: datetime(2026, 11, 18, tzinfo=UTC))
    old, old_info = FI.read_events(REAL)
    new, new_info = CAL.read_calendar_events(acon, flake, datetime(2026, 11, 19, tzinfo=UTC))
    assert sorted(old, key=lambda e: e.event_id) == sorted(new, key=lambda e: e.event_id)
    assert old_info["sha256"] == new_info["source_sha256"] and len(new) == old_info["events"] > 0
