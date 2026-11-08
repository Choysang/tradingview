"""Short-term market state at a session's close, from main-board daily bars (E054).

heat: the day's main-board limit-up count ranked within the previous HISTORY sessions (only earlier days, so
the state is known at that close): "cold" below the lower third, "hot" from the upper third, else "mid".
failed-board rate = touched the limit but did not close there / all that touched; "hi_fail" from its upper third.
On 2023-11 ~ 2026-10, selling a held limit-up at the next open beat selling at the next close by 0.5 ~ 0.7
points in mid / hot states and was flat or worse in cold ones; buying yesterday's limit-ups at the next open
lost in every state, most with a high failed-board rate.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import polars as pl

HISTORY, MIN_HISTORY = 250, 60
LOW, HIGH = 1 / 3, 2 / 3
EPS = 1e-6


@dataclass(frozen=True)
class MarketState:
    day: date
    limit_ups: int
    failed_rate: float | None
    heat: str  # cold | mid | hot | unknown
    failed: str  # hi_fail | lo_fail | unknown


def daily_counts(bars: pl.DataFrame) -> pl.DataFrame:
    """Per date: main-board limit-up closes and touches that failed. `bars`: code, date, high, close (main board)."""
    b = bars.unique(["code", "date"]).sort("code", "date").with_columns(pl.col("close").shift(1).over("code").alias("pc"))
    up = (pl.col("pc") * 1.1 * 100).round(0) / 100
    b = b.filter(pl.col("pc").is_not_null()).with_columns((pl.col("close") >= up - EPS).alias("zt"), (pl.col("high") >= up - EPS).alias("touch"))
    out = b.group_by("date").agg(pl.col("zt").sum().alias("limit_ups"), (pl.col("touch") & ~pl.col("zt")).sum().alias("failed"))
    return out.with_columns((pl.col("failed") / (pl.col("limit_ups") + pl.col("failed"))).alias("failed_rate")).sort("date")


def _rank(history: list[float], value: float) -> float:
    return sum(h < value for h in history) / len(history)


def state_at(counts: pl.DataFrame, day: date) -> MarketState:
    """State at the close of `day` (the latest session on or before it in `counts`)."""
    rows = counts.filter(pl.col("date") <= day).sort("date")
    if rows.is_empty():
        return MarketState(day, 0, None, "unknown", "unknown")
    last = rows.row(-1, named=True)
    past = rows.head(rows.height - 1).tail(HISTORY)
    if past.height < MIN_HISTORY:
        return MarketState(last["date"], last["limit_ups"], last["failed_rate"], "unknown", "unknown")
    q = _rank(past["limit_ups"].to_list(), last["limit_ups"])
    heat = "cold" if q < LOW else "hot" if q >= HIGH else "mid"
    rates = [r for r in past["failed_rate"].to_list() if r is not None]
    failed = "unknown" if last["failed_rate"] is None or not rates else \
        "hi_fail" if _rank(rates, last["failed_rate"]) >= HIGH else "lo_fail"
    return MarketState(last["date"], last["limit_ups"], last["failed_rate"], heat, failed)
