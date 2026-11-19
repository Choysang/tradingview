"""Operator semantics of the fusion expression engine, each against a hand-computed case (lane fusion, legacy triage batch 3)."""
from __future__ import annotations

import math

import numpy as np
import pytest

from pulse.domain.fusion import expr as X

nan = float("nan")


def col(*v: float) -> np.ndarray:
    """One stock, several days: a [T, 1] float32 grid."""
    return np.array(v, dtype=np.float32).reshape(-1, 1)


def flat(a: np.ndarray) -> list[float]:
    return [float(v) for v in a[:, 0]]


def same(a: list[float], b: list[float], tol: float = 1e-5) -> None:
    assert len(a) == len(b)
    for x, y in zip(a, b):
        if math.isnan(y):
            assert math.isnan(x), (a, b)
        else:
            assert x == pytest.approx(y, abs=tol), (a, b)


# ---- rolling sums and means ------------------------------------------------------------------------------------------
def test_sum_and_mean_need_three_quarters_of_the_window_finite():
    x = col(1, 2, nan, 4, 5)
    # d=4 needs ceil(3) = 3 finite bars: the window of row 3 is [1, 2, nan, 4]
    same(flat(X.ts_sum(x, 4)), [nan, nan, nan, 7, 11])
    same(flat(X.ts_mean(x, 4)), [nan, nan, nan, 7 / 3, 11 / 3])
    # d=3 needs 3 finite bars: a single gap kills the window
    same(flat(X.ts_sum(x, 3)), [nan, nan, nan, nan, nan])


def test_std_and_var_are_sample_statistics():
    x = col(1, 2, 4, 7)
    same(flat(X.ts_var(x, 4)), [nan, nan, nan, 7.0])           # ((2.5)^2 + (1.5)^2 + (.5)^2 + (3.5)^2) / 3
    same(flat(X.ts_std(x, 4)), [nan, nan, nan, math.sqrt(7.0)])


def test_cov_and_corr_use_the_bars_where_both_exist():
    x, y = col(1, 2, 3, 4, 5), col(2, 4, 6, 8, 10)
    same(flat(X.ts_cov(x, y, 5)), [nan] * 4 + [5.0])           # sum((x-3)(y-6)) / 4 = 20 / 4
    same(flat(X.ts_cov(x, y, 5, corr=True)), [nan] * 4 + [1.0])
    # a bar that is missing in one series is missing in both
    y2 = col(2, 4, nan, 8, 10)
    same(flat(X.ts_cov(x, y2, 4, corr=True))[3:], [1.0, 1.0])


def test_corr_of_a_constant_series_is_missing():
    x, y = col(1, 2, 3, 4), col(5, 5, 5, 5)
    same(flat(X.ts_cov(x, y, 4, corr=True)), [nan] * 4)


# ---- regression -----------------------------------------------------------------------------------------------------
def test_slope_resi_rsquare_of_a_five_bar_window():
    x = col(1, 3, 2, 5, 4)
    # against index 0..4: Sxy = 8, Sxx = 10, Syy = 10 -> slope 0.8, fit at last bar = 3 + 0.8 * 2 = 4.6, R^2 = 64 / 100
    same(flat(X.ts_slope(x, 5))[4:], [0.8])
    same(flat(X.ts_resi(x, 5))[4:], [4 - 4.6])
    same(flat(X.ts_rsquare(x, 5))[4:], [0.64])


def test_regression_needs_a_full_window():
    x = col(1, 3, nan, 5, 4)
    same(flat(X.ts_slope(x, 5)), [nan] * 5)
    same(flat(X.ts_rsquare(col(2, 2, 2, 2), 4)), [nan] * 4)       # a flat window has no R^2


# ---- extremes and positions -----------------------------------------------------------------------------------------
def test_max_min_and_positions_oldest_bar_is_one_and_ties_keep_the_oldest():
    x = col(3, 1, 3, 2)
    same(flat(X.ts_max(x, 3)), [nan, nan, 3, 3])
    same(flat(X.ts_min(x, 3)), [nan, nan, 1, 1])
    same(flat(X.ts_argmax(x, 3)), [nan, nan, 1, 2])             # [3,1,3]: oldest 3 wins; [1,3,2]: the 3 is 2nd
    same(flat(X.ts_argmin(x, 3)), [nan, nan, 2, 1])


def test_extremes_skip_missing_bars_when_enough_remain():
    x = col(5, nan, 1, 4)
    same(flat(X.ts_max(x, 4)), [nan, nan, nan, 5])              # 3 of 4 finite
    same(flat(X.ts_argmin(x, 4)), [nan, nan, nan, 3])


def test_rank_counts_ties_half_and_includes_the_bar_itself():
    x = col(1, 2, 2, 3)
    # [1,2,2] last=2: below 1, equal 2 (itself included) -> (1 + 1) / 3; [2,2,3] last=3: below 2, equal 1 -> 2.5 / 3
    same(flat(X.ts_rank(x, 3)), [nan, nan, 2 / 3, 2.5 / 3])


def test_wma_weights_the_newest_bar_most():
    same(flat(X.ts_wma(col(1, 2, 3), 3)), [nan, nan, (1 * 1 + 2 * 2 + 3 * 3) / 6])


def test_ema_skips_missing_bars_and_keeps_state():
    x = col(1, 2, nan, 4, 5)
    # alpha = 2 / 5; adjust=False: 1, 1.4, (held 1.4), 0.4*4 + 0.6*1.4 = 2.44, 0.4*5 + 0.6*2.44 = 3.464; rows with fewer than 3 finite
    # bars in the 4-bar window are missing
    same(flat(X.ts_ema(x, 4)), [nan, nan, nan, 2.44, 3.464])


def test_quantile_median_and_mad():
    x = col(1, 2, 3, 4)
    same(flat(X.ts_quantile(x, 4, 0.5))[3:], [2.5])
    same(flat(X.ts_quantile(x, 4, 0.2))[3:], [1.6])             # position 0.2 * 3 = 0.6 between 1 and 2
    same(flat(X.ts_median(col(nan, 1, 3, 5), 4))[3:], [3.0])    # three finite bars of four are enough
    same(flat(X.ts_quantile(col(nan, 1, 3, 5), 4, 0.25))[3:], [2.0])
    same(flat(X.ts_mad(col(1, 2, 6), 3))[2:], [2.0])            # mean 3: (2 + 1 + 3) / 3


def test_ref_and_delta_look_back_only():
    x = col(1, 4, 9, 16)
    same(flat(X.shift(x, 1)), [nan, 1, 4, 9])
    same(flat(X.ts_delta(x, 2)), [nan, nan, 8, 12])
    same(flat(X.shift(x, -1)), [4, 9, 16, nan])                  # only the label code reads forward


# ---- elementwise ----------------------------------------------------------------------------------------------------
def ev(text: str, **grids) -> list[float]:
    fields = {k: np.array(v, dtype=np.float32).reshape(-1, 1) for k, v in grids.items()}
    fields.setdefault("close", np.zeros_like(next(iter(fields.values()))))
    return flat(X.Evaluator(fields)(text))


def test_div_by_near_zero_and_inv_are_missing_not_infinite():
    same(ev("$close / $open", close=[1, 2], open=[0, 4]), [nan, 0.5])
    same(ev("Inv($close)", close=[0, 4]), [nan, 0.25])


def test_log_uses_absolute_value_plus_epsilon_and_sign_of_missing_is_zero():
    out = ev("Log($close)", close=[math.e, -math.e, 0])
    same(out[:2], [1.0, 1.0], tol=1e-5)
    assert out[2] == pytest.approx(math.log(1e-12), rel=1e-5)
    same(ev("Sign($close)", close=[-3, 0, nan, 7]), [-1, 0, 0, 1])
    same(ev("Sqrt($close)", close=[-4, 9]), [2, 3])


def test_greater_less_propagate_missing_and_comparisons_are_zero_one():
    same(ev("Greater($close, $open)", close=[1, nan, 3], open=[2, 5, 1]), [2, nan, 3])
    same(ev("Less($close, $open)", close=[1, nan, 3], open=[2, 5, 1]), [1, nan, 1])
    same(ev("$close > $open", close=[1, nan, 3], open=[2, 5, 1]), [0, nan, 1])
    same(ev("$close < $open", close=[1, nan, 3], open=[2, 5, 1]), [1, nan, 0])


def test_cross_section_ops_work_on_each_row_and_ties_rank_in_column_order():
    g = np.array([[10, 30, 20, nan], [5, 5, 5, 5]], dtype=np.float32)
    np.testing.assert_allclose(X.cs_rank(g)[0, :3], [0.5 / 3, 2.5 / 3, 1.5 / 3], rtol=1e-6)
    assert np.isnan(X.cs_rank(g)[0, 3])
    np.testing.assert_allclose(X.cs_rank(g)[1], [0.125, 0.375, 0.625, 0.875])        # equal values: consecutive ranks, column order
    z = X.cs_zscore(np.array([[1, 2, 3, nan]], dtype=np.float32))[0]
    np.testing.assert_allclose(z[:3], [-1 / (1 + 1e-9), 0, 1 / (1 + 1e-9)], atol=1e-6)   # sample std of 1,2,3 is 1
    np.testing.assert_allclose(X.cs_demean(np.array([[1, 2, 6, nan]], dtype=np.float32))[0, :3], [-2, -1, 3])


def test_results_are_float32_and_non_finite_final_values_are_missing():
    out = X.Evaluator({"close": col(1e30, 1e30)})("$close * $close")
    assert out.dtype == np.float32 and np.isnan(out).all()          # float32 overflow is inf -> missing


# ---- parser ---------------------------------------------------------------------------------------------------------
def test_precedence_unary_minus_and_literals():
    same(ev("1 + 2 * 3 - $close / 2", close=[4]), [5.0])
    same(ev("-$close + 1e-12 * 0 - -2", close=[4]), [-2.0])
    same(ev("(1 + 2) * $close", close=[2]), [6.0])
    assert X.canon(X.parse("Sum($close, 5) > 2")) == "gt(Sum($close,5),2.0)"


@pytest.mark.parametrize("bad", ["$nosuch", "Mean($close, 0)", "Mean($close, 251)", "Mean($close, 2.5)", "Std($close, 1)",
                                 "Corr($close, $open, 2)", "Quantile($close, 5, 1.5)", "Foo($close)", "$close $open", "Mean($close)",
                                 "Abs($close", "1 +"])
def test_invalid_expressions_are_refused(bad):
    with pytest.raises(X.ExprError):
        X.parse(bad)


def test_fields_of_and_unknown_characters():
    assert X.fields_of(X.parse("Corr($close, Log($volume+1), 10) + $ret")) == {"close", "volume", "ret"}
    with pytest.raises(X.ExprError):
        X.parse("$close # 2")


def test_a_row_never_reads_a_later_row():
    base = col(1, 2, 3, 4, 5, 6)
    changed = base.copy()
    changed[5, 0] = 99
    for e in ("Mean($close, 3)", "Rank($close, 4)", "Slope($close, 3)", "WMA($close, 3)", "IdxMax($close, 4)", "Corr($close, $close*2, 4)"):
        a = X.Evaluator({"close": base})(e)[:5]
        b = X.Evaluator({"close": changed})(e)[:5]
        np.testing.assert_array_equal(a, b)
