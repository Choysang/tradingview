"""Helpers: write a small synthetic OLD warehouse (parquet files in the old store's layout) from rows or from the recorded Eastmoney bodies."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import polars as pl

from pulse.domain.intelligence import feeds, seats

ING = datetime(2026, 11, 10, 11, 0, tzinfo=timezone.utc)
STOCK_SCHEMA = {"code": pl.Utf8, "date": pl.Date, "name": pl.Utf8, "reason": pl.Utf8, "ingested_at": pl.Datetime("us", "UTC")}
SEAT_SCHEMA = {"code": pl.Utf8, "date": pl.Date, "side": pl.Utf8, "seat": pl.Utf8, "seat_code": pl.Utf8, "rank": pl.Int32, "reason": pl.Utf8,
               "buy": pl.Float64, "sell": pl.Float64, "net": pl.Float64, "ingested_at": pl.Datetime("us", "UTC")}


def write_old(root: Path, stocks: list[dict], seat_rows: list[dict], name: str = "a.parquet", ingested_at: datetime = ING) -> None:
    """``stocks``: code/date/reason; ``seat_rows``: code/date/reason/side/seat/rank/buy/sell (亿元 floats, None allowed)."""
    for table, schema, rows in (("lhb_stocks", STOCK_SCHEMA, stocks), ("lhb_seats", SEAT_SCHEMA, seat_rows)):
        folder = root / table
        folder.mkdir(parents=True, exist_ok=True)
        full = [{**{k: None for k in schema}, "name": "x", "seat_code": "1", "net": None, "ingested_at": ingested_at, **r} for r in rows]
        pl.DataFrame(full, schema=schema).write_parquet(folder / name)


def old_rows_from_recorded(recorded, day: date, *, only_codes: set[str] | None = None) -> tuple[list[dict], list[dict]]:
    """The old store's rows (amounts as float 亿元) for the recorded 2026-11-10 captures."""
    lst = feeds.parse_lhb_list(recorded("lhb_daily_list_20261110.json")).rows
    stocks = [{"code": str(r["SECURITY_CODE"]), "date": day, "reason": str(r["EXPLANATION"])} for r in lst
              if seats.is_equity_code(str(r["SECURITY_CODE"]))]
    seat_rows: list[dict] = []
    rank: dict[tuple, int] = {}
    for side, f, col in (("buy", "lhb_seat_buy_20261110.json", "BUY"), ("sell", "lhb_seat_sell_20261110.json", "SELL")):
        for r in feeds.parse_lhb_seats(recorded(f)).rows:
            code = str(r["SECURITY_CODE"])
            if not seats.is_equity_code(code):
                continue
            key = (code, str(r["EXPLANATION"]), side)
            rank[key] = rank.get(key, -1) + 1
            amount = None if r[col] is None else float(r[col]) / 1e8
            seat_rows.append({"code": code, "date": day, "reason": str(r["EXPLANATION"]), "side": side, "seat": str(r["OPERATEDEPT_NAME"]), "rank": rank[key],
                              "buy": amount if side == "buy" else None, "sell": amount if side == "sell" else None})
    if only_codes is not None:
        stocks = [s for s in stocks if s["code"] in only_codes]
        seat_rows = [s for s in seat_rows if s["code"] in only_codes]
    return stocks, seat_rows
