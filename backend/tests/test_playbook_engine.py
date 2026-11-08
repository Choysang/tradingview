"""Playbook engine: backtester rules, statistics, verdicts, IS/OOS split, HTTP smoke.

Panels are built by hand so every rule is checked against a number worked out on
paper. Costs are zeroed except in the test that is about costs.
"""
from __future__ import annotations

from datetime import date, timedelta
import math
import time
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest

from backend.app.playbooks import evaluate as ev
from backend.app.playbooks.backtest import Costs, DEFAULT_COSTS, INITIAL_CASH, simulate
from backend.app.playbooks.base import Playbook
from contracts.schema import (
    ExitRules,
    PerformanceStats,
    PlaybookSpec,
    PlaybookTrade,
    RegimePerformance,
    SizingRule,
)

ZERO = Costs(0, 0, 0)
D0 = date(2026, 1, 5)


def day(i: int) -> date:
    return D0 + timedelta(days=i)


def bar(o, h=None, l=None, c=None, **flags):
    c = o if c is None else c
    return {"open": o, "high": max(o, c) if h is None else h, "low": min(o, c) if l is None else l, "close": c, **flags}


def make_panel(series: dict[str, list[dict | None]]) -> pl.DataFrame:
    """One row per (code, day); a None entry means the code is suspended that day."""
    rows = []
    for code, bars in series.items():
        for i, b in enumerate(bars):
            if b is None:
                continue
            rows.append({
                "code": code, "date": day(i), "atr_14": b.get("atr_14", 1.0),
                "is_limit_down": b.get("is_limit_down", False),
                "open_at_limit": b.get("open_at_limit", False),
                "open_at_floor": b.get("open_at_floor", False),
                **{k: float(b[k]) for k in ("open", "high", "low", "close")},
            })
    return pl.DataFrame(rows)


def make_calendar(n: int) -> list[dict]:
    """A filler code that trades flat so the calendar always has n days."""
    return [bar(100.0)] * n


def sig(*items: tuple[str, int, float]) -> pl.DataFrame:
    return pl.DataFrame({"code": [i[0] for i in items], "date": [day(i[1]) for i in items], "score": [i[2] for i in items]})


EXITS = ExitRules(holding_days=3, stop_loss_pct=None, take_profit_pct=None, trailing_stop_pct=None)
ONE = SizingRule(method="equal", max_positions=1, max_new_per_day=1)


def run(series, signals, exits=EXITS, sizing=ONE, costs=ZERO, **kwargs):
    n = max(len(v) for v in series.values())
    series = {**series, "ZZZ": make_calendar(n)}
    return simulate(make_panel(series), signals, exits, sizing, costs=costs, **kwargs)


# --- entries ----------------------------------------------------------------------

def test_entry_is_next_bar_open():
    series = {"A": [bar(10), bar(11, c=12), bar(12), bar(13), bar(14), bar(15)]}
    trades, _ = run(series, sig(("A", 0, 1.0)))
    assert len(trades) == 1
    t = trades[0]
    assert t.entry_date == day(1).isoformat() and t.entry_price == 11
    # time exit: held == 3 bars after the entry bar -> close of day 4
    assert t.exit_date == day(4).isoformat() and t.exit_price == 14 and t.exit_reason == "time"
    assert t.holding_days == 3
    assert t.return_pct == pytest.approx((14 / 11 - 1) * 100, abs=1e-3)


def test_one_word_open_and_suspension_are_skipped():
    series = {
        "A": [bar(10), bar(11, open_at_limit=True), bar(11), bar(11), bar(11), bar(11)],
        "B": [bar(10), None, bar(10), bar(10), bar(10), bar(10)],
    }
    trades, _ = run(series, sig(("A", 0, 1.0), ("B", 0, 0.5)))
    assert trades == []  # next bar of A is locked at the limit, B has no bar


def test_signal_on_last_bar_cannot_trade():
    series = {"A": [bar(10)] * 4}
    trades, _ = run(series, sig(("A", 3, 1.0)))
    assert trades == []


def test_lot_size_and_equity_identity():
    # 1,000,000 / 33.33 = 30003 shares -> 30000
    series = {"A": [bar(33.33), bar(33.33, c=34.0), bar(34.0), bar(35.0), bar(36.0), bar(36.0)]}
    trades, equity = run(series, sig(("A", 0, 1.0)), exits=ExitRules(holding_days=2, stop_loss_pct=None))
    assert len(trades) == 1
    shares = 30000
    cash = INITIAL_CASH - shares * 33.33
    row = equity.filter(pl.col("date") == day(1)).row(0, named=True)
    nav = (cash + shares * 34.0) / INITIAL_CASH * 100
    assert row["strategy"] == pytest.approx(nav)
    # exposure * equity / close is a whole number of 100-share lots
    held = row["exposure"] * nav / 100 * INITIAL_CASH / 34.0
    assert held == pytest.approx(shares)
    # after the exit at day 3 close (35) everything is cash
    final = equity.filter(pl.col("date") == day(3)).row(0, named=True)
    assert final["strategy"] == pytest.approx((cash + shares * 35.0) / INITIAL_CASH * 100)
    assert final["exposure"] == 0


def test_max_positions_and_new_per_day_limits():
    flat = [bar(10)] * 8
    series = {c: list(flat) for c in "ABC"}
    signals = sig(("A", 0, 3.0), ("B", 0, 2.0), ("C", 0, 1.0))
    trades, _ = run(series, signals, sizing=SizingRule(method="equal", max_positions=2, max_new_per_day=3))
    assert sorted(t.code for t in trades) == ["A", "B"]  # best two scores, third slot missing
    trades, _ = run(series, signals, sizing=SizingRule(method="equal", max_positions=5, max_new_per_day=1))
    assert [t.code for t in trades] == ["A"]


def test_no_exit_on_entry_bar_t_plus_one():
    # the entry bar itself crashes through the stop; the exit may only happen next bar
    series = {"A": [bar(10), bar(10, h=10, l=8, c=9), bar(9, h=9, l=9, c=9), bar(9), bar(9)]}
    trades, _ = run(series, sig(("A", 0, 1.0)), exits=ExitRules(holding_days=5, stop_loss_pct=5))
    t = trades[0]
    assert t.entry_date == day(1).isoformat()
    assert t.exit_date == day(2).isoformat() and t.exit_reason == "stop"
    assert t.exit_price == 9  # open 9 is below the stop price 9.5 -> fill at the open


# --- exits ------------------------------------------------------------------------

def test_stop_fills_at_stop_price_when_no_gap():
    series = {"A": [bar(10), bar(10), bar(10, h=10.2, l=9.0, c=9.2), bar(9)]}
    trades, _ = run(series, sig(("A", 0, 1.0)), exits=ExitRules(holding_days=5, stop_loss_pct=5))
    assert trades[0].exit_reason == "stop" and trades[0].exit_price == pytest.approx(9.5)


def test_take_profit_fills_at_target_or_gap_open():
    series = {"A": [bar(10), bar(10), bar(10, h=11.5, l=9.9, c=11), bar(11)]}
    exits = ExitRules(holding_days=5, stop_loss_pct=None, take_profit_pct=10)
    trades, _ = run(series, sig(("A", 0, 1.0)), exits=exits)
    assert trades[0].exit_reason == "target" and trades[0].exit_price == pytest.approx(11.0)
    gap = {"A": [bar(10), bar(10), bar(12, h=12.5, l=11.8, c=12.2), bar(12)]}
    trades, _ = run(gap, sig(("A", 0, 1.0)), exits=exits)
    assert trades[0].exit_price == 12  # opened above the target: fill at the open


def test_stop_is_checked_before_target_on_the_same_bar():
    series = {"A": [bar(10), bar(10), bar(10, h=12, l=8, c=10), bar(10)]}
    exits = ExitRules(holding_days=5, stop_loss_pct=10, take_profit_pct=10)
    trades, _ = run(series, sig(("A", 0, 1.0)), exits=exits)
    assert trades[0].exit_reason == "stop" and trades[0].exit_price == pytest.approx(9.0)


def test_trailing_stop_exits_next_open():
    closes = [10, 10, 11, 12, 11.2, 11.0, 11.0]
    series = {"A": [bar(c) for c in closes]}
    series["A"][5] = bar(10.8, h=11.0, l=10.7, c=11.0)
    exits = ExitRules(holding_days=20, stop_loss_pct=None, trailing_stop_pct=5)
    trades, _ = run(series, sig(("A", 0, 1.0)), exits=exits)
    t = trades[0]
    # high close 12 -> trigger below 11.4: day 4 closes 11.2, so the exit is day 5's open 10.8
    assert t.exit_reason == "trailing" and t.exit_date == day(5).isoformat() and t.exit_price == pytest.approx(10.8)


def test_limit_down_lock_defers_the_exit():
    locked = bar(9.0, h=9.0, l=9.0, c=9.0, is_limit_down=True, open_at_floor=True)
    series = {"A": [bar(10), bar(10), locked, bar(8.5, h=8.8, l=8.4, c=8.6), bar(8.6)]}
    exits = ExitRules(holding_days=10, stop_loss_pct=5)
    trades, _ = run(series, sig(("A", 0, 1.0)), exits=exits)
    t = trades[0]
    assert t.exit_reason == "stop" and t.exit_date == day(3).isoformat() and t.exit_price == 8.5


def test_open_positions_are_closed_at_the_end():
    series = {"A": [bar(10), bar(10), bar(11), bar(12)]}
    trades, equity = run(series, sig(("A", 0, 1.0)), exits=ExitRules(holding_days=50, stop_loss_pct=None))
    assert trades[0].exit_reason == "end" and trades[0].exit_date == day(3).isoformat()
    assert equity["exposure"][-1] == 0


def test_costs_are_applied_both_ways():
    series = {"A": [bar(10)] * 6}
    trades, equity = run(series, sig(("A", 0, 1.0)), costs=DEFAULT_COSTS)
    buy = (1 + 5e-4) * (1 + 2.5e-4)
    sell = (1 - 5e-4) * (1 - 7.5e-4)
    assert trades[0].return_pct == pytest.approx((sell / buy - 1) * 100, abs=1e-3)
    assert equity["strategy"][-1] < 100


def test_volatility_sizing_favours_the_calmer_stock():
    # A is calm (atr 0.2) and rises 10%; B is wild (atr 2) and stays flat
    calm = [bar(10, atr_14=0.2)] * 2 + [bar(10, c=11, atr_14=0.2)] * 4
    wild = [bar(10, atr_14=2.0)] * 6
    series = {"A": calm, "B": wild}
    signals = sig(("A", 0, 1.0), ("B", 0, 0.9))
    exits = ExitRules(holding_days=4, stop_loss_pct=None)
    equal = SizingRule(method="equal", max_positions=2, max_new_per_day=2)
    vol = SizingRule(method="volatility", max_positions=2, max_new_per_day=2)
    _, eq_equal = run(series, signals, exits=exits, sizing=equal)
    _, eq_vol = run(series, signals, exits=exits, sizing=vol)
    assert eq_vol["strategy"][-1] > eq_equal["strategy"][-1] > 100


def test_benchmark_is_rebased_and_drawdown_is_nonpositive():
    series = {"A": [bar(10), bar(10), bar(9), bar(9), bar(9), bar(9)]}
    bench = pl.DataFrame({"date": [day(i) for i in range(6)], "close": [3000.0, 3030.0, 3060.0, 3000.0, 3000.0, 3300.0]})
    trades, equity = run(series, sig(("A", 0, 1.0)), benchmark=bench)
    assert equity["benchmark"][0] == 100 and equity["benchmark"][-1] == pytest.approx(110)
    assert equity["drawdown"].max() <= 0
    assert equity["drawdown"].min() < 0


def test_start_end_window_clips_the_calendar():
    series = {"A": [bar(10)] * 10}
    _, equity = run(series, sig(("A", 0, 1.0), ("A", 6, 1.0)), start=day(3), end=day(8))
    assert equity["date"][0] == day(3) and equity["date"][-1] == day(8)


def test_simulate_is_fast_on_a_full_market():
    rng = np.random.default_rng(1)
    days, codes = 750, 5000
    n = days * codes
    code_ids = np.repeat(np.arange(codes), days)
    walk = np.cumprod(1 + rng.normal(0, 0.02, n).reshape(codes, days), axis=1).ravel() * 10
    dates = np.tile(np.datetime64(D0) + np.arange(days), codes)
    panel = pl.DataFrame({
        "code": [f"{c:06d}" for c in code_ids], "date": pl.Series(dates).cast(pl.Date),
        "open": walk, "high": walk * 1.01, "low": walk * 0.99, "close": walk,
        "is_limit_down": False, "open_at_limit": False, "open_at_floor": False, "atr_14": walk * 0.02,
    })
    pick = rng.integers(0, n, 20000)
    signals = pl.DataFrame({"code": panel["code"][pick], "date": panel["date"][pick], "score": rng.random(20000)})
    start = time.perf_counter()
    trades, equity = simulate(
        panel, signals, ExitRules(holding_days=10, stop_loss_pct=8, take_profit_pct=15, trailing_stop_pct=6),
        SizingRule(method="volatility", max_positions=10, max_new_per_day=3),
    )
    elapsed = time.perf_counter() - start
    assert trades and equity.height == days
    assert elapsed < 2.0, f"simulate took {elapsed:.2f}s"


# --- statistics ---------------------------------------------------------------------

def trade(ret, entry="2026-01-05", days=3, reason="time"):
    return PlaybookTrade(code="A", entry_date=entry, exit_date=entry, entry_price=1, exit_price=1,
                         return_pct=ret, holding_days=days, exit_reason=reason)


def test_trade_stats_formulas():
    s = ev.stats([trade(r) for r in (4.0, -2.0, 6.0, -1.0)])
    arr = np.array([4.0, -2.0, 6.0, -1.0])
    assert s.trades == 4 and s.win_rate == 50.0
    assert s.avg_return == pytest.approx(1.75)
    assert s.median_return == pytest.approx(1.5)
    assert s.profit_factor == pytest.approx(10 / 3, abs=1e-3)
    assert s.t_stat == pytest.approx(arr.mean() / (arr.std(ddof=1) / 2), abs=1e-3)
    assert s.avg_holding_days == 3


def test_stats_without_trades_is_empty():
    s = ev.stats([])
    assert s.trades == 0 and s.win_rate is None and s.t_stat is None


def test_curve_stats_annual_sharpe_drawdown_exposure():
    nav = [100.0, 101.0, 100.0, 102.0, 101.0, 103.0]
    equity = pl.DataFrame({"date": [day(i) for i in range(6)], "strategy": nav, "exposure": [0.5] * 6})
    s = ev.stats([], equity)
    daily = np.array(nav[1:]) / np.array(nav[:-1]) - 1
    assert s.sharpe == pytest.approx(daily.mean() / daily.std(ddof=1) * math.sqrt(252), abs=1e-2)
    assert s.annual_return == pytest.approx(((103 / 100) ** (252 / 5) - 1) * 100, abs=0.05)
    assert s.max_drawdown == pytest.approx((100 / 101 - 1) * 100, abs=0.01)
    assert s.exposure_pct == 50.0


# --- verdict ------------------------------------------------------------------------

def perf(trades, avg, sharpe=None, t=None):
    return PerformanceStats(trades=trades, avg_return=avg, sharpe=sharpe, t_stat=t)


def bucket(dim, key, n, avg, t):
    return RegimePerformance(dimension=dim, regime=key, label=ev.REGIME_LABELS[dim][key], stats=perf(n, avg, t=t))


def test_verdict_insufficient():
    verdict, reason = ev.judge(perf(12, 1.0, 1, 3), perf(8, 1.0, 1), perf(4, 1.0, 1), [])
    assert verdict == "insufficient" and "12" in reason


def test_verdict_overfit_by_sharpe_and_by_sign():
    verdict, _ = ev.judge(perf(60, 1.0, 2.0, 3), perf(40, 1.5, 2.0), perf(20, 0.4, 0.4), [])
    assert verdict == "overfit"
    verdict, _ = ev.judge(perf(60, 0.5, 0.8, 3), perf(40, 0.9, 0.8), perf(20, -0.2, -0.3), [])
    assert verdict == "overfit"


def test_verdict_effective():
    verdict, reason = ev.judge(perf(80, 1.0, 1.2, 3.1), perf(55, 1.0, 1.2), perf(25, 0.9, 0.9), [])
    assert verdict == "effective" and "0.90" in reason


def test_verdict_regime_dependent_names_the_regime():
    buckets = [bucket("trend", "bull", 40, 2.0, 3.0), bucket("trend", "bear", 40, -1.0, -2.0)]
    verdict, reason = ev.judge(perf(80, 0.3, 0.3, 1.0), perf(55, 0.3, 0.4), perf(25, 0.1, 0.2), buckets)
    assert verdict == "regime_dependent" and "牛市" in reason


def test_verdict_weak():
    buckets = [bucket("trend", "bull", 10, 2.0, 3.0)]  # too few trades in the good bucket
    verdict, _ = ev.judge(perf(80, 0.1, 0.1, 0.5), perf(55, 0.1, 0.2), perf(25, 0.1, 0.1), buckets)
    assert verdict == "weak"


def test_fitness_prefers_the_current_regime_and_explains_itself():
    good = [bucket("trend", "bull", 30, 2.0, 3.0), bucket("sentiment", "climax", 30, 2.0, 3.0)]
    bad = [bucket("trend", "bull", 30, -1.0, -2.0), bucket("sentiment", "climax", 30, -1.0, -2.0)]
    full, oos, recent = perf(100, 1.0, 1.0, 3.0), perf(30, 1.0, 1.0, 2.0), perf(10, 1.0)
    high, reasons = ev.fitness(full, oos, recent, good, "effective", "bull", "climax")
    low, _ = ev.fitness(full, oos, recent, bad, "effective", "bull", "climax")
    assert 0 <= low < high <= 100
    assert len(reasons) >= 6 and any("样本外" in r for r in reasons) and any("t 值" in r for r in reasons)
    # a thin regime bucket falls back with a penalty and says so
    _, thin = ev.fitness(full, oos, recent, [bucket("trend", "bull", 3, 5.0, 2.0)], "effective", "bull", "climax")
    assert any("回退" in r for r in thin)
    capped, _ = ev.fitness(full, oos, recent, good, "overfit", "bull", "climax")
    assert capped <= 25


# --- IS / OOS split on a dummy playbook ----------------------------------------------

def dummy_playbook(every=5, requires=()):
    spec = PlaybookSpec(
        id="dummy", name="哑战法", style="trend", horizon="swing", summary="s", rule="r", source="test",
        exits=ExitRules(holding_days=2, stop_loss_pct=None), sizing=SizingRule(method="equal", max_positions=3, max_new_per_day=1),
    )

    def signal(panel: pl.LazyFrame, params):
        return (
            panel.with_columns(pl.int_range(pl.len()).over("code").alias("_i"))
            .filter(pl.col("_i") % every == 0)
            .select("code", "date", pl.lit(1.0).alias("score"))
        )

    return Playbook(spec=spec, signal=signal, requires_history=tuple(requires))


def dummy_context(n_days=101, requires_col=None) -> ev.Context:
    rng = np.random.default_rng(7)
    frames = []
    for code in ("A", "B"):
        close = 10 * np.cumprod(1 + rng.normal(0.002, 0.01, n_days))
        frames.append(pl.DataFrame({
            "code": code, "date": [day(i) for i in range(n_days)], "open": close, "high": close * 1.005,
            "low": close * 0.995, "close": close, "atr_14": close * 0.02, "is_limit_down": False,
            "open_at_limit": False, "open_at_floor": False, "name": f"名{code}",
            **({requires_col: 15.0} if requires_col else {}),
        }))
    panel = pl.concat(frames)
    regimes = {day(i).isoformat(): {"trend": "bull" if i < 50 else "bear", "liquidity": "steady",
                                     "sentiment": "repair", "style": "balanced"} for i in range(n_days)}
    wh = SimpleNamespace(dataset="sim", root="x")
    return ev.Context(wh=wh, version="v", panel=panel, dates=[day(i) for i in range(n_days)], regimes=regimes,
                      benchmark=None, names={"A": "名A", "B": "名B"})


def test_split_boundary_and_regime_attribution():
    ctx = dummy_context()
    pb = dummy_playbook()
    result = ev._compute(pb, ctx, None, pb.spec.exits, pb.spec.sizing, "bear", "repair")
    split = day(69).isoformat()  # int(101 * 0.7) = 70 dates are in-sample
    assert result.split_date == split
    # Each half is its own portfolio: the in-sample run closes out at the split, the
    # out-of-sample run starts flat the next day, so no position or P&L straddles it.
    signals = ev.run_signals(pb, ctx)
    args = (ctx.panel, signals, pb.spec.exits, pb.spec.sizing, ctx.benchmark)
    is_trades, is_equity = ev.simulate(*args, end=day(69))
    oos_trades, oos_equity = ev.simulate(*args, start=day(70))
    assert all(t.exit_date <= split for t in is_trades) and all(t.entry_date > split for t in oos_trades)
    assert result.in_sample == ev.stats(is_trades, is_equity)
    assert result.out_of_sample == ev.stats(oos_trades, oos_equity)
    assert result.in_sample.trades > 0 and result.out_of_sample.trades > 0
    entries = ev.simulate(*args)[0]
    # regime attribution by entry date: bull before day 50, bear after
    by_trend = {r.regime: r.stats.trades for r in result.by_regime if r.dimension == "trend"}
    assert by_trend["bull"] == sum(t.entry_date < day(50).isoformat() for t in entries)
    assert by_trend["bull"] + by_trend["bear"] == result.full.trades
    labels = {r.label for r in result.by_regime if r.dimension == "trend"}
    assert labels <= {"牛市", "熊市", "震荡"}
    assert len(result.equity) <= 400 and len(result.equity_sparkline) <= 60
    assert all(t.name for t in result.trades) and len(result.trades) <= 80
    assert result.signals_today == 2  # index 100 is a multiple of 5: both codes fire on the last day


def test_snapshot_only_columns_make_a_playbook_unavailable():
    ctx = dummy_context(requires_col="pe_ttm")
    pb = dummy_playbook(requires=("pe_ttm",))
    result = ev._compute(pb, ctx, None, pb.spec.exits, pb.spec.sizing, "bull", "repair")
    assert result.verdict == "unavailable" and result.fitness is None and result.trades == []
    assert result.signals_today == 2  # still ranks today's names
    assert result.full.trades == 0


def test_time_varying_required_column_is_testable():
    ctx = dummy_context()
    ctx.panel = ctx.panel.with_columns((pl.col("close") * 2).alias("pe_ttm"))  # varies over time
    pb = dummy_playbook(requires=("pe_ttm",))
    assert ev._requires_history_missing(ctx.panel, ("pe_ttm",)) == []


# --- HTTP smoke on the SIM dataset ------------------------------------------------------

def test_playbook_api_smoke():
    pytest.importorskip("backend.app.playbooks.library")
    pytest.importorskip("backend.app.playbooks.regime")
    from fastapi.testclient import TestClient

    from backend.app.main import app

    client = TestClient(app)
    board = client.get("/api/v1/playbooks")
    assert board.status_code == 200
    body = board.json()
    assert body["playbooks"] and body["provenance"]["mode"] in ("SIM", "LIVE")
    first = body["playbooks"][0]["spec"]["id"]
    detail = client.get(f"/api/v1/playbooks/{first}")
    assert detail.status_code == 200 and detail.json()["assumptions"]
    assert client.get("/api/v1/playbooks/nope").status_code == 404
    run_response = client.post(f"/api/v1/playbooks/{first}/run", json={"exits": {"holding_days": 3, "stop_loss_pct": 5}})
    assert run_response.status_code == 200
    assert run_response.json()["spec"]["exits"]["holding_days"] == 3
