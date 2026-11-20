"""Synthetic raw windows for the batch 7 tests: exact Decimal bars, hand-computable prices."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal as D

import polars as pl

from pulse.application import bf_batch7_inputs as IN
from pulse.application.refdata_service import instrument_id_of

DEC = pl.Decimal(38, 12)


def weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def raw_frame(series: dict[str, list[tuple]], days: list[date]) -> pl.DataFrame:
    """series: code -> per-day (open, close, volume) with Decimal-able prices; high / low are the max / min of open and close."""
    rows = []
    for code, bars in series.items():
        for d, (o, c, v) in zip(days, bars):
            o, c = D(str(o)), D(str(c))
            rows.append({"instrument_id": instrument_id_of(code), "code": code, "trade_date": d, "open_price_raw": o, "high_price_raw": max(o, c), "low_price_raw": min(o, c),
                         "close_price_raw": c, "volume_shares": int(v)})
    return pl.DataFrame(rows, schema={"instrument_id": pl.String, "code": pl.String, "trade_date": pl.Date, "open_price_raw": DEC, "high_price_raw": DEC, "low_price_raw": DEC,
                                      "close_price_raw": DEC, "volume_shares": pl.Int64})


def window(series, days, source=IN.NATIVE, factors=None) -> IN.RawWindow:
    return IN.RawWindow(raw_frame(series, days), factors, source, ["synthetic"])


def flat(price, n, volume=1000):
    return [(price, price, volume)] * n
