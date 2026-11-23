"""E099 pure checks: seat dedupe, L1 / L3 / T1 with the point-in-time boundary, the sample (first / consecutive), both legs, H8a / H8b arithmetic, sample cut, Holm, judge, append-only guard."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

from bf_batch7.support import weekdays, window
from pulse.application import bf_batch7_inputs as IN7
from pulse.application import bf_batch8_core as CORE
from pulse.application import bf_batch8_forward as FW
from pulse.application.bf_batch6_inputs import HistoryInputs
from pulse.domain.batch8 import rules as R
from pulse.domain.batch8 import seats as SE
from pulse.domain.batch8 import stats as ST

CAL = weekdays(date(2026, 9, 1), 80)                       # an open-day calendar without holidays: 09-01 .. 12-18
S = {"code": pl.String, "date": pl.Date, "side": pl.String, "seat": pl.String, "amount": pl.Float64}


def seats(rows):
    return pl.DataFrame(rows, schema=S, orient="row")


def at(i):
    return CAL[i]


# ---- frozen prefix ---------------------------------------------------------------------------------------------------------------------
def test_the_frozen_prefix_of_the_preregistration_matches_its_hash():
    path = Path(__file__).resolve().parents[3] / R.SPEC_PATH
    if not path.exists():
        pytest.skip("pre-registration not present in this checkout")
    raw = path.read_bytes()
    assert R.frozen_hash(raw) == R.SPEC_SHA256 and len(R.frozen_prefix(raw)) == 4741
    assert R.frozen_hash(raw + "\n追加的结果。\n".encode("utf-8")) == R.SPEC_SHA256
    assert R.frozen_hash(raw.replace("0.252%".encode("utf-8"), "0.300%".encode("utf-8"), 1)) != R.SPEC_SHA256


# ---- dedupe, L1 / L3 --------------------------------------------------------------------------------------------------------------------
def test_dedupe_collapses_a_row_repeated_under_another_listing_reason_but_not_a_different_amount():
    d = at(10)
    s = seats([("600001", d, "buy", "拉萨东环路", 5.0), ("600001", d, "buy", "拉萨东环路", 5.0), ("600001", d, "buy", "拉萨东环路", 6.0), ("600001", d, "sell", "拉萨东环路", 5.0),
               ("600001", d, "buy", "X", None), ("600001", d, "buy", "X", None)])
    assert SE.dedupe(s).height == 4


def test_lhasa_counts_distinct_buy_seats_and_a_null_amount_still_names_a_buyer():
    d = at(10)
    s = seats([("600001", d, "buy", "拉萨东环路营业部", 5.0), ("600001", d, "buy", "拉萨东环路营业部", 7.0),          # the same seat twice (two amounts): one seat
               ("600001", d, "buy", "拉萨团结路营业部", None), ("600001", d, "buy", "拉萨金融城营业部", 1.0), ("600001", d, "sell", "拉萨八一路营业部", 9.0),
               ("600002", d, "buy", "拉萨东环路营业部", 1.0), ("600002", d, "buy", "普通营业部", 3.0)])
    f = SE.flag_table(s, CAL)
    r = {x["code"]: x for x in f.iter_rows(named=True)}
    assert r["600001"]["lhasa"] == 3 and r["600001"]["l1"] and r["600001"]["l3"] and r["600001"]["flag"]            # the sell-side Lhasa seat is not counted
    assert r["600002"]["lhasa"] == 1 and r["600002"]["l1"] and not r["600002"]["l3"] and r["600002"]["flag"]


def test_a_stock_day_without_flags_is_not_flagged_and_a_stock_without_a_report_is_absent():
    d = at(10)
    f = SE.flag_table(seats([("600003", d, "buy", "普通营业部", 1.0)]), CAL)
    r = f.row(0, named=True)
    assert (r["lhasa"], r["l1"], r["l3"], r["t1"], r["flag"]) == (0, False, False, False, False)
    assert SE.flags_of_day(f, at(11)) == {}


# ---- T1: the point-in-time boundary ------------------------------------------------------------------------------------------------------
def history(trips: list[bool], seat="游资甲", first=0):
    """One seat buys a different stock on consecutive open days ``first``..; ``trips[i]`` says whether it sold that stock on the next open day."""
    rows = []
    for i, t in enumerate(trips):
        d, code = at(first + i), f"60{i:04d}"
        rows.append((code, d, "buy", seat, 10.0))
        if t:
            rows.append((code, at(first + i + 1), "sell", seat, 10.0))
    return rows


def t1_on(rows, day, code="600999", buyer="游资甲"):
    f = SE.flag_table(seats(rows + [(code, day, "buy", buyer, 1.0)]), CAL)
    return f.filter((pl.col("code") == code) & (pl.col("date") == day)).row(0, named=True)


def test_t1_needs_five_settled_buys_strictly_before_d_and_sixty_percent_next_day_sells():
    rows = history([True] * 5)                              # events on days 0..4, settled (third open day later) on days 3..7
    assert t1_on(rows, at(8))["t1"] is True                 # the last event settled on day 7: before day 8
    assert t1_on(rows, at(7))["t1"] is False                # day 7 itself: an event settling on D is not used (strictly before D); only 4 were settled
    assert t1_on(rows, at(7))["trip_seats"] == 0
    assert t1_on(history([True] * 4), at(30))["t1"] is False                      # four settled buys are not enough
    assert t1_on(history([True, True, True, False, False]), at(30))["t1"] is True      # 3 of 5 = 60 % exactly
    assert t1_on(history([True, True, False, False, False]), at(30))["t1"] is False    # 2 of 5 = 40 %
    assert t1_on(history([True, True, True, False, False, False, False, False]), at(30))["t1"] is False   # 3 of 8


def test_t1_ignores_the_institution_seat_other_seats_and_a_sell_on_another_stock():
    inst = history([True] * 5, seat="机构专用")
    assert t1_on(inst, at(30), buyer="机构专用")["t1"] is False
    other = history([True] * 5, seat="游资乙")
    assert t1_on(other, at(30), buyer="游资甲")["t1"] is False
    rows = [(f"60{i:04d}", at(i), "buy", "游资甲", 10.0) for i in range(5)] + [(f"61{i:04d}", at(i + 1), "sell", "游资甲", 10.0) for i in range(5)]   # sells are on OTHER stocks
    assert t1_on(rows, at(30))["t1"] is False


def test_t1_uses_only_reports_known_by_d_a_later_trip_cannot_leak_back():
    rows = history([False] * 5)                             # never sold the next day: no one-day seat on day 8
    leak = rows + [("600000", at(1), "sell", "游资甲", 10.0)]       # a next-day sell of event 0 that exists: now 1 of 5
    assert t1_on(rows, at(8))["t1"] is False and t1_on(leak, at(8))["t1"] is False
    late = history([True] * 5) + history([False] * 5, first=20)      # later behaviour (events on days 20..24) does not change day 8
    assert t1_on(late, at(8))["t1"] is True


# ---- sample: first / consecutive, legs -----------------------------------------------------------------------------------------------------
def test_flags_and_groups_of_the_limit_up_sample():
    sess = [at(i) for i in range(4)]
    schema = {"date": pl.Date, "code": pl.String, "in_scope": pl.Boolean, "lu": pl.Boolean, "flag_ok": pl.Boolean, "streak": pl.Int64}
    rows = [(sess[2], "A", True, True, True, 2), (sess[1], "A", True, True, True, 1),                      # A: limit-up yesterday too -> consecutive
            (sess[2], "B", True, True, True, 1), (sess[1], "B", True, False, True, 0),                     # B: first board
            (sess[2], "C", True, True, True, 1), (sess[1], "C", True, False, False, 0),                    # C: yesterday's flags unknown
            (sess[2], "D", True, True, True, 1),                                                            # D: no bar yesterday (suspended): first board
            (sess[2], "E", False, True, True, 1), (sess[2], "F", True, False, True, 0)]                   # E out of scope, F no limit-up
    inp = HistoryInputs(datetime(2026, 11, 20, tzinfo=timezone.utc), sess, pl.DataFrame(rows, schema=schema, orient="row"), seats([]), set(), pl.DataFrame(), {}, [])
    flag_rows = seats([("A", sess[2], "buy", "拉萨甲", 1.0), ("B", sess[2], "buy", "普通", 1.0)])
    sf = CORE.SeatFlags.build(flag_rows, {("A", sess[2]), ("B", sess[2])}, sess[3])
    got = {s["code"]: s for s in CORE.limit_up_samples(inp, sess[2], sf)}
    assert {k: v["group"] for k, v in got.items()} == {"A": "consecutive", "B": "first", "C": "unknown_prev", "D": "first"}
    assert got["A"]["flag"] and got["A"]["l1"] and not got["A"]["l3"] and got["A"]["listed"]
    assert not got["B"]["flag"] and got["B"]["listed"] and not got["C"]["flag"] and not got["C"]["listed"]


def test_the_held_leg_ignores_the_entry_rule_but_the_gated_leg_does_not_and_both_exit_at_the_d5_close():
    days = weekdays(date(2026, 11, 2), 12)
    flatb = [("10.00", "10.00", 1000)] * 4
    # X closes limit-up on d3 (11.00), opens limit-up again on d4 (12.10) and closes 11.00 on d8 (D+5); Y opens at 11.50 on d4 (buyable) and closes 11.00 on d8
    x = flatb[:3] + [("10.00", "11.00", 1000), ("12.10", "12.00", 1000)] + [("12.00", "12.00", 1000)] * 3 + [("12.00", "11.00", 1000)] + [("11.00", "11.00", 1000)] * 3
    y = flatb[:3] + [("10.00", "11.00", 1000), ("11.50", "11.40", 1000)] + [("11.40", "11.40", 1000)] * 3 + [("11.40", "11.00", 1000)] + [("11.00", "11.00", 1000)] * 3
    w = window({"600001": x, "600002": y}, days)
    panel, sessions, _ = IN7.exact_panel(w, frozenset())
    panel = panel.with_columns(pl.lit(True).alias("in_scope"), pl.lit(None, dtype=pl.Float64).alias("cap"), pl.lit(None, dtype=pl.String).alias("sw2"))
    inp = HistoryInputs(datetime(2026, 12, 30, tzinfo=timezone.utc), sessions, panel, seats([]), set(), pl.DataFrame(), {}, [])
    g, h = CORE.legs(inp)
    gx = {r["code"]: r for r in g.filter(pl.col("date") == days[3]).iter_rows(named=True)}
    hx = {r["code"]: r for r in h.filter(pl.col("date") == days[3]).iter_rows(named=True)}
    assert gx["600001"]["status"] == "blocked_buy" and gx["600002"]["status"] == "filled"
    assert hx["600001"]["status"] == "filled" and hx["600001"]["entry_c"] == 1210 and hx["600001"]["exit_c"] == 1100 and hx["600001"]["exit_si"] == 8
    assert hx["600001"]["ret"] == pytest.approx(11.00 / 12.10 - 1) and gx["600002"]["ret"] == pytest.approx(11.00 / 11.50 - 1) and gx["600002"]["exit_si"] == 8


# ---- H8a / H8b arithmetic -----------------------------------------------------------------------------------------------------------------
def rec(day, code, flag, held_ex=None, gated_ex=None, status="filled", group="first", l1=None, l3=False, t1=False):
    return ST.NameRec(day, code, group, flag if l1 is None else l1, l3, t1, flag, held_ex, None if held_ex is None else held_ex + 0.01, status, gated_ex, None if gated_ex is None else gated_ex + 0.01)


DAYS = [d.isoformat() for d in weekdays(date(2026, 11, 24), 60)]


def test_h8a_effect_is_the_name_weighted_mean_of_the_flag_names_matched_excess_and_the_p_follows_its_sign():
    recs = []
    for i, d in enumerate(DAYS):
        recs += [rec(d, "a", True, held_ex=-0.02 - 0.001 * (i % 3)), rec(d, "b", True, held_ex=-0.03), rec(d, "c", False, held_ex=0.05), rec(d, "z", True, held_ex=None)]
    e = ST.h8a(recs, DAYS)
    exp = sum((-0.02 - 0.001 * (i % 3) - 0.03) for i in range(len(DAYS))) / (2 * len(DAYS))
    assert e.effect == pytest.approx(exp) and e.n_arm == 2 * len(DAYS) and e.n_eligible == len(DAYS) and e.p_value < 0.01 and e.blocks == 12 and e.reliable
    flipped = [rec(r.day, r.code, r.flag, held_ex=None if r.held_ex is None else -r.held_ex) for r in recs]
    assert ST.h8a(flipped, DAYS).p_value > 0.99                    # the wrong sign: not worse, p near 1
    assert ST.h8a([r for r in recs if not r.flag], DAYS).effect is None                 # no flag names: no effect


def test_h8b_is_the_mean_over_sessions_with_both_groups_of_flag_mean_minus_other_mean_and_blocked_names_do_not_count():
    recs = []
    for i, d in enumerate(DAYS):
        recs += [rec(d, "f1", True, gated_ex=-0.04), rec(d, "f2", True, gated_ex=0.00), rec(d, "n1", False, gated_ex=0.01), rec(d, "n2", False, gated_ex=0.03),
                 rec(d, "fb", True, gated_ex=None, status="blocked_buy"), rec(d, "nb", False, gated_ex=None, status="blocked_buy")]
    recs += [rec("2027-06-01", "lonely", True, gated_ex=-0.5)]                             # a session with only a flag name is not eligible
    e = ST.h8b(recs, DAYS + ["2027-06-01"])
    assert e.effect == pytest.approx((-0.04 + 0.00) / 2 - (0.01 + 0.03) / 2) and e.n_eligible == len(DAYS) and e.p_value < 0.01
    fr = ST.fill_rates(recs)
    assert fr["flag"]["status"] == {"filled": 2 * len(DAYS) + 1, "blocked_buy": len(DAYS)} and fr["flag"]["fill_rate"] == pytest.approx((2 * len(DAYS) + 1) / (3 * len(DAYS) + 1))
    assert fr["noflag"]["status"] == {"filled": 2 * len(DAYS), "blocked_buy": len(DAYS)} and fr["noflag"]["fill_rate"] == pytest.approx(2 / 3)


def test_reports_split_groups_and_components_and_count_flag_samples_per_session():
    recs = [rec(DAYS[0], "a", True, held_ex=-0.01, group="first", l3=True), rec(DAYS[0], "b", True, held_ex=-0.02, group="consecutive", t1=True, l1=False),
            rec(DAYS[1], "c", True, held_ex=None), rec(DAYS[1], "d", False, held_ex=0.0)]
    rep = ST.report(recs, DAYS[:2])
    assert set(rep["by_group"]) == {"first", "consecutive"} and rep["by_component"]["L3"]["arm_names"] == 1 and rep["whole"]["arm_names"] == 3
    assert ST.flag_samples_per_day(recs, DAYS[:3]) == [2, 0, 0]


# ---- judge --------------------------------------------------------------------------------------------------------------------------------
def test_sample_cut_refuses_until_60_sessions_and_150_flag_samples_and_judges_the_earliest_prefix():
    assert R.sample_cut([3] * 59).state == "refused"
    assert R.sample_cut([2] * 60).state == "refused"                               # 120 flag samples
    assert R.sample_cut([5] * 60).state == "judge" and R.sample_cut([5] * 60).sessions == 60
    assert R.sample_cut([5] * 60 + [9] * 40).sessions == 60                        # later days never move the cut
    c = R.sample_cut([2] * 60 + [5] * 20)                                           # 120 flag samples after 60 sessions, +5 per session: 150 is first reached at session 66
    assert c.state == "judge" and c.sessions == 66 and c.flag_n == 150
    assert R.sample_cut([0] * 60 + [1] * 59).state == "refused"                     # 119 sessions: still extending
    assert R.sample_cut([1] * 120).state == "inconclusive" and R.sample_cut([1] * 120).sessions == 120
    assert R.sample_cut([1] * 119 + [30, 60]).state == "inconclusive"               # 150 only reached after session 120: too late
    assert R.sample_cut([1] * 119 + [31]).state == "judge"                          # reached at exactly session 120


def test_holm_over_two_and_the_size_rule_is_at_least_0_252_percent():
    v = R.judge({"H8a": (-0.004, 0.03), "H8b": (-0.004, 0.04)})
    assert v["H8a"].p_holm == pytest.approx(0.06) and v["H8b"].p_holm == pytest.approx(0.06) and not v["H8a"].passed and not v["H8b"].passed
    v = R.judge({"H8a": (-0.004, 0.02), "H8b": (-0.004, 0.04)})
    assert v["H8a"].p_holm == pytest.approx(0.04) and v["H8a"].passed and v["H8b"].p_holm == pytest.approx(0.04) and v["H8b"].passed       # Holm: max(0.04, 1 x 0.04)
    assert R.judge({"H8a": (-0.00252, 0.001), "H8b": (-0.0025, 0.001)})["H8a"].passed is True
    assert R.judge({"H8a": (-0.00252, 0.001), "H8b": (-0.0025, 0.001)})["H8b"].passed is False       # -0.25 % is smaller than one round trip
    assert R.judge({"H8a": (0.01, 0.001), "H8b": (-0.01, 0.05)})["H8a"].passed is False              # wrong sign never passes; p = 0.05 is not below 0.05
    none = R.judge({"H8a": (None, None), "H8b": (-0.01, 0.001)})
    assert none["H8a"].passed is False and none["H8b"].p_holm == pytest.approx(0.002) and none["H8b"].passed is True


# ---- append-only guard ---------------------------------------------------------------------------------------------------------------------
def test_publishing_a_row_that_exists_is_refused_before_anything_is_written():
    row = FW._row(date(2026, 11, 24), "signal", "600001", {"x": 1})
    with pytest.raises(RuntimeError, match="never rewritten"):
        FW.publish(None, [row], as_of=datetime(2026, 11, 25, tzinfo=timezone.utc), stored_keys={FW.key_of(row)})
    with pytest.raises(RuntimeError, match="never rewritten"):
        FW.publish(None, [row, dict(row)], as_of=datetime(2026, 11, 25, tzinfo=timezone.utc))
    assert FW._row(date(2026, 11, 24), "signal", "600001", {"x": 1}) == row                  # a row is a pure function of its content


def test_overlay_ratio_is_against_the_fill_price_and_net_of_the_round_trip_cost():
    from decimal import Decimal as D

    r = FW._ratio(D("11.20"), D("11.00"))
    assert r["ret_gross"] == pytest.approx(11.20 / 11.00 - 1) and r["ret_net"] == pytest.approx(11.20 / 11.00 - 1 - 0.00252) and FW._ratio(None, D("11"))["ret_gross"] is None


def test_the_overlay_summary_pairs_the_counterfactual_with_each_actual_exit_and_pays_no_attention_to_unflagged_fills():
    from bf_batch8.support import overlay_row
    from pulse.application import bf_batch8_reads as RD

    rows = [overlay_row("B1", "BASE", True, cf=0.02, actual={"S1_decay": 0.03, "HOLD_close": 0.01}), overlay_row("B3", "BASE", True, cf=0.00, actual={"S1_decay": -0.01}),
            overlay_row("B1", "BASE", False), overlay_row("B1", "OPTIMISTIC", True, cf=0.02, actual={})]
    s = RD.overlay_summary(rows)["scenarios"]
    base = s["BASE"]
    assert (base["fills"], base["FLAG"], base["with_counterfactual"]) == (3, 2, 2) and base["counterfactual_mean_ret_gross"] == pytest.approx(0.01)
    assert base["actual_exit_rules"]["S1_decay"]["n"] == 2 and base["actual_exit_rules"]["S1_decay"]["counterfactual_minus_actual"] == pytest.approx(((0.02 - 0.03) + (0.00 + 0.01)) / 2)
    assert base["actual_exit_rules"]["HOLD_close"]["counterfactual_minus_actual"] == pytest.approx(0.01) and s["OPTIMISTIC"]["actual_exit_rules"] == {}


# ---- M5: judge only when no earlier day is still pending --------------------------------------------------------------------------------
def _sessions(n=64):
    return FW.candidate_days(R.FORWARD_START, date(2027, 6, 30))[:n]


def _day_rows(days, *, status=None, resolved_skip=(), per_day_flags=3):
    """Stored rows of ``days``: a decided day with flagged and unflagged names and a day_outcome, except the overrides in ``status`` (day index -> status or None = no day row)."""
    status = status or {}
    rows = []
    for i, d in enumerate(days):
        st = status.get(i, "decided")
        if st is None:
            continue
        rows.append(FW._row(d, "day", None, {"status": st}))
        if st != "decided" or i in resolved_skip:
            continue
        for k in range(per_day_flags + 2):
            flag = k < per_day_flags
            leg = {"status": "filled", "ex_sv": -0.01 if flag else 0.0, "ret_gross": -0.01 if flag else 0.0}
            rows.append(FW._row(d, "outcome", f"60{k:04d}", {"group": "first", "l1": flag, "l3": False, "t1": False, "flag": flag, "gated": dict(leg), "held": dict(leg)}))
        rows.append(FW._row(d, "day_outcome", None, {"n_signals": per_day_flags + 2}))
    return rows


def _overview(rows):
    from pulse.application import bf_batch8_reads as RD

    return RD.overview(None, None, date(2027, 3, 1), rows=rows)


def test_judge_runs_only_when_every_day_up_to_the_cutoff_is_final_and_every_decided_day_resolved():
    days = _sessions()
    ok = _overview(_day_rows(days))
    assert ok["cut"]["state"] == "judge" and ok["cut"]["sessions"] == 60 and ok["verdicts"] is not None
    pending = _overview(_day_rows(days, resolved_skip={10}))                        # day 10 is decided, its D+5 exit is not resolved yet (a deferred sell): the prefix would skip it
    assert pending["cut"]["state"] == "refused" and "not resolved" in pending["cut"]["reason"] and pending["verdicts"] is None
    missing = _overview(_day_rows(days, status={10: None}))                         # day 10 has no day row at all: still waiting for bars or seats
    assert missing["cut"]["state"] == "refused" and "without a day row" in missing["cut"]["reason"]
    final = _overview(_day_rows(days, status={10: "no_seats", 20: "no_data"}))      # lost days are final rows: they do not block (and are not sessions)
    assert final["cut"]["state"] == "judge" and final["cut"]["sessions"] == 60
    after = _overview(_day_rows(days, resolved_skip={63}))                          # an unresolved day AFTER the judged prefix does not matter
    assert after["cut"]["state"] == "judge" and after["cut"]["sessions"] == 60


def test_the_blocked_judge_writes_nothing_and_the_cli_exit_code_is_3():
    days = _sessions()
    ov = _overview(_day_rows(days, resolved_skip={10}))
    assert ov["cut"]["state"] == "refused"                                          # judge_and_store writes a judgement only for 'judge' / 'inconclusive'; the worker returns 3 for 'refused'
    src = (Path(__file__).resolve().parents[2] / "src/pulse/workers/batch8.py").read_text(encoding="utf-8")
    assert 'return 3 if ov["cut"]["state"] == "refused" else 0' in src


# ---- L6: unique overlay keys --------------------------------------------------------------------------------------------------------------
def test_overlay_keys_are_unique_even_for_two_intents_of_the_same_rule_code_and_scenario():
    used: set[str] = set()
    d = date(2026, 11, 24)
    a = FW._overlay_key({"opportunity_id": "2026-11-24-B3-600101-BASE", "rule": "B3", "code": "600101", "scenario": "BASE"}, d, used)
    b = FW._overlay_key({"opportunity_id": "2026-11-24-B3-600101-BASE-2", "rule": "B3", "code": "600101", "scenario": "BASE"}, d, used)
    c = FW._overlay_key({"rule": "B3", "code": "600101", "scenario": "BASE"}, d, used)                        # no id: the rule-code-scenario fallback
    e = FW._overlay_key({"rule": "B3", "code": "600101", "scenario": "BASE"}, d, used)                        # the same fallback again: numbered
    assert len({a, b, c, e}) == 4 and c.endswith("#2") and e.endswith("#3")


# ---- M7: the frozen text ships with the app and the check fails closed ------------------------------------------------------------------------
def test_the_packaged_frozen_spec_matches_the_pin_and_a_missing_or_changed_copy_fails_closed(tmp_path):
    from pulse.application import bf_batch8_service as SV
    from pulse.application import frozen_specs as FS

    assert SV.check_document() == R.SPEC_SHA256                                     # the copy under pulse/research_specs/
    with pytest.raises(FS.FrozenSpecError, match="missing or unreadable"):
        SV.check_document(specs_dir=tmp_path)                                       # not deployed: never None, never a pass
    packaged = Path(FS.SPECS_DIR) / SV.SPEC_FILE
    (tmp_path / SV.SPEC_FILE).write_bytes(packaged.read_bytes().replace("0.252%".encode(), "0.300%".encode(), 1))
    with pytest.raises(FS.FrozenSpecError, match="pinned"):
        SV.check_document(specs_dir=tmp_path)
    repo = tmp_path / "repo" / R.SPEC_PATH
    repo.parent.mkdir(parents=True)
    repo.write_bytes(packaged.read_bytes().replace("0.252%".encode(), "0.300%".encode(), 1))
    with pytest.raises(FS.FrozenSpecError, match="repository copy"):
        SV.check_document(repo_root=tmp_path / "repo")                              # a repository copy that disagrees stops the run too


def test_the_packaged_copy_has_the_same_frozen_prefix_as_the_repository_document():
    from pulse.application import frozen_specs as FS

    repo = Path(__file__).resolve().parents[3] / R.SPEC_PATH
    if not repo.exists():
        pytest.skip("pre-registration not present in this checkout")
    pk = (Path(FS.SPECS_DIR) / Path(R.SPEC_PATH).name).read_bytes()
    assert R.frozen_prefix(pk) == R.frozen_prefix(repo.read_bytes()) and len(R.frozen_prefix(pk)) == 4741


# ---- L4: the institution seat is never a one-day seat ---------------------------------------------------------------------------------------
def test_t1_excludes_the_institution_seat_as_a_buyer_even_with_a_perfect_record():
    rows = history([True] * 8, seat="机构专用") + [("600999", at(30), "buy", "机构专用", 1.0)]
    f = SE.flag_table(seats(rows), CAL)
    r = f.filter((pl.col("code") == "600999") & (pl.col("date") == at(30))).row(0, named=True)
    assert r["t1"] is False and r["trip_seats"] == 0 and r["flag"] is False
