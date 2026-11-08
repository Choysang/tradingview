"""Shared daily-bar features every research module builds on.

Limit rules by board: 10% main board, 20% ChiNext/STAR, 30% BSE. Historical ST
status is not stored, and applying today's name backwards would be look-ahead
(a stock that became ST last month would get 5% limits for years), so research
bars use the board ratio; a 5% limit-up during an ST period is not recognised.
`limit_ratio(code, name)` with a name is for *today's* rows only. Limit price is
rounded to the cent the way exchanges publish it; a close within half a cent
counts as sealed.
"""
from __future__ import annotations

import polars as pl

HALF_CENT = 0.005


def limit_ratio(code: pl.Expr, name: pl.Expr | None = None) -> pl.Expr:
    board = (
        pl.when(code.str.starts_with("30") | code.str.starts_with("68")).then(0.20)
        .when(code.str.starts_with("4") | code.str.starts_with("8") | code.str.starts_with("92")).then(0.30)
        .otherwise(0.10)
    )
    if name is None:
        return board
    return pl.when(name.str.contains("ST")).then(0.05).otherwise(board)


def daily_features(bars: pl.LazyFrame, securities: pl.LazyFrame | None = None) -> pl.LazyFrame:
    """Add prev_close, pct_chg (%), limit flags, consecutive limit-up streak and
    an amount estimate (亿元) when the provider has no turnover value.

    Input columns: code, date, open, high, low, close, volume (shares) and
    optionally amount (亿元). Output keeps every input column. `securities`
    no longer affects the limits (see the module note); callers may still pass
    it to carry names along.
    """
    frame = bars
    joined = False
    if securities is not None and "name" not in frame.collect_schema().names():
        frame = frame.join(securities.select("code", "name"), on="code", how="left")
        joined = True
    # Every window expression below reads neighbouring rows; a join does not keep
    # row order, so sort after it, never before.
    frame = frame.sort(["code", "date"])
    columns = frame.collect_schema().names()
    ratio = limit_ratio(pl.col("code"))
    amount = (
        pl.coalesce(pl.col("amount"), _amount_estimate()) if "amount" in columns else _amount_estimate()
    )
    frame = frame.with_columns(
        pl.col("close").shift(1).over("code").alias("prev_close"),
        ratio.alias("limit_ratio"),
        amount.alias("amount"),
    ).with_columns(
        ((pl.col("close") / pl.col("prev_close") - 1) * 100).alias("pct_chg"),
        (pl.col("prev_close") * (1 + pl.col("limit_ratio"))).round(2).alias("limit_up_price"),
        (pl.col("prev_close") * (1 - pl.col("limit_ratio"))).round(2).alias("limit_down_price"),
    ).with_columns(
        (pl.col("close") >= pl.col("limit_up_price") - HALF_CENT).fill_null(False).alias("is_limit_up"),
        (pl.col("high") >= pl.col("limit_up_price") - HALF_CENT).fill_null(False).alias("touched_limit"),
        (pl.col("close") <= pl.col("limit_down_price") + HALF_CENT).fill_null(False).alias("is_limit_down"),
        (pl.col("open") >= pl.col("limit_up_price") - HALF_CENT).fill_null(False).alias("open_at_limit"),
        (pl.col("open") <= pl.col("limit_down_price") + HALF_CENT).fill_null(False).alias("open_at_floor"),
    ).with_columns(
        (pl.col("touched_limit") & ~pl.col("is_limit_up")).alias("is_broken"),
        # 一字板: opened, traded and closed at the limit — not buyable at the open.
        (pl.col("is_limit_up") & (pl.col("low") >= pl.col("limit_up_price") - HALF_CENT)).alias("one_word"),
    )
    # Consecutive limit-ups: reset the running count at every non-limit day.
    frame = frame.with_columns(
        (~pl.col("is_limit_up")).cast(pl.Int32).cum_sum().over("code").alias("_run"),
    ).with_columns(
        pl.col("is_limit_up").cast(pl.Int32).cum_sum().over(["code", "_run"]).alias("limit_streak"),
    ).drop("_run")
    if joined:
        frame = frame.drop("name")
    return frame


def _amount_estimate() -> pl.Expr:
    """Typical price × volume, in 亿元. Providers without turnover get this
    approximation; research notes state it whenever it is used."""
    typical = (pl.col("high") + pl.col("low") + pl.col("close")) / 3
    return typical * pl.col("volume") / 1e8
