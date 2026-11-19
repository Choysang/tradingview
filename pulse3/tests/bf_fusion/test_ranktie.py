"""Tie order of the old engine's unstable sort (ranktie): exact against torch.argsort fixtures, plus the rank semantics."""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from pulse.domain.fusion import expr as X
from pulse.domain.fusion import ranktie as R

FIXTURE = Path(__file__).parent / "fixtures" / "torch_argsort_cases.json"


def cases():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", cases(), ids=lambda c: f"n{c['n']}-card{c['card']}")
def test_argsort_stl_equals_the_old_engines_torch_argsort(case):
    row = np.array([np.inf if v == "inf" else float(v) for v in case["values"]], dtype=np.float32)
    assert R.argsort_stl(row).tolist() == case["torch_argsort"]


def test_the_fixtures_are_not_all_stable_orders():
    """A stable sort would fail some fixtures: the unstable order is what the port is for."""
    differs = 0
    for c in cases():
        row = np.array([np.inf if v == "inf" else float(v) for v in c["values"]], dtype=np.float32)
        differs += int(not np.array_equal(np.argsort(row, kind="stable"), c["torch_argsort"]))
    assert differs >= 4


def test_distinct_values_have_one_order_whatever_the_algorithm():
    rng = np.random.default_rng(1)
    for n in (5, 33, 50, 400, 3000):
        x = rng.permutation(n).astype(np.float32)
        assert R.argsort_stl(x).tolist() == np.argsort(x).tolist()


def test_rank_rows_uses_column_order_for_small_ties_and_the_stl_order_for_big_ones():
    row = np.array([[5.0, 3.0, 3.0, 9.0, np.nan]], dtype=np.float32)
    r = R.rank_rows(row)[0]
    assert r[:4].tolist() == pytest.approx([2.5 / 4, 0.5 / 4, 1.5 / 4, 3.5 / 4]) and math.isnan(r[4])   # a pair: column order
    rng = np.random.default_rng(2)
    big = rng.integers(0, 3, (1, 200)).astype(np.float32)
    got = R.rank_rows(big)[0]
    order = R.argsort_stl(big[0])
    expect = np.empty(200, dtype=np.float32)
    expect[order] = (np.arange(200) + 0.5) / 200
    assert got.tolist() == pytest.approx(expect.tolist())
    assert not np.array_equal(got, X.cs_rank_stable(big)[0])               # the unstable order differs from the stable one


def test_cs_rank_of_the_engine_is_the_tie_aware_one_and_ranks_are_a_permutation():
    rng = np.random.default_rng(3)
    x = rng.integers(0, 4, (3, 120)).astype(np.float32)
    x[:, ::7] = np.nan
    r = X.cs_rank(x)
    for t in range(3):
        n = int(np.isfinite(x[t]).sum())
        assert sorted(np.round(r[t][np.isfinite(r[t])] * n - 0.5).astype(int).tolist()) == list(range(n))
