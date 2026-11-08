"""Crowd features: theme contagion and Dragon-Tiger seats, point-in-time, joined into the panel.

A-share short-term money moves in packs. Three traces of that are measurable on
daily data:

- Industry contagion: how many stocks of the same Sina industry hit the limit
  today (`ind_limit_ups`). Membership is the current snapshot (`boards` table);
  industries change slowly, so the look-ahead is small. Concepts are not used
  for research because they are often created after a theme got hot.
- Name clusters: puns and shared characters (生肖 years, "羊" names) make stocks
  move together. `name_heat` is the most limit-ups among *other* stocks that
  share one of this stock's rare name characters that day.
- Seats: a seat (营业部) earns a track record from the stocks it bought on the
  Dragon-Tiger list: buy at the next open, sell at the close three bars later.
  An event only counts once that window has closed, so a seat's record on day t
  uses events settled by t. "Smart" seats have at least SMART_MIN_EVENTS settled
  buys and a shrunk mean above SMART_MIN_RETURN; "one-day-trip" seats sold the
  same stock on the next day's list in at least TRIP_MIN_SHARE of their buys.
  `机构专用` is an anonymous pool, so it gets its own column (`inst_net`) instead
  of a track record.

Every feature is null when its source table is missing (the SIM warehouse has no
Dragon-Tiger list or boards), so playbooks that need them report "cannot test".
The seat columns are also null on days the list was never fetched (`lhb_known_days`):
the latest list publishes after the close and the sync can skip days, and an unknown
day is not a day on which nobody was listed.
"""
from __future__ import annotations

from datetime import date
import re

import polars as pl

from ..warehouse.store import Warehouse

FORWARD_BARS = 3
SHRINK = 5.0
SMART_MIN_EVENTS = 5
SMART_MIN_RETURN = 1.0  # % per trade, shrunk
TRIP_MIN_EVENTS = 5
TRIP_MIN_SHARE = 0.6
INSTITUTION = "机构专用"
LHB_DATES_DOC = ("meta", "lhb_dates")  # written by warehouse/lhb.py: every fetched day, empty ones included
RARE_MAX_NAMES = 20  # a character in at most this many names is distinctive enough to cluster on
# Characters too generic to signal a pun even when rare-ish.
STOP_CHARS = set("股份有限公司集团控科技发展中国新华电子业实智能源环保材料医药生物")
CJK = re.compile(r"[一-鿿]")

CROWD_COLUMNS: tuple[str, ...] = (
    "industry", "ind_limit_ups", "name_heat", "name_char",
    "lhb_on", "smart_buyers", "smart_score", "trip_buyers", "inst_net",
)
SCHEMA = {
    "industry": pl.Utf8, "ind_limit_ups": pl.Int32, "name_heat": pl.Int32, "name_char": pl.Utf8,
    "lhb_on": pl.Boolean, "smart_buyers": pl.Int32, "smart_score": pl.Float64, "trip_buyers": pl.Int32,
    "inst_net": pl.Float64,
}


def crowd_features(wh: Warehouse, panel: pl.DataFrame) -> pl.DataFrame:
    """Panel with the crowd columns added (null where the source is missing), sorted by code, date."""
    base = panel.select("code", "date", "name", "is_limit_up", "open", "close", "amount")
    out = panel
    for part in (_industry(wh, base), _names(base), _seats(wh, base)):
        out = out.join(part, on=["code", "date"], how="left") if part is not None else out
    for column, dtype in SCHEMA.items():
        out = out.with_columns(pl.col(column).cast(dtype)) if column in out.columns else out.with_columns(
            pl.lit(None, dtype=dtype).alias(column)
        )
    if out.height and "ind_limit_ups" in out.columns:
        # A stock with a known industry and no limit-up in it today has zero, not unknown.
        out = out.with_columns(
            pl.when(pl.col("industry").is_not_null()).then(pl.col("ind_limit_ups").fill_null(0)).otherwise(None)
            .alias("ind_limit_ups"),
        )
    return out.sort(["code", "date"])


# --- industry contagion --------------------------------------------------------------

def _industry(wh: Warehouse, base: pl.DataFrame) -> pl.DataFrame | None:
    boards = wh.scan("boards")
    if boards is None:
        return None
    mapping = (
        boards.filter(pl.col("kind") == "industry").sort(["code", "board_id"])
        .group_by("code").agg(pl.col("board_name").first().alias("industry")).collect()
    )
    if mapping.is_empty():
        return None
    tagged = base.select("code", "date", "is_limit_up").join(mapping, on="code", how="inner")
    counts = tagged.group_by(["date", "industry"]).agg(pl.col("is_limit_up").sum().cast(pl.Int32).alias("ind_limit_ups"))
    return tagged.join(counts, on=["date", "industry"], how="left").select("code", "date", "industry", "ind_limit_ups")


# --- name clusters --------------------------------------------------------------------

def rare_chars(names: pl.DataFrame) -> pl.DataFrame:
    """(code, char) for distinctive CJK characters of each current name."""
    rows = []
    for code, name in names.iter_rows():
        for char in set(CJK.findall(name or "")):
            if char not in STOP_CHARS:
                rows.append((code, char))
    pairs = pl.DataFrame(rows, schema={"code": pl.Utf8, "char": pl.Utf8}, orient="row")
    if pairs.is_empty():
        return pairs
    freq = pairs.group_by("char").agg(pl.len().alias("n"))
    keep = freq.filter((pl.col("n") >= 2) & (pl.col("n") <= RARE_MAX_NAMES)).select("char")
    return pairs.join(keep, on="char", how="inner")


def _names(base: pl.DataFrame) -> pl.DataFrame | None:
    names = base.select("code", "name").drop_nulls().unique("code")
    chars = rare_chars(names)
    if chars.is_empty():
        return None
    hits = base.filter(pl.col("is_limit_up")).select("code", "date").join(chars, on="code", how="inner")
    counts = hits.group_by(["date", "char"]).agg(pl.len().cast(pl.Int32).alias("count"))
    if counts.is_empty():
        return None
    own = hits.select("code", "date", "char", pl.lit(1, dtype=pl.Int32).alias("own"))
    heat = (
        counts.join(chars, on="char", how="inner")
        .join(own, on=["code", "date", "char"], how="left")
        .with_columns((pl.col("count") - pl.col("own").fill_null(0)).alias("heat"))
        .filter(pl.col("heat") > 0)
        .sort(["heat", "char"], descending=[True, False])
        .group_by(["code", "date"]).agg(pl.col("heat").first().alias("name_heat"), pl.col("char").first().alias("name_char"))
    )
    return heat


# --- Dragon-Tiger seats ---------------------------------------------------------------

def seat_history(wh: Warehouse, base: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame] | None:
    """(seats, listed, buys, settled): raw seat rows, stocks on the list per day, buy events
    (non-institution), and each seat's running record by the day its events settled."""
    seats_lazy, stocks_lazy = wh.scan("lhb_seats"), wh.scan("lhb_stocks")
    if seats_lazy is None or stocks_lazy is None:
        return None
    seats = seats_lazy.select("code", "date", "side", "seat", "buy", "sell", "net").collect()
    listed = stocks_lazy.select("code", "date").unique().collect().with_columns(pl.lit(True).alias("lhb_on"))
    if listed.is_empty():
        return None

    dates = base.select(pl.col("date").unique().sort()).with_row_index("idx")
    # Forward return of an event: next open to the close FORWARD_BARS bars later, on the stock's own bars.
    # The event settles on the date of that close: a suspended stock settles later than the market calendar.
    fwd = (
        base.select("code", "date", "open", "close").sort(["code", "date"])
        .with_columns(
            ((pl.col("close").shift(-FORWARD_BARS).over("code") / pl.col("open").shift(-1).over("code") - 1) * 100).alias("fwd"),
            pl.col("date").shift(-FORWARD_BARS).over("code").alias("settle_date"),
        )
        .select("code", "date", "fwd", "settle_date")
    )
    buys = seats.filter((pl.col("side") == "buy") & (pl.col("seat") != INSTITUTION)).select("code", "date", "seat").unique()
    sells = seats.filter(pl.col("side") == "sell").select("code", "date", "seat").unique()

    events = (
        buys.join(fwd, on=["code", "date"], how="left")
        .join(dates, on="date", how="inner")
    )
    # One-day trip: the same seat on the next trading day's sell list for the same stock.
    next_sell = sells.join(dates, on="date", how="inner").select(
        "code", "seat", (pl.col("idx") - 1).alias("idx"), pl.lit(True).alias("tripped")
    ).unique()
    events = events.join(next_sell, on=["code", "seat", "idx"], how="left").with_columns(pl.col("tripped").fill_null(False))

    settled = (
        events.filter(pl.col("fwd").is_not_null() & pl.col("settle_date").is_not_null())
        .sort(["seat", "settle_date"])
        .with_columns(
            pl.int_range(1, pl.len() + 1).over("seat").alias("n"),
            pl.col("fwd").cum_sum().over("seat").alias("sum_fwd"),
            pl.col("tripped").cast(pl.Int32).cum_sum().over("seat").alias("trips"),
        )
        .select("seat", "settle_date", "n", "sum_fwd", "trips")
        .group_by(["seat", "settle_date"]).agg(pl.all().last())
        .sort("settle_date")
    )
    return seats, listed, buys, settled


def _seats(wh: Warehouse, base: pl.DataFrame) -> pl.DataFrame | None:
    history = seat_history(wh, base)
    if history is None:
        return None
    seats, listed, buys, settled = history
    today = buys.sort("date")
    if settled.is_empty():
        scored = today.with_columns(
            pl.lit(None, dtype=pl.Int64).alias("n"), pl.lit(None, dtype=pl.Float64).alias("score"), pl.lit(False).alias("trip"),
        )
    else:
        scored = today.join_asof(
            settled, left_on="date", right_on="settle_date", by="seat", strategy="backward", check_sortedness=False,
        ).with_columns(
            (pl.col("sum_fwd") / (pl.col("n") + SHRINK)).alias("score"),
            ((pl.col("n") >= TRIP_MIN_EVENTS) & (pl.col("trips") / pl.col("n") >= TRIP_MIN_SHARE)).fill_null(False).alias("trip"),
        )
    smart = (pl.col("n") >= SMART_MIN_EVENTS) & (pl.col("score") >= SMART_MIN_RETURN)
    per_stock = scored.with_columns(smart.fill_null(False).alias("smart")).group_by(["code", "date"]).agg(
        pl.col("smart").sum().cast(pl.Int32).alias("smart_buyers"),
        pl.col("score").filter(pl.col("smart")).sum().alias("smart_score"),
        pl.col("trip").sum().cast(pl.Int32).alias("trip_buyers"),
    )
    # Every row carries both the seat's buy and sell amount, whichever list it is on. An institution that
    # is top-5 on both lists shows up twice with the same amounts (and again under each listing reason),
    # so identical (stock, day, buy, sell) entries count once. Unknown amounts stay unknown.
    inst = (
        seats.filter(pl.col("seat") == INSTITUTION).select("code", "date", "buy", "sell").unique()
        .with_columns((pl.col("buy") - pl.col("sell")).alias("_net"))
        .group_by(["code", "date"]).agg(
            pl.when(pl.col("_net").count() > 0).then(pl.col("_net").sum()).alias("inst_net"),
            pl.lit(True).alias("_inst"),
        )
    )
    out = (
        listed.join(per_stock, on=["code", "date"], how="left")
        .join(inst, on=["code", "date"], how="left")
        .with_columns(
            pl.col("smart_buyers").fill_null(0), pl.col("trip_buyers").fill_null(0),
            pl.col("smart_score").fill_null(0.0),
        )
    )
    # A day whose list was fetched where the stock was not on it: known zero. A day that was never
    # fetched (before the history starts, skipped, not published yet) stays null.
    known = lhb_known_days(wh) or set()
    covered = base.select("code", "date").filter(pl.col("date").is_in(sorted(known)))
    return covered.join(out, on=["code", "date"], how="left").with_columns(
        pl.col("lhb_on").fill_null(False), pl.col("smart_buyers").fill_null(0), pl.col("trip_buyers").fill_null(0),
        pl.col("smart_score").fill_null(0.0),
        # No institution row on a fetched day is a known zero; an institution row with unknown amounts is not.
        pl.when(pl.col("_inst").is_null()).then(0.0).otherwise(pl.col("inst_net")).alias("inst_net"),
    ).drop("_inst")


def lhb_known_days(wh: Warehouse) -> set[date] | None:
    """Days whose Dragon-Tiger list was fetched (days with no list included), or None without the tables.

    Same definition as the sync's resume logic: the `lhb_dates` meta doc plus every day with a stored list."""
    stocks = wh.scan("lhb_stocks")
    if stocks is None or wh.scan("lhb_seats") is None:
        return None
    days = set(stocks.select(pl.col("date").unique()).collect()["date"].to_list())
    for text in (wh.read_doc(*LHB_DATES_DOC) or {}).get("dates", []):
        try:
            days.add(date.fromisoformat(str(text)[:10]))
        except ValueError:
            continue
    return days


def seat_records(settled: pl.DataFrame, as_of) -> pl.DataFrame:
    """Each seat's record as of a day: events, shrunk mean (%), trip share, smart / trip flags."""
    if settled.is_empty():
        return pl.DataFrame(schema={"seat": pl.Utf8, "n": pl.Int64, "score": pl.Float64, "trip_share": pl.Float64,
                                    "smart": pl.Boolean, "trip": pl.Boolean})
    last = settled.filter(pl.col("settle_date") <= as_of).sort("settle_date").group_by("seat").agg(pl.all().last())
    return last.select(
        "seat", "n",
        (pl.col("sum_fwd") / (pl.col("n") + SHRINK)).alias("score"),
        (pl.col("trips") / pl.col("n")).alias("trip_share"),
    ).with_columns(
        ((pl.col("n") >= SMART_MIN_EVENTS) & (pl.col("score") >= SMART_MIN_RETURN)).alias("smart"),
        ((pl.col("n") >= TRIP_MIN_EVENTS) & (pl.col("trip_share") >= TRIP_MIN_SHARE)).alias("trip"),
    )
