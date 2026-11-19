"""Tear sheet (LEGACY_TRIAGE #160 / #161): hand-computed fixtures. Every expected number below is worked out in the test's docstring
from the series, not copied from the function's output."""
from __future__ import annotations

from decimal import Decimal as D

import pytest

from pulse.domain.lab import tearsheet as T

N = lambda *xs: [(f"d{i + 1:02d}", None if x is None else D(x)) for i, x in enumerate(xs)]  # noqa: E731


def val(ts: T.TearSheet, name: str) -> D | None:
    return ts.metrics[name].value


def close(a: D | None, b: D) -> bool:
    """Equal to 1e-25: the function works at precision 50, the test's own arithmetic at the default 28 digits."""
    return a is not None and abs(a - b) < D("1e-25")


def test_hand_fixture_with_four_sessions_and_four_periods_a_year():
    """Initial 100, NAV 102, 99.96, 101.9592, 103.998384, periods_per_year = 4 so that annualising is exact.
    Returns by hand: 102/100-1 = 0.02; 99.96/102-1 = -0.02; 101.9592/99.96-1 = 0.02; 103.998384/101.9592-1 = 0.02.
    mean = 0.04/4 = 0.01. Deviations -> 0.01, -0.03, 0.01, 0.01; squares 0.0001+0.0009+0.0001+0.0001 = 0.0012; sample variance 0.0012/3 = 0.0004,
    sample std 0.02. Annualised volatility = 0.02*sqrt(4) = 0.04. Sharpe = 0.01/0.02*2 = 1.
    Downside: only -0.02 is below 0 -> sum of squares 0.0004, / n(4) = 0.0001, DD = 0.01. Sortino = 0.01/0.01*2 = 2.
    Total return = 103.998384/100-1 = 0.03998384; span 4 sessions, exponent 4/4 = 1 -> annualised = 0.03998384.
    Peak 102 (session d01), trough 99.96 -> max drawdown 99.96/102-1 = -0.02. Calmar = 0.03998384/0.02 = 1.999192.
    The NAV is back above 102 at d04 (d03 = 101.9592 is still below): longest drawdown = d04 - d01 = 3 sessions, recovered."""
    ts = T.compute(N("102", "99.96", "101.9592", "103.998384"), initial=D(100), periods_per_year=4)
    assert val(ts, "total_return") == D("0.03998384")
    assert val(ts, "annualised_return") == D("0.03998384")
    assert val(ts, "annualised_volatility") == D("0.04")
    assert val(ts, "sharpe") == D(1)
    assert val(ts, "sortino") == D(2)
    assert val(ts, "max_drawdown") == D("-0.02")
    assert val(ts, "calmar") == D("1.999192")
    assert val(ts, "longest_drawdown_sessions") == D(3)
    assert ts.longest_drawdown == T.DrawdownSpan(3, "d01", "d04", True)
    assert (ts.sessions, ts.returns, ts.missing_nav_sessions) == (4, 4, 0)


def test_default_252_sessions_a_year_scales_by_the_square_root():
    """Same series, 252 periods a year: the daily figures are unchanged, volatility and the ratios are scaled by sqrt(252).
    Volatility 0.02*sqrt(252); Sharpe 0.5*sqrt(252); Sortino 1*sqrt(252); annualised return (1.03998384)^(252/4) - 1 (a 63rd power)."""
    ts = T.compute(N("102", "99.96", "101.9592", "103.998384"), initial=D(100))
    root = D(252).sqrt()
    assert ts.periods_per_year == 252
    assert close(val(ts, "annualised_volatility"), D("0.02") * root)
    assert close(val(ts, "sharpe"), D("0.5") * root)
    assert close(val(ts, "sortino"), root)
    assert float(val(ts, "annualised_return")) == pytest.approx(1.03998384 ** 63 - 1, rel=1e-12)


def test_up_only_series_has_no_downside_no_drawdown_and_says_why():
    """Initial 100, NAV 104, 104, 108.16, 108.16, 110.3232: returns 0.04, 0, 0.04, 0, 0.02, mean 0.02, deviations +-0.02,0,+-0.02,0 -> sample
    variance 0.0016/4 = 0.0004, std 0.02 -> Sharpe = sqrt(252) (ppy 252). No return below 0: Sortino missing (no_downside). No drawdown: max
    drawdown 0, Calmar missing (no_drawdown), longest drawdown 0 sessions."""
    ts = T.compute(N("104", "104", "108.16", "108.16", "110.3232"), initial=D(100))
    assert close(val(ts, "sharpe"), D(252).sqrt())
    assert ts.metrics["sortino"] == T.Metric(None, "no_downside")
    assert val(ts, "max_drawdown") == 0 and ts.metrics["calmar"] == T.Metric(None, "no_drawdown")
    assert val(ts, "longest_drawdown_sessions") == 0
    assert val(ts, "total_return") == D("0.103232")


def test_unrecovered_drawdown_is_measured_to_the_last_session_and_flagged():
    """Initial 100, NAV 110, 105, 100, 102: peak 110 at d01, never regained. Longest stretch = d04 - d01 = 3 sessions, not recovered.
    Max drawdown 100/110-1 = -1/11 = -0.090909..."""
    ts = T.compute(N("110", "105", "100", "102"), initial=D(100))
    assert ts.longest_drawdown == T.DrawdownSpan(3, "d01", None, False)
    assert close(val(ts, "max_drawdown"), D(100) / D(110) - 1)


def test_longest_of_two_drawdowns_wins_and_the_initial_capital_can_be_the_peak():
    """Initial 100, NAV 98, 99, 101, 100, 99, 98, 102. First stretch: peak = initial capital (position before d01); d01..d02 below 100;
    d03 = 101 recovers -> 3 sessions from the base to d03 (positions -1 -> 2). Second: peak 101 at d03, d04..d06 below, d07 = 102 recovers
    -> 4 sessions. Longest = 4, peak d03, recovery d07. Max drawdown: 98/101-1 (second) vs 98/100-1 = -0.02 (first): the deeper is -0.0297..."""
    ts = T.compute(N("98", "99", "101", "100", "99", "98", "102"), initial=D(100))
    assert ts.longest_drawdown == T.DrawdownSpan(4, "d03", "d07", True)
    assert close(val(ts, "max_drawdown"), D(98) / D(101) - 1)


def test_without_an_initial_capital_the_first_nav_is_the_base():
    """NAV 100, 110, 99: base 100 (no return for d01). Returns 0.1 and -0.1 (n=2): mean 0, Sharpe 0; total -0.01; span 2 sessions."""
    ts = T.compute(N("100", "110", "99"))
    assert ts.returns == 2 and val(ts, "total_return") == D("-0.01") and val(ts, "sharpe") == 0
    assert float(val(ts, "annualised_return")) == pytest.approx(0.99 ** 126 - 1, rel=1e-12)


def test_a_missing_nav_is_not_bridged_and_not_counted_as_zero():
    """Initial 100, NAV 102, missing, 99.96: only 100->102 is an adjacent pair (one return 0.02); the gap drops two pairs. Total return
    99.96/100-1 = -0.0004 over 3 sessions elapsed. One return: volatility and Sharpe missing (fewer_than_2_returns), Sortino has 1 return."""
    ts = T.compute(N("102", None, "99.96"), initial=D(100))
    assert (ts.returns, ts.missing_nav_sessions, ts.sessions) == (1, 1, 3)
    assert val(ts, "total_return") == D("-0.0004")
    assert ts.metrics["annualised_volatility"] == T.Metric(None, "fewer_than_2_returns") and ts.metrics["sharpe"].value is None
    assert ts.metrics["sortino"] == T.Metric(None, "no_downside")
    assert float(val(ts, "annualised_return")) == pytest.approx(0.9996 ** 84 - 1, rel=1e-12)


def test_degenerate_series_say_why_never_zero():
    assert T.compute([]).metrics["total_return"] == T.Metric(None, "no_nav")
    only = T.compute(N("100"))
    assert only.metrics["total_return"] == T.Metric(None, "single_nav_point") and only.metrics["max_drawdown"].reason == "single_nav_point"
    flat = T.compute(N("100", "100", "100"), initial=D(100))
    assert flat.metrics["sharpe"] == T.Metric(None, "zero_volatility") and val(flat, "total_return") == 0
    wiped = T.compute(N("50", "0"), initial=D(100))
    assert wiped.metrics["annualised_return"] == T.Metric(None, "final_nav_not_positive") and val(wiped, "max_drawdown") == D(-1)
    assert T.compute(N("50"), initial=D(0)).metrics["total_return"].reason == "base_nav_not_positive"
    with pytest.raises(ValueError):
        T.compute(N("1"), periods_per_year=0)


def test_trade_statistics_by_hand():
    """Closed pnl: +300, -100, +200, -100, 0 (break-even); one trade still open. wins 2 (gross 500), losses 2 (gross -200), 1 break-even.
    profit factor 500/200 = 2.5; win rate 2/5 = 0.4 (the break-even counts in the denominator); average win 250, average loss -100,
    payoff 250/100 = 2.5. Returns +0.30 +0.20 / -0.10 -0.10: average win return 0.25, average loss return -0.10."""
    tr = [T.TradeIn("d01", "d02", D(300), D("0.30")), T.TradeIn("d02", "d03", D(-100), D("-0.10")), T.TradeIn("d03", "d04", D(200), D("0.20")),
          T.TradeIn("d04", "d05", D(-100), D("-0.10")), T.TradeIn("d05", "d06", D(0), D(0)), T.TradeIn("d06", None, None)]
    ts = T.compute(N("1", "1", "1", "1", "1", "1"), tr)
    assert (ts.trades_closed, ts.trades_open, ts.trades_breakeven) == (5, 1, 1)
    assert val(ts, "profit_factor") == D("2.5") and val(ts, "win_rate") == D("0.4")
    assert (val(ts, "avg_win"), val(ts, "avg_loss"), val(ts, "payoff_ratio")) == (D(250), D(-100), D("2.5"))
    assert (val(ts, "avg_win_return"), val(ts, "avg_loss_return")) == (D("0.25"), D("-0.10"))


def test_no_losses_and_no_wins_and_no_trades_are_missing_with_a_reason():
    only_wins = T.compute(N("1", "1"), [T.TradeIn("d01", "d02", D(5))])
    assert only_wins.metrics["profit_factor"] == T.Metric(None, "no_losses") and only_wins.metrics["payoff_ratio"].reason == "no_losses"
    assert val(only_wins, "win_rate") == 1 and val(only_wins, "avg_win") == 5
    only_loss = T.compute(N("1", "1"), [T.TradeIn("d01", "d02", D(-5))])
    assert val(only_loss, "profit_factor") == 0 and only_loss.metrics["avg_win"] == T.Metric(None, "no_wins") and val(only_loss, "win_rate") == 0
    empty = T.compute(N("1", "1"), [])
    assert empty.metrics["win_rate"] == T.Metric(None, "no_closed_trades") and empty.metrics["profit_factor"].reason == "no_closed_trades"
    assert val(empty, "exposure") == 0                                 # a real zero: nothing was held
    none = T.compute(N("1", "1"), None)
    assert none.metrics["win_rate"] == T.Metric(None, "trade_list_unavailable") and none.metrics["exposure"].reason == "trade_list_unavailable"
    assert none.trades_closed is None


def test_exposure_counts_sessions_with_a_position_at_the_close():
    """Ten sessions d01..d10. T1 d02->d05 is held at the close of d02, d03, d04 (it sells at the open of d05); T2 d04->d08 is held d04, d05,
    d06, d07; T3 is still open since d09: held d09, d10. Union: d02 d03 d04 d05 d06 d07 d09 d10 = 8 of 10 sessions -> 0.8."""
    tr = [T.TradeIn("d02", "d05", D(1)), T.TradeIn("d04", "d08", D(1)), T.TradeIn("d09", None, None)]
    ts = T.compute(N(*["1"] * 10), tr)
    assert val(ts, "exposure") == D("0.8")


def test_inputs_are_not_mutated():
    nav = N("102", "99.96")
    trades = [T.TradeIn("d01", "d02", D(1))]
    before = (list(nav), list(trades))
    T.compute(nav, trades, initial=D(100))
    assert (nav, trades) == before
