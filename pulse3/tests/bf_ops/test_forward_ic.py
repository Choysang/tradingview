"""Pure statistics of the forward monitor: hand computations, ties, pending, missing is not zero."""
from __future__ import annotations

from datetime import date
from decimal import Decimal as D

from pulse.domain.research import forward_ic as F


def test_spearman_equals_the_textbook_formula_without_ties():
    # ranks of y: 0.1->1, 0.2->3, 0.15->2, 0.3->5, 0.25->4 ; d = 0,-1,1,-1,1 ; rho = 1 - 6*4/(5*24) = 0.8
    assert F.spearman([1, 2, 3, 4, 5], [D("0.1"), D("0.2"), D("0.15"), D("0.3"), D("0.25")]) == D("0.800000")


def test_spearman_ties_use_average_ranks_and_constant_series_is_none_not_zero():
    # x = 1,2,3,4 ; y = 1,1,2,2 -> ranks y 1.5,1.5,3.5,3.5 ; Pearson(rank) = 0.894427
    assert F.spearman([1, 2, 3, 4], [D(1), D(1), D(2), D(2)]) == D("0.894427")
    assert F.spearman([1, 2, 3, 4], [D(5)] * 4) is None
    assert F.spearman([1, 2], [D(1), D(2)]) is None


def test_matured_window_is_pending_until_the_exit_session_exists():
    cal = [date(2026, 10, d) for d in (5, 6, 7, 8, 9, 12, 13)]
    assert F.matured_window(cal, date(2026, 10, 5), 5) == (date(2026, 10, 6), date(2026, 10, 12))
    assert F.matured_window(cal, date(2026, 10, 7), 5) == "HORIZON_NOT_REACHED"
    assert F.matured_window(cal, date(2026, 9, 1), 5) == "DECISION_SESSION_NOT_OBSERVED"
    assert F.matured_window(cal, None, 5) == "DECISION_SESSION_NOT_OBSERVED"
    assert F.sessions_after(cal, date(2026, 10, 8)) == 3


def test_evaluate_ic_top_decile_and_missing_returns_are_not_zero():
    ranked = [(f"i{k}", k) for k in range(1, 11)]
    ret = {f"i{k}": D(v) for k, v in zip(range(1, 11), ["0.10", "0.02", "0.08", "-0.01", "0.06", "0.04", "0.00", "-0.03", "0.03", "0.01"])}
    got = F.evaluate(ranked, ret)
    # sorted returns: -0.03 -0.01 0 0.01 0.02 0.03 0.04 0.06 0.08 0.10 -> median (0.02+0.03)/2 = 0.025
    assert got["universe_median_gross"] == D("0.025000")
    assert got["top_n"] == 1 and got["top_evaluable"] == 1                      # ceil(10 * 0.1) = 1: instrument i1 (0.10)
    assert got["top_decile_mean_gross"] == D("0.100000") and got["top_decile_excess"] == D("0.075000")
    # return ranks (asc) of i1..i10: 10,5,9,2,8,7,3,1,6,4 ; score ranks (better alpha rank = higher): 10,9,8,7,6,5,4,3,2,1
    # d = 0,4,-1,5,-2,-2,1,2,-4,-3 ; sum d^2 = 0+16+1+25+4+4+1+4+16+9 = 80 ; rho = 1 - 6*80/(10*99) = 1 - 480/990 = 0.515152
    assert got["rank_ic"] == D("0.515152") and got["n_evaluable"] == 10 and got["missing_reason"] is None
    missing = dict(ret, i1=None, i2=None, i3=None, i4=None, i5=None, i6=None)
    few = F.evaluate(ranked, missing)
    assert few["rank_ic"] is None and few["top_decile_excess"] is None and few["missing_reason"] == "TOO_FEW_EVALUABLE" and few["n_evaluable"] == 4
    top_missing = F.evaluate(ranked, dict(ret, i1=None))
    assert top_missing["top_evaluable"] == 0 and top_missing["top_decile_excess"] is None and top_missing["missing_reason"] == "NOT_COMPUTABLE"
