"""E101 pure rules: frozen hash, classes, D0 / R dating, events, the forward window, H1 / group statistics, the judge, market series and groups."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import polars as pl

from pulse.application import bf_e101_core as C
from pulse.application import bf_e101_plan as PL
from pulse.domain.e101 import rules as R
from pulse.domain.e101 import stats as ST

ROOT = Path(__file__).resolve().parents[3]


def at(y, m, d, hh, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=R.CST)


def test_the_frozen_prefix_of_the_preregistration_matches_its_hash():
    raw = (ROOT / R.SPEC_PATH).read_bytes()
    assert R.frozen_hash(raw) == R.SPEC_SHA256
    assert len(R.frozen_prefix(raw)) == 6684


def test_classes_use_e073_patterns_plus_core_pce_and_full_width_colons():
    assert R.classify("美国:核心PCE物价指数:同比(报告期:2026年10月)") == "US_CORE_PCE"
    assert R.classify("美国：核心CPI:季调:当月同比(报告期:2026年11月)") == "US_CPI"
    assert R.classify("美国:新增非农就业人数:当月值:季调(报告期:2026年11月)") == "US_NFP"
    assert R.classify("美国:联邦基金利率目标:上限(报告期:2026年12月)") == "FOMC"
    assert R.classify("美国:PCE物价指数:季调(报告期:2026年10月)") is None          # headline PCE is not a class
    assert R.classify("美国:新增非农私营就业人数:季调") is None


def test_d0_is_the_last_close_before_the_release_and_r_the_first_close_after():
    s = C.open_sessions(date(2026, 12, 1), date(2026, 12, 15))
    assert R.d0_r(at(2026, 12, 4, 21, 30), s) == (date(2026, 12, 4), date(2026, 12, 7))     # Friday evening: R is Monday
    assert R.d0_r(at(2026, 12, 10, 3), s) == (date(2026, 12, 9), date(2026, 12, 10))       # FOMC at 03:00: D0 the day before
    assert R.d0_r(at(2026, 12, 9, 14, 59), s) == (date(2026, 12, 8), date(2026, 12, 9))


def test_one_event_per_class_and_d0_with_the_earliest_release_and_first_knowledge():
    s = C.open_sessions(date(2026, 12, 1), date(2026, 12, 15))
    rows = [{"name": "美国:核心CPI:季调:环比(报告期:2026年11月)", "release_at": at(2026, 12, 10, 21, 30), "known_at": at(2026, 11, 20, 8)},
            {"name": "美国:CPI:非季调:同比(报告期:2026年11月)", "release_at": at(2026, 12, 10, 21, 30), "known_at": at(2026, 11, 19, 8)},
            {"name": "美国:联邦基金利率目标:上限(报告期:2026年12月)", "release_at": at(2026, 12, 10, 3), "known_at": at(2026, 12, 9, 10)},
            {"name": "中国:CPI:当月同比", "release_at": at(2026, 12, 9, 9, 30), "known_at": None}]
    ev = R.build_events(rows, s)
    assert [(e.cls, e.d0, e.r) for e in ev] == [("FOMC", date(2026, 12, 9), date(2026, 12, 10)), ("US_CPI", date(2026, 12, 10), date(2026, 12, 11))]
    cpi = ev[1]
    assert len(cpi.names) == 2 and cpi.known_at == at(2026, 11, 19, 8) and cpi.known_before_open()
    assert not ev[0].known_before_open()                     # first seen after the D0 open: no H3 trade possible


def test_the_forward_sample_starts_the_session_after_the_freeze():
    s = C.open_sessions(date(2026, 11, 20), date(2026, 12, 5))
    pce = R.build_events([{"name": "美国:核心PCE物价指数:同比", "release_at": at(2026, 11, 25, 21, 30)}], s)[0]
    nfp = R.build_events([{"name": "美国:新增非农就业人数:当月值", "release_at": at(2026, 11, 26, 21, 30)}], s)[0]
    assert not R.in_forward(pce) and R.in_forward(nfp)


def test_h1_compares_d0_with_same_weekday_non_event_days_and_leaves_r_days_out_of_the_control():
    days = C.open_sessions(date(2026, 11, 2), date(2026, 12, 31))
    mk = {d.isoformat(): 0.001 for d in days}
    s = C.open_sessions(date(2026, 11, 1), date(2027, 1, 10))
    events = R.build_events([{"name": "美国:新增非农就业人数", "release_at": at(2026, 12, 4, 21, 30)},
                             {"name": "美国:核心CPI:同比", "release_at": at(2026, 12, 10, 21, 30)}], s)
    for e in events:
        mk[e.d0.isoformat()] = -0.01
        mk[e.r.isoformat()] = 0.05                              # an R day must not move the control
    ed = {e.d0.isoformat() for e in events} | {e.r.isoformat() for e in events}
    out = ST.h1(mk, events, sorted(mk), ed)
    assert abs(out.effect - (-0.011)) < 1e-12 and out.n == 2
    assert out.p_value is not None and out.p_value < 0.5        # the pre-registered direction is negative


def test_group_excess_counts_a_name_once_when_two_events_share_the_decision_day():
    recs = [("2026-12-09|FOMC", "2026-12-08", "leader", "600001", 0.02), ("2026-12-09|US_CPI", "2026-12-08", "leader", "600001", 0.02),
            ("2026-12-04|US_NFP", "2026-12-03", "leader", "600002", -0.01), ("2026-12-04|US_NFP", "2026-12-03", "core", "600003", 0.03)]
    slots = [d.isoformat() for d in C.open_sessions(date(2026, 11, 26), date(2026, 12, 31))]
    e = ST.group_excess(recs, "leader", slots)
    assert e.n == 2 and abs(e.effect - 0.005) < 1e-12
    groups, p = ST.two_groups(recs, slots)
    assert p == max(groups["leader"].p_value, groups["core"].p_value)


def test_judge_needs_both_groups_and_counts_untested_hypotheses_as_p_one():
    v = R.judge({"H1": ([-0.004], 0.001), "H3": ([0.004, 0.001], 0.001)})
    assert set(v) == {"H1", "H3"}
    assert v["H1"].passed and abs(v["H1"].p_holm - 0.004) < 1e-12        # family of four: 4 x 0.001
    assert not v["H3"].passed                                             # the core group's effect is below one round trip
    assert not R.judge({"H1": ([-0.002], 0.0001)})["H1"].passed           # size below 0.252 %
    assert not R.judge({"H1": ([0.01], 0.0001)})["H1"].passed             # wrong sign


def _panel() -> pl.DataFrame:
    days = C.open_sessions(date(2026, 11, 2), date(2026, 12, 4))
    rows = []
    for code, cap, step, streak_tail in (("600001", 100.0, 0.02, 0), ("600002", 50.0, 0.0, 3), ("600003", 10.0, 0.0, 1)):
        c = 1000
        for i, d in enumerate(days):
            pct = None if i == 0 else step
            c = int(round(c * (1 + (pct or 0))))
            streak = streak_tail if d == days[-1] else 0
            rows.append({"code": code, "date": d, "si": i, "o": c, "c": c, "pct": pct, "in_scope": True, "streak": streak, "cap": cap})
    return pl.DataFrame(rows)


def test_market_needs_the_previous_session_and_groups_follow_section_3():
    p = C.with_helpers(_panel())
    days = sorted(p["date"].unique().to_list())
    mk = C.market_days(p)
    assert days[0] not in mk                                   # no previous session yet
    assert abs(mk[days[-1]]["r"] - 0.02 / 3) < 1e-12 and mk[days[-1]]["n"] == 3
    g = C.groups_at(p, days[-1])
    assert g["leader"] == ["600002"]                           # highest streak, at least 3
    assert g["core"] == ["600001"]                             # top cap and top 20-session return
    g2 = C.groups_at(p.with_columns(pl.lit(2).alias("streak")), days[-1])
    assert g2["leader"] == []                                  # a top streak below 3 makes no leader group


def test_the_plan_report_carries_the_banner_and_no_trading_words():
    e = R.Event("US_NFP", date(2026, 12, 4), date(2026, 12, 7), at(2026, 12, 4, 21, 30), ("美国:新增非农就业人数:当月值",), None)
    for kind in ("d0", "r"):
        b = PL._body(kind, date(2026, 12, 4), e, date(2026, 12, 3), {"leader": ["600002"], "core": []}, {"600002": "示例"}, None)
        text = PL.markdown(b)
        assert text.startswith(f"# {PL.BANNER}")
        assert "买入" not in text and "卖出" not in text
        assert "600002 示例" in text and "一致预期：无" in text


def test_r20_needs_twenty_consecutive_sessions_without_a_suspension_gap():
    p = _panel()
    gap = p.filter(~((pl.col("code") == "600001") & (pl.col("si") == 10)))          # one suspended session inside the last 20
    out = C.with_helpers(gap)
    last = out.filter(pl.col("date") == out["date"].max())
    r20 = dict(last.select("code", "r20").iter_rows())
    assert r20["600001"] is None and r20["600002"] is not None
