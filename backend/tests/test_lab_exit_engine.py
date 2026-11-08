"""Exit engine (scripts/lab/exit_tails.py) and gates (scripts/lab/sameday_base.py) of E058 / E060."""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "lab"))

import exit_tails as X  # noqa: E402
import sameday_base as B  # noqa: E402


def _series(opens, highs, lows, closes, ma10=None) -> X.Series:
    n = len(opens)
    d0 = date(2024, 1, 1)
    ma = [float(x) for x in ma10] if ma10 is not None else [0.0] * n
    f = lambda xs: [float(x) for x in xs]  # noqa: E731
    opens, highs, lows, closes = f(opens), f(highs), f(lows), f(closes)
    return X.Series(pl.DataFrame({"code": ["600000"] * n, "date": [d0 + timedelta(days=i) for i in range(n)],
                                  "open": opens, "high": highs, "low": lows, "close": closes,
                                  "ma5": ma, "ma10": ma}))


def test_fixed_exit_takes_close_of_last_session() -> None:
    s = _series([10, 10, 10], [11, 11, 11], [9.5, 9.5, 9.5], [10, 10.5, 11])
    g, n, open_end = X.run_exit(s, 0, 3, 9.0, "fixed_2")
    assert g == pytest.approx(5.0) and n == 2 and not open_end


def test_stop_fills_at_open_when_gapping_below() -> None:
    s = _series([10, 8.5], [10.2, 8.6], [9.8, 8.0], [10, 8.2])
    g, n, _ = X.run_exit(s, 0, 2, 9.0, "trail_ma10")
    assert g == pytest.approx(-15.0) and n == 2


def test_trail_has_cap_before_profit_and_none_after() -> None:
    k = 25
    flat = _series([10.0] * k, [10.05] * k, [9.95] * k, [9.99] * k)
    g, n, _ = X.run_exit(flat, 0, k, 9.0, "trail_ma10")
    assert n == X.CAP
    up = _series([10.0] * k, [10.6] * k, [9.95] * k, [10.5] * k, ma10=[10.0] * k)
    g, n, open_end = X.run_exit(up, 0, k, 9.0, "trail_ma10")
    assert open_end and n == k  # never closed below MA10, no cap after the first profitable close


def test_david_sells_half_at_tp1_then_breakeven() -> None:
    # entry 10, stop 9 -> R 1, TP1 11.5; day 1 reaches 11.6, day 2 falls to 9.9 (below entry)
    s = _series([10, 11.0, 10.5], [10.2, 11.6, 10.6], [9.8, 10.9, 9.9], [10.1, 11.4, 10.0], ma10=[0, 0, 0])
    g, n, _ = X.run_exit(s, 0, 3, 9.0, "david_ma10")
    assert g == pytest.approx(((0.5 * 11.5 + 0.5 * 10.0) / 10 - 1) * 100)
    assert n == 3


def test_verdict_gates() -> None:
    rows = [{"period": "PRE", "hold": 20, "n": 150, "excess": 0.5}, {"period": "DEV", "hold": 20, "n": 30, "excess": 0.2}]
    v = B.verdict(rows)
    assert v["too_few"] and not v["standalone_ok"] and not v["structural_fail"]
    rows = [{"period": "PRE", "hold": 20, "n": 900, "excess": -0.1}, {"period": "DEV", "hold": 20, "n": 90, "excess": -0.3}]
    v = B.verdict(rows)
    assert v["structural_fail"] and not v["standalone_ok"]


def test_mc_baseline_matches_pool_mean() -> None:
    rng = np.random.default_rng(0)
    d1, d2 = date(2024, 1, 2), date(2024, 1, 3)
    pools = {d1: np.arange(100, dtype=float), d2: np.full(50, 7.0)}
    mc = B._mc(pools, {d1: 10, d2: 10}, rng)
    assert mc.shape == (B.N_MC,)
    assert mc.mean() == pytest.approx((49.5 + 7.0) / 2, abs=0.5)


def _out(rows):
    return pl.DataFrame(rows, schema=["code", "date", "in_pool", "period", "size5", "vol5", "industry", "r10"], orient="row")


def test_matched_excess_uses_style_cell_and_falls_back() -> None:
    d = date(2024, 3, 1)
    rows = []
    # cell (1,1): six names, one signal at 5.0, others 1.0 -> cell mean (5 + 5*1)/6
    for i in range(6):
        rows.append((f"60000{i}", d, True, "PRE", 1, 1, "A", 5.0 if i == 0 else 1.0))
    # cell (5,5): two names only -> falls back to size5 == 5 (these two plus four more at vol 4)
    rows.append(("600100", d, True, "PRE", 5, 5, "B", 9.0))
    rows.append(("600101", d, True, "PRE", 5, 5, "B", 3.0))
    for i in range(4):
        rows.append((f"60020{i}", d, True, "PRE", 5, 4, "B", 0.0))
    out = _out(rows)
    sig = pl.DataFrame({"code": ["600000", "600100"], "date": [d, d]})
    r = next(x for x in B.evaluate_matched(sig, out, "t", holds=(10,)) if x["period"] == "PRE")
    all_mean = sum(x[-1] for x in rows) / len(rows)
    sv = ((5.0 - 10.0 / 6) + (9.0 - 12.0 / 6)) / 2
    assert r["ex_all"] == pytest.approx(round((5.0 + 9.0) / 2 - all_mean, 3))
    assert r["ex_sv"] == pytest.approx(round(sv, 3))
    assert r["ex_svi"] == pytest.approx(round(sv, 3))  # industry cells are no finer here
