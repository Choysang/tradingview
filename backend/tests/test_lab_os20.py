"""E081 forward test helpers (scripts/lab/os20_forward.py): cell-matched excess, the per-date table, the summary."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "lab"))

import os20_forward as O  # noqa: E402

D = date(2026, 11, 2)


def _out(r10: list[float]) -> pl.DataFrame:
    n = len(r10)
    return pl.DataFrame({"code": [f"c{i}" for i in range(n)], "date": [D] * n, "in_pool": [True] * n, "r10": r10,
                         "size5": [1] * n, "vol5": [1] * n})


def test_daily_excess_against_the_cell_mean() -> None:
    pool = O.cells(_out([1.0, 2.0, 3.0, 4.0, 5.0, 6.0]))
    sig = pl.DataFrame({"code": ["c4", "c5", "c0"], "date": [D] * 3, "group": ["OS20_GAP", "OS20_GAP", "OS20_NOGAP"]})
    d = O.wide(O.daily_excess(sig, pool))
    row = d.row(0, named=True)
    assert row["n_OS20_GAP"] == 2 and row["n_OS20_NOGAP"] == 1
    assert row["ex_OS20_GAP"] == pytest.approx(5.5 - 3.5)
    assert row["raw_OS20_GAP"] == pytest.approx(5.5)
    assert row["ex_OS20_NOGAP"] == pytest.approx(1.0 - 3.5)


def test_small_cell_falls_back_to_the_size_cell() -> None:
    out = _out([1.0, 2.0, 3.0, 4.0, 5.0, 6.0]).with_columns(pl.Series("vol5", [1, 1, 1, 1, 2, 2]))
    pool = O.cells(out)
    m = dict(pool.select("code", "m_sv").iter_rows())
    assert m["c0"] == pytest.approx(3.5)  # the vol cell has 4 names < MIN_CELL: size mean
    assert m["c5"] == pytest.approx(3.5)


def test_summary_and_market_state_split() -> None:
    rows = [{"date": "d1", "mkt20": -2.0, "n_OS20_GAP": 100, "ex_OS20_GAP": 1.0, "ex_OS20_NOGAP": 0.0, "raw_OS20_GAP": 0.5},
            {"date": "d2", "mkt20": -1.0, "n_OS20_GAP": 80, "ex_OS20_GAP": 2.0, "ex_OS20_NOGAP": 1.0, "raw_OS20_GAP": 1.0},
            {"date": "d3", "mkt20": 3.0, "n_OS20_GAP": 60, "ex_OS20_GAP": -1.0, "ex_OS20_NOGAP": None, "raw_OS20_GAP": None}]
    s = O.summarize(rows)
    assert s["dates"] == 3
    assert s["ex_gap"] == pytest.approx(2.0 / 3, abs=1e-3)  # equal weight per date, not per signal
    assert s["diff_gap_nogap"] == pytest.approx(1.0)  # a date missing one group is left out of the difference
    assert s["net_raw_gap"] == pytest.approx(0.75 - O.COST)
    assert s["ex_gap_down"]["dates"] == 2 and s["ex_gap_down"]["mean"] == pytest.approx(1.5)
    assert s["ex_gap_down"]["p"] == 0.0
    assert s["ex_gap_up"] == {"dates": 1, "mean": -1.0, "p": 1.0}
    assert O.summarize([]) == {"dates": 0}
