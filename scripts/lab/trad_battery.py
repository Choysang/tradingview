"""Traditional strategy battery: ~75 textbook / contest / trader rules as daily boolean signals on real
daily bars, each tested with one event study (spec: .work/lab/trad_spec.md).

Protocol (same for every rule)
- Universe: rows where harness.tradable() holds on the signal day. Entry at the next open; rows whose next
  day opens at the limit (open_at_limit) are dropped.
- Labels: f5 = open[t+6]/open[t+1]-1, f10 = open[t+11]/open[t+1]-1 (percent), minus the same-day mean over
  tradable rows (f5_ex, f10_ex), minus 0.2% round-trip cost. A label is null unless the t+1 / t+6 / t+11 rows
  sit exactly 1 / 6 / 11 trading days after t on the global calendar (suspension gaps invalidate it).
- Periods: PRE (through 2026-06-30), SELECT (2026-07-01..2026-08-31). VERIFY runs only with env VERIFY=1.
- Stats per rule and period: n_events, n_days, mean f5_ex / f10_ex, win rate, day-clustered t (events are
  averaged per day first). Benjamini-Hochberg (q = 0.10) over all rules, per period, on one-sided normal p.

No rule looks at future bars: every window is trailing, "prior" windows are shifted by one bar, and the
only centered window (the 5-bar local low of w_bottom) is only read for centers at least 2 bars old.
A truncation test (signals on a panel cut at 2026-03-31 must equal the full-panel signals up to that date)
checks this empirically. Set NO_TRUNC=1 to skip it.

Conventions shared by all rules
- Windows are row based per stock (the panel only holds trading days of each stock). "n-day range" of H/L
  includes today unless it says "prior".
- Expressions are built with helpers that wrap EXACTLY ONE `.over(CODE)` around a window-free (raw) inner
  expression, because Polars cannot nest `.over()`. `*_raw` helpers must be wrapped by cnt()/over() before use.
- Rules that need helper columns return (frame_with_helpers, expr); rules that need numpy return an array.
"""
from __future__ import annotations

import math
import os
import sys
import time
from datetime import date
from functools import reduce
from operator import and_, or_
from pathlib import Path

import numpy as np
import polars as pl
from numpy.lib.stride_tricks import sliding_window_view

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H  # noqa: E402
from harness import CODE, c  # noqa: E402

COST = 0.2          # round-trip cost, percent
BH_Q = 0.10
MIN_DAYS_PASS = 10
CAUSAL_CUT = date(2026, 3, 31)
OUT_CSV = H.LAB / "trad_battery.csv"
OUT_GATES = H.LAB / "trad_battery_gates.csv"

C, O, HI, LO, V, A, PC, PCT = (c(x) for x in ("close", "open", "high", "low", "volume", "amount", "prev_close", "pct_chg"))
ZT = c("is_limit_up")


# ----------------------------------------------------------------------------- expression helpers
def sh(e: pl.Expr, n: int = 1) -> pl.Expr:
    """Value n bars ago (e must be window-free)."""
    return e.shift(n).over(CODE)


def lag(e: pl.Expr, n: int) -> pl.Expr:
    return e if n == 0 else sh(e, n)


def _p(e: pl.Expr, prior: bool) -> pl.Expr:
    return e.shift(1) if prior else e


def rmax(e, n, prior=False):
    return _p(e, prior).rolling_max(n).over(CODE)


def rmin(e, n, prior=False):
    return _p(e, prior).rolling_min(n).over(CODE)


def rmean(e, n, prior=False):
    return _p(e, prior).rolling_mean(n).over(CODE)


def cnt(e, n, prior=False):
    """Number of True in the last n bars (prior = excluding today)."""
    return _p(e.cast(pl.Int32), prior).rolling_sum(n).over(CODE)


def xup_raw(a, b):
    return (a > b) & (a.shift(1) <= b.shift(1))


def xdn_raw(a, b):
    return (a < b) & (a.shift(1) >= b.shift(1))


def xup(a, b):
    return xup_raw(a, b).over(CODE)


def allof(es):
    return reduce(and_, es)


def anyof(es):
    return reduce(or_, es)


def cs_pct(col: str) -> pl.Expr:
    """Cross-sectional percentile (0..1] of `col` among tradable rows of the same day (null if not tradable)."""
    x = pl.when(c("_ok")).then(c(col)).otherwise(None)
    return x.rank("average").over("date") / x.count().over("date")


# ----------------------------------------------------------------------------- base frame
PANEL_COLS = [
    "code", "date", "open", "high", "low", "close", "volume", "amount", "prev_close", "pct_chg",
    "is_limit_up", "touched_limit", "is_broken", "is_limit_down", "one_word", "open_at_limit", "limit_streak",
    "ma5", "ma10", "ma20", "ma60", "ma120", "ma250", "ret_5", "ret_20", "ret_250",
    "macd_dif", "macd_dea", "macd_hist", "kdj_k", "kdj_d", "rsi_6", "rsi_14",
    "boll_mid", "boll_upper", "boll_lower", "industry", "high_20_prev", "ind_limit_ups",
]


def build_base(panel: pl.DataFrame, bench: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Per-row frame (sorted by code, date) with shared helper columns, plus the daily market frame."""
    fc = H.float_cap_pit()
    p = panel.sort("code", "date").with_columns(
        H.tradable().fill_null(False).alias("_ok"),
        pl.when(fc > 0).then(c("amount") / fc * 100).alias("turn"),
    )
    d = p.select(*PANEL_COLS, "_ok", "turn").with_columns(pl.col(pl.Float64).fill_nan(None))
    del p

    # ---- daily market series from the whole panel
    mk = d.group_by("date").agg(
        c("is_limit_up").sum().cast(pl.Int64).alias("zt_cnt"),
        c("is_limit_down").sum().cast(pl.Int64).alias("dt_cnt"),
        c("touched_limit").sum().cast(pl.Int64).alias("touched"),
        c("is_broken").sum().cast(pl.Int64).alias("broken"),
        c("limit_streak").max().alias("max_streak"),
        (c("pct_chg") > 0).mean().alias("up_ratio"),
        c("amount").sum().alias("tot_amount"),
    ).sort("date")
    # yesterday's limit-up cohort: mean return of those stocks today (known at today's close)
    prem = (
        d.select("date", "pct_chg", ZT.shift(1).over(CODE).fill_null(False).alias("_zp"))
        .filter(c("_zp")).group_by("date").agg(c("pct_chg").mean().alias("prem"))
    )
    idx = bench.select("date", c("close").alias("idx_close"))
    mk = (
        mk.join(prem, on="date", how="left").join(idx, on="date", how="left").sort("date")
        .with_columns(
            pl.when(c("touched") > 0).then(c("broken") / c("touched")).otherwise(0.0).alias("broken_rate"),
            (c("idx_close") / c("idx_close").shift(1) - 1).alias("idx_ret"),
        )
        .with_columns(
            (c("idx_close") > c("idx_close").rolling_mean(5)).alias("index_ma5_gate"),
            (c("idx_close") > c("idx_close").rolling_mean(20)).alias("index_ma20_gate"),
            (c("idx_close") > c("idx_close").rolling_mean(60)).alias("index_ma60_gate"),
            (~((c("zt_cnt") > 80) & (c("max_streak") > 5) & (c("broken_rate") < 0.15))).alias("euphoria_off"),
            ((c("idx_ret") <= -0.002) & (c("tot_amount") > c("tot_amount").shift(1))).fill_null(False).alias("_dn_up"),
            # ice point: zt_cnt in the bottom 10% of its PRIOR 250 days (min 120 days of history) or a washout day
            (
                (c("zt_cnt") <= c("zt_cnt").shift(1).rolling_quantile(0.10, window_size=250, min_samples=120))
                | ((c("zt_cnt") < 30) & (c("dt_cnt") > 10))
            ).fill_null(False).alias("ice"),
        )
        .with_columns((c("_dn_up").cast(pl.Int32).rolling_sum(25, min_samples=1) < 5).alias("distribution_gate"))
    )
    ind = d.group_by("date", "industry").agg(ZT.sum().cast(pl.Int64).alias("ind_zt"))
    d = (
        d.join(mk.select("date", "prem", "idx_close", "ice"), on="date", how="left")
        .join(ind, on=["date", "industry"], how="left")
        .sort("code", "date")
    )

    # ---- stage A: window columns on raw inputs
    d = d.with_columns(
        (C - O).abs().alias("body"),
        (HI - pl.max_horizontal(O, C)).alias("upsh"),
        (pl.min_horizontal(O, C) - LO).alias("lowsh"),
        C.rolling_mean(30).over(CODE).alias("ma30"),
        C.rolling_mean(50).over(CODE).alias("ma50"),
        C.rolling_mean(150).over(CODE).alias("ma150"),
        C.rolling_mean(200).over(CODE).alias("ma200"),
        V.rolling_mean(5).over(CODE).alias("vma5"),
        V.rolling_mean(10).over(CODE).alias("vma10"),
        V.rolling_mean(20).over(CODE).alias("vma20"),
        HI.shift(1).rolling_max(20).over(CODE).alias("hh20p"),
        LO.shift(1).rolling_min(20).over(CODE).alias("ll20p"),
        HI.rolling_max(60).over(CODE).alias("hhv60"),
        LO.rolling_min(60).over(CODE).alias("llv60"),
        HI.rolling_max(120).over(CODE).alias("hhv120"),
        LO.rolling_min(120).over(CODE).alias("llv120"),
        (C >= C.rolling_max(60).over(CODE)).alias("hi60c"),
        (PCT.sign() * V).fill_null(0.0).cum_sum().over(CODE).alias("obv"),
        pl.int_range(pl.len()).over(CODE).alias("_pos"),
        cs_pct("ret_5").alias("rk5"),
        cs_pct("ret_20").alias("rk20"),
        cs_pct("ret_250").alias("rk250"),
    )
    # ---- stage B: values that depend on stage A
    d = d.with_columns(
        c("vma5").shift(1).over(CODE).alias("vma5p"),
        c("vma10").shift(1).over(CODE).alias("vma10p"),
        c("vma20").shift(1).over(CODE).alias("vma20p"),
        pl.when(c("hhv60") > c("llv60")).then((C - c("llv60")) / (c("hhv60") - c("llv60"))).alias("pos60"),
        pl.when(c("hhv120") > c("llv120")).then((C - c("llv120")) / (c("hhv120") - c("llv120"))).alias("pos120"),
    )
    d = d.with_columns(pl.when(c("vma5p") > 0).then(V / c("vma5p")).alias("vr5"))
    return d, mk


# ----------------------------------------------------------------------------- rule registry
RULES: dict[str, tuple[str, object]] = {}


def rule(rid: str, group: str):
    def deco(fn):
        RULES[rid] = (group, fn)
        return fn
    return deco


# ============================================================================= K-line
@rule("xianren_zhilu", "kline")
def _(d, res):
    # INTERP: "within the last 20 days" includes today.
    return allof([c("upsh") > 0.02 * PC, c("body") < 0.02 * PC, c("turn").is_between(3, 7), V > c("vma5p"),
                  C > c("ma20"), cnt(ZT | (PCT > 7), 20) > 0])


@rule("duofang_pao", "kline")
def _(d, res):
    b = c("body")
    return allof([sh(C > O, 2), sh(PCT, 2) > 3, sh(b, 1) < sh(b, 2), sh(C < O, 1), C > sh(C, 2), V > sh(V, 1),
                  C > c("ma20")])


@rule("rising_three", "kline")
def _(d, res):
    b = c("body")
    es = [sh(PCT, 4) >= 4, C > sh(C, 4), C > O]
    for k in (1, 2, 3):
        es += [sh(b, k) < sh(b, 4), sh(C, k) >= sh(LO, 4), sh(C, k) <= sh(HI, 4)]
    return allof(es)


@rule("bottom_three_yang", "kline")
def _(d, res):
    # INTERP: range of H/L over 120 days incl. today; "rising C" = C > C(t-1) > C(t-2).
    return allof([C > O, sh(C > O, 1), sh(C > O, 2), C > sh(C, 1), sh(C, 1) > sh(C, 2), C < c("ma60"),
                  c("pos120") <= 0.3, V > sh(V, 2)])


@rule("xuri_dongsheng", "kline")
def _(d, res):
    return allof([sh(PCT, 1) < -2, O > PC, C > O, C > sh(O, 1), C < c("ma20")])


@rule("piercing", "kline")
def _(d, res):
    return allof([sh(PCT, 1) < -3, O < sh(LO, 1), C > O, C > (sh(O, 1) + sh(C, 1)) / 2, C < sh(O, 1),
                  V > 1.3 * c("vma5p")])


@rule("bullish_harami", "kline")
def _(d, res):
    hi1, lo1 = sh(pl.max_horizontal(O, C), 1), sh(pl.min_horizontal(O, C), 1)
    return allof([sh(PCT, 1) < -3, C > O, O >= lo1, C <= hi1, c("body") < 0.5 * sh(c("body"), 1),
                  LO <= 1.03 * rmin(LO, 20)])


@rule("inverted_hammer_confirm", "kline")
def _(d, res):
    b1 = sh(c("body"), 1)
    return allof([sh(c("upsh"), 1) > 2 * b1, sh(c("lowsh"), 1) < 0.3 * b1, sh(c("pos60"), 1) <= 0.25, C > O,
                  C > sh(HI, 1)])


@rule("big_yang_break", "kline")
def _(d, res):
    return allof([PCT >= 5, c("body") >= 0.04 * PC, C > c("hh20p"), V >= 2 * c("vma5p")])


@rule("gap_up_hold", "kline")
def _(d, res):
    # INTERP: gap day = t-3 (L(t-3) > H(t-4)); the gap's lower edge is H(t-4); closes of t-2, t-1, t must stay above it.
    edge = sh(HI, 4)
    return allof([sh(LO, 3) > edge, sh(C, 2) > edge, sh(C, 1) > edge, C > edge])


@rule("tweezer_bottom", "kline")
def _(d, res):
    return allof([(LO - sh(LO, 1)).abs() < 0.003 * LO, sh(C < O, 1), C > O, LO <= 1.02 * rmin(LO, 20)])


@rule("double_pin", "kline")
def _(d, res):
    # INTERP: pin = lower shadow > 2*body (literal). Pair (a, b) = (older, newer) offsets with a-b >= 2 so that
    # "between them" is non-empty; the newer bar may be today.
    pin = c("lowsh") > 2 * c("body")
    d = d.with_columns(
        *[lag(pin, k).alias(f"_pin{k}") for k in range(10)],
        *[lag(LO, k).alias(f"_l{k}") for k in range(10)],
        *[lag(V, k).alias(f"_v{k}") for k in range(10)],
        *[lag(C, k).alias(f"_c{k}") for k in range(10)],
    )
    terms = []
    for a in range(2, 10):
        for b in range(0, a - 1):
            between = pl.max_horizontal([c(f"_c{k}") for k in range(b + 1, a)])
            terms.append(allof([c(f"_pin{a}"), c(f"_pin{b}"), (c(f"_l{b}") - c(f"_l{a}")).abs() <= 0.015 * c(f"_l{a}"),
                                c(f"_v{b}") < c(f"_v{a}"), C > between]))
    return d, anyof(terms)


# ============================================================================= moving averages
def _yiyang(with60: bool):
    mas = [c("ma5"), c("ma10"), c("ma20")] + ([c("ma60")] if with60 else [])
    return allof([PCT >= 3, O < pl.min_horizontal(mas), C > pl.max_horizontal(mas), V > 1.5 * c("vma5p"),
                  c("ma20") >= sh(c("ma20"), 1)])


@rule("yiyang_3line", "ma")
def _(d, res):
    return _yiyang(False)


@rule("yiyang_4line", "ma")
def _(d, res):
    return _yiyang(True)


@rule("ma_converge_diverge", "ma")
def _(d, res):
    m5, m10, m20 = c("ma5"), c("ma10"), c("ma20")
    disp = (pl.max_horizontal(m5, m10, m20) - pl.min_horizontal(m5, m10, m20)) / m20
    return allof([rmax(disp, 5, prior=True) <= 0.015, C > pl.max_horizontal(m5, m10, m20), m5 > sh(m5, 1),
                  V > 1.3 * c("vma5p")])


@rule("jin_zhizhu", "ma")
def _(d, res):
    m5, m10, m20 = c("ma5"), c("ma10"), c("ma20")
    return allof([cnt(xup_raw(m5, m10), 3) > 0, cnt(xup_raw(m10, m20), 3) > 0, cnt(xup_raw(m5, m20), 3) > 0, C > m5])


@rule("jinshan_gu", "ma")
def _(d, res):
    m5, m10, m20 = c("ma5"), c("ma10"), c("ma20")
    return allof([xup(m5, c("ma60")), m5 > m10, m10 > m20, m20 > sh(m20, 1)])


@rule("ma_bull_early", "ma")
def _(d, res):
    st = (c("ma5") > c("ma10")) & (c("ma10") > c("ma20")) & (c("ma20") > c("ma60"))
    return allof([st, cnt(st, 5, prior=True) == 0, c("ma20") > sh(c("ma20"), 1), c("ma60") > sh(c("ma60"), 1)])


@rule("first_above_ma60", "ma")
def _(d, res):
    m60 = c("ma60")
    return allof([xup(C, m60), cnt(C > m60, 20, prior=True) == 0, m60 >= 0.999 * sh(m60, 1), V > 1.2 * c("vma20p")])


@rule("pullback_ma250", "ma")
def _(d, res):
    m = c("ma250")
    return allof([C > m, LO <= 1.01 * m, C >= 0.99 * m, C > O, cnt(C > 1.05 * m, 20, prior=True) > 0])


@rule("break_ma250", "ma")
def _(d, res):
    m = c("ma250")
    return allof([xup(C, m), V >= 1.5 * c("vma20p"), m >= sh(m, 20)])


@rule("pullback_ma10_leader", "ma")
def _(d, res):
    # INTERP: "within 10 days": >= 2 ZT in the last 10 bars, or the max 3-day return over the last 10 bars > 20%.
    leader = (cnt(ZT, 10) >= 2) | (rmax(C / C.shift(3) - 1, 10) > 0.20)
    return allof([leader, LO <= 1.01 * c("ma10"), C >= c("ma10"), V < c("vma5p")])


@rule("ma10_30_golden", "ma")
def _(d, res):
    return allof([xup(c("ma10"), c("ma30")), V > c("vma10p"), c("ma30") > sh(c("ma30"), 1)])


@rule("ma20_turn_up", "ma")
def _(d, res):
    m = c("ma20")
    return allof([m > sh(m, 1), cnt(m < m.shift(1), 5, prior=True) == 5, C > m])


@rule("ma_tangle_break", "ma")
def _(d, res):
    ms = [c("ma5"), c("ma10"), c("ma20"), c("ma60")]
    disp = (pl.max_horizontal(ms) - pl.min_horizontal(ms)) / c("ma60")
    return allof([rmax(disp, 10, prior=True) <= 0.03, C > pl.max_horizontal(ms), PCT > 3])


@rule("ma5_cross_ma60", "ma")
def _(d, res):
    return allof([xup(c("ma5"), c("ma60")), c("ma60") >= sh(c("ma60"), 1), C > c("ma20")])


@rule("bias_revert", "ma")
def _(d, res):
    return (C <= 0.92 * c("ma20")) & (C > O)


# ============================================================================= volume and price
@rule("beiliang_zhu", "volprice")
def _(d, res):
    return allof([V >= 2 * sh(V, 1), C > O, PCT > 3, c("pos60") <= 0.5])


@rule("beiliang_pullback", "volprice")
def _(d, res):
    d = d.with_columns(pl.Series("_bz", res["beiliang_zhu"]))
    # INTERP: any beiliang_zhu day among the prior 10 that satisfies the two conditions counts.
    return d, anyof([sh(c("_bz"), k) & (V < 0.6 * sh(V, k)) & (C >= sh(LO, k)) for k in range(1, 11)])


@rule("suoliang_zt", "volprice")
def _(d, res):
    return allof([ZT, HI != LO, V < 0.8 * sh(V, 1)])


@rule("fangliang_zt", "volprice")
def _(d, res):
    return allof([ZT, HI != LO, V >= 2 * c("vma5p"), c("turn") > 8])


@rule("liangjia_qisheng", "volprice")
def _(d, res):
    # INTERP: three consecutive up-steps in both C and V (t-3 < t-2 < t-1 < t), each of the 3 days with pct in (0, 7).
    es = []
    for k in range(3):
        es += [lag(C, k) > lag(C, k + 1), lag(V, k) > lag(V, k + 1), lag(PCT, k) > 0, lag(PCT, k) < 7]
    return allof(es)


@rule("diliang", "volprice")
def _(d, res):
    return (V <= 1.05 * rmin(V, 60)) & (c("pos60") <= 0.25)


@rule("break_retest", "volprice")
def _(d, res):
    # INTERP: "big up day" = big_yang_break signal; breakout level = that day's prior HHV(H,20).
    d = d.with_columns(pl.Series("_bb", res["big_yang_break"]))
    base = (V < c("vma5p")) & (C > O)
    terms = [sh(c("_bb"), k) & (LO >= 0.98 * sh(c("hh20p"), k)) for k in range(1, 11)]
    return d, base & anyof(terms)


@rule("obv_break", "volprice")
def _(d, res):
    # OBV = cumulative sum of sign(pct)*V from the start of each stock's history.
    return (c("obv") >= rmax(c("obv"), 60)) & (C < 0.97 * rmax(C, 60))


@rule("obv_divergence", "volprice")
def _(d, res):
    return allof([C <= rmin(C, 20), c("obv") > rmin(c("obv"), 20), C > O])


@rule("vol_surge_first", "volprice")
def _(d, res):
    return allof([V >= 3 * c("vma5p"), PCT > 3, PCT < 9, rmax(c("vr5"), 10, prior=True) < 2, C > c("ma20")])


@rule("dui_liang", "volprice")
def _(d, res):
    r5 = C / sh(C, 5) - 1
    return allof([cnt(V > c("vma20p"), 5) >= 4, r5 >= 0.03, r5 <= 0.12, C >= c("ma20")])


@rule("wash_shrink", "volprice")
def _(d, res):
    return allof([rmax(HI, 5) / rmin(LO, 5) - 1 <= 0.05, c("vma5") <= 0.6 * sh(c("vma5"), 5),
                  c("ma20") > sh(c("ma20"), 1)])


# ============================================================================= pattern breakouts
@rule("box_break", "pattern")
def _(d, res):
    hh, ll = rmax(HI, 30, prior=True), rmin(LO, 30, prior=True)
    return allof([hh / ll - 1 <= 0.20, C > hh, V >= 1.5 * c("vma20p")])


@rule("donchian20", "pattern")
def _(d, res):
    return C > c("hh20p")


@rule("w_bottom", "pattern")
def _(d, res):
    """Two confirmed 5-bar local lows (centered window, only read for centers >= 2 bars old), 10-40 bars apart,
    both inside the last 60 bars, within 3% of each other; neckline = max H from first to second low (inclusive);
    C > 1.01*neckline; V > 1.3*VMA20(prior). Evaluated on tradable rows with the volume condition only."""
    ok, pos = d["_ok"].to_numpy(), d["_pos"].to_numpy()
    Lw, Cc, Vv, vp = (d[k].to_numpy() for k in ("low", "close", "volume", "vma20p"))
    lf = d.select((LO == LO.rolling_min(5, center=True).over(CODE)).fill_null(False)).to_series().to_numpy()
    idx = np.flatnonzero(ok & (Vv > 1.3 * vp) & (pos >= 12))
    pi = pos[idx]
    lfk, lk = {}, {}
    for k in range(2, 60):
        j = np.maximum(idx - k, 0)
        lfk[k] = lf[j] & (pi >= k)
        lk[k] = Lw[j]
    hit = np.zeros(len(idx), bool)
    for g in range(10, 41):
        hmax = d.select(HI.rolling_max(g + 1).over(CODE)).to_series().to_numpy()
        for a in range(2, 60 - g):
            b = a + g
            m = lfk[a] & lfk[b]
            if not m.any():
                continue
            neck = hmax[np.maximum(idx - a, 0)]
            hit |= m & (np.abs(lk[a] - lk[b]) <= 0.03 * lk[b]) & (Cc[idx] > 1.01 * neck)
    out = np.zeros(d.height, bool)
    out[idx[hit]] = True
    return out


@rule("n_pattern", "pattern")
def _(d, res):
    # INTERP: peak p = t-a (a = 4..11 so that 3-10 bars lie between the peak and today). Run-up = H(p) / min L over
    # p-10..p - 1 >= 15%. Pullback = H(p) - min L over p+1..t-1 <= 50% of the run-up (H(p) - min L). H(p) must be the
    # highest high since p. Today C > H(p) and V > VMA5(prior).
    terms = []
    for a in range(4, 12):
        pk = sh(HI, a)
        ru_low = LO.rolling_min(11).shift(a).over(CODE)
        pb_min = rmin(LO, a - 1, prior=True)
        terms.append(allof([pk >= 1.15 * ru_low, (pk - pb_min) <= 0.5 * (pk - ru_low),
                            rmax(HI, a - 1, prior=True) <= pk, C > pk]))
    return anyof(terms) & (V > c("vma5p"))


@rule("golden_pit", "pattern")
def _(d, res):
    # INTERP (closes): pre-drop bar d0 = t-n-m, trough b = t-n (lowest close since d0), drop C[b] <= 0.9*C[d0] within
    # m <= 5 bars, rise C[t] >= 1.1*C[b] within n <= 5 bars (signal on the completing day); C[t] > LLV(L,5) as of d0.
    terms = []
    for m in range(1, 6):
        for n in range(1, 6):
            terms.append(allof([sh(C, n) <= 0.9 * sh(C, n + m), C >= 1.1 * sh(C, n), sh(C, n) <= rmin(C, m + n),
                                C > LO.rolling_min(5).shift(n + m).over(CODE)]))
    return anyof(terms)


@rule("flag_break", "pattern")
def _(d, res):
    # INTERP: pole ends at p = t-1-m (flag length m = 5..15 bars p+1..t-1); pole = 5-bar close return >= 20%;
    # pullback = C[p] - min L over the flag <= 38% of the pole gain; VMA5 at t-1 below VMA5 at p; C > HHV(H) of flag.
    terms = []
    for m in range(5, 16):
        a = m + 1
        gain = sh(C, a) - sh(C, a + 5)
        terms.append(allof([sh(C, a) >= 1.2 * sh(C, a + 5), sh(C, a) - rmin(LO, m, prior=True) <= 0.38 * gain,
                            sh(c("vma5"), 1) < sh(c("vma5"), a), C > rmax(HI, m, prior=True)]))
    return anyof(terms)


@rule("new_high_pullback", "pattern")
def _(d, res):
    return allof([cnt(c("hi60c"), 10, prior=True) > 0, (LO / c("ma10") - 1).abs() <= 0.01, V < c("vma5p")])


@rule("false_breakdown", "pattern")
def _(d, res):
    return (LO < c("ll20p")) & (C > c("ll20p"))


# ============================================================================= limit-up and short term
@rule("zt_double", "limitup")
def _(d, res):
    # INTERP: "the volume falls" = every bar between the two ZT days has V below the first ZT day's V.
    terms = [sh(ZT, k) & (rmin(C, k - 1, prior=True) >= sh(O, k)) & (rmax(V, k - 1, prior=True) < sh(V, k))
             for k in range(3, 7)]
    return ZT & anyof(terms)


@rule("kongzhong_jiayou", "limitup")
def _(d, res):
    return allof([sh(ZT | (PCT >= 7), 1), (HI - LO) < 0.05 * PC, PCT.abs() < 3, LO > sh(C, 2)])


@rule("zt_tight", "limitup")
def _(d, res):
    terms = [sh(ZT, k) & (rmax((HI - LO) / PC, k) < 0.05) & (rmax(V, k) < 0.5 * sh(V, k)) & (rmin(C, k) > 0.97 * sh(C, k))
             for k in (3, 4, 5)]
    return anyof(terms) & (C > O)


@rule("yang_then_zt", "limitup")
def _(d, res):
    return allof([ZT, c("limit_streak") == 1, sh(C > O, 1), sh(C > O, 2), sh(C > O, 3), C > rmax(C, 20, prior=True)])


@rule("daying_then_zt", "limitup")
def _(d, res):
    return (sh(PCT, 1) <= -5) & ZT


@rule("zt_pullback_ma5", "limitup")
def _(d, res):
    return allof([cnt(ZT, 10) >= 2, LO <= 1.01 * c("ma5"), C >= c("ma5"), C > O])


@rule("zt_gene_first", "limitup")
def _(d, res):
    # INTERP: count of ZT in the last 250 bars incl. today (min_samples=1: the truncated count is a lower bound).
    n = ZT.cast(pl.Int32).rolling_sum(250, min_samples=1).over(CODE)
    return (n >= 5) & (c("limit_streak") == 1)


@rule("leader_first_red", "limitup")
def _(d, res):
    return allof([sh(c("limit_streak"), 1) >= 3, ~ZT, C < O])


@rule("weak_to_strong", "limitup")
def _(d, res):
    # prem = mean pct today of the stocks that closed limit-up yesterday (whole panel), known at today's close.
    weak = sh(c("is_broken"), 1) | (sh(c("upsh"), 1) > 0.03 * sh(PC, 1))
    return allof([weak, O > PC, C >= O, c("prem") > 0])


@rule("leader_dip_buy", "limitup")
def _(d, res):
    # INTERP: "L >= LLV(L,3)" is a tautology if it includes today, so it is read as L >= prior-3-day lowest low.
    # "peak close since then" is approximated by the max close of the prior 30 bars.
    return allof([rmax(c("limit_streak"), 30, prior=True) >= 3, C <= 0.75 * rmax(C, 30, prior=True),
                  allof([lag(V, k) < lag(c("vma5p"), k) for k in range(3)]), LO >= rmin(LO, 3, prior=True)])


# ============================================================================= indicators
@rule("macd_hist_expand", "indicator")
def _(d, res):
    h = c("macd_hist")
    return allof([h > 0, h > sh(h, 1), sh(h, 1) > sh(h, 2), sh(h, 2) > sh(h, 3), c("macd_dif") > 0])


@rule("macd_second_cross", "indicator")
def _(d, res):
    dif, dea = c("macd_dif"), c("macd_dea")
    return allof([xup(dif, dea), dif > 0, dea > 0, cnt(xdn_raw(dif, dea), 20, prior=True) > 0, C > c("ma20")])


@rule("macd_green_shrink", "indicator")
def _(d, res):
    h = c("macd_hist")
    return allof([h < 0, h > sh(h, 1), sh(h, 1) > sh(h, 2), sh(h, 2) > sh(h, 3), C > c("ma5")])


@rule("kdj_second_cross", "indicator")
def _(d, res):
    k, dd = c("kdj_k"), c("kdj_d")
    return allof([xup(k, dd), k > 50, cnt(xdn_raw(k, dd), 10, prior=True) > 0, rmin(dd, 10) >= 40])


@rule("rsi_cross_50", "indicator")
def _(d, res):
    r = c("rsi_14")
    return allof([r > 50, sh(r, 1) <= 50, C > c("ma60")])


@rule("rsi_bull_div", "indicator")
def _(d, res):
    """C at a 20-day low close, RSI6 < 30 and RSI6 above its value on the lowest close of days t-20..t-5."""
    ok, pos = d["_ok"].to_numpy(), d["_pos"].to_numpy()
    Cc, r6 = d["close"].to_numpy(), d["rsi_6"].to_numpy()
    c20 = d.select(rmin(C, 20)).to_series().to_numpy()
    idx = np.flatnonzero(ok & (Cc <= c20) & (r6 < 30) & (pos >= 20))
    offs = np.arange(20, 4, -1)
    win = Cc[idx[:, None] - offs[None, :]]
    prev = idx - offs[np.argmin(win, axis=1)]
    hit = r6[idx] > r6[prev]
    out = np.zeros(d.height, bool)
    out[idx[hit]] = True
    return out


@rule("boll_expand", "indicator")
def _(d, res):
    bw = (c("boll_upper") - c("boll_lower")) / c("boll_mid")
    low_hist = (bw.shift(1) <= bw.shift(1).rolling_quantile(0.20, window_size=120)).over(CODE)
    return allof([bw >= 1.3 * sh(bw, 5), C > c("boll_upper"), low_hist])


@rule("wr_oversold", "indicator")
def _(d, res):
    d = d.with_columns(rmax(HI, 14).alias("_h14"), rmin(LO, 14).alias("_l14"))
    d = d.with_columns(pl.when(c("_h14") > c("_l14")).then((c("_h14") - C) / (c("_h14") - c("_l14")) * -100).alias("_wr"))
    wr = c("_wr")
    return d, allof([wr > -80, sh(wr, 1) <= -80, rmin(wr, 3, prior=True) < -90])


def _cci_array(d: pl.DataFrame, n: int = 14) -> np.ndarray:
    tp = ((d["high"] + d["low"] + d["close"]) / 3).to_numpy()
    pos = d["_pos"].to_numpy()
    out = np.full(len(tp), np.nan)
    blk = 400_000
    for s in range(0, len(tp) - n + 1, blk):
        e = min(s + blk, len(tp) - n + 1)
        w = sliding_window_view(tp[s:e + n - 1], n)
        m = w.mean(axis=1)
        mad = np.abs(w - m[:, None]).mean(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            v = (tp[s + n - 1:e + n - 1] - m) / (0.015 * mad)
        v[mad <= 0] = np.nan
        out[s + n - 1:e + n - 1] = v
    out[pos < n - 1] = np.nan  # windows that would span two stocks
    return out


@rule("cci_cross_up", "indicator")
def _(d, res):
    d = d.with_columns(pl.Series("_cci", _cci_array(d), nan_to_null=True))
    cci = c("_cci")
    return d, (cci > -100) & (sh(cci, 1) <= -100)


@rule("adx_trend", "indicator")
def _(d, res):
    # INTERP: Wilder smoothing = EMA with alpha 1/14 (adjust=False), seeded by the first value (not the first-14 SMA).
    w = dict(alpha=1 / 14, adjust=False, min_samples=14)
    d = d.with_columns(
        pl.max_horizontal(HI - LO, (HI - PC).abs(), (LO - PC).abs()).alias("_tr"),
        (HI - HI.shift(1)).over(CODE).alias("_up"),
        (LO.shift(1) - LO).over(CODE).alias("_dn"),
    )
    d = d.with_columns(
        pl.when((c("_up") > c("_dn")) & (c("_up") > 0)).then(c("_up")).otherwise(0.0).alias("_pdm"),
        pl.when((c("_dn") > c("_up")) & (c("_dn") > 0)).then(c("_dn")).otherwise(0.0).alias("_mdm"),
    )
    d = d.with_columns(*(c(k).ewm_mean(**w).over(CODE).alias(k + "s") for k in ("_tr", "_pdm", "_mdm")))
    d = d.with_columns(
        pl.when(c("_trs") > 0).then(100 * c("_pdms") / c("_trs")).alias("_pdi"),
        pl.when(c("_trs") > 0).then(100 * c("_mdms") / c("_trs")).alias("_mdi"),
    )
    d = d.with_columns(
        pl.when(c("_pdi") + c("_mdi") > 0).then(100 * (c("_pdi") - c("_mdi")).abs() / (c("_pdi") + c("_mdi"))).alias("_dx"))
    d = d.with_columns(c("_dx").ewm_mean(**w).over(CODE).alias("_adx"))
    return d, allof([xup(c("_pdi"), c("_mdi")), c("_adx") > 20, c("_adx") > sh(c("_adx"), 1)])


@rule("rev_5_bottom", "indicator")
def _(d, res):
    return (c("rk5") <= 0.10) & (C > O)


@rule("mom_20_top", "indicator")
def _(d, res):
    return (c("rk20") >= 0.90) & (C >= 0.97 * rmax(C, 20))


@rule("rs_new_high", "indicator")
def _(d, res):
    d = d.with_columns((C / c("idx_close")).alias("_rs"))
    return d, (c("_rs") >= rmax(c("_rs"), 60)) & (C < rmax(C, 60))


# ============================================================================= contest and trader methods
@rule("minervini_template", "contest")
def _(d, res):
    return allof([C > c("ma150"), C > c("ma200"), c("ma150") > c("ma200"), c("ma200") > sh(c("ma200"), 21),
                  c("ma50") > c("ma150"), C > c("ma50"), C >= 1.25 * rmin(LO, 250), C >= 0.75 * rmax(HI, 250),
                  c("rk250") >= 0.70])


@rule("minervini_pivot", "contest")
def _(d, res):
    d = d.with_columns(pl.Series("_mt", res["minervini_template"]))
    return d, allof([c("_mt"), C > c("hh20p"), A >= 1.5 * rmean(A, 50, prior=True)])


@rule("vcp_tight", "contest")
def _(d, res):
    # INTERP: ATR% = 10-day mean of true range / close; its 60-day median includes today.
    d = d.with_columns(
        pl.max_horizontal(HI - LO, (HI - PC).abs(), (LO - PC).abs()).alias("_tr"),
        pl.Series("_mt", res["minervini_template"]),
    )
    d = d.with_columns((c("_tr").rolling_mean(10).over(CODE) / C).alias("_atrp"))
    d = d.with_columns(c("_atrp").rolling_median(60).over(CODE).alias("_atrmed"))
    return d, allof([c("_mt"), c("_atrp") < c("_atrmed"), rmax(HI, 10) / rmin(LO, 10) - 1 < 0.10])


@rule("dragon_second_board", "contest")
def _(d, res):
    return (c("limit_streak") == 2) & (c("ind_zt") >= 3)


@rule("ice_point_first_board", "contest")
def _(d, res):
    return c("ice") & (c("limit_streak") == 1)


# ----------------------------------------------------------------------------- evaluation of the rules
def run_rules(d: pl.DataFrame, quiet: bool = False) -> tuple[dict[str, np.ndarray], list[tuple[str, str]]]:
    res: dict[str, np.ndarray] = {}
    skipped: list[tuple[str, str]] = []
    for rid, (_, fn) in RULES.items():
        t = time.perf_counter()
        try:
            out = fn(d, res)
            if isinstance(out, np.ndarray):
                sig = out
            else:
                frame, expr = out if isinstance(out, tuple) else (d, out)
                sig = frame.select(expr.fill_null(False).alias("s")).to_series().to_numpy()
            res[rid] = np.asarray(sig, dtype=bool)
        except Exception as exc:  # recorded, never silent
            skipped.append((rid, f"{type(exc).__name__}: {str(exc)[:160]}"))
            print(f"[rule] {rid} FAILED {type(exc).__name__}: {exc}", flush=True)
            continue
        if not quiet:
            n = int((res[rid] & d["_ok"].to_numpy()).sum())
            print(f"[rule] {rid:24s} {time.perf_counter() - t:5.1f}s  tradable signals {n}", flush=True)
    return res, skipped


# ----------------------------------------------------------------------------- labels and statistics
def add_labels(d: pl.DataFrame, dates: list) -> pl.DataFrame:
    cal = {x: i for i, x in enumerate(dates)}
    d = d.with_columns(c("date").replace_strict(cal, return_dtype=pl.Int32).alias("_di"))
    d = d.with_columns(
        *(O.shift(-k).over(CODE).alias(f"_o{k}") for k in (1, 6, 11)),
        *((c("_di").shift(-k) - c("_di")).over(CODE).alias(f"_g{k}") for k in (1, 6, 11)),
        c("open_at_limit").shift(-1).over(CODE).fill_null(False).alias("_block"),
    )
    ok1 = (c("_g1") == 1) & (c("_o1") > 0)
    d = d.with_columns(
        pl.when(ok1 & (c("_g6") == 6) & (c("_o6") > 0)).then((c("_o6") / c("_o1") - 1) * 100).alias("f5"),
        pl.when(ok1 & (c("_g11") == 11) & (c("_o11") > 0)).then((c("_o11") / c("_o1") - 1) * 100).alias("f10"),
    )
    m = lambda col: pl.when(c("_ok")).then(c(col)).otherwise(None).mean().over("date")  # noqa: E731
    return d.with_columns((c("f5") - m("f5") - COST).alias("f5x"), (c("f10") - m("f10") - COST).alias("f10x"))


def bh_reject(p: np.ndarray, q: float = BH_Q) -> np.ndarray:
    m = len(p)
    order = np.argsort(p)
    ok = np.flatnonzero(p[order] <= (np.arange(1, m + 1) / m) * q)
    sig = np.zeros(m, bool)
    if len(ok):
        sig[order[: ok.max() + 1]] = True
    return sig


def one_sided_p(t: float) -> float:
    return 1.0 if not np.isfinite(t) else 0.5 * math.erfc(t / math.sqrt(2))


def event_stats(m: np.ndarray, di: np.ndarray, f5x: np.ndarray, f10x: np.ndarray, ndays: int) -> dict:
    n = int(m.sum())
    nan = float("nan")
    if n == 0:
        return dict(n_events=0, n_days=0, mean_f5_ex=nan, mean_f10_ex=nan, win_rate=nan, mean_f5_day=nan, t_f5=nan, p_one=1.0)
    dd, v = di[m], f5x[m]
    s, k = np.bincount(dd, weights=v, minlength=ndays), np.bincount(dd, minlength=ndays)
    nz = k > 0
    dm = s[nz] / k[nz]
    nd = int(nz.sum())
    sd = dm.std(ddof=1) if nd >= 2 else nan
    t = float(dm.mean() / (sd / math.sqrt(nd))) if nd >= 2 and sd > 0 else nan
    f10 = f10x[m]
    f10 = f10[np.isfinite(f10)]
    return dict(n_events=n, n_days=nd, mean_f5_ex=float(v.mean()), mean_f10_ex=float(f10.mean()) if len(f10) else nan,
                win_rate=float((v > 0).mean()), mean_f5_day=float(dm.mean()), t_f5=t, p_one=one_sided_p(t))


def gate_test(mk: pl.DataFrame, ew: pl.DataFrame, periods: dict[str, tuple[date, date]]) -> list[dict]:
    """Equal-weight tradable portfolio: mean NEXT-day close-to-close return (%) when the gate (known at today's
    close) is on versus off."""
    g = mk.join(ew, on="date", how="left").sort("date").with_columns(c("ew").shift(-1).alias("ew_next"))
    rows = []
    for name in ("index_ma5_gate", "index_ma20_gate", "index_ma60_gate", "euphoria_off", "distribution_gate"):
        for p, (a, b) in periods.items():
            x = g.filter((c("date") >= a) & (c("date") <= b) & c(name).is_not_null() & c("ew_next").is_not_null())
            on, off = x.filter(c(name))["ew_next"].to_numpy(), x.filter(~c(name))["ew_next"].to_numpy()
            se = math.sqrt(on.var(ddof=1) / len(on) + off.var(ddof=1) / len(off)) if len(on) > 1 and len(off) > 1 else float("nan")
            mo = float(on.mean()) if len(on) else float("nan")
            mf = float(off.mean()) if len(off) else float("nan")
            rows.append(dict(gate=name, period=p, n_on=len(on), n_off=len(off), mean_on=mo, mean_off=mf, diff=mo - mf,
                             t_diff=(mo - mf) / se if se and se > 0 else float("nan")))
    return rows


# ----------------------------------------------------------------------------- sanity checks
def sanity(d: pl.DataFrame, sigs: dict[str, np.ndarray], dates: list, elig: np.ndarray, pm: dict[str, np.ndarray]) -> None:
    ok = d["_ok"].to_numpy()
    di = d["_di"].to_numpy()
    nd = len(dates)
    print("\n--- sanity: daily event counts (signal & tradable), per calendar day ---")
    for rid in ("beiliang_zhu", "minervini_template", "donchian20"):
        cts = np.bincount(di[sigs[rid] & ok], minlength=nd)
        late = cts[260:]
        print(f"{rid:20s} total {cts.sum():7d} | all days min {cts.min()} median {int(np.median(cts))} max {cts.max()} "
              f"zero-days {(cts == 0).sum()} | after day 260 min {late.min()} median {int(np.median(late))} max {late.max()}")
    # cross-check donchian20 against the panel's own high_20_prev
    hp = d["high_20_prev"].to_numpy()
    cl = d["close"].to_numpy()
    valid = ok & np.isfinite(hp)
    agree = ((cl > hp) == sigs["donchian20"])[valid].mean()
    print(f"donchian20 vs panel high_20_prev: agreement on tradable rows {agree:.5f}")

    # label check against a manual calculation on one row
    codes = d["code"].to_numpy()
    cand = np.flatnonzero(ok & (di == 300) & np.isfinite(d["f5"].to_numpy()) & np.isfinite(d["f10"].to_numpy()))
    j = int(cand[len(cand) // 2])
    code, dt = codes[j], dates[300]
    sub = H.ctx().panel.filter(c("code") == code).sort("date")
    sd, so = sub["date"].to_list(), sub["open"].to_list()
    r = sd.index(dt)
    assert sd[r + 1] == dates[301] and sd[r + 6] == dates[306] and sd[r + 11] == dates[311], "calendar gap in the check row"
    f5_man, f10_man = (so[r + 6] / so[r + 1] - 1) * 100, (so[r + 11] / so[r + 1] - 1) * 100
    row_f5, row_f10 = float(d["f5"][j]), float(d["f10"][j])
    assert abs(f5_man - row_f5) < 1e-9 and abs(f10_man - row_f10) < 1e-9, (f5_man, row_f5, f10_man, row_f10)
    on_day = ok & (di == 300) & np.isfinite(d["f5"].to_numpy())
    f5x_man = f5_man - d["f5"].to_numpy()[on_day].mean() - COST
    assert abs(f5x_man - float(d["f5x"][j])) < 1e-9, (f5x_man, float(d["f5x"][j]))
    print(f"label check OK: {code} {dt} f5 {f5_man:.4f}% f10 {f10_man:.4f}% f5_ex {f5x_man:.4f}% (manual == column)")
    f5 = d["f5"].to_numpy()
    f5 = f5[ok & np.isfinite(f5)]
    print(f"f5 on tradable rows: mean {f5.mean():.3f}% sd {f5.std():.2f}% min {f5.min():.1f}% max {f5.max():.1f}% n {len(f5)}")
    f5x = d["f5x"].to_numpy()
    for p, m in pm.items():
        b = f5x[elig & m]
        print(f"baseline (all eligible rows) {p}: mean f5_ex {b.mean():+.3f}% (about -{COST}% cost; rows whose next open is at the limit are dropped), n {len(b)}")
    if os.environ.get("NO_XCHECK") != "1":
        # independent label: harness.features(5) builds fwd_ex through its own code path (no calendar-gap guard)
        fx = H.features(5).select("code", "date", "fwd_ex")
        j = fx.join(d.select("code", "date", "f5x"), on=["code", "date"], how="left")
        both = j.filter(c("f5x").is_not_null() & c("fwd_ex").is_not_null() & c("f5x").is_finite() & c("fwd_ex").is_finite())
        both = both.with_columns((c("f5x") + COST - c("fwd_ex").cast(pl.Float64)).alias("_df"))
        diff = both["_df"].abs().to_numpy()
        # My day mean leaves out rows that the calendar-gap guard nulls (resumed-from-suspension returns), so the two
        # labels may differ by a per-day constant; the within-day spread of the difference must be ~0 (float32 noise).
        per = both.group_by("date").agg(c("_df").std().alias("sd"), c("_df").mean().abs().alias("mu"))
        print(f"label cross-check vs harness.features(5): harness rows {fx.height}, compared {len(diff)}, "
              f"mine null (calendar-gap guard) {j['f5x'].null_count()}; raw diff max {diff.max():.4f} "
              f"(share within 0.01: {(diff < 0.01).mean():.3f}); within-day sd of diff max {per['sd'].max():.2e}; "
              f"per-day constant shift median {per['mu'].median():.4f} max {per['mu'].max():.4f}")
        assert per["sd"].max() < 1e-3, "labels differ beyond a per-day constant"


def causality_test(panel: pl.DataFrame, bench: pl.DataFrame, sigs: dict[str, np.ndarray], dates_np: np.ndarray) -> list[str]:
    """Signals computed on a panel cut at CAUSAL_CUT must equal the full-panel signals up to that date."""
    cut = np.datetime64(CAUSAL_CUT)
    keep = dates_np <= cut
    t0 = time.perf_counter()
    dt_, _ = build_base(panel.filter(c("date") <= CAUSAL_CUT), bench.filter(c("date") <= CAUSAL_CUT))
    assert dt_.height == int(keep.sum()), (dt_.height, int(keep.sum()))
    ts, _ = run_rules(dt_, quiet=True)
    bad = []
    print(f"\n--- causality test: signals on the panel cut at {CAUSAL_CUT} vs full panel ({time.perf_counter() - t0:.0f}s) ---")
    for rid, a in ts.items():
        diff = int((a != sigs[rid][keep]).sum())
        if diff:
            bad.append(rid)
            print(f"  MISMATCH {rid}: {diff} rows differ ({int(a.sum())} vs {int(sigs[rid][keep].sum())} signals)")
    print(f"  rules compared {len(ts)}, mismatching {len(bad)}: {bad}")
    return bad


# ----------------------------------------------------------------------------- main
def main() -> None:
    t_start = time.perf_counter()
    k = H.ctx()
    dates = k.dates
    periods = {"PRE": H.period_bounds("PRE"), "SELECT": H.period_bounds("SELECT")}
    if os.environ.get("VERIFY") == "1":  # code path kept; not used unless explicitly requested
        periods["VERIFY"] = H.period_bounds("VERIFY")
    print(f"[battery] periods {periods}", flush=True)

    d, mk = build_base(k.panel, k.benchmark)
    print(f"[battery] base frame {d.height} rows x {d.width} cols, tradable rows {int(d['_ok'].sum())} "
          f"({time.perf_counter() - t_start:.0f}s)", flush=True)
    own = d.select((c("ind_zt") == c("ind_limit_ups")).mean()).item()
    print(f"[battery] own industry ZT count equals panel ind_limit_ups on {own:.4f} of rows", flush=True)

    sigs, skipped = run_rules(d)
    d = add_labels(d, dates)
    print(f"[battery] rules coded {len(sigs)}, skipped {len(skipped)} ({time.perf_counter() - t_start:.0f}s)", flush=True)

    ok, di = d["_ok"].to_numpy(), d["_di"].to_numpy()
    f5x, f10x = d["f5x"].to_numpy(), d["f10x"].to_numpy()
    elig = ok & np.isfinite(f5x) & ~d["_block"].to_numpy()
    dnp = d["date"].to_numpy()
    pm = {p: (dnp >= np.datetime64(a)) & (dnp <= np.datetime64(b)) for p, (a, b) in periods.items()}
    nd = len(dates)

    rows = []
    for rid, sig in sigs.items():
        for p in periods:
            rows.append(dict(rule=rid, group=RULES[rid][0], period=p, **event_stats(sig & elig & pm[p], di, f5x, f10x, nd)))
    out = pl.DataFrame(rows)
    flags = []
    for p in periods:
        sub = out.filter(c("period") == p)
        sig_ = bh_reject(sub["p_one"].to_numpy())
        sub = sub.with_columns(pl.Series("bh_sig", sig_))
        sub = sub.with_columns((c("bh_sig") & (c("mean_f5_ex") > 0) & (c("n_days") >= MIN_DAYS_PASS)).alias("passed"))
        flags.append(sub)
    out = pl.concat(flags).sort("rule", "period")
    out.write_csv(OUT_CSV)
    print(f"[battery] wrote {OUT_CSV}")

    ew = (d.filter(c("_ok") & (PCT.abs() < 35)).group_by("date").agg(PCT.mean().alias("ew")).sort("date"))
    gates = gate_test(mk, ew, periods)
    pl.DataFrame(gates).write_csv(OUT_GATES)

    # ---- full table
    wide = {}
    for r in out.iter_rows(named=True):
        wide.setdefault(r["rule"], {})[r["period"]] = r
    print("\n--- all rules (f5_ex / f10_ex are net of cost, percent) ---")
    print(f"{'rule':24s} " + " | ".join(f"{p:>6s}: events days f5_ex  f10_ex  win%   t   BH pass" for p in periods))
    for rid in sigs:
        cells = []
        for p in periods:
            r = wide[rid][p]
            cells.append(f"{r['n_events']:7d} {r['n_days']:4d} {r['mean_f5_ex']:+6.2f} {r['mean_f10_ex']:+6.2f} "
                         f"{100 * r['win_rate']:5.1f} {r['t_f5']:+5.1f} {'Y' if r['bh_sig'] else '.'}  {'PASS' if r['passed'] else '.'}")
        print(f"{rid:24s} " + " | ".join(cells))

    # ---- survival
    def passed(rid, p):
        return bool(wide[rid][p]["passed"])

    sel_pass = [r for r in sigs if passed(r, "SELECT")]
    robust, regime, undef = [], [], []
    for r in sel_pass:
        pre_mean = wide[r]["PRE"]["mean_f5_ex"]
        if passed(r, "PRE") or (np.isfinite(pre_mean) and pre_mean >= 0):
            robust.append(r)
        elif np.isfinite(pre_mean) and pre_mean < 0:
            regime.append(r)
        else:
            undef.append(r)

    def line(r):
        s, p = wide[r]["SELECT"], wide[r]["PRE"]
        return (f"  {r:24s} SELECT f5_ex {s['mean_f5_ex']:+6.2f}% t {s['t_f5']:+5.2f} days {s['n_days']:3d} events {s['n_events']:5d} | "
                f"PRE f5_ex {p['mean_f5_ex']:+6.2f}% t {p['t_f5']:+5.2f} days {p['n_days']:3d} events {p['n_events']:6d}")

    print("\n===== SUMMARY =====")
    print(f"rules in spec 75 | coded {len(sigs)} | skipped {len(skipped)}")
    for rid, why in skipped:
        print(f"  skipped {rid}: {why}")
    if not skipped:
        print("  (no rule skipped)")
    for p in periods:
        sub = out.filter(c("period") == p)
        tested = int((sub["n_events"] > 0).sum())
        print(f"{p}: rules with events {tested}/{len(sigs)}, BH-significant (q={BH_Q}) {int(sub['bh_sig'].sum())}, "
              f"passed (BH & mean>0 & n_days>={MIN_DAYS_PASS}) {int(sub['passed'].sum())}")
    print(f"survived robust (SELECT pass and PRE pass-or-mean>=0): {len(robust)}")
    for r in robust:
        print(line(r))
    print(f"survived regime-only (SELECT pass, PRE mean<0): {len(regime)}")
    for r in regime:
        print(line(r))
    if undef:
        print(f"SELECT pass with PRE undefined: {len(undef)}")
        for r in undef:
            print(line(r))
    if "VERIFY" in periods:
        print("VERIFY computed (env VERIFY=1)")
    else:
        print("VERIFY not computed")
    print("market gates (equal-weight tradable portfolio, mean NEXT-day close-to-close return %, gate known at close):")
    for g in gates:
        print(f"  {g['gate']:18s} {g['period']:6s} on {g['mean_on']:+.3f}% (n={g['n_on']:3d}) off {g['mean_off']:+.3f}% (n={g['n_off']:3d}) "
              f"diff {g['diff']:+.3f}% t {g['t_diff']:+.2f}")
    print("===== END SUMMARY =====")

    sanity(d, sigs, dates, elig, pm)
    if os.environ.get("NO_TRUNC") != "1":
        del d
        causality_test(k.panel, k.benchmark, sigs, dnp)
    print(f"\n[battery] total runtime {time.perf_counter() - t_start:.0f}s")


if __name__ == "__main__":
    main()
