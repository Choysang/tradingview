"""E097 robustness report pieces: break / role listing and the exact-versus-adjusted comparison counts (hand-built)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import polars as pl

from pulse.application import bf_batch7_history as HI
from pulse.application.bf_batch6_inputs import WARMUP_SESSIONS, HistoryInputs

SESS = [date(2026, 1, 5) + timedelta(days=i) for i in range(WARMUP_SESSIONS + 3)]
D0, D1 = SESS[WARMUP_SESSIONS - 1], SESS[WARMUP_SESSIONS]
SCHEMA = {"date": pl.Date, "code": pl.String, "in_scope": pl.Boolean, "sw2": pl.String, "streak": pl.Int64, "lu": pl.Boolean, "amt": pl.Float64, "cap": pl.Float64}


def row(d, code, streak, lu, amt, cap):
    return {"date": d, "code": code, "in_scope": True, "sw2": "x", "streak": streak, "lu": lu, "amt": amt, "cap": cap}


def inputs(rows):
    seat = {"code": pl.String, "date": pl.Date, "side": pl.String, "seat": pl.String, "amount": pl.Float64}
    return HistoryInputs(datetime(2026, 11, 20, tzinfo=timezone.utc), SESS, pl.DataFrame(rows, schema=SCHEMA), pl.DataFrame([], schema=seat), set(), pl.DataFrame(), {}, [])


def test_breaks_list_targets_with_roles_and_stop_at_the_last_day():
    rows = [row(D0, "L", 3, True, 100.0, 50.0), row(D1, "L", 0, False, 80.0, 50.0), row(D1, "a", 0, False, 70.0, 60.0), row(D1, "f", 1, True, 5.0, 10.0)]
    b = HI.breaks_with_roles(inputs(rows))
    assert [(x["day"], x["leader"], x["targets"]) for x in b] == [(D1, "L", [("a", "core"), ("f", "follower")])]
    assert HI.breaks_with_roles(inputs(rows), last_day=D0) == []


def test_compare_counts_leaders_breaks_targets_and_role_changes():
    exact = [{"day": D1, "industry": "x", "leader": "L", "targets": [("a", "core"), ("f", "follower"), ("g", "follower")]},
             {"day": D1, "industry": "y", "leader": "M", "targets": [("m", "core")]}]
    adj = [{"day": D1, "industry": "x", "leader": "L", "targets": [("a", "core"), ("f", "core")]},
           {"day": D1, "industry": "z", "leader": "N", "targets": [("n", "core")]}]
    c = HI.compare(exact, adj)
    assert (c["breaks_exact"], c["breaks_adjusted"], c["breaks_both"], c["breaks_only_exact"], c["breaks_only_adjusted"]) == (2, 2, 1, 1, 1)
    assert (c["targets_exact"], c["targets_adjusted"], c["targets_both"], c["targets_only_exact"], c["targets_only_adjusted"]) == (4, 3, 2, 2, 1)
    assert c["same_target_different_role"] == 1 and c["followers_exact"] == 2 and c["core_adjusted"] == 3 and c["same_day_industry_different_leader"] == 0
    other = HI.compare([{"day": D1, "industry": "x", "leader": "L", "targets": []}], [{"day": D1, "industry": "x", "leader": "K", "targets": []}])
    assert other["same_day_industry_different_leader"] == 1 and other["breaks_both"] == 0
