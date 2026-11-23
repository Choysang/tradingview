"""E099 flags from the single-day seat reports of a stock-day: L1 / L3 (Lhasa buyers) and T1 (a one-day seat among the buyers), point in time.

Why: pre-registration section 2. Flags are known on the evening of D and used from D+1 open on; they read only the single-day reports of D (window start = window end = D) and, for T1,
the history of each seat's earlier buys. Pure polars functions: no database, no clock.
Rules (literal readings):
  * dedupe: rows of one stock-day that repeat under different listing reasons are removed on (side, seat name, amount), batch 6 section 7 rule 7 (``bf_batch6_inputs.load_seats`` already
    does it in SQL; ``dedupe`` repeats it so the rule holds for any caller; a null amount equals a null amount).
  * L1 / L3: the number of DISTINCT buy-side seat names containing "拉萨" on (code, D) is >= 1 / >= 3. A row with an unknown amount still names a buyer and counts.
  * T1: some buyer of (code, D) other than "机构专用" is a one-day seat on D: among that seat's buys (any stock, single-day reports) whose event is SETTLED STRICTLY BEFORE D there are at
    least 5, and at least 60 % of them were followed by the same seat on the SELL side of the same stock on the next open day. An event of day t is settled on the third open day after t
    (E048: buy at the next open, sell at the close three bars later); the strict "before D" means an event settling on D itself is not used. The settle day comes from the market calendar
    passed in, not from the stock's own bars (a suspension would delay the stock's own settle; the market-calendar rule is the PIT-safe simplification, see the pre-registration report).
  * a stock-day with no single-day report has no flags (False everywhere).
Not covered: loading the reports (``bf_batch6_inputs.load_seats``), the calendar (built by the caller), the sample of limit-up closes.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

import polars as pl

from pulse.domain.batch8 import rules as R

SEAT_SCHEMA = {"code": pl.String, "date": pl.Date, "side": pl.String, "seat": pl.String, "amount": pl.Float64}
FLAG_SCHEMA = {"code": pl.String, "date": pl.Date, "lhasa": pl.Int64, "trip_seats": pl.Int64, "l1": pl.Boolean, "l3": pl.Boolean, "t1": pl.Boolean, "flag": pl.Boolean}


def dedupe(seats: pl.DataFrame) -> pl.DataFrame:
    """Rows with the same (code, date, side, seat, amount) count once."""
    return seats.unique(["code", "date", "side", "seat", "amount"], keep="first", maintain_order=True)


def calendar_maps(calendar: Sequence[date]) -> tuple[dict[date, date], dict[date, date]]:
    """(next open day, settle day = the ``SETTLE_SESSIONS``-th open day after) of every open day of the sorted calendar that has them."""
    cal = sorted(set(calendar))
    nxt = {d: cal[i + 1] for i, d in enumerate(cal[:-1])}
    settle = {d: cal[i + R.SETTLE_SESSIONS] for i, d in enumerate(cal[:-R.SETTLE_SESSIONS])}
    return nxt, settle


def lhasa_counts(seats: pl.DataFrame) -> pl.DataFrame:
    """code, date, lhasa = distinct buy-side seat names containing 拉萨."""
    buys = seats.filter((pl.col("side") == "buy") & pl.col("seat").str.contains(R.LHASA, literal=True)).unique(["code", "date", "seat"])
    return buys.group_by("code", "date").agg(pl.len().cast(pl.Int64).alias("lhasa"))


def seat_events(seats: pl.DataFrame, calendar: Sequence[date]) -> pl.DataFrame:
    """One row per (seat, code, date) buy of a non-institution seat: ``tripped`` (the same seat sold the same stock on the next open day) and ``settle`` (None when the calendar ends first)."""
    buys = seats.filter((pl.col("side") == "buy") & (pl.col("seat") != R.INSTITUTION)).select("code", "date", "seat").unique()
    sells = seats.filter(pl.col("side") == "sell").select(pl.col("code"), pl.col("date").alias("next"), "seat").unique().with_columns(pl.lit(True).alias("tripped"))
    nxt, settle = calendar_maps(calendar)
    days = sorted(set(nxt) | set(settle))
    cal = pl.DataFrame({"date": days, "next": [nxt.get(d) for d in days], "settle": [settle.get(d) for d in days]}, schema={"date": pl.Date, "next": pl.Date, "settle": pl.Date})
    ev = buys.join(cal, on="date", how="left").join(sells, on=["code", "seat", "next"], how="left")
    return ev.with_columns(pl.col("tripped").fill_null(False)).select("seat", "code", "date", "tripped", "settle")


def seat_scores(events: pl.DataFrame) -> pl.DataFrame:
    """Running record of each seat by the day its events settled: seat, settle, n (settled buys so far), trips (of them followed by a next-day sell)."""
    s = events.filter(pl.col("settle").is_not_null()).sort(["seat", "settle"])
    s = s.with_columns(pl.int_range(1, pl.len() + 1).over("seat").cast(pl.Int64).alias("n"), pl.col("tripped").cast(pl.Int64).cum_sum().over("seat").alias("trips"))
    return s.group_by(["seat", "settle"]).agg(pl.col("n").last(), pl.col("trips").last()).sort("settle")


def flag_table(seats: pl.DataFrame, calendar: Sequence[date]) -> pl.DataFrame:
    """Flags of every stock-day that has a single-day report: columns of ``FLAG_SCHEMA``."""
    s = dedupe(seats)
    stock_days = s.select("code", "date").unique()
    if stock_days.is_empty():
        return pl.DataFrame(schema=FLAG_SCHEMA)
    lh = lhasa_counts(s)
    scores = seat_scores(seat_events(s, calendar))
    q = (s.filter((pl.col("side") == "buy") & (pl.col("seat") != R.INSTITUTION)).select("code", "date", "seat").unique()
         .with_columns((pl.col("date") - pl.duration(days=1)).alias("_key")).sort("_key"))
    scored = q.join_asof(scores, left_on="_key", right_on="settle", by="seat", strategy="backward", check_sortedness=False)
    trip = (pl.col("n").fill_null(0) >= R.TRIP_MIN_EVENTS) & (pl.col("trips").fill_null(0) * R.TRIP_MIN_SHARE_DEN >= pl.col("n").fill_null(0) * R.TRIP_MIN_SHARE_NUM)
    t1 = scored.with_columns(trip.alias("_trip")).group_by("code", "date").agg(pl.col("_trip").sum().cast(pl.Int64).alias("trip_seats"))
    out = (stock_days.join(lh, on=["code", "date"], how="left").join(t1, on=["code", "date"], how="left")
           .with_columns(pl.col("lhasa").fill_null(0), pl.col("trip_seats").fill_null(0))
           .with_columns((pl.col("lhasa") >= R.LHASA_MIN_L1).alias("l1"), (pl.col("lhasa") >= R.LHASA_MIN_L3).alias("l3"), (pl.col("trip_seats") >= 1).alias("t1"))
           .with_columns((pl.col("l1") | pl.col("t1")).alias("flag")))
    return out.select(list(FLAG_SCHEMA)).sort(["date", "code"])


def flags_of_day(table: pl.DataFrame, day: date) -> Mapping[str, dict]:
    """code -> {lhasa, trip_seats, l1, l3, t1, flag} of the stock-days listed on ``day``."""
    sub = table.filter(pl.col("date") == day)
    return {r["code"]: {k: r[k] for k in ("lhasa", "trip_seats", "l1", "l3", "t1", "flag")} for r in sub.iter_rows(named=True)}

