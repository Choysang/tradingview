"""Pure parts of the M5-E01 reconciliation tool: rank correlation and top-K overlap."""
from __future__ import annotations

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("reconcile_m5", Path(__file__).resolve().parents[2] / "tools" / "reconcile_m5.py")
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)


def test_spearman_identical_and_reversed():
    a = ["a", "b", "c", "d"]
    assert R._spearman(a, a) == 1.0
    assert R._spearman(a, list(reversed(a))) == -1.0
    assert R._spearman(a, ["x", "y"]) is None


def test_signals_overlap_counts_only_the_top_k():
    out = R.signals_section(["a", "b", "c", "d"], {"20td": ["b", "z", "a", "c"]}, 2)
    assert out["20td"]["overlap"] == 1 and out["20td"]["overlap_codes"] == ["b"]
    assert out["20td"]["common"] == 3


def test_old_st_heuristic_counts_exact_5pct_limit_closes():
    import polars as pl

    closes = [10.0, 10.5, 10.6, 10.07]           # +5% at the high, small move, -5% at the low
    g = pl.DataFrame({"close": closes, "high": [10.0, 10.5, 10.7, 10.2], "low": [10.0, 10.3, 10.5, 10.07]})
    assert R.old_st_like(g) is True
    assert R.old_st_like(g.head(2)) is False
