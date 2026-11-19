"""Pure tests of the fill-research domain code with hand-computed expectations: playbook board evaluation (statistics, verdict order, fitness
components), monthly returns, the style-matched baseline (E062) and the block bootstrap (E082). No database."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal as D

import numpy as np
import pytest

from pulse.domain.lab import tearsheet as T
from pulse.domain.playbooks import evaluation as EV
from pulse.domain.research import baseline as BL
from pulse.domain.research import bootstrap as BS


def approx(x, y, tol="1e-9"):
    assert abs(D(str(x)) - D(str(y))) < D(tol), (x, y)


def days(n, start=date(2026, 1, 5)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


# ---- statistics -------------------------------------------------------------------------------------------------------------------------
def test_daily_returns_skip_missing_marks_and_never_spread_them():
    nav = [("2026-01-02", D(110)), ("2026-01-05", None), ("2026-01-06", D(121)), ("2026-01-07", D("133.1"))]
    r = EV.daily_returns(nav, D(100))
    assert len(r) == 2
    approx(r[0], "0.1")
    approx(r[1], "0.1")                      # 133.1 / 121 - 1; the return INTO 01-06 (previous mark missing) does not exist
    r2 = EV.daily_returns(nav, D(100), start="2026-01-06")
    assert len(r2) == 1                      # 01-06 has no previous mark, 01-07 does
    r3 = EV.daily_returns(nav, D(100), start=None, end="2026-01-06")
    assert len(r3) == 1                      # only the 01-02 return lies before the end


def test_sample_stats_hand_computed():
    trades = [EV.TradeIn("2026-01-05", D(x)) for x in ("0.02", "-0.01", "0.03", "0.00")]
    s = EV.sample_stats(trades, [D("0.01"), D("0.03")])
    assert s.trades == 4 and s.win_rate == D("0.5")
    approx(s.avg_return, "0.01")
    # sample std = sqrt(0.001 / 3) = 0.0182574186; t = 0.01 / (std / 2)
    approx(s.t_stat, D("0.01") / (D("0.0182574185835055") / 2), "1e-9")
    # daily returns 0.01, 0.03: mean 0.02, sample std sqrt(0.0002) = 0.0141421356; sharpe = mean / std * sqrt(252)
    approx(s.sharpe, D("0.02") / D("0.0141421356237310") * D(252).sqrt(), "1e-7")
    assert s.sessions == 2


def test_missing_statistics_are_none_not_zero():
    s = EV.sample_stats([EV.TradeIn("2026-01-05", D("0.01"))], [D("0.01")])
    assert s.t_stat is None and s.sharpe is None and s.avg_return == D("0.01")      # one trade has no std, one return has no std
    e = EV.sample_stats([], [])
    assert e.trades == 0 and e.avg_return is None and e.win_rate is None


def S(n, avg=None, t=None, sharpe=None):
    f = lambda v: None if v is None else D(str(v))  # noqa: E731
    return EV.Sample(n, None, f(avg), f(t), f(sharpe), 0)


# ---- verdict (rule order of the old judge) ---------------------------------------------------------------------------------------------------------
def test_verdict_rule_order():
    j = EV.judge
    assert j(S(10), S(5), S(5), [], None)[0] == "insufficient"
    assert j(S(40, t=3), S(37), S(3), [], None)[0] == "insufficient"
    assert j(S(40, t=3), S(30, avg=0.01, sharpe=2.0), S(10, avg=0.01, sharpe=0.5), [], None)[0] == "overfit"          # 0.5 < 0.3 * 2.0 = 0.6
    assert j(S(40, t=3), S(30, avg=0.01, sharpe=0.5), S(10, avg=-0.002, sharpe=0.9), [], None)[0] == "overfit"        # sign flip
    eff = (S(40, t=2.5), S(30, avg=0.01, sharpe=0.9), S(10, avg=0.01, sharpe=1.0))
    assert j(*eff, [], None)[0] == "effective"
    assert j(S(40, t=1.9), eff[1], eff[2], [], None)[0] == "weak"
    bucket = EV.RegimeStat("trend", "bull", "牛市", S(20, avg=0.02, t=2.5))
    assert j(S(40, t=1.0), S(30, avg=0.01, sharpe=0.2), S(10, avg=0.005, sharpe=0.2), [bucket], None)[0] == "regime_dependent"
    small = EV.RegimeStat("trend", "bull", "牛市", S(14, avg=0.02, t=2.5))
    assert j(S(40, t=1.0), S(30, avg=0.01, sharpe=0.2), S(10, avg=0.005, sharpe=0.2), [small], None)[0] == "weak"


def test_block_bootstrap_confirms_effective_only_when_reliable():
    eff = (S(40, t=2.5), S(30, avg=0.01, sharpe=0.9), S(10, avg=0.01, sharpe=1.0))
    bad_reliable = EV.BlockInfo(0.20, 12, 5, True, None)
    assert EV.judge(*eff, [], bad_reliable)[0] == "weak"
    bad_unreliable = EV.BlockInfo(0.20, 4, 20, False, "fewer_than_10_blocks_report_only")
    v, why = EV.judge(*eff, [], bad_unreliable)
    assert v == "effective" and "仅供参考" in why                                                                       # E082: fewer than 10 blocks are report-only
    assert EV.judge(*eff, [], EV.BlockInfo(0.01, 12, 5, True, None))[0] == "effective"
    assert EV.judge(*eff, [], EV.BlockInfo(None, 0, 1, False, "no_signals"))[0] == "effective"


# ---- fitness (hand-computed components) ----------------------------------------------------------------------------------------------------------
def test_fitness_components_add_up_and_the_cap_applies():
    full = S(200, avg=0.02, t=3)
    oos = S(60, avg=0.02, sharpe=2)
    recent = S(10, avg=0.01)
    trend = EV.RegimeStat("trend", "bull", "牛市", S(12, avg=0.01))
    score, reasons, missing = EV.fitness(full, oos, recent, [trend], "effective", "bull", "repair")
    # oos 35 * (0.5*1 + 0.5*1) = 35; trend bucket 20 * 0.5 = 10; sentiment falls back to 0.6 * 1 -> 15 * 0.6 = 9; recent 15 * 0.5 = 7.5; t 10; size 5
    assert score == D("76.5") and missing == [] and len(reasons) == 6
    capped, why, _ = EV.fitness(full, oos, recent, [trend], "overfit", "bull", "repair")
    assert capped == D("25.0") and "上限" in why[-1]
    none_regime, _, miss = EV.fitness(full, oos, recent, [trend], "effective", None, None)
    assert "current_trend" in miss and "current_sentiment" in miss                                                       # a missing regime is reported, not guessed


def test_fitness_is_zero_for_a_hopeless_playbook():
    score, _, missing = EV.fitness(S(0), S(0), S(0), [], "insufficient", "bull", "repair")
    assert score == D("0.0") and "oos_sharpe" in missing


# ---- evaluate end to end on a synthetic series --------------------------------------------------------------------------------------------------
def test_evaluate_splits_trades_and_nav_at_the_context_split():
    ds = days(100)
    nav, v = [], D(100)
    for d in ds:
        v = v * D("1.001")
        nav.append((d, v))
    trades = [EV.TradeIn(ds[i], D("0.01") if i % 2 == 0 else D("-0.004"), 5) for i in range(0, 90, 2)]
    split = ds[60]
    ev = EV.evaluate(trades, nav, D(100), split, trend="bull", sentiment="repair")
    assert ev.split_day == split
    assert ev.in_sample.trades == len([t for t in trades if t.entry_day < split]) == 30
    assert ev.out_of_sample is not None and ev.out_of_sample.trades == 15
    assert ev.full.trades == 45
    assert ev.in_sample.sessions == 60 and ev.out_of_sample.sessions == 40
    assert ev.verdict in {"effective", "regime_dependent", "weak", "overfit"} and ev.fitness is not None
    assert ev.recent.sessions == 60 and ev.block is not None
    again = EV.evaluate(trades, nav, D(100), split, trend="bull", sentiment="repair")
    assert again == ev                                                                                                      # deterministic, block bootstrap included


def test_without_a_recorded_split_there_is_no_verdict_and_the_label_says_so():
    ds = days(30)
    nav = [(d, D(100) + i) for i, d in enumerate(ds)]
    ev = EV.evaluate([EV.TradeIn(ds[1], D("0.01"))], nav, D(100), None, trend=None, sentiment=None)
    assert ev.verdict is None and ev.fitness is None and ev.out_of_sample is None and "split_day" in ev.missing
    assert "样本内" in ev.verdict_reason


def test_downsample_keeps_both_ends():
    xs = list(range(100))
    out = EV.downsample(xs, 10)
    assert len(out) == 10 and out[0] == 0 and out[-1] == 99
    assert EV.downsample([1, 2, 3], 10) == [1, 2, 3]


def test_regime_bucket_needs_an_ok_basis():
    tag = {"trend": "bull", "basis": {"trend": "ok", "style": "ok"}, "style": "unknown"}
    assert EV.regime_bucket(tag, "trend") == "bull"
    assert EV.regime_bucket(tag, "style") is None                                         # unmeasured label
    assert EV.regime_bucket({"trend": "bull", "basis": {"trend": "insufficient_history"}}, "trend") is None
    assert EV.regime_bucket(None, "trend") is None


# ---- monthly returns (#35) ----------------------------------------------------------------------------------------------------------------
def test_monthly_returns_hand_computed():
    nav = [("2026-01-02", D(101)), ("2026-01-30", D(102)), ("2026-02-02", None), ("2026-03-02", D("100.98")), ("2026-03-31", D(110))]
    m = T.monthly_returns(nav, D(100))
    assert [(x.year, x.month, x.sessions) for x in m] == [(2026, 1, 2), (2026, 2, 1), (2026, 3, 2)]
    approx(m[0].value, "0.02")                                                                   # 102 / 100 - 1 (the capital before the first session is the base)
    assert m[1].value is None and m[1].reason == "no_nav_in_month"                               # missing is missing, not 0
    approx(m[2].value, D(110) / D(102) - 1)                                                      # March is measured against the last valid NAV before it (January's)


def test_monthly_returns_without_initial_start_from_the_first_valid_nav():
    m = T.monthly_returns([("2026-01-02", D(101)), ("2026-01-30", D(102)), ("2026-02-27", D("103.02"))])
    approx(m[0].value, D(102) / D(101) - 1)
    approx(m[1].value, D("103.02") / D(102) - 1)
    one = T.monthly_returns([("2026-01-30", D(102))])
    assert one[0].value is None and one[0].reason == "single_nav_point"
    assert T.monthly_returns([("2026-01-30", D(1))], D(0))[0].reason == "base_nav_not_positive"


def test_monthly_returns_do_not_change_the_tearsheet():
    nav = [(d, D(100) + i) for i, d in enumerate(days(30))]
    before = T.compute(nav, initial=D(100))
    T.monthly_returns(nav, D(100))
    assert T.compute(nav, initial=D(100)) == before


# ---- style-matched baseline (E062) -------------------------------------------------------------------------------------------------------------
def test_quintiles_by_average_rank():
    assert BL.quintiles([D(x) for x in (10, 20, 30, 40, 50)]) == [1, 2, 3, 4, 5]
    assert BL.quintiles([D(x) for x in range(1, 11)]) == [1, 1, 2, 2, 3, 3, 4, 4, 5, 5]
    assert BL.quintiles([D(5)] * 5) == [3] * 5                                          # all tied: average rank 3 -> ceil(3 * 5 / 5) = 3
    assert BL.quintiles([None, D(3), D(1)]) == [None, 5, 3]                              # among the 2 present: ranks 1, 2 -> ceil(2.5) = 3, ceil(5) = 5
    assert BL.quintiles([None, None]) == [None, None]


def _pool():
    mk = BL.PoolName
    a = [mk(f"a{i}", D(r), 1, 1, "X" if i <= 5 else "Y") for i, r in enumerate(("0.10", "0.02", "0.04", "0.06", "0.08", "0.00"), start=1)]
    b = [mk("b1", D("0.20"), 1, 2), mk("b2", D("0.10"), 1, 2)]
    c = [mk("c1", D("0.50"), 2, 1), mk("c2", D("0.30"), 2, 1)]
    u = [mk("u", D("0.01"), None, None)]
    return a + b + c + u


def test_matched_means_fall_back_to_the_coarser_cell():
    m = BL.matched_means(_pool())
    approx(m["a1"]["all"], D("1.41") / 11)
    approx(m["a1"]["sv"], "0.05")                       # cell (1,1) has 6 names
    approx(m["a1"]["svi"], "0.06")                      # cell (1,1,X) has 5 names: mean of a1..a5
    approx(m["a6"]["svi"], "0.05")                      # cell (1,1,Y) has 1 name -> falls back to sv
    approx(m["b1"]["sv"], "0.075")                      # cell (1,2) has 2 names -> size-1 mean = 0.60 / 8
    approx(m["c1"]["sv"], "0.40")                       # cell (2,1) has 2 names -> size-2 mean
    assert set(m["u"]) == {"all"}                       # an unclassified name has only the whole-pool baseline


def test_matched_excess_hand_computed_and_unmatched_counted():
    r = BL.matched_excess({"2026-01-05": ["a1", "b1", "c1", "u", "zzz"]}, {"2026-01-05": _pool()})
    assert r.n == 3 and r.unmatched == 2 and r.days == 1
    approx(r.ex_sv, (D("0.05") + D("0.125") + D("0.10")) / 3)
    approx(r.ex_svi, (D("0.04") + D("0.125") + D("0.10")) / 3)
    approx(r.ex_all, (D("0.80") - 3 * D("1.41") / 11) / 3)
    assert r.by_day[0].k == 3
    empty = BL.matched_excess({"2026-01-05": ["zzz"]}, {"2026-01-05": _pool()})
    assert empty.n == 0 and empty.ex_sv is None                                          # nothing matched: missing, never 0


def test_survival_rule_needs_every_figure():
    assert BL.survives(D("0.01"), 0.04, D("0")) is True
    assert BL.survives(D("0.01"), 0.05, D("0.01")) is False                                # p must be below 0.05
    assert BL.survives(D("0.01"), 0.01, D("-0.001")) is False                              # DEV excess must be >= 0
    assert BL.survives(None, 0.01, D("0.01")) is False and BL.survives(D("0.01"), None, D("0.01")) is False


# ---- block bootstrap (E082) ---------------------------------------------------------------------------------------------------------------------
def test_moving_block_indices_hand_computed():
    starts = np.array([[0, 3], [2, 1]])
    idx = BS.moving_block_indices(starts, 2, 3)
    assert idx.tolist() == [[0, 1, 3], [2, 3, 1]]                                         # runs [0,1][3,4] and [2,3][1,2], each row cut to 3 slots
    with pytest.raises(ValueError):
        BS.moving_block_indices(starts, 0, 3)


def test_block_bootstrap_extremes_and_reliability():
    ds = days(40)
    pos = BS.block_bootstrap_p(ds, ds, [D("0.01")] * 40, [1] * 40, block=4, seed=1)
    assert pos.p_value == 0.0 and pos.blocks == 10 and pos.reliable and pos.valid_draws == pos.draws == BS.N_BOOT
    neg = BS.block_bootstrap_p(ds, ds, [D("-0.01")] * 40, [1] * 40, block=4, seed=1)
    assert neg.p_value == 1.0
    zero = BS.block_bootstrap_p(ds, ds, [D("0")] * 40, [1] * 40, block=4, seed=1)
    assert zero.p_value == 1.0                                                            # statistic 0 is <= 0
    short = BS.block_bootstrap_p(ds, ds, [D("0.01")] * 40, [1] * 40, block=20, seed=1)
    assert short.blocks == 2 and not short.reliable and short.reason == "fewer_than_10_blocks_report_only" and short.p_value == 0.0
    capped = BS.block_bootstrap_p(ds, ds, [D("0.01")] * 40, [1] * 40, block=100, seed=1)
    assert capped.block == 40 and capped.blocks == 1                                      # block capped at the number of slots


def test_block_bootstrap_missing_cases_are_none_with_a_reason():
    ds = days(20)
    assert BS.block_bootstrap_p([], [], [], [], block=3, seed=1).reason == "no_slots"
    assert BS.block_bootstrap_p(ds, [], [], [], block=3, seed=1).p_value is None
    assert BS.block_bootstrap_p(ds, ds[:2], [D("0.1")] * 2, [0, 0], block=3, seed=1).reason == "no_signals"
    other = BS.block_bootstrap_p(ds, ["1999-01-01"], [D("0.1")], [1], block=3, seed=1)                # a signal day outside the segment is ignored
    assert other.reason == "no_signals"


def test_block_bootstrap_is_deterministic_and_days_without_signal_count_as_slots():
    ds = days(60)
    sig = ds[::3]
    e = [D("0.02") if i % 2 == 0 else D("-0.015") for i in range(len(sig))]
    a = BS.block_bootstrap_p(ds, sig, e, [1] * len(sig), block=5, seed=BS.seed_for(5))
    b = BS.block_bootstrap_p(ds, sig, e, [1] * len(sig), block=5, seed=BS.seed_for(5))
    assert a == b and 0.0 < a.p_value < 1.0 and a.valid_draws == a.draws == BS.N_BOOT
    one = BS.block_bootstrap_p(ds, ds[:1], [D("0.02")], [1], block=2, seed=7)                    # one signal day among 60 slots: draws that miss it are dropped
    assert one.valid_draws < one.draws and one.p_value == 0.0
    assert BS.seed_for(10) == 20261117


def test_day_bootstrap_extremes():
    assert BS.day_bootstrap_p([D("0.01")] * 8, [1] * 8, seed=3) == 0.0
    assert BS.day_bootstrap_p([D("-0.01")] * 8, [1] * 8, seed=3) == 1.0
    assert BS.day_bootstrap_p([], [], seed=3) is None and BS.day_bootstrap_p([D("0.1")], [0], seed=3) is None
