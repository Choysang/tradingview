"""PEX10 domain: the deterministic parser, the whitelisted AST, three-valued evaluation, presets, 12 patterns (PIT), factor lab."""
from __future__ import annotations

import copy
import math
from datetime import datetime, timezone

import pytest

from pulse import contracts
from pulse.domain.playbooks.builder import RawSeries, build_views
from pulse.domain.screening import ast as A
from pulse.domain.screening import catalog as K
from pulse.domain.screening import factor_lab as FL
from pulse.domain.screening import parser
from pulse.domain.screening import patterns as P
from pulse.domain.screening import presets

from .synth import panel, sessions

NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)


def reasons(r):
    return {(i["text"], i["reason_code"]) for i in r["unresolved"]}


def test_sentence_becomes_typed_ast_and_passes_the_contract():
    r = parser.parse("首板，量比大于1.5，剔除ST，剔除次新")
    assert r["complete"] and r["unresolved"] == []
    contracts.check("ScreenAst", r["ast"], contracts.EXT_ID)
    assert A.used_fields(r["ast"]) == ["limit_streak", "volume_ratio_5", "is_st", "is_new"]


def test_unmatched_fragments_are_flagged_not_dropped():
    r = parser.parse("放量上涨 换手率大于5% 概念龙头 市值大于100")
    assert not r["complete"]
    got = reasons(r)
    assert any(t.startswith("换手率") and c == "unsupported" for t, c in got)     # the data does not exist
    assert any("市值" in t and c == "unit_required" for t, c in got)              # units are never guessed
    assert any("概念龙头" in t for t, _ in got)
    assert r["ast"] is not None                                                     # the resolved part is still returned for review
    for i in r["unresolved"]:                                                       # spans point into the normalised text
        assert i["source_start"] < i["source_end"]


def test_or_groups_ranges_and_sort():
    r = parser.parse("3连板以上 或 5连阳以上 涨幅3到9% 按涨幅降序")
    assert r["complete"], r["unresolved"]
    cond = r["ast"]["condition"]
    assert cond["node"] == "any" and len(cond["children"]) == 2
    assert r["ast"]["sort"] == [{"field_id": "pct_chg", "direction": "desc", "nulls": "last"}]
    r2 = parser.parse("流通市值小于80亿元")
    assert r2["complete"] and r2["ast"]["condition"]["right"]["value"] == "80"
    r3 = parser.parse("涨幅大于5% 涨幅小于3%")
    assert any(c == "ambiguous" for _, c in reasons(r3)) and not r3["complete"]


def test_parser_never_returns_a_stock_list_or_executes_anything():
    r = parser.parse("给我万科A和比亚迪; DROP TABLE core.instrument; select * from x")
    assert r["ast"] is None and not r["complete"] and r["unresolved"]


def test_presets_all_parse_completely_and_use_catalog_fields_only():
    assert len(presets.PRESETS) == 5
    for p in presets.PRESETS:
        r = parser.parse(p.text)
        assert r["complete"], (p.id, r["unresolved"])
        assert all(f in K.BY_ID for f in A.used_fields(r["ast"]))


def test_ast_validation_rejects_scripts_unknown_fields_units_and_budget():
    base = parser.parse("首板")["ast"]
    bad = copy.deepcopy(base)
    bad["condition"]["left"]["feature_id"] = "close; drop table x"
    with pytest.raises(A.ScreenAstError) as e:
        A.validate_ast(bad)
    assert e.value.code == "SCREEN_FIELD_UNKNOWN"
    bad = copy.deepcopy(base)
    bad["condition"]["left"]["unit"] = "pct"
    with pytest.raises(A.ScreenAstError) as e:
        A.validate_ast(bad)
    assert e.value.code == "SCREEN_UNIT_MISMATCH"
    crosses = {"node": "crosses", "clock": A.CLOCK, "direction": "above", "left": A.operand("close"), "right": A.operand("close"),
               "previous_observation": {"selection": "previous_valid", "max_age_ms": 1}}
    with pytest.raises(A.ScreenAstError) as e:
        A.validate_ast(A.make_ast(crosses))
    assert e.value.code == "SCREEN_NODE_UNSUPPORTED"
    deep = A.cmp("close", "gt", 1)
    for _ in range(A.MAX_DEPTH + 1):
        deep = A.C.not_(A.CLOCK, deep)
    with pytest.raises(A.ScreenAstError) as e:
        A.validate_ast(A.make_ast(deep))
    assert e.value.code == "SCREEN_BUDGET_EXCEEDED"


def test_missing_value_is_unknown_never_zero_and_kleene_logic_holds():
    ast = A.make_ast(A.all_of(A.cmp("pct_chg", "lt", 1), A.cmp("rsi_14", "lt", 30)))
    assert A.evaluate_row(ast, {"pct_chg": 5.0, "rsi_14": None}, NOW).value.value == "FALSE"        # a decisive FALSE wins over UNKNOWN
    assert A.evaluate_row(ast, {"pct_chg": 0.5, "rsi_14": None}, NOW).value.value == "UNKNOWN"
    assert A.evaluate_row(ast, {"pct_chg": 0.5, "rsi_14": 10.0}, NOW).value.value == "TRUE"
    only = A.make_ast(A.cmp("rsi_14", "lt", 30))
    assert A.evaluate_row(only, {"rsi_14": None}, NOW).value.value == "UNKNOWN"                      # not "0 < 30"
    assert A.evaluate_row(A.make_ast(A.cmp("ret_5d", "lt", 1)), {"ret_5d": 0.0}, NOW).value.value == "TRUE"  # a real zero is a value


def test_stable_sort_with_explicit_nulls_and_instrument_tiebreak():
    rows = [("c", {"pct_chg": 2.0}), ("a", {"pct_chg": 2.0}), ("b", {"pct_chg": None}), ("d", {"pct_chg": 5.0})]
    assert A.order_rows([{"field_id": "pct_chg", "direction": "desc", "nulls": "last"}], rows) == ["d", "a", "c", "b"]
    assert A.order_rows([{"field_id": "pct_chg", "direction": "asc", "nulls": "first"}], rows) == ["b", "a", "c", "d"]


# ---- patterns ---------------------------------------------------------------------------------------------------
def _view(closes, opens=None, highs=None, lows=None, vols=None):
    n = len(closes)
    days = sessions(n)
    o = opens or closes
    return build_views([RawSeries("x", "main", tuple(days), tuple(o), tuple(highs or [max(a, b) * 1.001 for a, b in zip(o, closes)]),
                                  tuple(lows or [min(a, b) * 0.999 for a, b in zip(o, closes)]), tuple(closes), tuple(vols or [1e7] * n))],
                       days)[0]


def test_turtle_breakout_and_insufficient_history_is_unknown_not_no():
    flat = [10.0] * 70
    hit = P.evaluate(_view(flat + [10.5]), "turtle_breakout")
    assert hit is not P.INSUFFICIENT and hit is not None and hit.strength > 0
    assert P.evaluate(_view(flat[:30]), "turtle_breakout") is P.INSUFFICIENT
    assert P.evaluate(_view(flat + [9.0]), "turtle_breakout") is None
    assert K.field_values(_view(flat[:30]), K.InstrumentFacts("main", False, None))["pattern.turtle_breakout"] is None


def test_ma_bull_volume_surge_and_macd_cross_fire_on_constructed_data():
    up = [10 + 0.05 * i for i in range(90)]
    assert P.evaluate(_view(up), "ma_bull") not in (None, P.INSUFFICIENT)
    closes = [10.0] * 20 + [10.5]
    opens = [10.0] * 20 + [10.1]
    vols = [1e6] * 20 + [3e6]
    assert P.evaluate(_view(closes, opens, vols=vols), "volume_surge") not in (None, P.INSUFFICIENT)
    path = [20 - 0.2 * i + (0.15 if i % 2 else -0.15) for i in range(60)] + [8 + 0.35 * i + (0.15 if i % 2 else -0.15) for i in range(12)]
    v = _view(path)
    assert any(P.evaluate(v, "macd_bottom_cross", i) not in (None, P.INSUFFICIENT) for i in range(36, v.n))


def test_every_pattern_is_point_in_time():
    days, raws, views = panel(30, 400, caps=False)
    cut = 330
    for pid in P.BY_ID:
        for v in views[:12]:
            tv = v.truncate(cut)
            for i in (cut - 1, cut - 20):
                a, b = P.evaluate(v, pid, i), P.evaluate(tv, pid, i)
                assert (a is P.INSUFFICIENT) == (b is P.INSUFFICIENT) and (a is None) == (b is None), (pid, i)


def test_twelve_rules_with_original_semantics_documented():
    assert len(P.DEFINITIONS) == 12 and {d.id for d in P.DEFINITIONS} == set(P.RULES)
    assert {"volume_surge", "climax_limitdown", "high_tight_flag"} <= {d.id for d in P.DEFINITIONS if d.omitted}


# ---- factor lab -------------------------------------------------------------------------------------------------
def test_rank_ic_is_exact_on_a_perfect_factor_and_missing_on_thin_cross_sections():
    n_days, n_names = 80, 40
    days = sessions(n_days)
    raws = []
    for k in range(n_names):
        px = [10 * math.exp(0.002 * (k - n_names / 2) * i) for i in range(n_days)]   # higher k -> higher forward return everywhere
        raws.append(RawSeries(f"s{k:02d}", "main", tuple(days), tuple(px), tuple(px), tuple(px), tuple(px), tuple([1e7] * n_days)))
    views = build_views(raws, days)
    f = FL.FactorDef("slope", "斜率", "perfect", lambda v, i: float(int(v.instrument_id[1:])))
    r = FL.run_factor(views, days, f, horizon=5, start=10, end=70)
    assert r.ic_mean == pytest.approx(1.0) and r.ic_positive_share == 1.0
    assert r.quantile_mean_return[-1] > r.quantile_mean_return[0] and r.long_short_return > 0
    thin = FL.run_factor(views[:10], days, f, horizon=5, start=10, end=70)
    assert thin.days == 0 and thin.ic_mean is None and thin.icir is None            # MISSING, not 0


def test_forward_windows_never_cross_the_slice_end():
    days, raws, views = panel(40, 200, caps=False)
    full = FL.run_factor(views, days, FL.BY_ID["momentum_20"], horizon=5, start=60, end=140)
    assert len(full.ic_series) == 140 - 60 - 5 and full.ic_series[-1][0] == days[134]


def test_six_factors_registered():
    assert [f.id for f in FL.FACTORS] == ["momentum_20", "reversal_5", "lowvol_20", "volume_surge", "dist_high_60", "ma_gap_20"]
