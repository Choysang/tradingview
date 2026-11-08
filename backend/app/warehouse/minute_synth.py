"""1-minute bars built from the recorder's snapshots, for names without fetched minute bars.

The recorder fetches Tencent minute bars only for the hot set (a whole-board fetch drew a WAF block on
2026-11-02), so the rest of the board, and any name a block cut off, gets bars from what was recorded anyway:
the 60 s full snapshots of every code (kinds full / close) and the 10 s hot polls (kind hot).

A bar labelled HHMM ends at HH:MM:00, as Tencent's do (checked on 2026-11-02: Tencent's 0931 cumulative volume
is the state at 09:31:00). It holds the last snapshot quoted in (HH:MM-1 min, HH:MM] plus LAG seconds, since
the full snapshots are taken 2 s into each minute: its price and the day's cumulative volume and amount; open / high / low are of the snapshot prices inside the minute
(not trade prices: with one snapshot a minute they are all the same price). lots / amount are the change in the
cumulative values since the previous bar, so a minute without a snapshot rolls into the next bar. vwap is the
day's cumulative amount over volume. n_snaps says how much each bar rests on. Only continuous-session quotes
(09:30-11:30, 13:00-15:00) are used; the auction rows stay out. Fetched bars stay the better source where both
exist (`merge`).
Usage: python -m backend.app.warehouse.minute_synth <day dir>   (writes minute_synth.parquet there)
"""
from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

KINDS = ("full", "close", "hot")
SESSION = (("09:30:00", "11:30:59"), ("13:00:00", "15:00:59"))
LAG = "5s"  # a snapshot this soon after a minute boundary still closes the bar ending there


def synth(snaps: pl.DataFrame) -> pl.DataFrame:
    """Snapshot rows (code, quote_ts, price, volume_lots, amount_yuan) to 1-minute bars."""
    t = pl.col("quote_ts").dt.strftime("%H:%M:%S")
    in_session = pl.any_horizontal([(t >= a) & (t <= b) for a, b in SESSION])
    rows = (snaps.filter(in_session & pl.col("price").is_not_null() & pl.col("volume_lots").is_not_null())
            .unique(subset=["code", "quote_ts"], keep="first")
            .sort("code", "quote_ts")
            .with_columns((pl.col("quote_ts").dt.offset_by(f"-{LAG}").dt.truncate("1m").dt.offset_by("1m"))
                          .dt.strftime("%H%M").alias("minute"))
            .filter(pl.col("minute").is_between(pl.lit("0930"), pl.lit("1130")) | pl.col("minute").is_between(pl.lit("1300"), pl.lit("1500"))))
    bars = (rows.group_by("code", "minute", maintain_order=True)
            .agg(pl.col("quote_ts").dt.date().first().alias("date"), pl.col("price").first().alias("open"),
                 pl.col("price").max().alias("high"), pl.col("price").min().alias("low"), pl.col("price").last(),
                 pl.col("volume_lots").last().alias("cum_lots"), pl.col("amount_yuan").last().alias("cum_amount_yuan"),
                 pl.len().alias("n_snaps"))
            .sort("code", "minute"))
    return bars.with_columns(
        pl.col("cum_lots").diff().over("code").fill_null(pl.col("cum_lots")).alias("lots"),
        pl.col("cum_amount_yuan").diff().over("code").fill_null(pl.col("cum_amount_yuan")).alias("amount_yuan"),
        (pl.col("cum_amount_yuan") / (pl.col("cum_lots") * 100)).alias("vwap"),
        pl.lit("snapshot").alias("source"),
    ).select("code", "date", "minute", "open", "high", "low", "price", "lots", "amount_yuan", "cum_lots",
             "cum_amount_yuan", "vwap", "n_snaps", "source")


def from_day(day_dir: Path) -> pl.DataFrame:
    cols = ["code", "kind", "quote_ts", "price", "volume_lots", "amount_yuan"]
    frames = [pl.read_parquet(day_dir / f"{name}.parquet", columns=cols) for name in ("quotes", "hot")
              if (day_dir / f"{name}.parquet").exists()]
    if not frames:
        return synth(pl.DataFrame(schema={"code": pl.Utf8, "kind": pl.Utf8, "quote_ts": pl.Datetime("ms"),
                                          "price": pl.Float64, "volume_lots": pl.Float64, "amount_yuan": pl.Float64}))
    return synth(pl.concat(frames).filter(pl.col("kind").is_in(KINDS)))


def merge(fetched: pl.DataFrame, built: pl.DataFrame) -> pl.DataFrame:
    """Fetched Tencent bars per code where they exist, snapshot bars for the other codes."""
    have = fetched["code"].unique().to_list()
    fetched = fetched.with_columns(pl.lit("tencent").alias("source"))
    return pl.concat([fetched, built.filter(~pl.col("code").is_in(have))], how="diagonal_relaxed").sort("code", "minute")


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    day_dir = Path(argv[0])
    bars = from_day(day_dir)
    bars.write_parquet(day_dir / "minute_synth.parquet", compression="zstd")
    print(f"minute_synth: {bars.height} bars, {bars['code'].n_unique()} codes -> {day_dir / 'minute_synth.parquet'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
