"""E093 / E094 signal construction on a small synthetic panel (hand-checked): events, targets, seat counts, the 5 percent institution rule."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import polars as pl

from pulse.application import bf_batch6_history as H
from pulse.application.bf_batch6_inputs import WARMUP_SESSIONS, HistoryInputs

SESS = [date(2026, 1, 5) + timedelta(days=i) for i in range(WARMUP_SESSIONS + 3)]
D0, D1, D2 = SESS[WARMUP_SESSIONS - 1], SESS[WARMUP_SESSIONS], SESS[WARMUP_SESSIONS + 1]
SCHEMA = {"date": pl.Date, "code": pl.String, "in_scope": pl.Boolean, "sw2": pl.String, "streak": pl.Int64, "lu": pl.Boolean, "amt": pl.Float64, "cap": pl.Float64}


def row(d, code, sw2, streak, lu, amt, cap, in_scope=True):
    return {"date": d, "code": code, "in_scope": in_scope, "sw2": sw2, "streak": streak, "lu": lu, "amt": amt, "cap": cap}


def inputs(rows, seats=None, lhb=None):
    seat_schema = {"code": pl.String, "date": pl.Date, "side": pl.String, "seat": pl.String, "amount": pl.Float64}
    return HistoryInputs(datetime(2026, 11, 18, tzinfo=timezone.utc), SESS, pl.DataFrame(rows, schema=SCHEMA), pl.DataFrame(seats or [], schema=seat_schema), lhb or set(),
                         pl.DataFrame(), {}, [])


def test_break_event_targets_core_and_first_board_followers():
    rows = [row(D0, "L", "x", 3, True, 100.0, 50.0), row(D0, "a", "x", 0, False, 10.0, 10.0),
            row(D1, "L", "x", 0, False, 80.0, 50.0), row(D1, "a", "x", 0, False, 70.0, 60.0), row(D1, "f", "x", 1, True, 5.0, 10.0), row(D1, "g", "x", 2, True, 5.0, 10.0)]
    sig, events = H.e093_signals(inputs(rows))
    # D1 caps: L 50, a 60, f 10, g 10 -> median 30; core = a (60, turnover 70); followers = first boards only: f
    assert sig["H1"] == [(D1, "a"), (D1, "f")] and sig["H2"] == []
    assert [(e["kind"], e["leader"], e["targets"]) for e in events] == [("break", "L", 2)]


def test_promotion_event_buys_the_core_only():
    rows = [row(D0, "L", "x", 3, True, 100.0, 50.0), row(D1, "L", "x", 4, True, 120.0, 50.0), row(D1, "a", "x", 0, False, 70.0, 60.0), row(D1, "f", "x", 1, True, 5.0, 10.0)]
    sig, events = H.e093_signals(inputs(rows))
    assert sig["H2"] == [(D1, "a")] and sig["H1"] == []
    assert events[0]["kind"] == "promotion"


def test_out_of_scope_members_are_ignored_and_warmup_days_have_no_events():
    rows = [row(D0, "L", "x", 3, True, 100.0, 50.0), row(D1, "L", "x", 0, False, 80.0, 50.0, in_scope=False), row(D1, "a", "x", 0, False, 70.0, 60.0)]
    sig, events = H.e093_signals(inputs(rows))
    assert events == [] and sig == {"H1": [], "H2": []}                  # the leader is not in scope on D1: no event
    early = [row(SESS[1], "L", "x", 3, True, 1.0, 1.0), row(SESS[2], "L", "x", 0, False, 1.0, 1.0)]
    assert H.e093_signals(inputs(early))[1] == []                         # D is inside the warm-up


def test_lhasa_industry_and_institution_signals():
    rows = [row(D1, "p", "ind", 0, False, 100.0, 10.0), row(D1, "q", "ind", 0, False, 100.0, 10.0), row(D1, "r", "ind", 0, False, 100.0, 10.0),
            row(D1, "t", "oth", 0, False, 1000.0, 10.0)]
    seats = [{"code": "p", "date": D1, "side": "buy", "seat": f"东方财富证券拉萨第{i}营业部", "amount": 1e6} for i in range(3)]
    seats += [{"code": "p", "date": D1, "side": "buy", "seat": "东方财富证券拉萨第0营业部", "amount": 2e6}]            # a repeated seat counts once
    seats += [{"code": "t", "date": D1, "side": "buy", "seat": "机构专用", "amount": 60.0}, {"code": "t", "date": D1, "side": "sell", "seat": "机构专用", "amount": 5.0},
              {"code": "t", "date": D1, "side": "buy", "seat": "中信建投北京商务中心区机构专用证券营业部", "amount": 9999.0}]
    sig, info = H.e094_signals(inputs(rows, seats, {("p", D1), ("t", D1)}))
    # p has 3 distinct Lhasa buy seats -> industry "ind"; targets are the other members that are not on the list: q, r
    assert sig["H3"] == [(D1, "q"), (D1, "r")]
    # t: institution net buy 55 against turnover 1000 -> 5.5 % >= 5 % ; the branch whose name merely contains the words is not an institution seat
    assert sig["H4"] == [(D1, "t")]


def test_institution_below_five_percent_or_net_negative_is_no_signal():
    rows = [row(D1, "t", "oth", 0, False, 1000.0, 10.0), row(D1, "u", "oth", 0, False, 1000.0, 10.0)]
    seats = [{"code": "t", "date": D1, "side": "buy", "seat": "机构专用", "amount": 49.0}, {"code": "u", "date": D1, "side": "buy", "seat": "机构专用", "amount": 10.0},
             {"code": "u", "date": D1, "side": "sell", "seat": "机构专用", "amount": 100.0}]
    assert H.e094_signals(inputs(rows, seats, set()))[0]["H4"] == []
