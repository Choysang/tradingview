"""The batch 8 forward step on real PostgreSQL with synthetic raw bars and seat tables: flags, groups, both legs, matched baselines, append-only rows, the overlay, the judge, the registry."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal as D
from hashlib import sha256

import polars as pl
import pytest

from bf_batch7.support import flat, weekdays, window
from pulse.application import bf_batch7_inputs as IN7
from pulse.application import bf_batch8_core as CORE
from pulse.application import bf_batch8_forward as FW
from pulse.application import bf_batch8_reads as RD
from pulse.application import bindings as B
from pulse.application.bf_batch6_inputs import Scope
from pulse.application.bf_batch8_service import Batch8Service
from pulse.application.refdata_service import instrument_id_of
from pulse.domain.batch8 import rules as R

pytestmark = pytest.mark.pg
UTC = timezone.utc
NOW = datetime(2026, 12, 10, 12, 0, tzinfo=UTC)
DAYS = weekdays(date(2026, 11, 2), 30)              # s16 = 2026-11-24 (first decision day D), s17 = D+1, s21 = D+5 (2026-12-01), s29 = 2026-12-11
D0, D1 = DAYS[16], DAYS[17]
A, B_, C, E, N = "600101", "600102", "600103", "600104", "600105"
SEAT_COLS = {"code": pl.String, "date": pl.Date, "side": pl.String, "seat": pl.String, "amount": pl.Float64}


def mv(n_before: int, base: str, moves: list[tuple[str, str]], vol=1000):
    out = flat(base, n_before, vol)
    last = base
    for o, c in moves:
        out.append((o, c, vol))
        last = c
    return out + flat(last, len(DAYS) - len(out), vol)


def series() -> dict:
    s = {
        # first board, three Lhasa buyers (L3): D+1 open 11.20 (limit 12.10: buyable), D+5 close 10.50
        A: mv(16, "10.00", [("10.00", "11.00"), ("11.20", "11.00"), ("11.00", "11.00"), ("11.00", "11.00"), ("11.00", "11.00"), ("11.00", "10.50")]),
        # first board on the list with an ordinary seat and the institution: no flag
        B_: mv(16, "10.00", [("10.00", "11.00"), ("11.10", "11.10")]),
        # consecutive board (limit-up on D-1 and D), a one-day seat among the buyers (T1): D+1 open 12.00
        C: mv(15, "10.00", [("10.00", "11.00"), ("11.00", "12.10"), ("12.00", "12.00")]),
        # first board, one Lhasa buyer (L1), D+1 opens AT the limit-up price 12.10: not buyable, still held (H8a), D+5 close 11.50
        E: mv(16, "10.00", [("10.00", "11.00"), ("12.10", "12.10"), ("12.10", "12.10"), ("12.10", "12.10"), ("12.10", "12.10"), ("12.10", "11.50")]),
        # first board, not on the list at all
        N: mv(16, "10.00", [("10.00", "11.00"), ("11.05", "11.05")]),
    }
    for i in range(24):
        s[f"6002{i:02d}"] = flat(f"{10 + i}.00", len(DAYS))
    return s


def seat_tables() -> tuple[pl.DataFrame, set]:
    rows = [(A, D0, "buy", "拉萨东环路营业部", 5e7), (A, D0, "buy", "拉萨团结路营业部", 4e7), (A, D0, "buy", "拉萨金融城营业部", 3e7), (A, D0, "buy", "拉萨东环路营业部", 5e7),      # a repeat row
            (E, D0, "buy", "拉萨东环路营业部", 2e7), (B_, D0, "buy", "某证券杭州营业部", 2e7), (B_, D0, "buy", "机构专用", 3e7), (C, D0, "buy", "游资甲", 6e7)]
    hist = [date(2026, 10, d) for d in (12, 13, 14, 15, 16)]                       # a seat that sold the same stock the next open day, five times (settled by 10-21)
    for i, d in enumerate(hist):
        code = f"6002{i:02d}"
        rows.append((code, d, "buy", "游资甲", 1e7))
        rows.append((code, weekdays(d, 2)[1], "sell", "游资甲", 1e7))
    df = pl.DataFrame(rows, schema=SEAT_COLS, orient="row")
    return df, {(c, d) for c, d, *_ in rows}


def paper_frame(rows: list[tuple[str, str, dict]]) -> pl.DataFrame:
    texts = [json.dumps(b, sort_keys=True, separators=(",", ":")) for _, _, b in rows]
    return pl.DataFrame({"session_date": [r[0] for r in rows], "row_key": [r[1] for r in rows], "decided_at": ["2026-11-25T00:00:00Z"] * len(rows), "body": texts,
                         "body_sha256": [sha256(t.encode()).hexdigest() for t in texts]})


def publish_paper(svc, dataset: str, rows) -> None:
    sessions = sorted({r[0] for r in rows})
    B.publish_derived(svc.svc, dataset, paper_frame(rows), {"dataset": dataset, "session_dates": sessions}, contract_id="pulse.analysis_artifact", watermark=NOW)


def buy(code, rule, scenario, status, qty, px, t="2026-11-24T06:57:00Z"):
    opp = f"{D0}-{rule}-{code}-{scenario}"
    return (D0.isoformat(), opp, {"opportunity_id": opp, "intent_id": f"{D0}-{rule}-{code}", "code": code, "rule": rule, "scenario": scenario, "status": status, "filled_quantity": qty, "avg_fill_price": px, "decision_time": t})


def sell(code, rule, px):
    return (D1.isoformat(), f"{code}|{rule}", {"code": code, "rule": rule, "status": "FILLED", "price": px, "return_pct": "0"})


@pytest.fixture
def world(conn, lake, monkeypatch):
    s = series()
    seats, lhb = seat_tables()
    svc = Batch8Service(conn, lake, clock=lambda: NOW, loader=lambda which, since: window(s, DAYS) if which == IN7.NATIVE else None,
                        flags_loader=lambda c, t: CORE.SeatFlags.build(seats, lhb, t))
    codes = list(s)
    iid_of = {c: instrument_id_of(c) for c in codes}
    code_of = {v: k for k, v in iid_of.items()}
    caps = {c: 10.0 + 3 * i for i, c in enumerate(codes)}
    frame = pl.DataFrame({"instrument_id": [iid_of[c] for c in codes], "as_of": ["2026-11-10"] * len(codes), "available_at": [datetime(2026, 11, 11, tzinfo=UTC)] * len(codes),
                          "float_cap_cny": [D(str(caps[c] * 1e8)) for c in codes], "total_cap_cny": [D(str(caps[c] * 2e8)) for c in codes]},
                         schema={"instrument_id": pl.String, "as_of": pl.String, "available_at": pl.Datetime("us", "UTC"), "float_cap_cny": pl.Decimal(38, 4), "total_cap_cny": pl.Decimal(38, 4)})
    B.publish_derived(svc.svc, B.CAP_DATASET, frame, {"dataset": B.CAP_DATASET, "as_of": "2026-11-10"}, contract_id="pulse.analysis_artifact", watermark=NOW)
    B.publish_derived(svc.svc, "bars_raw_1d", pl.DataFrame({"x": [1]}), {"dataset": "bars_raw_1d", "session_date": "2026-12-10"}, contract_id="pulse.analysis_artifact", watermark=NOW)
    monkeypatch.setattr(IN7, "load_scope", lambda conn_, as_of: (code_of, iid_of, Scope({}, frozenset(), {}), {c: date(2010, 1, 1) for c in codes}))
    monkeypatch.setattr(CORE, "seat_status", lambda conn_, day, as_of: (25, datetime(2026, 11, 24, 12, 0, tzinfo=UTC), 0))
    return svc


def rows_of(svc, kind=None):
    return [r for r in FW.stored_rows(svc.conn, svc.lake) if kind is None or r["kind"] == kind]


def by_code(svc, kind, session=D0):
    return {r["code"]: json.loads(r["body"]) for r in rows_of(svc, kind) if r["session"] == session.isoformat()}


def test_before_the_first_decision_day_nothing_is_evaluated(world):
    out = world.forward(date(2026, 11, 23), as_of=NOW)
    assert not out["appended"] and "before the first decision day" in out["reason"] and rows_of(world) == []


def test_the_decision_day_records_the_sample_with_groups_and_flags(world):
    out = world.forward(date(2026, 12, 10), as_of=NOW)
    assert out["appended"]
    day = {r["session"]: json.loads(r["body"]) for r in rows_of(world, "day")}
    assert min(day) == "2026-11-24" and "2026-11-23" not in day and all(b["status"] == "decided" and b["source"] == "native" for b in day.values())
    sig = by_code(world, "signal")
    assert set(sig) == {A, B_, C, E, N}
    assert (sig[A]["group"], sig[A]["lhasa"], sig[A]["l1"], sig[A]["l3"], sig[A]["t1"], sig[A]["flag"]) == ("first", 3, True, True, False, True)         # the repeated row counts once
    assert (sig[E]["lhasa"], sig[E]["l1"], sig[E]["l3"], sig[E]["flag"]) == (1, True, False, True)
    assert (sig[C]["group"], sig[C]["t1"], sig[C]["l1"], sig[C]["flag"], sig[C]["trip_seats"]) == ("consecutive", True, False, True, 1)
    assert (sig[B_]["flag"], sig[B_]["listed"], sig[N]["flag"], sig[N]["listed"]) == (False, True, False, False)
    assert day[D0.isoformat()]["n_signals"] == 5 and day[D0.isoformat()]["n_flag"] == 3 and day[D0.isoformat()]["n_l3"] == 1
    nxt = by_code(world, "signal", D1)
    assert set(nxt) == {E} and nxt[E]["group"] == "consecutive" and nxt[E]["streak"] == 2       # E closed at the limit again on D+1: a consecutive board of the next decision day


def test_both_legs_the_entry_rule_and_the_matched_excess(world):
    world.forward(date(2026, 12, 10), as_of=NOW)
    o = by_code(world, "outcome")
    a, e, c = o[A], o[E], o[C]
    assert a["gated"]["status"] == "filled" and a["gated"]["entry_c"] == 1120 and a["gated"]["exit_c"] == 1050 and a["held"]["entry_c"] == 1120
    assert a["held"]["ret_gross"] == pytest.approx(10.50 / 11.20 - 1) and a["held"]["ex_sv"] == pytest.approx(a["held"]["ret_gross"] - a["held"]["base_sv"]) and a["held"]["ex_sv"] < 0
    assert e["gated"]["status"] == "blocked_buy" and e["gated"].get("ex_sv") is None                 # D+1 open at the limit-up price: not bought
    assert e["held"]["status"] == "filled" and e["held"]["entry_c"] == 1210 and e["held"]["exit_c"] == 1150 and e["held"]["ex_sv"] is not None      # still a holder
    assert c["gated"]["status"] == "filled" and c["held"]["ret_gross"] == pytest.approx(12.00 / 12.00 - 1)
    assert all(o[x]["flag"] is False for x in (B_, N)) and o[B_]["gated"]["status"] == "filled"
    dout = {r["session"]: json.loads(r["body"]) for r in rows_of(world, "day_outcome")}
    assert dout[D0.isoformat()]["n_signals"] == 5 and dout[D0.isoformat()]["n_flag"] == 3 and dout[D0.isoformat()]["flag_matched"] == 3 and dout[D0.isoformat()]["gated_filled"] == 4
    assert dout["2026-11-26"]["n_signals"] == 0                                                       # a day without a limit-up close is a slot at once


def test_append_only_and_a_second_run_is_a_noop(world):
    world.forward(date(2026, 12, 10), as_of=NOW)
    n = len(rows_of(world))
    again = world.forward(date(2026, 12, 10), as_of=NOW)
    assert not again["appended"] and len(rows_of(world)) == n
    keys = [FW.key_of(r) for r in rows_of(world)]
    assert len(keys) == len(set(keys))


def test_an_outcome_waits_for_its_exit_and_is_appended_later(world, conn, lake, monkeypatch):
    s = series()
    seats, lhb = seat_tables()
    short = Batch8Service(conn, lake, clock=lambda: NOW, loader=lambda which, since: window({c: b[:20] for c, b in s.items()}, DAYS[:20]) if which == IN7.NATIVE else None,
                          flags_loader=lambda c, t: CORE.SeatFlags.build(seats, lhb, t))
    short.forward(date(2026, 11, 30), as_of=NOW)                         # bars end on s19 (11-27): D+5 = s21 is not there yet
    assert by_code(world, "signal") and not by_code(world, "outcome") and D0.isoformat() not in {r["session"] for r in rows_of(world, "day_outcome")}
    world.forward(date(2026, 12, 10), as_of=NOW)
    assert A in by_code(world, "outcome") and D0.isoformat() in {r["session"] for r in rows_of(world, "day_outcome")}
    keys = [FW.key_of(r) for r in rows_of(world)]
    assert len(keys) == len(set(keys))


def test_a_day_without_seats_is_recorded_only_after_two_sessions(world, monkeypatch):
    monkeypatch.setattr(CORE, "seat_status", lambda conn_, day, as_of: (3, None, 0))
    world.forward(date(2026, 11, 25), as_of=NOW)                         # 11-24 is one open day behind: wait, nothing recorded
    assert not [r for r in rows_of(world, "day") if r["session"] == "2026-11-24"]
    out = world.forward(date(2026, 11, 26), as_of=NOW)                   # two later open days: recorded as no_seats, no signal rows
    st = {r["session"]: json.loads(r["body"])["status"] for r in rows_of(world, "day")}
    assert out["appended"] and st["2026-11-24"] == "no_seats" and not rows_of(world, "signal")


def test_overlay_records_flags_and_the_counterfactual_next_to_the_actual_exits(world):
    publish_paper(world, FW.PAPER_DAYS, [(D0.isoformat(), D0.isoformat(), {}), (D1.isoformat(), D1.isoformat(), {})])
    publish_paper(world, FW.PAPER_BUYS, [buy(A, "B1", "BASE", "FILLED", 900, "11.00"), buy(A, "B1", "PESSIMISTIC", "UNFILLED", 0, None), buy(B_, "B3", "BASE", "FILLED", 900, "11.00"),
                                         buy(C, "B1", "BASE", "FILLED", 800, "12.10"), buy(N, "B1", "BASE", "PARTIALLY_FILLED", 300, "11.00")])
    publish_paper(world, FW.PAPER_SELLS, [sell(A, "S0_open", "11.20"), sell(A, "S1_decay", "11.30"), sell(A, "POLICY_v2", "11.10"), sell(B_, "S0_open", "11.10")])
    world.forward(date(2026, 12, 10), as_of=NOW)
    ovd = {r["session"]: json.loads(r["body"]) for r in rows_of(world, "overlay_day")}
    assert ovd[D0.isoformat()]["fills"] == 4 and ovd[D0.isoformat()]["flags_known"] and ovd[D0.isoformat()]["paper_day"] and ovd[D0.isoformat()]["paper_next_day"]
    ov = {r["code"]: json.loads(r["body"]) for r in rows_of(world, "overlay") if r["session"] == D0.isoformat()}
    assert set(ov) == {f"{D0}-B1-{A}-BASE", f"{D0}-B3-{B_}-BASE", f"{D0}-B1-{C}-BASE", f"{D0}-B1-{N}-BASE"}                 # the unfilled scenario is not a fill
    a = ov[f"{D0}-B1-{A}-BASE"]
    assert a["flags"]["l3"] and a["flags"]["flag"] and a["counterfactual_d1_open"]["source"] == "paper_S0_open"
    assert a["counterfactual_d1_open"]["ret_gross"] == pytest.approx(11.20 / 11.00 - 1) and a["counterfactual_d1_open"]["ret_net"] == pytest.approx(11.20 / 11.00 - 1 - 0.00252)
    assert set(a["actual_exits"]) == {"S0_open", "S1_decay", "POLICY_v2"} and a["actual_exits"]["S1_decay"]["ret_gross"] == pytest.approx(11.30 / 11.00 - 1) and a["actual_status"] == "recorded"
    c = ov[f"{D0}-B1-{C}-BASE"]
    assert c["flags"]["t1"] and c["counterfactual_d1_open"]["source"] == "daily_bar_open" and c["counterfactual_d1_open"]["ret_gross"] == pytest.approx(12.00 / 12.10 - 1)
    assert c["actual_exits"] == {} and c["actual_status"] == "not_in_book"
    b, n = ov[f"{D0}-B3-{B_}-BASE"], ov[f"{D0}-B1-{N}-BASE"]
    assert b["flags"]["flag"] is False and b["flags"]["listed"] and "counterfactual_d1_open" not in b and n["flags"]["listed"] is False and n["status"] == "PARTIALLY_FILLED"
    n_rows = len(rows_of(world))
    assert not world.forward(date(2026, 12, 10), as_of=NOW)["appended"] and len(rows_of(world)) == n_rows                  # append-only: nothing rewritten


def test_overlay_without_seat_reports_has_unknown_flags_and_no_counterfactual(world, monkeypatch):
    monkeypatch.setattr(CORE, "seat_status", lambda conn_, day, as_of: (3, None, 0))
    publish_paper(world, FW.PAPER_DAYS, [(D0.isoformat(), D0.isoformat(), {}), (D1.isoformat(), D1.isoformat(), {})])
    publish_paper(world, FW.PAPER_BUYS, [buy(A, "B1", "BASE", "FILLED", 900, "11.00")])
    world.forward(date(2026, 12, 10), as_of=NOW)
    ovd = {r["session"]: json.loads(r["body"]) for r in rows_of(world, "overlay_day")}
    assert ovd[D0.isoformat()]["flags_known"] is False
    ov = [json.loads(r["body"]) for r in rows_of(world, "overlay") if r["session"] == D0.isoformat()]
    assert len(ov) == 1 and ov[0]["flags"] is None and "counterfactual_d1_open" not in ov[0]


def test_registered_as_shadow_with_the_frozen_hash_once(world):
    world.forward(date(2026, 12, 10), as_of=NOW)
    runs = world.conn.execute("SELECT run_purpose, run_state, run_manifest FROM research.experiment_run WHERE run_manifest->>'lane'='batch8'").fetchall()
    assert len(runs) == 1 and runs[0]["run_purpose"] == "shadow" and runs[0]["run_state"] == "running" and R.SPEC_SHA256 in json.dumps(runs[0]["run_manifest"])
    world.forward(date(2026, 12, 10), as_of=NOW)
    assert world.conn.execute("SELECT count(*) n FROM research.experiment_run WHERE run_manifest->>'lane'='batch8'").fetchone()["n"] == 1


def test_status_and_judge_refuse_then_write_once_when_the_sample_condition_is_met(world, monkeypatch):
    world.forward(date(2026, 12, 10), as_of=NOW)
    ov = world.judge(today=date(2026, 12, 10), as_of=NOW)
    assert ov["cut"]["state"] == "refused" and ov["rows_written"] == [] and not rows_of(world, "judgement")
    assert ov["resolved_sessions"] >= 5 and ov["sample"]["effects"]["H8a"][0] is not None and ov["sample"]["report"]["whole"]["H8b"]["n_eligible"] >= 1
    assert "refused" in RD.describe(ov)
    monkeypatch.setattr(R, "MIN_SESSIONS", 3)
    monkeypatch.setattr(R, "MIN_FLAG", 3)
    monkeypatch.setattr(R, "OVERLAY_SUMMARY_SESSIONS", 2)
    ov = world.judge(today=date(2026, 12, 10), as_of=NOW)
    assert ov["cut"]["state"] == "judge" and ov["cut"]["sessions"] == 3 and ov["cut"]["flag_n"] == 3 and ov["rows_written"] == ["judgement", "overlay_summary"]
    j = json.loads(rows_of(world, "judgement")[0]["body"])
    assert j["state"] == "judged" and set(j["verdicts"]) == {"H8a", "H8b"} and j["sample_sessions"] == 3 and j["verdicts"]["H8a"]["p_holm"] is not None
    again = world.judge(today=date(2026, 12, 11), as_of=NOW)
    assert again["rows_written"] == [] and len(rows_of(world, "judgement")) == 1 and len(rows_of(world, "overlay_summary")) == 1


def test_history_is_report_only_uses_the_same_statistics_and_publishes_one_report(world, monkeypatch):
    from pulse.application import bf_batch8_history as HI

    s = series()
    monkeypatch.setattr(IN7, "pick_window", lambda conn_, lake_, as_of, since, code_of, need_day=None, prefer="auto": window(s, DAYS))
    seats, lhb = seat_tables()
    out = HI.run(world.conn, world.lake, as_of=NOW, since=date(2026, 11, 17), until=date(2026, 11, 27), flags=CORE.SeatFlags.build(seats, lhb, date(2026, 11, 27)))
    assert out["status"] == "ok" and out["decides"] is False and out["source"] == "native" and out["window_used"][2] == 9 and out["window_complete"]
    assert out["sessions_resolved"] == 9 and out["flag_names"] == 3 and out["flag_matched"] == 3          # A, C, E on 11-24 (E on 11-25 has no report that day)
    whole = out["report"]["whole"]
    assert whole["H8a"]["effect"] is not None and out["direction_vs_preregistered"]["H8a"] is not None and set(out["report"]["by_group"]) == {"first", "consecutive"}
    assert "p_holm" not in json.dumps(out, default=str)                                          # no Holm, no verdict in the history
    again = world.history(publish=True, as_of=NOW, since=date(2026, 11, 17), until=date(2026, 11, 27))        # real seat loader on the empty test database: no flags, still a report
    assert again["status"] == "ok" and again["flag_names"] == 0 and again["snapshot_id"]
    assert world.conn.execute("SELECT count(*) n FROM market.dataset_manifest WHERE dataset_id='batch8_history'").fetchone()["n"] == 1


def test_two_intents_of_the_same_rule_code_and_scenario_do_not_block_the_overlay(world):
    publish_paper(world, FW.PAPER_DAYS, [(D0.isoformat(), D0.isoformat(), {}), (D1.isoformat(), D1.isoformat(), {})])
    first = buy(A, "B3", "BASE", "FILLED", 900, "11.00", t="2026-11-24T02:10:00Z")
    second = buy(A, "B3", "BASE", "FILLED", 900, "11.00", t="2026-11-24T05:10:00Z")
    second[2].update({"opportunity_id": f"{first[1]}-2", "intent_id": f"{D0}-B3-{A}-2"})
    publish_paper(world, FW.PAPER_BUYS, [first, (D0.isoformat(), f"{first[1]}-2", second[2])])
    out = world.forward(date(2026, 12, 10), as_of=NOW)                                       # publish would raise on a repeated key and stop every later day
    keys = {r["code"] for r in rows_of(world, "overlay") if r["session"] == D0.isoformat()}
    assert out["appended"] and keys == {first[1], f"{first[1]}-2"}
    assert rows_of(world, "day_outcome") and len({FW.key_of(r) for r in rows_of(world)}) == len(rows_of(world))


def test_the_forward_step_refuses_to_run_without_the_packaged_frozen_spec(world, monkeypatch, tmp_path):
    from pulse.application import frozen_specs as FS

    monkeypatch.setattr(FS, "SPECS_DIR", tmp_path)
    with pytest.raises(FS.FrozenSpecError):
        world.forward(date(2026, 12, 10), as_of=NOW)
    assert rows_of(world) == []


def put_report(conn, iid, start, end, rows, *, report, revision, available, source="s1"):
    import uuid

    rid = uuid.uuid4()
    conn.execute(
        "INSERT INTO intelligence.seat_report(report_revision_id, source_id, raw_object_id, source_report_id, revision, exchange_code, instrument_id, window_start, window_end,"
        " disclosure_reason, rule_version, trade_scope, published_at, available_at, recorded_at, quality_status)"
        " VALUES (%s,%s,%s,%s,%s,'SSE',%s,%s,%s,'r','v1','unknown',NULL,%s,%s,'valid')", (rid, source, uuid.uuid4(), report, revision, iid, start, end, available, available))
    for k, (side, name, amt) in enumerate(rows):
        conn.execute("INSERT INTO intelligence.seat_report_row(report_revision_id, side, rank, raw_seat_name, raw_amount, raw_unit, canonical_amount_cny, resolution_status, resolution_evidence)"
                     " VALUES (%s,%s,%s,%s,%s,'CNY',%s,'report_local_label','{}')", (rid, side, k + 1, name, amt, amt))


def test_seat_reports_are_read_point_in_time_one_revision_per_report(conn):
    import uuid

    conn.execute("SET session_replication_role = replica")                        # the seat tables reference raw objects and sources this test does not need
    x, y, z = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    code_of = {x: "600301", y: "600302", z: "600303"}

    def t(d, h):
        return datetime(2026, 11, d, h, 0, tzinfo=UTC)

    put_report(conn, x, D0, D0, [("buy", "拉萨甲", 1e7)], report="x1", revision=1, available=t(24, 12))
    put_report(conn, x, D0, D0, [("buy", "拉萨乙", 2e7)], report="x1", revision=2, available=t(24, 13))
    put_report(conn, x, D0, D0, [("buy", "拉萨丙", 3e7)], report="x1", revision=3, available=t(25, 3))     # 11:00 on D+1: after the 09:30 open, the cut is 01:30 UTC
    put_report(conn, y, D0, D0, [("buy", "拉萨甲", 1e7)], report="y1", revision=1, available=t(25, 3))     # arrives after the D+1 open: not usable at all
    put_report(conn, z, date(2026, 11, 10), date(2026, 11, 10), [("buy", "普通", 1e7)], report="z1", revision=1, available=t(26, 0))   # a bulk-imported earlier window
    put_report(conn, z, date(2026, 11, 9), date(2026, 11, 10), [("buy", "多日", 1e7)], report="zm", revision=1, available=t(26, 0))     # multi-day window: listed, never rows
    seats, lhb = CORE.load_seat_history(conn, code_of, datetime(2026, 12, 10, 12, 0, tzinfo=UTC))
    got = {(r["code"], r["date"], r["seat"]) for r in seats.iter_rows(named=True)}
    assert got == {("600301", D0, "拉萨乙"), ("600303", date(2026, 11, 10), "普通")}                  # the LATEST revision usable at the cut (rev 2), not rev 1 and not the late rev 3
    assert ("600301", D0) in lhb and ("600302", D0) not in lhb and ("600303", date(2026, 11, 10)) in lhb
    early, lhb_early = CORE.load_seat_history(conn, code_of, datetime(2026, 11, 24, 12, 30, tzinfo=UTC))       # run the same evening before rev 2 existed: rev 1, nothing of the later import
    assert {(r["code"], r["seat"]) for r in early.iter_rows(named=True)} == {("600301", "拉萨甲")} and ("600303", date(2026, 11, 10)) not in lhb_early
    n, at_, late = CORE.seat_status(conn, D0, datetime(2026, 12, 10, 12, 0, tzinfo=UTC))
    assert n == 1 and at_ == t(24, 13) and late == 2                                 # one instrument usable; rev 3 of x and the report of y arrived after the cut
