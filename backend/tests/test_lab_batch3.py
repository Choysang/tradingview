"""Batch 3 helpers (scripts/lab/batch3_lib.py, score_baskets.pick): the pre-registered rules of E070 ~ E073."""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "lab"))

import batch3_lib as L  # noqa: E402


@pytest.mark.parametrize("title, expected", [
    ("关于收到中国证监会<em>立案</em>告知书的公告", "regulatory"),
    ("关于公司收到行政处罚事先告知书的公告", "regulatory"),
    ("关于中国证监会终止调查暨撤销立案调查的公告", None),
    ("2026年半年度业绩预亏公告", "earnings"),
    ("2026年前三季度业绩预减公告", "earnings"),
    ("关于公司股票被实施退市风险警示的公告", "delisting"),
    ("关于申请撤销其他风险警示的公告", None),
    ("关于股东减持计划的预披露公告", "sell_down"),
    ("股东减持股份结果公告", None),
    ("控股股东关于承诺不减持公司股份的公告", None),
    ("关于收到上海证券交易所问询函的公告", "inquiry"),
    ("首次公开发行限售股解除限售上市流通的提示性公告", "unlock"),
    ("关于2024年限制性股票激励计划第一个解除限售期解除限售的公告", None),
    ("关于签订日常经营重大合同的公告", None),
])
def test_neg_class_follows_the_preregistered_table(title: str, expected: str | None) -> None:
    assert L.neg_class(title) == expected


def test_us_window_return_uses_only_sessions_closed_before_the_open() -> None:
    a_days = [date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5)]
    us = [(date(2026, 10, 30), 100.0), (date(2026, 11, 2), 101.0), (date(2026, 11, 3), 99.99), (date(2026, 11, 4), 99.99)]
    r = L.us_window_return(a_days, us)
    # A-share 11-04: US sessions dated 11-03 only (11-03 closes 05:00 Beijing on 11-04)
    assert r[date(2026, 11, 4)] == pytest.approx(99.99 / 101.0 - 1)
    # A-share 11-05: US session dated 11-04, flat
    assert r[date(2026, 11, 5)] == pytest.approx(0.0)
    assert date(2026, 11, 3) not in r  # the first A-share day has no previous day


def test_us_window_return_spans_a_long_a_share_holiday() -> None:
    a_days = [date(2026, 9, 30), date(2026, 10, 8)]
    us = [(date(2026, 9, 29), 100.0), (date(2026, 10, 1), 102.0), (date(2026, 10, 7), 110.0)]
    assert L.us_window_return(a_days, us)[date(2026, 10, 8)] == pytest.approx(0.10)


def test_reaction_day_rules() -> None:
    days = [date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 11)]
    assert L.reaction_day(datetime(2026, 11, 6, 21, 30), True, days) == date(2026, 11, 9)   # US NFP Friday night
    assert L.reaction_day(datetime(2026, 11, 5, 2, 0), True, days) == date(2026, 11, 5)    # FOMC before the open
    assert L.reaction_day(datetime(2026, 11, 10, 9, 30), True, days) == date(2026, 11, 10)  # China CPI 09:30
    assert L.reaction_day(datetime(2026, 11, 10, 15, 30), True, days) == date(2026, 11, 11)  # after the close
    assert L.reaction_day(datetime(2026, 11, 7, 0, 0), True, days) == date(2026, 11, 9)    # weekend release
    assert L.reaction_day(datetime(2026, 11, 10, 9, 0), False, days) == date(2026, 11, 11)  # meeting: next session
    assert L.reaction_day(datetime(2026, 11, 11, 16, 0), True, days) is None


def test_rebalance_cost_counts_both_sides() -> None:
    assert L.rebalance_cost(set(), {"a", "b"}, 0.0015) == pytest.approx(0.0015)
    assert L.rebalance_cost({"a", "b"}, {"b", "c"}, 0.0015) == pytest.approx(0.0015)
    assert L.rebalance_cost({"a", "b"}, {"a", "b"}, 0.0015) == 0.0
    assert L.rebalance_cost({"a"}, set(), 0.0015) == 0.0


def test_basket_buffers() -> None:
    import score_baskets as S
    ranks = {f"s{i}": (i + 1) / 10 for i in range(10)}  # s0 = 0.1 (worst) ... s9 = 1.0 (best)
    assert S.pick("T30", ranks, set()) == {"s7", "s8", "s9"}
    # s5 (0.6) is held and still inside the top 50%: kept, and only two of the top 30% join
    assert S.pick("T30b", ranks, {"s5"}) == {"s5", "s9", "s8"}
    # s4 (0.5) is not above 0.5: dropped
    assert S.pick("T30b", ranks, {"s4"}) == {"s7", "s8", "s9"}
    assert S.pick("X20", ranks, set()) == {f"s{i}" for i in range(2, 10)}
    # X20b keeps a held name in the 10-20% band (s1 = 0.2) but not one in the bottom 10% (s0 = 0.1)
    assert S.pick("X20b", ranks, {"s0", "s1"}) == {f"s{i}" for i in range(1, 10)}


def test_block_boot_p() -> None:
    import numpy as np
    assert L.block_boot_p(np.full(50, 0.01), 20, 500, 1) == 0.0
    assert L.block_boot_p(np.full(50, -0.01), 20, 500, 1) == 1.0
    assert L.block_boot_p(np.array([np.nan, np.nan]), 20, 500, 1) is None
    # a block as long as the series resamples the whole series: the answer is the sign of its mean
    assert L.block_boot_p(np.array([0.03, -0.01, np.nan, -0.01]), 10, 200, 1) == 0.0
    x = np.random.default_rng(0).normal(0.001, 0.01, 300)
    assert L.block_boot_p(x, 20, 1000, 7) == L.block_boot_p(x, 20, 1000, 7)


def _adj_panel(rets: dict[str, tuple[str, float]]):
    import polars as pl
    days = [date(2026, 10, 19 + i) for i in range(6)]
    rows = [{"code": k, "date": d, "close": 100.0 * (1 + r) if i == 5 else 100.0}
            for k, (_, r) in rets.items() for i, d in enumerate(days)]
    pop = pl.DataFrame([{"code": k, "date": days[5], "industry": ind} for k, (ind, _) in rets.items()])
    return pl.DataFrame(rows), pop


def test_adj_quantile_ranks_against_the_industry() -> None:
    rets = {f"a{i}": ("A", 0.20 + i / 100) for i in range(5)}
    rets.update({"b0": ("B", 0.0), "b1": ("B", 0.01), "b2": ("B", 0.02), "b3": ("B", 0.03), "b4": ("B", 0.10)})
    panel, pop = _adj_panel(rets)
    q = L.adj_quantile(panel, pop, 5)
    assert q.height == 10
    top = q.filter(q["q"] > 0.9)["code"].to_list()
    assert top == ["b4"]  # +0.068 over its industry beats a4's +0.02
    raw = L.adj_quantile(panel, pop, 5, adjust=False)
    assert raw.filter(raw["q"] > 0.9)["code"].to_list() == ["a4"]
    # only the population is ranked; a stock without n rows back has no return and is left out
    assert L.adj_quantile(panel, pop.filter(pop["code"] != "a0"), 5).height == 9
    assert L.adj_quantile(panel, pop, 6).height == 0


def test_adj_quantile_small_industry_uses_the_population_mean() -> None:
    rets = {f"a{i}": ("A", 0.20 + i / 100) for i in range(5)}
    rets["c0"] = ("C", 0.21)  # alone in its industry: compared with the population mean (0.2183), not with itself
    panel, pop = _adj_panel(rets)
    q = dict(L.adj_quantile(panel, pop, 5).select("code", "q").iter_rows())
    assert q["c0"] == pytest.approx(3 / 6)  # adj -0.0083 sits between a1 (-0.01) and a2 (0.0)


def test_boot_block_handles_days_without_signals() -> None:
    import numpy as np
    import sameday_base as B
    days = [date(2026, 1, 1) + __import__("datetime").timedelta(days=i) for i in range(30)]
    sig = days[::3]
    assert B._boot_block(sig, np.full(len(sig), 0.5), np.ones(len(sig)), days, 5, 1) == 0.0
    assert B._boot_block(sig, np.full(len(sig), -0.5), np.ones(len(sig)), days, 5, 1) == 1.0
    assert B._boot_block([], np.array([]), np.array([]), days, 5, 1) is None
    # a signal day outside the segment is ignored
    assert B._boot_block([date(2027, 1, 1)], np.array([1.0]), np.array([1.0]), days, 5, 1) is None


def test_boot_block_is_wider_than_day_level_on_runs() -> None:
    import numpy as np
    import sameday_base as B
    days = [date(2024, 1, 1) + __import__("datetime").timedelta(days=i) for i in range(400)]
    e = np.tile(np.r_[np.full(20, 1.0), np.full(20, -0.8)], 10)  # mean +0.1, in runs of 20 days
    k = np.ones(400)
    iid = B._boot_block(days, e, k, days, 1, 3)
    blk = B._boot_block(days, e, k, days, 20, 3)
    assert iid < 0.05 < 0.1 < blk


def test_sameday_evaluate_repeats_exactly() -> None:
    """E082: the random draws follow the day order, so the day aggregations are sorted and a rerun repeats."""
    import numpy as np
    import polars as pl
    import sameday_base as B
    rng = np.random.default_rng(0)
    days = [date(2024, 1, 1) + __import__("datetime").timedelta(days=i) for i in range(60)]
    n = 40
    rows = {"code": [f"c{i}" for i in range(n)] * len(days), "date": [d for d in days for _ in range(n)]}
    m = len(rows["code"])
    out = pl.DataFrame({**rows, "in_pool": [True] * m, "period": ["PRE"] * m,
                        **{k: rng.normal(0, 2, m) for k in ("r5", "leg_intraday", "leg_overnight", "leg_rest5")},
                        "size5": rng.integers(1, 3, m), "vol5": rng.integers(1, 3, m), "industry": ["x"] * m})
    sig = out.filter(pl.col("code").is_in([f"c{i}" for i in range(6)])).select("code", "date")
    a, b = B.evaluate(sig, out, "s", holds=(5,)), B.evaluate(sig, out, "s", holds=(5,))
    assert a == b
    assert B.evaluate_matched(sig, out, "s", holds=(5,)) == B.evaluate_matched(sig, out, "s", holds=(5,))
