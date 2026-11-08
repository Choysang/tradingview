"""Research panel: every (code, date) row with the indicators playbooks rely on.

One vectorised Polars pipeline (window expressions `.over("code")`, no Python
row loops). Recursive indicators follow research/indicators.py exactly so a
chart line, a pattern signal and a playbook can never disagree:

- MACD: EMA seeded with the first close (`ewm_mean(adjust=False)`), DIF blank for
  the first 25 bars, DEA/hist blank for the first 33, hist = 2*(DIF-DEA).
- KDJ(9,3,3): K/D seeded at 50 *before* the first RSV bar. `ewm_mean` starts at
  its first element, so every input before that bar is set to the seed (50) and
  the first output equals 2/3*50 + 1/3*RSV.
- Wilder RSI/ATR: first value is the simple mean of the first `period` changes
  (ranges), then `alpha = 1/period` recursion; the same trick — pre-seed
  elements are replaced by the seed so the ewm starts exactly on it.

`cap_est` = total_cap x close / last close of the code: the latest share count
held constant over history (the warehouse stores no historical share capital),
so past market caps are an approximation.

The collected panel is cached per `Warehouse.version()` because 20+ playbooks
and the regime detector share it.
"""
from __future__ import annotations

import threading
from typing import Any

import polars as pl

from ..warehouse.features import daily_features
from .crowd import CROWD_COLUMNS, SCHEMA as CROWD_SCHEMA, crowd_features
from ..warehouse.store import Warehouse

PANEL_COLUMNS: tuple[str, ...] = (
    "code", "date", "open", "high", "low", "close", "volume", "amount",
    "prev_close", "pct_chg", "limit_ratio", "limit_up_price", "limit_down_price",
    "is_limit_up", "touched_limit", "is_broken", "is_limit_down", "one_word",
    "open_at_limit", "open_at_floor", "limit_streak",
    "ma5", "ma10", "ma20", "ma60", "ma120", "ma250", "vol_ma5", "vol_ma20",
    "ret_5", "ret_20", "ret_60", "ret_120", "ret_250", "volatility_20", "atr_14",
    "high_20_prev", "high_55_prev", "high_60_prev", "high_250_prev", "low_20_prev",
    "macd_dif", "macd_dea", "macd_hist", "kdj_k", "kdj_d", "kdj_j", "rsi_6", "rsi_14",
    "boll_mid", "boll_upper", "boll_lower", "days_listed",
    "name", "board", "total_cap", "float_cap", "pe_ttm", "pb",
    "cap_est",
    *CROWD_COLUMNS,
)
SECURITY_COLUMNS = ("name", "board", "total_cap", "float_cap", "pe_ttm", "pb")

_CACHE: dict[str, pl.DataFrame] = {}
_LOCK = threading.Lock()


def _idx() -> pl.Expr:
    return pl.int_range(pl.len()).over("code")


LONG_LISTED = 10_000


def _days_listed() -> pl.Expr:
    """Sessions since listing.

    With a listing date (warehouse table `listings`, from the exchanges): the stock's bars plus the weekdays
    between its listing and its first bar, so a stock listed long before the warehouse starts counts as old
    (weekdays include a few holidays: at most a few percent too many). Without one, the fallback: a stock
    already trading on the warehouse's first date counts as long listed (+LONG_LISTED), a later one counts its
    bars. (Before 2026-11-01 this was the bar count alone, so every stock looked new for its first 120 bars.)"""
    first_bar = pl.col("date").min().over("code")
    before = pl.business_day_count(pl.col("list_date"), first_bar).clip(lower_bound=0)
    already_trading = first_bar == pl.col("date").min()
    fallback = pl.when(already_trading).then(LONG_LISTED).otherwise(0)
    return _idx() + 1 + pl.when(pl.col("list_date").is_not_null()).then(before).otherwise(fallback)


def _ema(expr: pl.Expr, period: int) -> pl.Expr:
    """EMA seeded with the first value, per code."""
    return expr.ewm_mean(alpha=2 / (period + 1), adjust=False).over("code")


def _wilder(raw: pl.Expr, period: int, first: int) -> pl.Expr:
    """Wilder smoothing whose first output (index `first`) is the plain mean of
    `raw[first-period+1 .. first]`. Elements before `first` are replaced by that
    seed so the recursion starts on it; outputs before `first` are null."""
    seed_mean = raw.rolling_mean(period).over("code")
    seed = seed_mean.drop_nulls().first().over("code")
    fed = pl.when(_idx() > first).then(raw).otherwise(seed)
    smoothed = fed.ewm_mean(alpha=1 / period, adjust=False).over("code")
    return pl.when(_idx() >= first).then(smoothed).otherwise(None)


def _indicators(frame: pl.LazyFrame) -> pl.LazyFrame:
    close, high, low, volume = pl.col("close"), pl.col("high"), pl.col("low"), pl.col("volume")
    over = "code"

    def sma(expr: pl.Expr, n: int) -> pl.Expr:
        return expr.rolling_mean(n).over(over)

    frame = frame.with_columns(
        *(sma(close, n).alias(f"ma{n}") for n in (5, 10, 20, 60, 120, 250)),
        sma(volume, 5).alias("vol_ma5"),
        sma(volume, 20).alias("vol_ma20"),
        *(((close / close.shift(n).over(over) - 1) * 100).alias(f"ret_{n}") for n in (5, 20, 60, 120, 250)),
        pl.col("pct_chg").rolling_std(20).over(over).alias("volatility_20"),
        high.rolling_max(20).shift(1).over(over).alias("high_20_prev"),
        high.rolling_max(55).shift(1).over(over).alias("high_55_prev"),
        high.rolling_max(60).shift(1).over(over).alias("high_60_prev"),
        high.rolling_max(250).shift(1).over(over).alias("high_250_prev"),
        low.rolling_min(20).shift(1).over(over).alias("low_20_prev"),
        _days_listed().alias("days_listed"),
        sma(close, 20).alias("boll_mid"),
    ).with_columns(
        (pl.col("boll_mid") + 2 * close.rolling_std(20, ddof=0).over(over)).alias("boll_upper"),
        (pl.col("boll_mid") - 2 * close.rolling_std(20, ddof=0).over(over)).alias("boll_lower"),
    )

    # --- MACD (12, 26, 9) ---------------------------------------------------
    frame = frame.with_columns((_ema(close, 12) - _ema(close, 26)).alias("_dif")).with_columns(
        _ema(pl.col("_dif"), 9).alias("_dea"),
    ).with_columns(
        pl.when(_idx() >= 25).then(pl.col("_dif")).otherwise(None).alias("macd_dif"),
        pl.when(_idx() >= 33).then(pl.col("_dea")).otherwise(None).alias("macd_dea"),
        pl.when(_idx() >= 33).then(2 * (pl.col("_dif") - pl.col("_dea"))).otherwise(None).alias("macd_hist"),
    )

    # --- KDJ (9, 3, 3), K/D seeded at 50 ------------------------------------
    top = high.rolling_max(9).over(over)
    bottom = low.rolling_min(9).over(over)
    rsv = pl.when(top == bottom).then(50.0).otherwise((close - bottom) / (top - bottom) * 100)
    ready = _idx() >= 8
    frame = frame.with_columns(pl.when(ready).then(rsv).otherwise(50.0).alias("_rsv")).with_columns(
        pl.col("_rsv").ewm_mean(alpha=1 / 3, adjust=False).over(over).alias("_k"),
    ).with_columns(
        pl.col("_k").ewm_mean(alpha=1 / 3, adjust=False).over(over).alias("_d"),
    ).with_columns(
        pl.when(ready).then(pl.col("_k")).otherwise(None).alias("kdj_k"),
        pl.when(ready).then(pl.col("_d")).otherwise(None).alias("kdj_d"),
        pl.when(ready).then(3 * pl.col("_k") - 2 * pl.col("_d")).otherwise(None).alias("kdj_j"),
    )

    # --- Wilder RSI(6, 14) and ATR(14) --------------------------------------
    change = close.diff().over(over)
    frame = frame.with_columns(
        change.clip(lower_bound=0).alias("_gain"),
        (-change).clip(lower_bound=0).alias("_loss"),
        pl.max_horizontal(
            high - low,
            (high - pl.col("prev_close")).abs(),
            (low - pl.col("prev_close")).abs(),
        ).alias("_tr"),
    )
    for period in (6, 14):
        gain = _wilder(pl.col("_gain"), period, period)
        loss = _wilder(pl.col("_loss"), period, period)
        frame = frame.with_columns(gain.alias("_ag"), loss.alias("_al")).with_columns(
            pl.when(pl.col("_ag").is_null()).then(None)
            .when((pl.col("_ag") == 0) & (pl.col("_al") == 0)).then(50.0)
            .when(pl.col("_al") == 0).then(100.0)
            .otherwise(100 - 100 / (1 + pl.col("_ag") / pl.col("_al")))
            .alias(f"rsi_{period}")
        )
    frame = frame.with_columns(_wilder(pl.col("_tr"), 14, 13).alias("atr_14"))
    return frame.drop("_dif", "_dea", "_rsv", "_k", "_d", "_gain", "_loss", "_tr", "_ag", "_al")


def _securities(wh: Warehouse) -> pl.LazyFrame | None:
    lazy = wh.scan("securities")
    if lazy is None:
        return None
    present = lazy.collect_schema().names()
    return lazy.select(
        "code",
        *(pl.col(c) if c in present else pl.lit(None).alias(c) for c in SECURITY_COLUMNS),
    ).with_columns(
        pl.col("total_cap").cast(pl.Float64),
        pl.col("float_cap").cast(pl.Float64),
        pl.col("pe_ttm").cast(pl.Float64),
        pl.col("pb").cast(pl.Float64),
    )


def _build(wh: Warehouse) -> pl.DataFrame:
    bars = wh.scan("bars")
    if bars is None:
        return pl.DataFrame(schema={c: CROWD_SCHEMA.get(c, pl.Null) for c in PANEL_COLUMNS})
    present = bars.collect_schema().names()
    bars = bars.select([c for c in ("code", "date", "open", "high", "low", "close", "volume", "amount") if c in present])
    securities = _securities(wh)
    if securities is not None:
        bars = bars.join(securities, on="code", how="left", maintain_order="left")
    else:
        bars = bars.with_columns(*(pl.lit(None).alias(c) for c in SECURITY_COLUMNS))
    listings = wh.scan("listings")
    if listings is not None:
        bars = bars.join(listings.select("code", pl.col("list_date").cast(pl.Date)), on="code", how="left", maintain_order="left")
    else:
        bars = bars.with_columns(pl.lit(None, dtype=pl.Date).alias("list_date"))
    frame = daily_features(bars)
    frame = _indicators(frame)
    frame = frame.with_columns(
        (pl.col("total_cap") * pl.col("close") / pl.col("close").last().over("code")).alias("cap_est"),
    )
    base = frame.select(c for c in PANEL_COLUMNS if c not in CROWD_COLUMNS).sort(["code", "date"]).collect()
    return crowd_features(wh, base).select(PANEL_COLUMNS)


def research_panel(wh: Warehouse) -> pl.LazyFrame:
    """The documented research panel (see playbooks/base.py), cached per warehouse version."""
    key = wh.version()
    with _LOCK:
        cached = _CACHE.get(key)
    if cached is None:
        built = _build(wh)
        with _LOCK:
            _CACHE.clear()  # one live version at a time keeps memory bounded
            _CACHE[key] = built
            cached = built
    return cached.lazy()


def latest_date(panel: pl.LazyFrame | pl.DataFrame) -> Any:
    """Most recent trade date in the panel (a `datetime.date`), or None when empty."""
    lazy = panel.lazy() if isinstance(panel, pl.DataFrame) else panel
    return lazy.select(pl.col("date").max()).collect().item()
