"""The batch 7 forward step on real PostgreSQL with synthetic raw bars: decisions, fills, outcomes, append-only rows, source recorded, no_data wait, registry, judge."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal as D

import polars as pl
import pytest

from bf_batch7.support import flat, weekdays, window
from pulse.application import bf_batch7_forward as FW
from pulse.application import bf_batch7_inputs as IN
from pulse.application import bf_batch7_reads as RD
from pulse.application import bindings as B
from pulse.application import bf_fwdlog_inputs as FI
from pulse.application.bf_batch7_service import Batch7Service
from pulse.application.refdata_service import instrument_id_of
from pulse.domain.batch6 import rules as R6
from pulse.domain.batch7 import rules as R7

pytestmark = pytest.mark.pg
UTC = timezone.utc
NOW = datetime(2026, 11, 30, 12, 0, tzinfo=UTC)
DAYS = weekdays(date(2026, 11, 6), 17)                 # s0 = 11-06 ... s11 = 11-23 (first decision day) ... s14 = 11-26 ... s16 = 11-30
D0 = DAYS[11]


def pick_codes() -> tuple[list[str], list[str]]:
    sw = FI.load_frozen().sw.filter(pl.col("code").str.slice(0, 3).is_in(FI.MAIN_PREFIX_LIST))
    g = sw.group_by("sw2").agg(pl.col("code").sort(), pl.len().alias("n")).filter(pl.col("n") >= 3).sort("n")
    x = g["code"][0][:3]
    others = [c for c in sw.sort("code")["code"].to_list() if c not in x and c not in set(g["code"][0])][:22]
    return list(x), others


def bars_move(n_before: int, base: str, moves: list[tuple[str, str]], vol=1000):
    """n_before flat days at ``base`` then the given (open, close) days; the rest of the days flat at the last close."""
    out = flat(base, n_before, vol)
    last = base
    for o, c in moves:
        out.append((o, c, vol))
        last = c
    return out + flat(last, len(DAYS) - len(out), vol)


def series() -> tuple[dict, list[str], list[str]]:
    (lead, follower, core), others = pick_codes()
    s = {}
    # leader: three limit-up closes on s8, s9, s10 (10.00 -> 11.00 -> 12.10 -> 13.31), breaks on s11 (13.00)
    s[lead] = bars_move(8, "10.00", [("10.00", "11.00"), ("11.00", "12.10"), ("12.10", "13.31"), ("13.31", "13.00")], vol=2000)
    # follower: first board on D (10.00 -> 11.00), buys at the s12 open 11.20, sells at the s14 close 10.50
    s[follower] = bars_move(11, "10.00", [("10.00", "11.00"), ("11.20", "11.00"), ("11.00", "10.80"), ("10.80", "10.50")])
    # core: biggest turnover, flat, D+1 open 20.00, D+3 close 19.90
    s[core] = bars_move(11, "20.00", [("20.00", "20.00"), ("20.00", "20.00"), ("20.00", "19.95"), ("19.95", "19.90")], vol=900000)
    for i, c in enumerate(others):
        base = f"{10 + i}.00"
        s[c] = flat(base, 12) + [(base, base, 1000)] * (len(DAYS) - 12)
    return s, [lead, follower, core], others


@pytest.fixture
def world(conn, lake, monkeypatch):
    s, (lead, follower, core), others = series()
    svc = Batch7Service(conn, lake, clock=lambda: NOW, loader=lambda which, since: window(s, DAYS) if which == IN.NATIVE else None)
    codes = list(s)
    iid_of = {c: instrument_id_of(c) for c in codes}
    code_of = {v: k for k, v in iid_of.items()}
    caps = {c: (100.0 if c == core else 50.0 if c == lead else 10.0 if c == follower else 20.0 + i) for i, c in enumerate(codes)}
    frame = pl.DataFrame({"instrument_id": [iid_of[c] for c in codes], "as_of": ["2026-11-10"] * len(codes), "available_at": [datetime(2026, 11, 11, tzinfo=UTC)] * len(codes),
                          "float_cap_cny": [D(str(caps[c] * 1e8)) for c in codes], "total_cap_cny": [D(str(caps[c] * 2e8)) for c in codes]},
                         schema={"instrument_id": pl.String, "as_of": pl.String, "available_at": pl.Datetime("us", "UTC"), "float_cap_cny": pl.Decimal(38, 4), "total_cap_cny": pl.Decimal(38, 4)})
    B.publish_derived(svc.svc, B.CAP_DATASET, frame, {"dataset": B.CAP_DATASET, "as_of": "2026-11-10"}, contract_id="pulse.analysis_artifact", watermark=NOW)
    B.publish_derived(svc.svc, "bars_raw_1d", pl.DataFrame({"x": [1]}), {"dataset": "bars_raw_1d", "session_date": "2026-11-30"}, contract_id="pulse.analysis_artifact", watermark=NOW)
    from pulse.application.bf_batch6_inputs import Scope

    monkeypatch.setattr(IN, "load_scope", lambda conn_, as_of: (code_of, iid_of, Scope({}, frozenset(), {}), {c: date(2010, 1, 1) for c in codes}))
    # institution net buying on D for the follower-listed names: I (the follower code is reused as the LHB stock "i1"), seats on the D day only
    listed = others[:10]
    inst, ctrl = listed[0], listed[1]
    seats = pl.DataFrame({"code": [inst, inst, ctrl], "date": [D0] * 3, "side": ["buy", "sell", "buy"], "seat": ["机构专用", "机构专用", "某证券营业部"], "amount": [9.0e8, 1.0e8, 5.0e7]},
                         schema={"code": pl.String, "date": pl.Date, "side": pl.String, "seat": pl.String, "amount": pl.Float64})
    monkeypatch.setattr(IN, "load_seats", lambda conn_, code_of_, since: (seats, {(c, D0) for c in listed}))
    monkeypatch.setattr(FW, "seat_status", lambda conn_, day: (25, None))
    return svc, s, (lead, follower, core), others, listed


def rows_of(svc, kind=None, exp=None):
    rows = FW.stored_rows(svc.conn, svc.lake)
    return [r for r in rows if (kind is None or r["kind"] == kind) and (exp is None or r["experiment"] == exp)]


def test_before_the_first_decision_day_nothing_is_evaluated(world):
    svc, *_ = world
    out = svc.forward(date(2026, 11, 20), as_of=NOW)
    assert not out["appended"] and "before the first decision day" in out["reason"] and rows_of(svc) == []


def test_e097_decides_the_break_fills_buys_and_resolves_the_d3_outcome(world):
    svc, s, (lead, follower, core), others, listed = world
    out = svc.forward(date(2026, 11, 30), as_of=NOW)
    assert out["appended"]
    day = {r["session"]: json.loads(r["body"]) for r in rows_of(svc, "day", "E097")}
    assert set(day) == {d.isoformat() for d in DAYS if d >= R7.FORWARD_START} and all(b["status"] == "decided" and b["source"] == "native" for b in day.values())
    ev = rows_of(svc, "event", "E097")
    assert len(ev) == 1 and ev[0]["session"] == "2026-11-23" and json.loads(ev[0]["body"])["leader"] == lead and json.loads(ev[0]["body"])["leader_streak_prev"] == 3
    sig = {r["code"]: json.loads(r["body"]) for r in rows_of(svc, "signal", "E097")}
    assert sig[core]["role"] == "core" and sig[follower]["role"] == "follower" and set(sig) == {core, follower}
    outc = {r["code"]: json.loads(r["body"]) for r in rows_of(svc, "outcome", "E097")}
    f = outc[follower]
    assert f["status"] == "filled" and f["entry_c"] == 1120 and f["exit_c"] == 1050            # D+1 open 11.20 (limit 12.10, not at it), D+3 close 10.50
    assert f["ret_gross"] == pytest.approx(10.50 / 11.20 - 1) and f["ret_net"] == pytest.approx(10.50 / 11.20 - 1 - 0.00252) and f["ex_sv"] < 0
    assert outc[core]["status"] == "filled" and outc[core]["ret_gross"] == pytest.approx(19.90 / 20.00 - 1)
    dout = {r["session"]: json.loads(r["body"]) for r in rows_of(svc, "day_outcome", "E097")}
    assert "2026-11-23" in dout and dout["2026-11-23"]["n_signals"] == 2 and dout["2026-11-23"]["n_filled"] == 2 and dout["2026-11-23"]["source"] == "native"
    assert dout["2026-11-30"]["n_signals"] == 0                                              # a day without a break is a (signal-free) slot at once


def test_e098_event_control_and_d5_outcome(world):
    svc, s, (lead, follower, core), others, listed = world
    svc.forward(date(2026, 11, 30), as_of=NOW)
    sig = {r["code"]: json.loads(r["body"]) for r in rows_of(svc, "signal", "E098")}
    inst = listed[0]
    assert set(sig) == {inst} and sig[inst]["net"] == pytest.approx(8.0e8) and sig[inst]["share"] > 0.05
    ctrl = {r["code"] for r in rows_of(svc, "control", "E098")}
    assert ctrl <= set(listed[1:]) and inst not in ctrl
    dout = {r["session"]: json.loads(r["body"]) for r in rows_of(svc, "day_outcome", "E098")}
    assert "2026-11-23" in dout and dout["2026-11-23"]["n_signals"] == 1                      # D+5 close is 11-30: observable
    assert dout["2026-11-24"]["n_signals"] == 0


def test_an_outcome_waits_for_its_exit_and_is_appended_later(world, conn, lake):
    svc, s, *_ = world
    short = Batch7Service(conn, lake, clock=lambda: NOW, loader=lambda which, since: window({c: b[:15] for c, b in s.items()}, DAYS[:15]) if which == IN.NATIVE else None)
    short.forward(date(2026, 11, 26), as_of=NOW)                                             # data to 11-26 (s14): E097 D=11-23 exits at s14, E098 D=11-23 needs s16
    assert "2026-11-23" in {r["session"] for r in rows_of(svc, "day_outcome", "E097")}
    assert "2026-11-23" not in {r["session"] for r in rows_of(svc, "day_outcome", "E098")}
    assert [r for r in rows_of(svc, "signal", "E098") if r["session"] == "2026-11-23"] and not rows_of(svc, "outcome", "E098")
    out = svc.forward(date(2026, 11, 30), as_of=NOW)                                         # the full data arrives: only the missing rows are appended
    assert out["appended"] and "2026-11-23" in {r["session"] for r in rows_of(svc, "day_outcome", "E098")}
    keys = [(r["experiment"], r["session"], r["kind"], r["code"]) for r in FW.stored_rows(conn, lake)]
    assert len(keys) == len(set(keys))


def test_append_only_and_second_run_is_a_noop(world):
    svc, *_ = world
    svc.forward(date(2026, 11, 30), as_of=NOW)
    n = len(FW.stored_rows(svc.conn, svc.lake))
    again = svc.forward(date(2026, 11, 30), as_of=NOW)
    assert not again["appended"] and len(FW.stored_rows(svc.conn, svc.lake)) == n
    keys = [(r["experiment"], r["session"], r["kind"], r["code"]) for r in FW.stored_rows(svc.conn, svc.lake)]
    assert len(keys) == len(set(keys))                                                       # a row exists once


def test_a_day_without_data_is_recorded_only_after_two_sessions_never_guessed(world):
    svc, *_ = world
    svc.forward(date(2026, 11, 30), as_of=NOW)
    out = svc.forward(date(2026, 12, 2), as_of=NOW)                                         # 12-01 is 1 open day behind: wait; 11-30 has data
    assert not [r for r in rows_of(svc, "day") if r["session"] == "2026-12-01"]
    out = svc.forward(date(2026, 12, 3), as_of=NOW)                                         # 12-01 now has two later open days: recorded as no data
    nd = [json.loads(r["body"]) for r in rows_of(svc, "day", "E097") if r["session"] == "2026-12-01"]
    assert out["appended"] and nd and nd[0]["status"] == "no_data" and nd[0]["source"] is None
    assert not [r for r in rows_of(svc, "signal") if r["session"] == "2026-12-01"]


def test_registered_as_shadow_with_the_frozen_hash(world):
    svc, *_ = world
    svc.forward(date(2026, 11, 30), as_of=NOW)
    runs = svc.conn.execute("SELECT run_purpose, run_state, run_manifest FROM research.experiment_run WHERE run_manifest->>'lane'='batch7'").fetchall()
    assert len(runs) == 2 and {r["run_purpose"] for r in runs} == {"shadow"} and all(R7.SPEC_SHA256 in json.dumps(r["run_manifest"]) for r in runs)
    svc.forward(date(2026, 11, 30), as_of=NOW)
    assert svc.conn.execute("SELECT count(*) n FROM research.experiment_run WHERE run_manifest->>'lane'='batch7'").fetchone()["n"] == 2


def test_summary_progress_and_judge_writes_once_only_after_the_condition(world):
    svc, *_ = world
    svc.forward(date(2026, 11, 30), as_of=NOW)
    ov = svc.judge(today=date(2026, 12, 1), as_of=NOW)
    e97 = ov["experiments"]["E097"]
    assert e97["judgement"]["state"] == "progress" and e97["filled"] >= 2 and e97["days_resolved"] >= 1 and ov["judgement_rows_written"] == 0
    assert not rows_of(svc, "judgement")
    late = svc.judge(today=date(2027, 7, 1), as_of=NOW)                                     # after the deadline the sample at hand is judged and flagged insufficient
    assert late["judgement_rows_written"] == 2 and late["experiments"]["E097"]["judgement"]["sufficient"] is False
    assert len(rows_of(svc, "judgement")) == 2
    again = svc.judge(today=date(2027, 7, 2), as_of=NOW)
    assert again["judgement_rows_written"] == 0 and len(rows_of(svc, "judgement")) == 2
    assert "E097" in RD.describe(again)
