"""IC, expanding ICIR weights and the static40 score on synthetic inputs; frozen data integrity."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, timedelta

import numpy as np
import pytest

from pulse.domain.fusion import expr as X
from pulse.domain.fusion import panel as P
from pulse.domain.fusion import scorer as S


def test_daily_ic_is_spearman_over_the_common_cells_and_needs_100_names():
    rng = np.random.default_rng(3)
    x = rng.normal(size=(3, 150)).astype(np.float32)
    y = (x * 2 + 1).astype(np.float32)
    y[1] = -y[1]
    y[2, :60] = np.nan                        # only 90 common cells: below the minimum of 100
    ic = S.daily_ic(x, y)
    assert ic[0] == pytest.approx(1.0, abs=1e-6) and ic[1] == pytest.approx(-1.0, abs=1e-6) and math.isnan(ic[2])


def test_rank16_is_float16_percentile_over_tradable_names_only():
    x = np.array([[3.0, 1.0, 2.0, 9.0]], dtype=np.float32)
    tr = np.array([[True, True, True, False]])
    r = S.rank16(x, tr)
    assert r.dtype == np.float16
    assert r[0, :3].tolist() == pytest.approx([2.5 / 3, 0.5 / 3, 1.5 / 3], abs=1e-3) and math.isnan(float(r[0, 3]))


def icmat(t: int, base: list[float]) -> np.ndarray:
    return np.tile(np.array(base, dtype=np.float32)[:, None], (1, t))


def test_icir_window_is_days_130_to_t_minus_6_so_a_label_not_complete_is_never_used():
    t = 200
    ic = icmat(t + 1, [0.1, 0.0])
    rng = np.random.default_rng(1)
    ic[0] = (0.1 + rng.normal(0, 0.05, t + 1)).astype(np.float32)
    ic[1] = (0.0 + rng.normal(0, 0.05, t + 1)).astype(np.float32)
    cs, cs2 = S.cumulative(ic)
    base = S.icir_at(cs, cs2, t)
    inside = ic.copy()
    inside[0, t - 6] = 50.0                    # the last complete label: used
    outside = ic.copy()
    outside[0, t - 5] = 50.0                   # label needs the open of day t+1: not used
    before = ic.copy()
    before[0, 129] = 50.0                      # before the first IC day: not used
    assert S.icir_at(*S.cumulative(outside), t)[0] == pytest.approx(base[0], rel=1e-5)
    assert S.icir_at(*S.cumulative(before), t)[0] == pytest.approx(base[0], rel=2e-3)   # float32 prefix sums: equal up to rounding
    assert S.icir_at(*S.cumulative(inside), t)[0] != pytest.approx(base[0], rel=1e-3)
    window = ic[0, 130: t - 5].astype(np.float64)
    assert base[0] == pytest.approx(window.mean() / math.sqrt(max(window.var(), 1e-12)), rel=2e-3)   # population variance, as the rule says


def test_a_missing_ic_counts_as_zero():
    ic = icmat(180, [0.2])
    ic[0, 150:160] = np.nan
    cs, _ = S.cumulative(ic)
    assert cs[0, 160] - cs[0, 150] == 0.0


def test_score_day_is_the_icir_weighted_mean_of_centred_ranks_of_the_top_k():
    icir = np.array([0.5, -1.0, 0.1], dtype=np.float32)
    ranks = np.array([[0.1, 0.9, 0.5, np.nan], [0.8, 0.2, 0.5, 0.5], [0.0, 0.0, 0.0, 0.0]], dtype=np.float16)
    tradable = np.array([True, True, True, False])
    s = S.score_day(icir, ranks, tradable, k=2)
    w = np.array([0.5, -1.0])
    z = ranks[[0, 1]].astype(np.float32) - 0.5
    expected = (z * w[:, None]).sum(0) / 1.5
    assert s[:3] == pytest.approx(expected[:3], abs=1e-4)
    assert math.isnan(s[3])                    # not tradable
    # a missing rank adds nothing but the divisor stays the full weight sum
    ranks2 = ranks.copy()
    ranks2[1, 0] = np.nan
    assert S.score_day(icir, ranks2, tradable, k=2)[0] == pytest.approx((-0.4 * 0.5) / 1.5, abs=1e-4)


def test_top_factors_are_the_largest_absolute_icir():
    assert set(S.top_factors(np.array([0.1, -0.9, 0.5, 0.2], dtype=np.float32), 2).tolist()) == {1, 2}


def synthetic_panel(n: int = 120, t: int = 24) -> P.Panel:
    """Stock j grows by g_j a day, so the 5-day forward return orders the stocks by g; ``$cap`` carries g."""
    g = np.linspace(-0.01, 0.02, n).astype(np.float32)
    o = (10 * (1 + g[None, :]) ** np.arange(t)[:, None]).astype(np.float32)
    f = {k: o.copy() for k in ("open", "high", "low", "close", "prev_close")}
    f["tradable"] = np.ones((t, n), dtype=np.float32)
    f["open_at_limit"] = np.zeros((t, n), dtype=np.float32)
    for k in ("volume", "amount", "vwap", "turn", "ret", "mkt", "pct_chg", "is_limit_up", "is_limit_down", "touched_limit", "is_broken",
              "limit_streak", "cgo", "tk"):
        f[k] = np.zeros((t, n), dtype=np.float32)
    f["cap"] = np.tile(g, (t, 1)).astype(np.float32)
    return P.Panel([date(2024, 1, 1) + timedelta(days=i) for i in range(t)], [f"6{j:05d}" for j in range(n)], f)


def test_run_factors_gives_ic_one_for_a_perfect_factor_and_minus_one_for_its_negative():
    p = synthetic_panel()
    run = S.run_factors(p, [("up", "$cap"), ("down", "Neg($cap)"), ("bad", "$cap +")], tail_start=20)
    assert run.ic[0, :18].tolist() == pytest.approx([1.0] * 18, abs=2e-3)
    assert run.ic[1, :18].tolist() == pytest.approx([-1.0] * 18, abs=2e-3)
    assert np.isnan(run.ic[0, 19:]).all()                        # the last rows have no complete label
    assert "bad" in run.skipped                                    # reported, not scored as zero
    assert run.ranks.shape == (3, 4, 120) and run.tail_rows.tolist() == [20, 21, 22, 23] and run.ranks.dtype == np.float16


def test_frozen_data_matches_its_manifest_and_every_expression_is_valid():
    base = S.DATA
    man = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    for name, digest in man["files"].items():
        assert hashlib.sha256((base / name).read_bytes()).hexdigest() == digest, name
    assert S.data_digest() == man["files"]
    facs = S.load_json("factor_expressions.json")["factors"]
    safe = S.load_json("level_safety.json")["safe"]
    assert len(facs) == 428 and [f["name"] for f in facs] == list(safe) and sum(safe.values()) == 392
    sources = {f["source"] for f in facs}
    assert "zoo" in sources and "alphagen" in sources
    for f in facs:
        node = X.parse(f["expr"])
        assert X.fields_of(node) <= set(X.FIELDS), f["name"]
    assert [n for n, _ in S.frozen_factors()] == [f["name"] for f in facs if safe[f["name"]]]
    assert len(S.frozen_factors(only_level_safe=False)) == 428
    assert set(man["preregistrations"]) == {"main", "h10", "monitor", "x20", "os20"}


def test_reproduction_rule_and_merge_and_asof(tmp_path):
    from pulse.application import bf_fusion_service as svc
    import polars as pl

    def frame(day: date, order: list[str]) -> pl.DataFrame:
        return pl.DataFrame({"code": order, "date": [day] * len(order), "score": [float(len(order) - i) for i in range(len(order))]})

    codes = [f"60{i:04d}" for i in range(60)]
    d = date(2026, 11, 2)
    old = pl.concat([frame(d + timedelta(days=i), codes) for i in range(3)])
    same = old
    assert svc.reproduction(old, same) == {"days": 3, "mean_overlap": 1.0, "min_overlap": 1.0, "old_last": "2026-11-04", "new_last": "2026-11-04"}
    shifted = pl.concat([frame(d + timedelta(days=i), codes[20:] + codes[:20]) for i in range(3)])      # top 40 shares 20 of 40
    assert svc.reproduction(old, shifted)["mean_overlap"] == 0.5
    merged = svc.merge_scores(old, frame(d + timedelta(days=1), codes[::-1]))
    assert merged.filter(pl.col("date") == d).height == 60 and merged["date"].n_unique() == 2
    # as-of: the first computed score of a date is never replaced
    asof = svc.append_asof(pl.DataFrame(schema=svc.ASOF_SCHEMA), frame(date(2026, 10, 30), codes), computed_on=date(2026, 11, 1), run_id="a")
    asof2 = svc.append_asof(asof, pl.concat([frame(date(2026, 10, 30), codes[::-1]), frame(date(2026, 11, 2), codes)]),
                            computed_on=date(2026, 11, 2), run_id="b")
    assert asof2.filter(pl.col("date") == date(2026, 10, 30))["run_id"].unique().to_list() == ["a"]
    assert asof2.filter(pl.col("date") == date(2026, 11, 2))["run_id"].unique().to_list() == ["b"]
    # forward experiments read the archived score and use the current one only for dates not archived
    eff = svc.effective_scores(asof2, pl.concat([frame(date(2026, 10, 30), codes[::-1]), frame(date(2026, 11, 3), codes)]), date(2026, 10, 30))
    assert eff.filter(pl.col("date") == date(2026, 10, 30))["score"].max() == 60.0
    assert eff["date"].n_unique() == 3


def test_level_safety_keeps_ratios_and_drops_price_levels():
    from pulse.domain.fusion import levelsafe as LS

    rng = np.random.default_rng(4)
    t, n = 200, 120
    close = (10 * np.exp(rng.normal(0, 0.3, (1, n)) + np.cumsum(rng.normal(0, 0.01, (t, n)), axis=0))).astype(np.float32)
    f = {k: np.zeros((t, n), dtype=np.float32) for k in X.FIELDS}
    for k in ("open", "high", "low", "close", "vwap", "prev_close"):
        f[k] = close.copy()
    f["amount"] = close * 3
    f["cap"] = np.exp(rng.normal(0, 1.5, (1, n))).astype(np.float32).repeat(t, 0)
    panel = P.Panel([date(2024, 1, 1) + timedelta(days=i) for i in range(t)], [f"6{j:05d}" for j in range(n)], f | {"tradable": np.ones((t, n), np.float32)})
    sc = LS.scaled_fields(panel)
    assert not np.array_equal(sc["close"], f["close"]) and np.array_equal(sc["volume"], f["volume"])
    assert LS.level_safe("$close / Ref($close, 5)", panel, sc)[0]                 # a ratio of two bars of one stock: scale cancels
    ok, rho = LS.level_safe("$close", panel, sc)
    assert not ok and rho < LS.CUT                                                  # the level itself is reordered by the rescaling


def random_market(t: int = 230, n: int = 110):
    import polars as pl

    rng = np.random.default_rng(8)
    days = []
    d = date(2024, 1, 1)
    while len(days) < t:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    frames = []
    for j in range(n):
        close = 10 * np.exp(np.cumsum(rng.normal(0, 0.012, t)))
        frames.append(pl.DataFrame({
            "code": [f"60{j:04d}"] * t, "date": days, "open": close * (1 + rng.normal(0, 0.003, t)), "high": close * 1.01, "low": close * 0.99, "close": close,
            "volume": rng.lognormal(18.5, 0.3, t).round(), "float_cap_100m": [50.0] * t, "has_total_cap": [True] * t, "list_date": [date(2000, 1, 3)] * t}))
    return P.to_panel(P.build_long(pl.concat(frames)))


def test_incremental_scores_with_a_stored_ic_history_equal_the_full_recomputation(monkeypatch):
    from pulse.application import bf_fusion_service as svc
    import polars as pl

    panel = random_market()
    monkeypatch.setattr(S, "FIRST_IC_DATE", panel.dates[S.FIRST])          # the synthetic window starts earlier than the real one
    facs = [("a", "Mean($close,5)/$close"), ("b", "Std($ret,10)"), ("c", "$turn"), ("d", "Rank($close,20)"), ("e", "Corr($close,$volume,10)")]
    full = S.run_factors(panel, facs, tail_start=S.FIRST_SCORE_ROW)
    want = S.score_rows(full, panel, k=3)
    assert len(want) == panel.T - S.FIRST_SCORE_ROW
    # the stored history holds every IC whose label was complete one session earlier; today only the newest IC row and the tail are ranked
    names = [n for n, _ in facs]
    old_rows = range(S.FIRST, panel.T - S.PURGE - 1)
    hist = pl.DataFrame({"factor": [n for r in old_rows for n in names], "date": [panel.dates[r] for r in old_rows for _ in names],
                         "ic": [None if not np.isfinite(full.ic[i, r]) else float(full.ic[i, r]) for r in old_rows for i in range(len(names))]}, schema=svc.IC_SCHEMA)
    fresh = svc.new_ic_rows(panel, hist)
    assert fresh == [panel.T - S.PURGE - 1]                                    # exactly the newest complete label
    tail = panel.T - 61
    run = S.run_factors(panel, facs, tail_start=tail, rows=sorted(set(fresh) | set(range(tail, panel.T))))
    ic = svc.ic_matrix(hist, names, panel.dates)
    for r in fresh:
        ic[:, r] = run.ic[:, r]
    got = S.score_rows(run, panel, ic=ic, k=3)
    assert sorted(got) == list(range(max(tail, S.FIRST_SCORE_ROW), panel.T))
    for t, s in got.items():
        np.testing.assert_allclose(s, want[t], rtol=1e-5, atol=1e-6, equal_nan=True)
    assert len(run.rows) == 61 and run.ranks.shape[1] == 61                    # one new IC row is inside the tail: nothing else was ranked
