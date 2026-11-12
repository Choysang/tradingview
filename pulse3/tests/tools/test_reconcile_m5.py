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
