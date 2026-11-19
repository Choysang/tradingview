"""Daily panel of the fusion lane: from per-stock bars to the [days x stocks] float32 grids the expressions read.

Why: the factor expressions (``expr``) read fields such as ``$turn`` or ``$is_limit_up`` that are not in a bar. This module
defines each of them once, with the definitions the round-5 scorer was frozen with (legacy triage batch 3):
* ``amount`` is typical price x volume / 1e8 (yuan to 100 million); the legacy source carries no turnover value;
* ``prev_close``/``pct_chg`` are per-stock row shifts (a suspension does not make a gap); limit flags use the board ratio
  (main board 10%) with the limit price rounded to the cent and half a cent of tolerance;
* ``float_cap`` is the float cap in 100 million yuan scaled to each day by ``close / last close`` (the latest share count
  held constant, which is what the source can say); ``turn = amount / float_cap * 100``;
* ``tradable`` = 20-row mean amount >= 0.5, listed >= 120 sessions, close >= 2, not price-ST, main board;
* ``cgo`` (disposition-effect reference price) and ``tk`` (prospect value of 60 returns) are the two behavioural fields.
Invariants: a missing bar is a NaN cell, never 0; no row of a field looks at a later row; codes and dates are sorted.
Not covered: loading from PostgreSQL / the lake (``application.bf_fusion_service``), the factor ranks (``scorer``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import polars as pl

from .expr import F32, nanmean_row, shift

MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")
MIN_AMOUNT, MIN_PRICE, MIN_DAYS = 0.5, 2.0, 120
LONG_LISTED = 10_000
HALF_CENT = 0.005
ST_WINDOW, ST_MIN_HITS, PRICE_TICK = 60, 2, 0.011
MAIN_LIMIT = 0.10
BAR_COLUMNS = ("code", "date", "open", "high", "low", "close", "volume", "float_cap_100m", "has_total_cap", "list_date")
GRID_FIELDS = ("open", "high", "low", "close", "volume", "amount", "prev_close", "pct_chg", "float_cap", "is_limit_up",
               "is_limit_down", "touched_limit", "is_broken", "open_at_limit", "open_at_floor", "limit_streak", "tradable")


def in_main_board(code: pl.Expr) -> pl.Expr:
    return code.str.slice(0, 3).is_in(MAIN_PREFIX)


def st_like_expr() -> pl.Expr:
    """Price-based ST flag: two closes on the exact 5% limit price (at the day's high / low) in 60 rows and no move above
    5.25% in them. Reads ``prev_close``, ``close``, ``high``, ``low``, ``pct_chg``; the windows run over the rows of the frame."""
    c = pl.col
    up5 = (c("prev_close") * 1.05 * 100).round(0) / 100
    down5 = (c("prev_close") * 0.95 * 100).round(0) / 100
    at_limit = (((c("close") - up5).abs() <= PRICE_TICK) & (c("close") >= c("high"))) | (
        ((c("close") - down5).abs() <= PRICE_TICK) & (c("close") <= c("low")))
    hits = at_limit.fill_null(False).cast(pl.Int32).rolling_sum(ST_WINDOW, min_samples=1).over("code")
    wide = (c("pct_chg").abs() > 5.25).fill_null(False).cast(pl.Int8).rolling_max(ST_WINDOW, min_samples=1).over("code") == 1
    return ((hits >= ST_MIN_HITS) & ~wide).fill_null(False)


def tradable_expr() -> pl.Expr:
    """The pool rule: 20-row mean amount >= 0.5, listed >= 120 sessions, close >= 2, not price-ST (reads ``st_like``)."""
    c = pl.col
    liquid = c("amount").rolling_mean(20).over("code") >= MIN_AMOUNT
    return (liquid & (c("days_listed") >= MIN_DAYS) & (c("close") >= MIN_PRICE) & ~c("st_like")).fill_null(False)


def retradable(frame: pl.DataFrame) -> pl.DataFrame:
    """Recompute ``st_like`` and ``tradable`` inside a frame that was cut from the full panel (rolling windows then start at
    the cut, which is what the forward logs of E081 / E085 were frozen with); every other column is kept as it was."""
    return frame.sort(["code", "date"]).with_columns(st_like_expr().alias("st_like")).with_columns(tradable_expr().alias("tradable"))


def build_long(bars: pl.DataFrame) -> pl.DataFrame:
    """Per (code, date) row: every derived column. ``bars`` has ``BAR_COLUMNS``; ``float_cap_100m`` / ``list_date`` may be
    null and ``has_total_cap`` says whether the source knew the total cap (a float cap without it stays unknown)."""
    c = pl.col
    typical = (c("high") + c("low") + c("close")) / 3
    f = bars.lazy().filter(in_main_board(c("code"))).sort(["code", "date"]).with_columns(
        (typical * c("volume") / 1e8).alias("amount"),
        c("close").shift(1).over("code").alias("prev_close"))
    f = f.with_columns(((c("close") / c("prev_close") - 1) * 100).alias("pct_chg"),
                       (c("prev_close") * (1 + MAIN_LIMIT)).round(2).alias("_up"),
                       (c("prev_close") * (1 - MAIN_LIMIT)).round(2).alias("_down"))
    f = f.with_columns((c("close") >= c("_up") - HALF_CENT).fill_null(False).alias("is_limit_up"),
                       (c("high") >= c("_up") - HALF_CENT).fill_null(False).alias("touched_limit"),
                       (c("close") <= c("_down") + HALF_CENT).fill_null(False).alias("is_limit_down"),
                       (c("open") >= c("_up") - HALF_CENT).fill_null(False).alias("open_at_limit"),
                       (c("open") <= c("_down") + HALF_CENT).fill_null(False).alias("open_at_floor"))
    f = f.with_columns((c("touched_limit") & ~c("is_limit_up")).alias("is_broken"),
                       (~c("is_limit_up")).cast(pl.Int32).cum_sum().over("code").alias("_run"))
    f = f.with_columns(c("is_limit_up").cast(pl.Int32).cum_sum().over(["code", "_run"]).alias("limit_streak"),
                       c("pct_chg").rolling_std(20).over("code").alias("volatility_20"))
    f = f.with_columns(st_like_expr().alias("st_like"))
    # sessions since listing: the stock's rows plus the weekdays between listing and its first bar; without a listing date a
    # stock already trading on the first date counts as long listed
    first_bar = c("date").min().over("code")
    before = pl.business_day_count(c("list_date"), first_bar).clip(lower_bound=0)
    fallback = pl.when(first_bar == c("date").min()).then(LONG_LISTED).otherwise(0)
    idx = pl.int_range(pl.len()).over("code")
    f = f.with_columns((idx + 1 + pl.when(c("list_date").is_not_null()).then(before).otherwise(fallback)).alias("days_listed"))
    cap = pl.when(c("has_total_cap")).then(c("float_cap_100m") * c("close") / c("close").last().over("code"))
    f = f.with_columns(cap.alias("float_cap"))
    f = f.with_columns(tradable_expr().alias("tradable"))
    return f.drop("_up", "_down", "_run").collect()


def capital_gains_overhang(close: np.ndarray, turn_pct: np.ndarray) -> np.ndarray:
    """Grinblatt-Han disposition proxy: ``close / RP - 1`` with the turnover-weighted reference price
    ``RP_t = v_t P_t + (1 - v_t) RP_(t-1)`` (v = turnover as a fraction in [0, 1]); NaN for a stock's first 60 bars."""
    v = np.clip(np.nan_to_num(turn_pct / F32(100), nan=0.0), 0, 1).astype(F32)
    rp = np.full(close.shape, np.nan, dtype=F32)
    prev = np.full(close.shape[1], np.nan, dtype=F32)
    age = np.zeros(close.shape[1], dtype=F32)
    for t in range(close.shape[0]):
        p = close[t]
        ok = np.isfinite(p)
        new = np.where(np.isfinite(prev), v[t] * p + (F32(1) - v[t]) * prev, p).astype(F32)
        prev = np.where(ok, new, prev).astype(F32)
        age = age + ok.astype(F32)
        rp[t] = np.where(age >= 60, prev, np.nan)
    with np.errstate(all="ignore"):
        return (close / rp - F32(1)).astype(F32)


def prospect_value(ret: np.ndarray, d: int = 60) -> np.ndarray:
    """Barberis-Mukherjee-Wang prospect value of the last ``d`` returns (Tversky-Kahneman value function, alpha 0.88,
    lambda 2.25, cumulative weighting 0.61 for gains and 0.69 for losses, every return equally likely)."""
    a, lam, dg, dl = 0.88, 2.25, 0.61, 0.69

    def w(p: np.ndarray, delta: float) -> np.ndarray:
        with np.errstate(all="ignore"):
            return p ** delta / (p ** delta + (1 - p) ** delta) ** (1 / delta)

    out = np.full(ret.shape, np.nan, dtype=F32)
    if d > ret.shape[0]:
        return out
    i = np.arange(1, d + 1, dtype=F32)
    wl = (w(i / d, dl) - w((i - 1) / d, dl)).astype(F32)
    j = d - i + 1
    wg = (w(j / d, dg) - w((j - 1) / d, dg)).astype(F32)
    minp = max(1, int(np.ceil(0.75 * d)))
    for start in range(0, ret.shape[1], 600):
        win = np.lib.stride_tricks.sliding_window_view(ret[:, start:start + 600], d, axis=0)
        s = np.sort(np.nan_to_num(win, nan=0.0), axis=-1)
        n = np.isfinite(win).sum(-1)
        with np.errstate(all="ignore"):
            val = np.where(s < 0, -lam * np.maximum(-s, 0) ** a * wl, np.maximum(s, 0) ** a * wg)
        out[d - 1:, start:start + 600] = np.where(n >= minp, val.sum(-1), np.nan)
    return out


@dataclass
class Panel:
    """Grids of the main-board universe. ``f`` holds float32 arrays shaped [T, N] (NaN = no bar)."""

    dates: list[date]
    codes: list[str]
    f: dict[str, np.ndarray]
    tradable: np.ndarray = field(init=False)
    _labels: dict[int, np.ndarray] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        f = self.f
        self.tradable = (f["tradable"] > 0) & np.isfinite(f["close"])

    @property
    def T(self) -> int:
        return len(self.dates)

    @property
    def N(self) -> int:
        return len(self.codes)

    def label(self, h: int) -> np.ndarray:
        """Forward relative return in percent: ``open[t+1+h] / open[t+1] - 1`` minus the day's mean over the names that
        have one. NaN when the name is not tradable at ``t``, a price is missing or the next open is sealed at the limit."""
        if h not in self._labels:
            o = self.f["open"]
            nxt = shift(o, -1)
            with np.errstate(all="ignore"):
                fwd = ((shift(o, -1 - h) / nxt - F32(1)) * F32(100)).astype(F32)
            blocked = shift(self.f["open_at_limit"], -1) > 0
            fwd = np.where(self.tradable & np.isfinite(fwd) & ~blocked, fwd, np.nan).astype(F32)
            self._labels[h] = (fwd - nanmean_row(fwd)[:, None]).astype(F32)
        return self._labels[h]


def to_panel(long: pl.DataFrame) -> Panel:
    """Pivot the long frame into grids and add the derived fields (``vwap`` ... ``tk``)."""
    dates = sorted(long["date"].unique().to_list())
    codes = sorted(long["code"].unique().to_list())
    di = {d: i for i, d in enumerate(dates)}
    ci = {s: i for i, s in enumerate(codes)}
    r = np.fromiter((di[d] for d in long["date"].to_list()), dtype=np.int64, count=long.height)
    k = np.fromiter((ci[s] for s in long["code"].to_list()), dtype=np.int64, count=long.height)

    def grid(col: str) -> np.ndarray:
        a = np.full((len(dates), len(codes)), np.nan, dtype=np.float32)
        a[r, k] = long[col].cast(pl.Float64).fill_null(np.nan).to_numpy().astype(np.float32)
        return a

    f = {x: grid(x) for x in GRID_FIELDS}
    f["turn"] = (f["amount"] / f["float_cap"] * F32(100)).astype(F32)
    f["vwap"] = ((f["high"] + f["low"] + f["close"]) / F32(3)).astype(F32)
    f["ret"] = (f["pct_chg"] / F32(100)).astype(F32)
    live = f["tradable"] > 0
    f["mkt"] = np.broadcast_to(nanmean_row(np.where(live, f["ret"], np.nan).astype(F32))[:, None], f["ret"].shape).astype(F32).copy()
    f["cap"] = f["float_cap"]
    f["cgo"] = capital_gains_overhang(f["close"], f["turn"])
    f["tk"] = prospect_value(f["ret"], 60)
    return Panel(dates, codes, f)
