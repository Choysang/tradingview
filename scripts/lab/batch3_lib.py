"""Pure helpers of batch 3 (E070 ~ E074), pre-registered in docs/research/2026-11-08-batch3-preregistration.md.

Kept free of I/O so backend/tests/test_lab_batch3.py can lock the rules:
- `neg_class(title)`: the E070 negative-announcement class of a cninfo title, or None;
- `us_window_return(a_days, us)`: per A-share day D, the US close-to-close return over the sessions dated in [P, D),
  P = the previous A-share day (E072);
- `reaction_day(release, timed, a_days)`: the first A-share session that can react to a calendar event (E073);
- `rebalance_cost(old, new, side_cost)`: the turnover cost of moving between two equal-weight sets (E071);
- `adj_quantile(panel, pop, n)`: per day, the quantile of the industry-adjusted n-day return inside a population
  (E070's oversold / overbought ranking; E079's veto, pre-registered in 2026-11-08-overbought-veto-preregistration.md);
- `block_boot_p(x, block, n_boot, seed)`: moving-block bootstrap share of resampled means <= 0 (E079).
"""
from __future__ import annotations

import bisect
import math
import re
from datetime import date, datetime, time

import numpy as np
import polars as pl

TAG = re.compile(r"<[^>]+>")

# (class, any of these must appear, none of these may appear)
NEG_RULES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("regulatory", ("立案调查", "立案告知", "行政处罚"), ("撤销", "终止调查")),
    ("earnings", ("预亏", "预减", "首亏", "续亏"), ()),
    ("delisting", ("退市风险", "风险警示"), ("撤销", "申请撤销", "解除")),
    ("sell_down", ("减持计划", "预披露", "拟减持"), ("不减持", "终止", "完成", "结果", "届满", "实施完毕")),
    ("inquiry", ("问询函",), ()),
    ("unlock", ("首次公开发行", "限售股", "非公开发行"), ("股权激励", "期权", "限制性股票", "员工持股")),
)
# the unlock rule only applies to titles about lifting a sale restriction
UNLOCK_GATE = "解除限售"


def clean(title: str) -> str:
    return TAG.sub("", title or "").strip()


def neg_class(title: str) -> str | None:
    t = clean(title)
    for name, need, ban in NEG_RULES:
        if name == "sell_down" and "减持" not in t:
            continue
        if name == "unlock" and UNLOCK_GATE not in t:
            continue
        if any(k in t for k in need) and not any(k in t for k in ban):
            return name
    return None


def us_window_return(a_days: list[date], us: list[tuple[date, float]]) -> dict[date, float | None]:
    """us: (US session date, close) sorted by date. For A-share day D with previous A-share day P, the return from
    the last US close dated before P to the last US close dated before D. A US session dated t closes at 04:00 or
    05:00 Beijing on t+1, so every session dated before D is known before D's opening auction."""
    us_dates = [d for d, _ in us]
    closes = [x for _, x in us]
    out: dict[date, float | None] = {}
    for p, d in zip(a_days, a_days[1:]):
        i_p, i_d = bisect.bisect_left(us_dates, p) - 1, bisect.bisect_left(us_dates, d) - 1
        out[d] = closes[i_d] / closes[i_p] - 1 if i_p >= 0 and i_d >= 0 and closes[i_p] else None
    return out


CLOSE = time(15, 0)


def reaction_day(release: datetime, timed: bool, a_days: list[date]) -> date | None:
    """First A-share session able to react. Timed release: the first trading day D with release < D 15:00 and
    release >= the previous trading day's 15:00. Date-only events (meetings, whose news comes out in the evening):
    the next trading day after the event date."""
    if not timed:
        i = bisect.bisect_right(a_days, release.date())
        return a_days[i] if i < len(a_days) else None
    for d in a_days[bisect.bisect_left(a_days, release.date()):]:
        if release < datetime.combine(d, CLOSE):
            return d
    return None


def rebalance_cost(old: set[str], new: set[str], side_cost: float) -> float:
    """Equal-weight sets, drift ignored: (weight sold + weight bought) x side_cost, as a fraction."""
    if not new:
        return 0.0
    sold = len(old - new) / len(old) if old else 0.0
    bought = len(new - old) / len(new)
    return (sold + bought) * side_cost


MIN_IND = 5


def adj_quantile(panel: pl.DataFrame, pop: pl.DataFrame, n: int, adjust: bool = True) -> pl.DataFrame:
    """(code, date, q) for every (code, date) of `pop` (code, date, industry) with a finite n-day return.

    Return = close / close n rows back - 1 in the stock's own rows of `panel`. With `adjust`, minus that day's
    industry mean inside `pop` when the industry has at least MIN_IND names there, else minus the day's `pop` mean.
    q = rank / count of the (adjusted) return within the day: q > 0.9 is the top 10%, q <= 0.1 the bottom 10%."""
    p = panel.sort("code", "date").with_columns((pl.col("close") / pl.col("close").shift(n).over("code") - 1).alias("ret"))
    j = pop.select("code", "date", "industry").join(p.select("code", "date", "ret"), on=["code", "date"])
    j = j.filter(pl.col("ret").is_finite())
    if adjust:
        j = j.with_columns(pl.col("ret").mean().over("date", "industry").alias("_mi"), pl.len().over("date", "industry").alias("_ni"),
                           pl.col("ret").mean().over("date").alias("_mp"))
        j = j.with_columns((pl.col("ret") - pl.when(pl.col("_ni") >= MIN_IND).then(pl.col("_mi")).otherwise(pl.col("_mp"))).alias("adj"))
    else:
        j = j.with_columns(pl.col("ret").alias("adj"))
    return j.with_columns((pl.col("adj").rank() / pl.col("adj").count()).over("date").alias("q")).select("code", "date", "q")


def block_boot_p(x: np.ndarray, block: int, n_boot: int, seed: int) -> float | None:
    """Moving-block bootstrap of the mean of a daily series: concatenate random blocks of `block` consecutive days
    (cut to the series length) n_boot times; return the share of resampled means <= 0. NaNs are dropped first."""
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if x.size == 0:
        return None
    b = min(block, x.size)
    k = math.ceil(x.size / b)
    starts = np.random.default_rng(seed).integers(0, x.size - b + 1, size=(n_boot, k))
    idx = (starts[:, :, None] + np.arange(b)).reshape(n_boot, -1)[:, :x.size]
    return float((x[idx].mean(axis=1) <= 0).mean())
