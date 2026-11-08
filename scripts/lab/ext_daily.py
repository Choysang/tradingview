"""Batch 3 outside data (backend venv): US index closes (E072), broad-ETF daily bars (E074) and the Eastmoney
economic calendar history (E073). A handful of requests, PACE seconds apart; any failure stops the job (no retry).
Outputs under .work/lab/ext/: us_index.parquet (symbol, date, close), etf_daily.parquet (symbol, date, open, close,
amount), calendar_hist.parquet (start, name, type, timed).
Usage: python ext_daily.py   (run from scripts/lab with PYTHONPATH=../..)
"""
from __future__ import annotations

import sys
import time
from datetime import date, datetime, timedelta

import akshare as ak
import httpx
import polars as pl

from backend.app.warehouse.calendar_events import fetch_em
from ann_fetch import ROOT

EXT = ROOT / ".work" / "lab" / "ext"
US = (".INX", ".IXIC", ".SOX", ".HXC")
ETF = ("sh510300", "sh510310", "sh510050", "sz159919", "sh510500", "sh512100", "sz159845")
CAL_START, CAL_END = date(2023, 11, 1), date(2026, 10, 31)
PACE = 1.5


def us() -> None:
    frames = []
    for s in US:
        df = pl.from_pandas(ak.index_us_stock_sina(symbol=s)).select(pl.lit(s).alias("symbol"), pl.col("date").cast(pl.Date),
                                                                   pl.col("close").cast(pl.Float64))
        frames.append(df)
        time.sleep(PACE)
    pl.concat(frames).write_parquet(EXT / "us_index.parquet")


def etf() -> None:
    frames = []
    for s in ETF:
        df = pl.from_pandas(ak.fund_etf_hist_sina(symbol=s)).select(
            pl.lit(s[2:]).alias("symbol"), pl.col("date").cast(pl.Date), *(pl.col(x).cast(pl.Float64) for x in ("open", "close", "amount")))
        frames.append(df)
        time.sleep(PACE)
    pl.concat(frames).write_parquet(EXT / "etf_daily.parquet")


def calendar() -> None:
    rows: list[dict] = []
    client = httpx.Client(timeout=20, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"})
    d = CAL_START
    while d <= CAL_END:
        rows += fetch_em(client, d, 30)
        d += timedelta(days=30)
        time.sleep(PACE)
    df = pl.DataFrame([{"start": r["START_DATE"], "name": str(r.get("FE_NAME") or ""), "type": str(r.get("FE_TYPE") or ""),
                        "code": str(r.get("FE_CODE") or "")} for r in rows]).unique(["start", "name", "code"])
    df = df.with_columns(pl.col("start").str.to_datetime("%Y-%m-%d %H:%M:%S").alias("start")).with_columns(
        (pl.col("start").dt.time() != datetime.min.time()).alias("timed")).sort("start")
    df.write_parquet(EXT / "calendar_hist.parquet")
    print("calendar rows", df.height)


if __name__ == "__main__":
    EXT.mkdir(parents=True, exist_ok=True)
    for step in (sys.argv[1:] or ["us", "etf", "calendar"]):
        {"us": us, "etf": etf, "calendar": calendar}[step]()
        print(step, "ok", flush=True)
