"""PEX04 domain (analogs, interval statistics) and the BOLL / KDJ shared kernels."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal as D

import pytest

from pulse import contracts
from pulse.domain import features as F
from pulse.domain.analogs import analog as AN
from pulse.domain.analogs import interval as IV
from pulse.domain.features import indicators as ind
from pulse.domain.features.definitions import Series
from pulse.domain.playbooks.builder import RawSeries, build_views

from .synth import panel


def test_boll_matches_hand_values_and_population_std():
    c = [float(x) for x in range(1, 21)]
    b = ind.boll(c, 20, 2.0)
    assert b["mid"][19] == pytest.approx(10.5)
    sd = (sum((x - 10.5) ** 2 for x in c) / 20) ** 0.5
    assert b["upper"][19] == pytest.approx(10.5 + 2 * sd) and b["lower"][19] == pytest.approx(10.5 - 2 * sd)
    assert b["mid"][18] is None                                 # warm-up is missing, not 0
    c2 = c[:]
    c2[5] = None
    assert ind.boll(c2, 20)["mid"][19] is None                  # a missing input inside the window -> missing


def test_kdj_seed_recursion_and_reset_after_gap():
    high = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
    low = [h - 2 for h in high]
    close = [h - 1 for h in high]
    k = ind.kdj(high, low, close, 9)
    rsv = (close[8] - min(low[:9])) / (max(high[:9]) - min(low[:9])) * 100
    assert k["k"][8] == pytest.approx(2 / 3 * 50 + rsv / 3)
    assert k["d"][8] == pytest.approx(2 / 3 * 50 + k["k"][8] / 3)
    assert k["j"][8] == pytest.approx(3 * k["k"][8] - 2 * k["d"][8])
    assert k["k"][7] is None
    gap = close[:]
    gap[9] = None
    kg = ind.kdj(high, low, gap, 9)
    assert kg["k"][9] is None


def test_new_kernels_are_registered_and_keys_pass_the_contract():
    for fid in ("boll", "kdj", "roc", "hhv", "llv", "ret_std", "up_streak", "limit_streak", "limit_count"):
        assert fid in F.DEFINITIONS
    vals = tuple(float(i) for i in range(1, 31))
    s = Series(("2026-01-01",) * 30, vals, vals, vals, vals, vals)
    out = F.compute("boll", s)
    assert set(out) == {"mid", "upper", "lower"} and all(len(v) == 30 for v in out.values())
    key = F.make_key("kdj", None, instrument_ids=("00000000-0000-0000-0000-000000000001",), timeframe="1d", adjustment_mode="forward_adjusted",
                     snapshot_id="00000000-0000-0000-0000-000000000002", decision_as_of=datetime(2026, 1, 1, tzinfo=timezone.utc),
                     availability_policy_version="p", session_policy_version="s", input_revision="r", input_schema_version="v",
                     universe_policy_version="u", calendar_version="c", adjustment_version="a", market_rules_version="m")
    contracts.check("FeatureKey", key.to_wire())
    with pytest.raises(ValueError):
        F.compute("kdj", s, {"nope": 1})                        # unknown parameters are errors (no silent defaults)


def test_limit_streak_kernel_counts_consecutive_limit_closes():
    c = [10, 11, 12.1, 13.31, 13.0, 14.3]
    assert ind.limit_streak(c, 10.0)[:4] == [None, 1.0, 2.0, 3.0]
    assert ind.limit_streak(c, 20.0)[3] == 0.0                  # the same prices are NOT limit-ups on a 20% board


# ---- analogs ------------------------------------------------------------------------------------------------------
def test_adding_future_bars_does_not_change_old_neighbours_and_immature_outcomes_are_none():
    days, raws, views = panel(30, 360, caps=False)
    cut = 300
    d_cut = days[cut - 1]
    short = build_views([RawSeries(r.instrument_id, r.board_code, r.dates[:cut], r.open[:cut], r.high[:cut], r.low[:cut], r.close[:cut],
                                   r.volume[:cut]) for r in raws], days[:cut])
    old = AN.find_neighbors(short, short[0], cut - 1, horizon=10, decision_date=d_cut)
    new = AN.find_neighbors(views, views[0], cut - 1, horizon=10, decision_date=d_cut)           # same study date, 60 more bars exist
    assert old and [(n.instrument_id, n.case_date) for n in old] == [(n.instrument_id, n.case_date) for n in new]
    assert all(n.forward_return is not None and n.outcome_end <= d_cut for n in new)             # matured policy: outcomes are known
    allp = AN.find_neighbors(views, views[0], cut - 1, horizon=10, decision_date=d_cut, policy=AN.POLICY_ALL_PRIOR)
    immature = [n for n in allp if n.outcome_end > d_cut]
    assert immature and all(n.forward_return is None for n in immature)                          # not yet due -> None, never 0
    assert all(n.case_date < d_cut for n in allp)


def test_selection_ignores_outcomes_and_dedups_same_stock_cases():
    days, raws, views = panel(25, 330, caps=False)
    nb = AN.find_neighbors(views, views[0], 300, horizon=10, decision_date=days[300])
    by: dict[str, list[int]] = {}
    for n in nb:
        by.setdefault(n.instrument_id, []).append(n.index)
    assert all(abs(a - b) >= 10 for xs in by.values() for i, a in enumerate(xs) for b in xs[i + 1:])
    assert all(not (n.instrument_id == views[0].instrument_id and n.index > 300 - AN.SAME_CODE_MIN_AGE) for n in nb)
    dist = AN.distribution(nb)
    assert dist["known_outcomes"] == len(nb) and 0 <= dist["up_frequency"] <= 1               # empirical frequency, not a calibrated probability
    with pytest.raises(ValueError):
        AN.find_neighbors(views, views[0], 300, horizon=10, decision_date=days[300], policy="best-outcome")


# ---- interval statistics -------------------------------------------------------------------------------------------
def _bars():
    cl = [D(x) for x in ("10", "11", "10.5", "12", "9", "9.9")]
    return [IV.Bar(date(2026, 3, 2 + i), c - D("0.2"), c + D("0.3"), c - D("0.4"), c, D(1000 * (i + 1))) for i, c in enumerate(cl)]


def test_interval_metrics_hand_calculated_and_missing_has_a_reason():
    bars = _bars()
    m = {x["name"]: x for x in IV.compute(bars, date(2026, 3, 3), date(2026, 3, 8))}
    assert m["period_return"]["value"] == (D("9.9") / D("10") - 1).quantize(D("0.000000000001"))   # base = last close BEFORE the window
    assert m["max_drawdown"]["value"] == (D("9") / D("12") - 1).quantize(D("0.000000000001"))
    assert m["sessions"]["value"] == D(5) and m["avg_volume_shares"]["value"] == D(4000)
    first = {x["name"]: x for x in IV.compute(bars, date(2026, 3, 2), date(2026, 3, 8))}
    assert first["period_return"]["value"] is None and first["period_return"]["missing_reason"] == "no_base_bar_before_window"
    assert first["period_return_from_first_open"]["value"] is not None
    empty = IV.compute(bars, date(2030, 1, 1), date(2030, 2, 1))
    assert all(x["value"] is None and x["missing_reason"] for x in empty)             # never 0
