"""E092 ``track`` (5-day outcomes, append-only) and ``judge`` (the pre-registered rule, written once) offline."""
from __future__ import annotations

import math
from datetime import date

import pytest

from pulse.application.bf_llm_live_store import MemoryLogStore, make_row
from pulse.domain.llm_live import judge as J
from pulse.domain.llm_live import rules as R
from bf_llm_live.support import DAY, EVENING, Recorder, build, kinds, plan, reply

ISO = DAY.isoformat()
CLOSED = R.sessions_after(DAY, 6)                     # 2026-12-01: the D+6 open exists from this session on


def labels_for(days, ex=lambda code, i: float(i), nan_codes=()):
    return {d: {j["code"]: ((None, None) if j["code"] in nan_codes else (ex(j["code"], i) + 1.0, ex(j["code"], i))) for i, j in enumerate(plan(d).jobs)} for d in days}


def answered(**kw):
    svc, store, vendor, clock = build(**kw)
    svc.decide(DAY)
    return svc, store, vendor, clock


def test_nothing_is_scored_before_the_window_can_have_closed():
    svc, store, _, _ = answered()
    assert svc.track(date(2026, 11, 30))["status"] == "nothing_due"                  # D+6 is 2026-12-01
    assert not kinds(store, "outcome")


def test_outcomes_are_written_once_per_answer_with_the_label_and_the_answer_fields():
    svc, store, _, _ = answered()
    svc.labels = lambda days: labels_for(days, nan_codes=("600005",))
    out = svc.track(CLOSED)
    assert out["status"] == "tracked" and out["written"] == 40 and out["by_status"] == {"scored": 39, "no_label": 1}
    rows = {r["key"]: r["body"] for r in kinds(store, "outcome")}
    one = rows[R.job_id(DAY, "600003")]
    assert one["ex5"] == 3.0 and one["ret5"] == 4.0 and one["group"] == "top" and one["action"] == "watch" and one["p_outperform"] == 55 and one["status"] == "scored"
    assert rows[R.job_id(DAY, "600005")]["ex5"] is None and rows[R.job_id(DAY, "600005")]["status"] == "no_label"          # an unknown label is not a 0
    again = svc.track(CLOSED)
    assert again["status"] == "nothing_due" and len(kinds(store, "outcome")) == 40                                           # append-only: a rerun writes nothing


def test_a_non_finite_label_is_stored_as_unknown():
    svc, store, _, _ = answered()
    svc.labels = lambda days: {d: {j["code"]: (math.nan, math.inf) for j in plan(d).jobs} for d in days}
    svc.track(CLOSED)
    assert {r["body"]["status"] for r in kinds(store, "outcome")} == {"no_label"} and all(r["body"]["ex5"] is None for r in kinds(store, "outcome"))


def test_a_day_whose_labels_are_not_in_the_data_yet_stays_pending():
    svc, store, _, _ = answered()
    svc.labels = lambda days: {}
    assert svc.track(CLOSED)["status"] == "waiting_for_data" and not kinds(store, "outcome")
    svc.labels = lambda days: labels_for(days)
    assert svc.track(CLOSED)["written"] == 40


def test_a_late_answer_is_recorded_but_never_scored():
    from bf_llm_live.support import Clock
    clock = Clock(EVENING)

    def fn(n, body):
        clock.now = R.deadline_after(DAY)
        return reply(rid=f"r{n}")
    svc, store, _, _ = build(clock=clock, vendor=Recorder(fn))
    svc.decide(DAY)
    svc.labels = lambda days: labels_for(days)
    svc.track(CLOSED)
    assert [r["body"]["status"] for r in kinds(store, "outcome")] == ["late_answer"]
    assert svc.judge()["progress"]["scored"] == 0


# -- the rule ---------------------------------------------------------------------------------------------------------------
def rows_for(days: int, *, informative: bool = True, avoid_below: int = 15, controls: bool = True) -> list[dict]:
    out = []
    for d in range(days):
        day = f"2026-{11 + d // 28:02d}-{d % 28 + 1:02d}"
        for i in range(30):
            p = i * 3 + 5
            ex = (p - 50) / 5 if informative else ((i * 7) % 11 - 5) / 5
            out.append({"decision_date": day, "group": "top", "action": "avoid" if i < avoid_below else "watch", "p_outperform": p, "ex5": ex, "code": f"{i}"})
        if controls:
            for i in range(10):
                out.append({"decision_date": day, "group": "control", "action": "watch", "p_outperform": i, "ex5": -i / 3, "code": f"c{i}"})
    return out


def test_an_informative_judge_with_20_valid_days_passes():
    ev = J.evaluate(rows_for(20))
    assert ev["verdict"] == "passed" and ev["ready"] and ev["days"] == 20 and ev["mean_ic"] == pytest.approx(1.0) and ev["p_boot"] < 0.05
    assert ev["spread_pct"] > J.ROUND_TRIP_PCT and ev["n_other"] == 300 and ev["control_mean_ic"] == pytest.approx(-1.0)          # reported, never judged


def test_fewer_than_20_valid_days_is_inconclusive_whatever_the_numbers():
    ev = J.evaluate(rows_for(19))
    assert ev["verdict"] == "inconclusive" and not ev["ready"] and ev["days"] == 19


def test_an_uninformative_or_inverted_judge_fails():
    inv = [{**r, "ex5": -r["ex5"]} for r in rows_for(20)]
    assert J.evaluate(inv)["verdict"] == "fails" and J.evaluate(rows_for(20, informative=False))["verdict"] == "fails"


def test_a_day_with_fewer_than_20_top_names_has_no_rank_correlation_and_is_not_counted():
    rows = rows_for(20)
    short = [r for r in rows if not (r["decision_date"] == rows[0]["decision_date"] and r["group"] == "top" and int(r["code"]) < 12)]       # that day keeps 18 top names
    ev = J.evaluate(short)
    assert ev["days"] == 19 and ev["days_without_ic"] == 1 and ev["verdict"] == "inconclusive"


def test_the_ic_passes_but_no_non_avoid_names_cannot_be_judged():
    ev = J.evaluate(rows_for(20, avoid_below=30))                      # every top name is "avoid": the spread has no other side
    assert ev["ic_pass"] and ev["n_other"] < J.MIN_NON_AVOID and ev["spread_pass"] is None and ev["verdict"] == "inconclusive" and ev["passed"] is False


def test_the_spread_must_beat_the_round_trip_cost():
    # the within-day order is kept (IC stays 1) but all names lie within 0.1 percent of each other
    squeezed = [{**r, "ex5": r["p_outperform"] * 0.001} if r["group"] == "top" else r for r in rows_for(20)]
    ev = J.evaluate(squeezed)
    assert ev["ic_pass"] and ev["spread_pass"] is False and ev["verdict"] == "fails"


def test_non_finite_labels_never_enter_the_statistics():
    rows = rows_for(20)
    rows[0] = {**rows[0], "ex5": math.nan}
    rows[1] = {**rows[1], "ex5": None}
    assert J.evaluate(rows)["days"] == 20


# -- the service -----------------------------------------------------------------------------------------------------------
def seeded_store(days: int, *, pending_answers_on: str | None = None) -> MemoryLogStore:
    """A log with ``days`` decision days, answers and scored outcomes (as ``track`` would have left them)."""
    store = MemoryLogStore()
    for r in rows_for(days):
        d = r["decision_date"]
        k = f"{d}_{r['code']}"
        if r["group"] == "top" and r["code"] == "0":
            store.append([make_row(d, "call", k, "t", {"n": 1, "id": k}, at=EVENING)])
        if pending_answers_on != d:
            store.append([make_row(d, "answer", k, "t", {"id": k, "code": r["code"], "group": r["group"], "action": r["action"], "p_outperform": r["p_outperform"], "valid": True}, at=EVENING)])
            store.append([make_row(d, "outcome", k, "t", {"id": k, "code": r["code"], "group": r["group"], "action": r["action"], "p_outperform": r["p_outperform"],
                                                          "ex5": r["ex5"], "status": "scored"}, at=EVENING)])
        else:
            store.append([make_row(d, "answer", k, "t", {"id": k, "code": r["code"], "group": r["group"], "action": r["action"], "p_outperform": r["p_outperform"], "valid": True}, at=EVENING)])
    return store


def test_judge_is_progress_only_until_20_days_are_scored():
    svc, store, _, _ = build(store=seeded_store(19))
    out = svc.judge()
    assert out["status"] == "progress" and out["progress"]["decision_days"] == 19 and not kinds(store, "judgement")


def test_judge_writes_the_verdict_once_when_20_days_are_scored():
    svc, store, _, _ = build(store=seeded_store(20))
    out = svc.judge()
    assert out["status"] == "judged" and out["evaluation"]["verdict"] == "passed" and out["stored"] is False
    row = kinds(store, "judgement")
    assert len(row) == 1 and row[0]["body"]["spec_sha256"] == R.SPEC_SHA256 and row[0]["body"]["decision_days"] == 20
    again = svc.judge()
    assert again["stored"] is True and len(kinds(store, "judgement")) == 1                      # never rewritten


def test_the_20_day_limit_with_everything_scored_but_too_few_valid_days_is_judged_inconclusive():
    store = seeded_store(20)
    store._rows = [r for r in store._rows if not (r["session"] == "2026-11-01" and r["kind"] in ("outcome",) and r["body"]["group"] == "top" and int(r["body"]["code"]) < 12)]
    store._rows = [r for r in store._rows if not (r["session"] == "2026-11-01" and r["kind"] == "answer" and r["body"]["group"] == "top" and int(r["body"]["code"]) < 12)]
    svc, store, _, _ = build(store=store)
    out = svc.judge()
    assert out["status"] == "judged" and out["evaluation"]["verdict"] == "inconclusive" and out["evaluation"]["days"] == 19


def test_the_limit_without_all_days_scored_waits():
    svc, store, _, _ = build(store=seeded_store(20, pending_answers_on="2026-11-20"))
    out = svc.judge()
    assert out["status"] == "progress" and out["progress"]["days_unresolved"] == ["2026-11-20"] and not kinds(store, "judgement")


def test_status_reports_caps_spend_and_progress_without_a_network():
    svc, store, vendor, _ = answered()
    s = svc.status()
    assert s["calls_today"] == 40 and s["calls_cap"] == 60 and s["cap_usd"] == 5.0 and s["decision_days"] == 1 and s["answers"] == 40 and s["spent_today_usd"] > 0
    assert s["model"] == "gpt-6.1-terra" and s["judgement"] is None and len(vendor.requests) == 40
