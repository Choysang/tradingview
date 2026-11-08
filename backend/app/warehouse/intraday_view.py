"""Time-locked view of one recorded day: what the system could have known at a given clock time.

Every snapshot row gets two times (design 1.0 section 7, the 8 time fields):
* `event_time` - the exchange's quote time (`quote_ts`);
* `known_time` - when the answer was in our hands: the poll start (`poll_ts`) plus the poll's normal duration
  (KNOWN_LAG: a 10 s hot poll ~1 s, a whole-board poll ~4 s, both rounded up), because poll_ts is stamped
  before the first request and a whole-board poll takes several batches.
A decision at clock time t may read only rows with known_time <= t (`asof`, `stream`). Rows from both tables
(quotes: auction / auction_final / full / close / after; hot: auction_hot / hot / after_hot) are merged, so a
hot name is seen every 10 s and every other name every 60 s.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import polars as pl

KNOWN_LAG = {"hot": 2.0, "auction_hot": 2.0, "after_hot": 2.0, "full": 5.0, "auction": 5.0, "auction_final": 5.0,
             "close": 5.0, "after": 5.0}
COLUMNS = ("code", "name", "kind", "poll_ts", "quote_ts", "price", "prev_close", "open", "high", "low", "volume_lots",
           "amount_yuan", "bid1_px", "bid1_lots", "ask1_px", "ask1_lots", "limit_up", "limit_down", "vwap", "turnover",
           "float_cap", "after_amount_wan", "after_lots")


def load_day(day_dir: Path) -> pl.DataFrame:
    """All snapshots of a day with event_time / known_time, sorted by (code, known_time)."""
    frames = []
    for table in ("quotes", "hot"):
        path = day_dir / f"{table}.parquet"
        if path.exists():
            have = pl.read_parquet_schema(path)
            frames.append(pl.read_parquet(path, columns=[c for c in COLUMNS if c in have]))
    frame = pl.concat(frames, how="diagonal_relaxed")
    lag = pl.col("kind").replace_strict(KNOWN_LAG, default=5.0, return_dtype=pl.Float64)
    return (frame.with_columns(pl.col("quote_ts").alias("event_time"),
                               (pl.col("poll_ts") + pl.duration(milliseconds=(lag * 1000).cast(pl.Int64))).alias("known_time"))
            .unique(subset=["code", "kind", "poll_ts"], keep="first")
            .sort("code", "known_time"))


def asof(frame: pl.DataFrame, t: datetime, kinds: tuple[str, ...] | None = None) -> pl.DataFrame:
    """The latest row per code known by `t`."""
    rows = frame.filter(pl.col("known_time") <= t)
    if kinds:
        rows = rows.filter(pl.col("kind").is_in(kinds))
    return rows.group_by("code").agg(pl.all().sort_by("known_time").last())


def stream(frame: pl.DataFrame, code: str, start: datetime, end: datetime) -> pl.DataFrame:
    """One code's rows known in [start, end], oldest first (the order a live process would have seen them)."""
    return frame.filter((pl.col("code") == code) & pl.col("known_time").is_between(start, end)).sort("known_time")


def at(day: datetime, hms: str) -> datetime:
    h, m, s = (int(x) for x in hms.split(":"))
    return day.replace(hour=h, minute=m, second=s, microsecond=0)


def seconds(a: datetime, b: datetime) -> float:
    return (b - a) / timedelta(seconds=1)
