"""Technical-analysis strategies for the lab: candlestick (K线) patterns, MACD divergence and crosses,
weekly and monthly MACD / moving-average crosses, multi-timeframe resonance, and index timing rules.

Every signal uses completed bars only. Weekly and monthly bars are rebuilt from daily bars; a
week's (month's) values become usable at the close of its last trading day, and on every other
day the latest completed week (month) is used. The trading calendar is known in advance, so
"last trading day of the week" needs no future prices.
Ties among triggered stocks are broken by a fixed random number, so the result measures the
pattern itself and not a hidden low-volatility or size tilt.
"""
from __future__ import annotations

import numpy as np
import polars as pl

from backend.app.playbooks.library import CODE

c = pl.col


def _period_lines(daily: pl.DataFrame, every: str, key: str = CODE) -> pl.DataFrame:
    """Per key and period (calendar week "1w" or month "1mo"): close, MACD(12, 26, 9), MA5, MA10,
    and `avail` = the period's last trading day, when its values become known."""
    cal = daily.select("date").unique().sort("date").with_columns(c("date").dt.truncate(every).alias("p"))
    ends = cal.group_by("p").agg(c("date").max().alias("avail"))
    per = (daily.with_columns(c("date").dt.truncate(every).alias("p")).sort(key, "date")
           .group_by(key, "p").agg(c("close").last().alias("pc")).sort(key, "p"))
    per = per.join(ends, on="p").sort(key, "p").with_columns(
        (c("pc").ewm_mean(span=12, adjust=False) - c("pc").ewm_mean(span=26, adjust=False)).over(key).alias("dif"),
        c("pc").rolling_mean(5).over(key).alias("ma5"),
        c("pc").rolling_mean(10).over(key).alias("ma10"),
        pl.int_range(pl.len()).over(key).alias("n"),
    ).with_columns(c("dif").ewm_mean(span=9, adjust=False).over(key).alias("dea"))
    return per.with_columns(
        (c("dif") > c("dea")).alias("macd_up"),
        ((c("dif") > c("dea")) & (c("dif").shift(1).over(key) <= c("dea").shift(1).over(key))).alias("macd_golden"),
        ((c("dif") < c("dea")) & (c("dif").shift(1).over(key) >= c("dea").shift(1).over(key))).alias("macd_dead"),
        ((c("ma5") > c("ma10")) & (c("ma5").shift(1).over(key) <= c("ma10").shift(1).over(key))).alias("ma_golden"),
        (c("ma5") > c("ma10")).alias("ma_up"),
    )


def _attach(daily: pl.DataFrame, per: pl.DataFrame, prefix: str, min_periods: int, key: str = CODE) -> pl.DataFrame:
    """Each daily row gets the latest period whose `avail` date is on or before it."""
    cols = ["macd_up", "macd_golden", "macd_dead", "ma_golden", "ma_up"]
    right = (per.filter(c("n") >= min_periods).select(key, c("avail").alias("date"), *cols)
             .rename({k: f"{prefix}_{k}" for k in cols}).sort("date"))
    fresh = right.select(key, "date", pl.lit(True).alias(f"{prefix}_fresh"))
    out = daily.sort("date").join_asof(right, on="date", by=key, strategy="backward")
    return out.join(fresh, on=[key, "date"], how="left").with_columns(c(f"{prefix}_fresh").fill_null(False))


def stock_strategies(panel: pl.DataFrame, ok: pl.Expr) -> dict[str, tuple[pl.DataFrame, int, str]]:
    """name -> (signals code/date/score, holding days, Chinese label)."""
    o, h, l, cl = c("open"), c("high"), c("low"), c("close")
    body = (cl - o).abs()
    rng = h - l
    upper = h - pl.max_horizontal("open", "close")
    lower = pl.min_horizontal("open", "close") - l
    dif, dea, hist = c("macd_dif"), c("macd_dea"), c("macd_hist")
    green = cl > o
    # Helper columns first, so no window expression is nested inside another.
    step1 = {
        "_down5": (cl / cl.shift(5).over(CODE) - 1) <= -0.05,
        "_down10": (cl / cl.shift(10).over(CODE) - 1) <= -0.08,
        "_near_low": cl <= cl.rolling_min(20).over(CODE) * 1.03,
        "_o1": o.shift(1).over(CODE), "_c1": cl.shift(1).over(CODE),
        "_o2": o.shift(2).over(CODE), "_c2": cl.shift(2).over(CODE), "_pc2": c("prev_close").shift(2).over(CODE),
        "_golden": (dif > dea) & (dif.shift(1).over(CODE) <= dea.shift(1).over(CODE)),
        "_hist1": hist.shift(1).over(CODE), "_j1": c("kdj_j").shift(1).over(CODE),
        "_cmin": cl.shift(10).rolling_min(30).over(CODE), "_dmin": dif.shift(10).rolling_min(30).over(CODE),
    }
    step2 = {"_down5_1": c("_down5").shift(1).over(CODE), "_down10_3": c("_down10").shift(3).over(CODE)}
    o1, c1, o2, c2 = c("_o1"), c("_c1"), c("_o2"), c("_c2")
    rules: dict[str, tuple[pl.Expr, int, str]] = {
        "k_hammer": ((rng > 0) & (lower >= 2 * body) & (upper <= body + 0.001 * cl) & c("_down5") & c("_near_low"), 5,
                     "锤子线（下跌后长下影）"),
        "k_engulfing": ((c1 < o1) & green & (o <= c1) & (cl >= o1) & c("_down5_1"), 5, "阳包阴（看涨吞没）"),
        "k_morning_star": ((c2 / c("_pc2") - 1 <= -0.03) & ((c1 - o1).abs() <= (c2 - o2).abs() * 0.3)
                           & green & (cl >= (o2 + c2) / 2) & c("_down10"), 5, "早晨之星"),
        "k_three_soldiers": (green & (c1 > o1) & (c2 > o2) & (cl > c1) & (c1 > c2) & (o >= o1) & (o1 >= o2)
                             & (c("pct_chg").abs() < 6) & c("_down10_3"), 5, "红三兵（下跌后）"),
        "k_doji_low": ((rng > 0.03 * c("prev_close")) & (body <= 0.1 * rng) & c("_near_low") & c("_down5"), 5, "低位十字星"),
        "k_lower_shadow_volume": ((lower >= 0.6 * rng) & (rng > 0.04 * c("prev_close")) & (c("volume") >= 1.5 * c("vol_ma5"))
                                  & c("_near_low"), 5, "低位放量长下影"),
        "macd_bottom_divergence": ((cl < c("_cmin")) & (dif > c("_dmin")) & (dif < 0) & (hist > c("_hist1")), 10, "MACD 底背离"),
        "macd_golden_below_zero": (c("_golden") & (dif < 0), 10, "MACD 零轴下金叉"),
        "macd_golden_above_zero": (c("_golden") & (dea > 0), 10, "MACD 零轴上金叉"),
        "kdj_j_oversold_turn": ((c("kdj_j") < 0) & (c("kdj_j") > c("_j1")) & c("_near_low"), 5, "KDJ J 值超卖拐头"),
        "boll_lower_rsi6": ((cl < c("boll_lower")) & (c("rsi_6") < 20) & (lower > body), 5, "跌破布林下轨 + RSI6<20 + 下影"),
    }
    base = panel.select(CODE, "date", "close", "ma20")
    flags = (panel.lazy().with_columns(ok.fill_null(False).alias("_ok"), *(e.alias(k) for k, e in step1.items()))
             .with_columns(*(e.alias(k) for k, e in step2.items()))
             .with_columns(*(e.fill_null(False).alias(k) for k, (e, _, _) in rules.items()))
             .select(CODE, "date", "_ok", *rules).collect())
    # Weekly and monthly lines (completed periods only).
    wk = _attach(base.select(CODE, "date", "close", "ma20"), _period_lines(base, "1w"), "w", 35)
    mo = _attach(base.select(CODE, "date", "close"), _period_lines(base, "1mo"), "m", 12)
    tf = wk.join(mo.drop("close"), on=[CODE, "date"], how="left").join(
        flags.select(CODE, "date", "_ok", "macd_golden_above_zero"), on=[CODE, "date"], how="left")
    daily_golden = flags.select(CODE, "date", (c("macd_golden_above_zero") | c("macd_golden_below_zero")).alias("_dg"))
    tf = tf.join(daily_golden, on=[CODE, "date"], how="left")
    multi = {
        "w_macd_golden": (c("w_fresh") & c("w_macd_golden").fill_null(False) & (c("close") > c("ma20")), 10, "周线 MACD 金叉"),
        "m_macd_golden": (c("m_fresh") & c("m_macd_golden").fill_null(False), 20, "月线 MACD 金叉"),
        "m_ma_golden": (c("m_fresh") & c("m_ma_golden").fill_null(False), 20, "月线 MA5 上穿 MA10"),
        "resonance": (c("_dg").fill_null(False) & c("w_macd_up").fill_null(False) & c("m_macd_up").fill_null(False), 10,
                      "日周月 MACD 共振（日线金叉，周、月线多头）"),
        "w_up_m_dead_avoid": (c("w_fresh") & c("w_macd_golden").fill_null(False) & ~c("m_macd_up").fill_null(True), 10,
                              "周线金叉但月线空头（反弹）"),
    }
    out: dict[str, tuple[pl.DataFrame, int, str]] = {}
    rng_ = np.random.default_rng(0)
    for name, (_, hold, label) in rules.items():
        f = flags.filter(c("_ok") & c(name)).select(CODE, "date")
        out[name] = (f.with_columns(score=pl.Series(rng_.random(f.height))), hold, label)
    for name, (expr, hold, label) in multi.items():
        f = tf.filter(c("_ok").fill_null(False) & expr).select(CODE, "date")
        out[name] = (f.with_columns(score=pl.Series(rng_.random(f.height))), hold, label)
    return out


def index_timing(benchmark: pl.DataFrame) -> pl.DataFrame:
    """date -> in-market flag (1/0) for each index timing rule, known at that day's close."""
    b = benchmark.select("date", "close").sort("date").with_columns(pl.lit("IDX").alias("k"))
    wk = _attach(b.select("k", "date", "close"), _period_lines(b, "1w", "k"), "w", 35, "k")
    mo = _attach(b.select("k", "date", "close"), _period_lines(b, "1mo", "k"), "m", 12, "k")
    t = wk.join(mo.drop("close"), on=["k", "date"]).sort("date").with_columns(
        c("close").rolling_mean(20).alias("ma20"), c("close").rolling_mean(60).alias("ma60"))
    return t.select(
        "date",
        (c("close") > c("ma20")).cast(pl.Float64).alias("idx_above_ma20"),
        (c("close") > c("ma60")).cast(pl.Float64).alias("idx_above_ma60"),
        c("w_macd_up").cast(pl.Float64).alias("idx_weekly_macd"),
        c("m_macd_up").cast(pl.Float64).alias("idx_monthly_macd"),
        c("m_ma_up").cast(pl.Float64).alias("idx_monthly_ma5_10"),
    ).fill_null(1.0)
