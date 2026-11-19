"""Native intraday recorder, pure part: vendor quote -> legacy-shaped row -> snapshot frame, limit queues, hot set, poll schedule.

Why: the old recorder's parquet files carry one row per code and poll with a fixed column set (``QUOTE_REQUIRED``); ``LiveService``'s import
turns them into the dataset ``quote_snapshots_60s`` and the seal facts read that dataset. The native recorder must hand downstream exactly
the same thing, so a vendor line is first normalised into the legacy row (same names, same units: prices yuan, book sizes and volumes in
LOTS of 100 shares, amount yuan, caps in 100 million yuan) and then run through the same column casts as ``LegacyRecordings.read_quotes``
(``rows_to_frame``; a test compares the two on the same rows).
Invariants: nothing here touches the network or the database; a value a vendor does not carry is None, never 0 and never derived (a
price of 0 means absent: it becomes NULL in the frame, like the import does); times are naive Asia/Shanghai like the old files and become
UTC in the frame; the schedule is a pure function of the clock so it can be tested.
Vendor notes: Sina quotes are in SHARES (converted to lots here), carry no outer/inner lots, turnover, caps, volume ratio or vwap, and no
limit prices (they come from the versioned price-limit rules, NULL when a band is unverified); Tencent carries all columns and its own limit
prices. Call-auction rows (09:15-09:24) are stored as received: bid1 == ask1 is the indicative price and its size the matched quantity,
bid2 / ask2 size the unmatched surplus (checked on the old recording); that meaning is not re-mapped into other columns.
Not covered: minute bars, Eastmoney pools, convertible bonds, news-linked names (the old recorder's other jobs).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import polars as pl

from pulse.adapters.sources import legacy_recordings as LR
from pulse.application.bf_live_service import limit_band

MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")
BOOK_LEVELS = range(1, 6)
STREAM_FULL, STREAM_HOT = "quotes", "hot"
HOT_KINDS = ("auction_hot", "hot", "after_hot")
HOT_BAND = 0.02          # names within 2% of either limit are hot
HOT_CAP = 800
TRADING_SHARE = 0.5      # share of quotes stamped today below which the day is not a trading day

RAW_SCHEMA: dict[str, Any] = {c: (pl.String if c in ("code", "name", "kind") else pl.Datetime("ms") if c.endswith("_ts") else pl.Float64)
                              for c in LR.QUOTE_REQUIRED}
_ZERO_IS_ABSENT = ("price", "open", "high", "low", "vwap")


# --- vendor record -> legacy row --------------------------------------------------------------------------------------------------

def num(text: Any) -> float | None:
    """Vendor text to float; empty, '-' and unparseable text are None."""
    if text is None:
        return None
    try:
        return float(str(text).strip())
    except ValueError:
        return None


def _scaled(text: Any, divisor: float) -> float | None:
    v = num(text)
    return None if v is None else v / divisor


def _stamp(text: Any, fmt: str) -> datetime | None:
    try:
        return datetime.strptime(str(text).strip(), fmt)
    except ValueError:
        return None


def _finish(row: dict[str, Any]) -> dict[str, Any]:
    for name in _ZERO_IS_ABSENT:
        if row.get(name) == 0:
            row[name] = None
    return row


def _blank(code: str, name: str, kind: str, poll_ts: datetime) -> dict[str, Any]:
    row: dict[str, Any] = {c: None for c in LR.QUOTE_REQUIRED}
    row.update({"code": code, "name": name.strip(), "kind": kind, "poll_ts": poll_ts})
    return row


def sina_row(rec: dict, kind: str, poll_ts: datetime, day: date) -> dict[str, Any]:
    """One ``sina.parse_hq_book`` record. Shares / 100 = lots; the limit prices come from the rules for ``day`` (None when unverified)."""
    row = _blank(rec["symbol"][2:], rec["name"], kind, poll_ts)
    row.update(quote_ts=_stamp(rec["quote_time"], "%Y-%m-%d %H:%M:%S"), amount_yuan=num(rec["amount"]), price=num(rec["last"]),
               prev_close=num(rec["prev_close"]), open=num(rec["open"]), high=num(rec["high"]), low=num(rec["low"]),
               volume_lots=_scaled(rec["volume"], 100))
    for i in BOOK_LEVELS:
        row[f"bid{i}_px"], row[f"bid{i}_lots"] = num(rec[f"bid{i}_price"]), _scaled(rec[f"bid{i}_volume"], 100)
        row[f"ask{i}_px"], row[f"ask{i}_lots"] = num(rec[f"ask{i}_price"]), _scaled(rec[f"ask{i}_volume"], 100)
    row["after_lots"] = _scaled(rec.get("after_shares"), 100)
    row["after_amount_wan"] = _scaled(rec.get("after_amount_yuan"), 10_000)
    prev = row["prev_close"]
    if prev:
        up, down = limit_band(row["code"], row["name"], Decimal(str(prev)), day)
        row["limit_up"], row["limit_down"] = (None if up is None else float(up)), (None if down is None else float(down))
    return _finish(row)


def tencent_row(rec: dict, kind: str, poll_ts: datetime) -> dict[str, Any]:
    """One ``tencent.parse_qt_book`` record; volumes are already lots and the limit prices are the vendor's."""
    row = _blank(rec["symbol"][2:], rec["name"], kind, poll_ts)
    row.update(quote_ts=_stamp(rec["quote_time"], "%Y%m%d%H%M%S"), amount_yuan=num(rec["amount_yuan"]), price=num(rec["last"]),
               prev_close=num(rec["prev_close"]), open=num(rec["open"]), high=num(rec["high"]), low=num(rec["low"]),
               volume_lots=num(rec["volume_lots"]), outer_lots=num(rec["outer_lots"]), inner_lots=num(rec["inner_lots"]),
               turnover=num(rec["turnover_pct"]), float_cap=num(rec["float_cap"]), total_cap=num(rec["total_cap"]),
               limit_up=num(rec["limit_up"]), limit_down=num(rec["limit_down"]), volume_ratio=num(rec["volume_ratio"]),
               vwap=num(rec["vwap"]), after_amount_wan=num(rec["after_amount_wan"]), after_lots=num(rec["after_lots"]))
    for i in BOOK_LEVELS:
        row[f"bid{i}_px"], row[f"bid{i}_lots"] = num(rec[f"bid{i}_price"]), num(rec[f"bid{i}_lots"])
        row[f"ask{i}_px"], row[f"ask{i}_lots"] = num(rec[f"ask{i}_price"]), num(rec[f"ask{i}_lots"])
    return _finish(row)


# --- rows -> frames ------------------------------------------------------------------------------------------------------------------

def legacy_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    """The rows as the old recorder wrote them (naive Asia/Shanghai millisecond times, Float64 numbers): what ``read_quotes`` expects."""
    return pl.DataFrame({c: [r.get(c) for r in rows] for c in LR.QUOTE_REQUIRED}, schema=RAW_SCHEMA)


def rows_to_frame(rows: list[dict[str, Any]], *, stream: str, part: str, day: date) -> pl.DataFrame:
    """Snapshot frame of the dataset ``quote_snapshots_60s``: the same select as ``LegacyRecordings.read_quotes``, so columns, order and
    dtypes are those of an imported legacy part (a zero price is NULL, prices Decimal(18,4), lots Float64, times UTC)."""
    df = legacy_frame(rows)
    sch = df.schema
    return df.select(
        pl.lit(stream).alias("source_stream"), pl.lit(part).alias("source_part"), pl.lit(day).alias("session_date"),
        pl.col("code").cast(pl.String), pl.col("name").cast(pl.String), pl.col("kind").cast(pl.String),
        LR._utc_expr("poll_ts", sch["poll_ts"]).alias("poll_at"), LR._utc_expr("quote_ts", sch["quote_ts"]).alias("quote_at"),
        *[LR._dec(c, 18, 4, zero_is_missing=True) for c in LR._QUOTE_PX],
        *[LR._dec(c, *p) for c, p in LR._QUOTE_DEC.items()],
        *[pl.col(c).cast(pl.Float64).fill_nan(None) for c in LR._QUOTE_FLOAT])


# --- limit queues and the hot set ------------------------------------------------------------------------------------------------------

def limit_queue(row: dict[str, Any]) -> tuple[str, float | None] | None:
    """The queue in front of a limit price: ('up', bid1 lots) when the price is at the limit-up, ('down', ask1 lots) at the limit-down,
    None otherwise. The size is the vendor's book size at level 1 (None when the vendor gave none); nothing is inferred."""
    price, up, down = row.get("price"), row.get("limit_up"), row.get("limit_down")
    if not price:
        return None
    if up and price >= up:
        return "up", row.get("bid1_lots")
    if down and price <= down:
        return "down", row.get("ask1_lots")
    return None


def is_hot(row: dict[str, Any]) -> bool:
    """Within HOT_BAND of either limit, or touched one today."""
    price, up, down = row.get("price"), row.get("limit_up"), row.get("limit_down")
    high, low = row.get("high"), row.get("low")
    if price and up and price >= up * (1 - HOT_BAND):
        return True
    if price and down and price <= down * (1 + HOT_BAND):
        return True
    return bool((high and up and high >= up) or (low and down and low <= down))


def _limit_distance(row: dict[str, Any]) -> float:
    price, up, down = row.get("price"), row.get("limit_up"), row.get("limit_down")
    dists = [abs(price / lim - 1) for lim in (up, down) if price and lim]
    return min(dists) if dists else 1.0


def grow_hot(hot: set[str], rows: list[dict[str, Any]], cap: int = HOT_CAP) -> tuple[set[str], bool]:
    """``hot`` plus the names of ``rows`` that are hot now; names already in stay and the nearest new ones take the free places.
    Returns (new set, capped)."""
    new = [r for r in rows if r["code"] not in hot and is_hot(r)]
    room = cap - len(hot)
    capped = len(new) > room
    if capped:
        new = sorted(new, key=lambda r: (_limit_distance(r), r["code"]))[:max(room, 0)]
    return hot | {r["code"] for r in new}, capped


def static_codes(frame: pl.DataFrame) -> set[str]:
    """Codes that touched a limit in a recorded session frame (day high at the limit-up or day low at the limit-down)."""
    if frame.is_empty():
        return set()
    hit = ((pl.col("limit_up").is_not_null() & (pl.col("high") >= pl.col("limit_up")))
           | (pl.col("limit_down").is_not_null() & (pl.col("low") <= pl.col("limit_down"))))
    return set(frame.filter(hit.fill_null(False))["code"].unique().to_list())


def stamped_today(rows: list[dict[str, Any]], day: date) -> bool:
    """False when most quotes carry another date (a holiday: the vendor repeats the last session)."""
    stamps = [r["quote_ts"] for r in rows if r.get("quote_ts") is not None]
    return bool(stamps) and sum(1 for s in stamps if s.date() == day) / len(stamps) >= TRADING_SHARE


# --- schedule (pure) -------------------------------------------------------------------------------------------------------------------------

AUCTION = (time(9, 15), time(9, 25))
AUCTION_DENSE = (time(9, 19, 30), time(9, 25, 10))
SESSIONS = ((time(9, 30), time(11, 30, 30)), (time(13, 0), time(15, 0, 30)))
AFTER_HOURS = (time(15, 5), time(15, 30, 30))      # 盘后固定价格交易
ONE_SHOTS = (("auction_final", time(9, 25, 10), time(9, 27)), ("close", time(15, 1, 30), time(15, 4)),
             ("after_open", time(15, 5, 5), time(15, 7)), ("after_mid", time(15, 15), time(15, 17)),
             ("after_end", time(15, 30, 5), time(15, 33)))
AFTER_SHOTS = ("after_open", "after_mid", "after_end")


@dataclass(frozen=True)
class Schedule:
    """Grid periods in seconds; the offsets keep a poll off the second the exchange's minute is still being printed."""
    full_every: int = 60
    hot_every: int = 10
    auction_every: int = 5
    after_every: int = 60
    full_offset: int = 2
    after_offset: int = 5


@dataclass
class Clock:
    """What the loop has already done today."""
    last_full: datetime | None = None
    last_hot: datetime | None = None
    last_auction: datetime | None = None
    last_after: datetime | None = None
    done: set[str] = field(default_factory=set)


def _inside(t: time, window: tuple[time, time]) -> bool:
    return window[0] <= t <= window[1]


def slot(now: datetime, every: int) -> datetime:
    """Start of the ``every``-second grid cell (counted from midnight) that holds ``now``."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    seconds = int((now - midnight).total_seconds())
    return midnight + timedelta(seconds=seconds - seconds % every)


def due(now: datetime, clock: Clock, cfg: Schedule = Schedule()) -> list[str]:
    """Steps to run at ``now`` (naive Asia/Shanghai), the fast grids first so a slow full snapshot does not delay them."""
    t = now.time()
    out: list[str] = []
    in_session = any(_inside(t, w) for w in SESSIONS)
    if _inside(t, AUCTION_DENSE) and clock.last_auction != slot(now, cfg.auction_every):
        out.append("auction_hot")
    if in_session and clock.last_hot != slot(now, cfg.hot_every):
        out.append("hot")
    if _inside(t, AFTER_HOURS) and now.second >= cfg.after_offset and clock.last_after != slot(now, cfg.after_every):
        out.append("after_hot")
    if ((in_session or _inside(t, AUCTION)) and (now - slot(now, cfg.full_every)).total_seconds() >= cfg.full_offset
            and clock.last_full != slot(now, cfg.full_every)):
        out.append("full")
    out += [name for name, start, end in ONE_SHOTS if name not in clock.done and start <= t <= end]
    return out


def _window(t: time) -> int:
    return next((i for i, w in enumerate((AUCTION, *SESSIONS, AFTER_HOURS)) if _inside(t, w)), -1)


def missed(previous: datetime | None, current: datetime, every: int) -> int:
    """Grid slots skipped between two polls of one grid inside the same window."""
    if previous is None or _window(previous.time()) != _window(current.time()):
        return 0
    gap = (current - previous).total_seconds()
    return int(gap // every) - 1 if gap > every else 0
