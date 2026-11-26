"""The E101 forward step and the plan report on real PostgreSQL with synthetic raw bars: market rows, events, D0 / R outcomes, the waiting rule, append-only rows, the report file."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal as D

import polars as pl
import pytest

from bf_batch7.support import flat, weekdays, window
from pulse.application import bf_batch7_inputs as IN7
from pulse.application import bf_e101_forward as FW
from pulse.application import bf_e101_plan as PL
from pulse.application import bindings as B
from pulse.application.bf_batch6_inputs import Scope
from pulse.application.bf_e101_service import E101Service
from pulse.application.refdata_service import instrument_id_of
from pulse.domain.e101 import rules as R

pytestmark = pytest.mark.pg
UTC = timezone.utc
NOW = datetime(2026, 12, 11, 12, 0, tzinfo=UTC)
DAYS = weekdays(date(2026, 11, 2), 30)                 # 2026-11-02 .. 2026-12-11
LEAD, CORE_, T = "600501", "600502", date(2026, 12, 11)


def at(m, d, hh, mm=0) -> datetime:
    return datetime(2026, m, d, hh, mm, tzinfo=R.CST)


EVENTS_ROWS = [{"name": "美国:新增非农就业人数:当月值:季调(报告期:2026年11月)", "release_at": at(12, 4, 21, 30), "known_at": at(11, 20, 8)},
               {"name": "美国:联邦基金利率目标:上限(报告期:2026年12月)", "release_at": at(12, 10, 3), "known_at": at(11, 20, 8)},
               {"name": "美国:核心PCE物价指数:同比(报告期:2026年10月)", "release_at": at(11, 25, 21, 30), "known_at": at(11, 20, 8)}]


def series() -> dict:
    i_d0 = DAYS.index(date(2026, 12, 4))
    # three limit-up closes up to the session before D0 (12-01, 12-02, 12-03), then D0 opens 13.50 and closes 14.00
    lead = flat("10.00", i_d0 - 3) + [("10.00", "11.00", 1000), ("11.00", "12.10", 1000), ("12.10", "13.31", 1000), ("13.50", "14.00", 1000)]
    lead += flat("14.00", len(DAYS) - len(lead))
    core, px = [], 50.0
    for _ in DAYS:                                     # the biggest name, up 1 % a day
        px = round(px * 1.01, 2)
        core.append((f"{px:.2f}", f"{px:.2f}", 1000))
    s = {LEAD: lead, CORE_: core}
    for i in range(28):
        s[f"6006{i:02d}"] = flat(f"{10 + i}.00", len(DAYS))
    return s


@pytest.fixture
def world(conn, lake, monkeypatch):
    s = series()
    svc = E101Service(conn, lake, clock=lambda: NOW, loader=lambda which, since: window(s, DAYS) if which == IN7.NATIVE else None)
    codes = list(s)
    iid_of = {c: instrument_id_of(c) for c in codes}
    code_of = {v: k for k, v in iid_of.items()}
    caps = {c: (1000.0 if c == CORE_ else 10.0 + i) for i, c in enumerate(codes)}
    frame = pl.DataFrame({"instrument_id": [iid_of[c] for c in codes], "as_of": ["2026-11-10"] * len(codes), "available_at": [datetime(2026, 11, 11, tzinfo=UTC)] * len(codes),
                          "float_cap_cny": [D(str(caps[c] * 1e8)) for c in codes], "total_cap_cny": [D(str(caps[c] * 2e8)) for c in codes]},
                         schema={"instrument_id": pl.String, "as_of": pl.String, "available_at": pl.Datetime("us", "UTC"), "float_cap_cny": pl.Decimal(38, 4), "total_cap_cny": pl.Decimal(38, 4)})
    B.publish_derived(svc.svc, B.CAP_DATASET, frame, {"dataset": B.CAP_DATASET, "as_of": "2026-11-10"}, contract_id="pulse.analysis_artifact", watermark=NOW)
    B.publish_derived(svc.svc, "bars_raw_1d", pl.DataFrame({"x": [1]}), {"dataset": "bars_raw_1d", "session_date": "2026-12-10"}, contract_id="pulse.analysis_artifact", watermark=NOW)
    monkeypatch.setattr(IN7, "load_scope", lambda conn_, as_of: (code_of, iid_of, Scope({}, frozenset(), {}), {c: date(2010, 1, 1) for c in codes}))
    return svc


def events():
    from pulse.application import bf_e101_core as C
    return C.events_of(EVENTS_ROWS)


def test_forward_records_market_events_and_outcomes_once_and_waits_for_the_r_plus_2_exit(world, conn, lake):
    out = world.forward(T, as_of=NOW, events=events())
    assert out["appended"], out
    ix = FW.index(FW.stored_rows(conn, lake))
    assert sorted(ix["event"]) == ["2026-12-04|US_NFP", "2026-12-09|FOMC"]                 # the 11-25 core PCE (D0 = freeze day) is out
    assert min(ix["market"]) == "2026-11-26" and max(ix["market"]) == "2026-12-10"      # sessions before the run date only
    assert sorted(ix["context_event"]) == ["2026-11-25|US_CORE_PCE"]                   # its R 2026-11-26 is no H1 control day
    nfp = ix["d0_outcome"]["2026-12-04|US_NFP"]
    assert nfp["status"] == "decided" and nfp["decision_day"] == "2026-12-03"
    lead = nfp["groups"]["leader"]["names"]
    assert [x["code"] for x in lead] == [LEAD] and lead[0]["status"] == "filled"
    assert abs(lead[0]["ret_gross"] - (14.00 / 13.50 - 1)) < 1e-9
    assert CORE_ in [x["code"] for x in nfp["groups"]["core"]["names"]]
    assert "2026-12-04|US_NFP" in ix["r_outcome"] and "2026-12-09|FOMC" not in ix["r_outcome"]     # FOMC R = 12-10: its R+2 exit is not observable yet
    r = ix["r_outcome"]["2026-12-04|US_NFP"]
    assert r["decision_day"] == "2026-12-04" and r["surprise"] is None and "no consensus source" in r["consensus_note"]
    again = world.forward(T, as_of=NOW, events=events())
    assert not again["appended"] and any("waiting R 2026-12-09|FOMC" in n for n in again["notes"])
    st = world.status(T)
    assert st["events_resolved"] == 2 and st["h1h3"]["state"] == "refused" and st["h2h4"]["state"] == "refused"


def test_the_plan_report_is_written_once_with_the_groups_of_the_previous_close(world, conn, lake, tmp_path):
    out = world.plan("d0", date(2026, 12, 4), out_dir=tmp_path, as_of=NOW, events=events())
    assert out["appended"] and out["groups_at"] == "2026-12-03"
    text = (tmp_path / "2026-12-04-d0-US_NFP.md").read_text(encoding="utf-8")
    assert text.startswith(f"# {PL.BANNER}") and LEAD in text and "买入" not in text and "卖出" not in text
    row = FW.index(FW.stored_rows(conn, lake))["plan_d0"]["2026-12-04|US_NFP"]
    assert row["groups"]["leader"][0]["code"] == LEAD and row["consensus"] is None
    assert not world.plan("d0", date(2026, 12, 4), out_dir=tmp_path, as_of=NOW, events=events())["appended"]
    assert world.plan("r", date(2026, 12, 5), out_dir=tmp_path, as_of=NOW, events=events())["reason"].startswith("no US release")
    assert json.loads(json.dumps(out))["files"]


def _cal(conn, cid, rev, name, *, at_=None, day=None, status="scheduled", avail):
    import hashlib
    prec = "minute" if at_ is not None else "date"
    conn.execute("INSERT INTO calendar.event_revision(calendar_event_id, revision, source_id, source_event_key, name, category, region, scheduled_at, scheduled_date,"
                 " time_precision, status, content_hash, available_at) VALUES (%s,%s,'eastmoney_calendar',%s,%s,'macro_release','US',%s,%s,%s,%s,%s,%s)",
                 (cid, rev, f"k{cid}", name, at_, day, prec, status, hashlib.sha256(f"{cid}{rev}".encode()).hexdigest(), avail))


def test_the_newest_calendar_revision_decides_and_known_at_needs_the_same_release_time(conn):
    import uuid
    from pulse.application import bf_e101_core as C
    a, b, c = (str(uuid.uuid4()) for _ in range(3))
    nfp = "美国:新增非农就业人数:当月值:季调"
    _cal(conn, a, 1, nfp, at_=at(12, 4, 21, 30), avail=at(11, 10, 8))
    _cal(conn, a, 2, nfp, day=date(2026, 12, 18), status="postponed", avail=at(12, 1, 8))            # newest: postponed, date only
    _cal(conn, b, 1, "美国:核心CPI:同比", at_=at(12, 9, 21, 30), avail=at(11, 10, 8))
    _cal(conn, b, 2, "美国:核心CPI:同比", at_=at(12, 10, 21, 30), avail=at(12, 9, 10))               # moved one day later, known 12-09 10:00
    _cal(conn, c, 1, "美国:联邦基金利率目标:上限", at_=at(12, 10, 3), avail=at(12, 20, 8))           # not visible yet
    rows = C.calendar_rows(conn, at(12, 9, 12))
    assert [r["calendar_event_id"] for r in rows] == [b]                                             # a: newest is postponed; c: not visible
    assert rows[0]["release_at"] == at(12, 10, 21, 30) and rows[0]["known_at"] == at(12, 9, 10)
    ev = C.events_of(rows)
    assert ev[0].ids == (b,) and ev[0].d0 == date(2026, 12, 10) and ev[0].known_before_open()
    before = C.calendar_rows(conn, at(11, 20, 8))
    assert {r["calendar_event_id"] for r in before} == {a, b}                                        # the postponement was not visible yet


def test_a_stored_event_whose_calendar_row_moves_is_marked_revised_and_left_out(world, conn, lake):
    from pulse.application import bf_e101_core as C
    first = C.events_of([{**EVENTS_ROWS[0], "calendar_event_id": "id-nfp"}])
    world.forward(date(2026, 12, 7), as_of=NOW, events=first)
    moved = C.events_of([{**EVENTS_ROWS[0], "release_at": at(12, 9, 21, 30), "calendar_event_id": "id-nfp"}])
    world.forward(T, as_of=NOW, events=moved)
    ix = FW.index(FW.stored_rows(conn, lake))
    assert "2026-12-04|US_NFP" in ix["event_revised"] and "2026-12-09|US_NFP" in ix["event"]
    assert [e.key for e, _ in FW._resolved(ix)] == ["2026-12-09|US_NFP"]


def test_h1h3_and_h2h4_are_inconclusive_after_the_deadline_and_written_once(world, conn, lake):
    world.forward(T, as_of=NOW, events=events())
    late = date(2028, 1, 3)
    ov = world.judge(late, as_of=NOW)
    assert ov["h1h3"]["state"] == "inconclusive" and ov["h2h4"]["state"] == "inconclusive"
    assert sorted(ov["rows_written"]) == ["judgement_h1h3", "judgement_h2h4"]
    assert world.judge(late, as_of=NOW)["rows_written"] == []
    assert world.judge(T, as_of=NOW)["h2h4"]["state"] == "refused"
