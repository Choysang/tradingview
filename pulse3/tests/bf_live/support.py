"""Small parquet fixtures for the live-lane tests: built in tmp_path, never real recordings.

The old recorder's files are naive Asia/Shanghai millisecond datetimes; builders write exactly that shape.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime
from pathlib import Path

import polars as pl

from pulse.adapters.sources import legacy_recordings as LR

DAY = date(2026, 11, 12)


def _naive(day: date, hhmmss: str) -> datetime:
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return datetime(day.year, day.month, day.day, h, m, s)


def quote_rows(code: str, name: str, prev_close: float, limit_up: float, points: list[tuple], *, kind: str = "full", day: date = DAY) -> list[dict]:
    """points: (HH:MM:SS, price, bid1_px, bid1_lots, ask1_px, ask1_lots)."""
    rows = []
    for t, price, b1, bl, a1, al in points:
        r = {c: 0.0 for c in LR.QUOTE_REQUIRED}
        r.update({"code": code, "name": name, "kind": kind, "poll_ts": _naive(day, t), "quote_ts": _naive(day, t), "amount_yuan": 1_000_000.0,
                  "price": price, "prev_close": prev_close, "open": prev_close, "high": max(price, prev_close), "low": min(price, prev_close),
                  "bid1_px": b1, "bid1_lots": bl, "ask1_px": a1, "ask1_lots": al, "volume_lots": 1000.0, "limit_up": limit_up,
                  "limit_down": round(prev_close * 0.9, 2), "turnover": 1.5, "float_cap": 100.0, "total_cap": 120.0, "volume_ratio": 1.0, "vwap": price})
        rows.append(r)
    return rows


def write_quotes(root: Path, stream: str, part: str, rows: list[dict], day: date = DAY, dir_name: str = "intraday") -> Path:
    d = root / dir_name / day.isoformat() / stream
    d.mkdir(parents=True, exist_ok=True)
    p = d / part
    sch = {c: (pl.String if c in ("code", "name", "kind") else pl.Datetime("ms") if c.endswith("_ts") else pl.Float64) for c in LR.QUOTE_REQUIRED}
    pl.DataFrame(rows, schema=sch).write_parquet(p)
    return p


def scenario_rows(day: date = DAY) -> tuple[list[dict], list[dict]]:
    """(quotes stream rows, hot stream rows) for limit-up 11.00 on prev close 10.00:
    000001 sealed, broken, re-sealed, sealed at close (last snapshot arrives through the hot stream);
    600000 touched the limit with asks standing (never sealed); 000002 never touched; 600519 sealed then broken for good."""
    A = [("09:20:00", 11.00, 11.00, 3000.0, 0.0, 0.0),            # auction match: ignored
         ("09:31:00", 10.50, 10.49, 800.0, 10.51, 900.0),
         ("09:35:00", 11.00, 11.00, 5000.0, 0.0, 0.0),            # first seal
         ("10:00:00", 11.00, 11.00, 6000.0, 0.0, 0.0),
         ("10:10:00", 10.90, 10.89, 400.0, 10.91, 500.0),         # broken
         ("10:20:00", 11.00, 11.00, 7000.0, 0.0, 0.0),            # sealed again
         ("14:50:00", 11.00, 11.00, 7500.0, 0.0, 0.0),
         ("14:56:00", 11.00, 11.00, 8000.0, 0.0, 0.0)]
    B = [("09:40:00", 11.00, 11.00, 100.0, 11.00, 300.0), ("14:55:00", 10.80, 10.79, 100.0, 10.81, 100.0)]
    C = [("09:40:00", 10.20, 10.19, 100.0, 10.21, 100.0), ("14:55:00", 10.30, 10.29, 100.0, 10.31, 100.0)]
    E = [("09:40:00", 11.00, 11.00, 4000.0, 0.0, 0.0), ("10:30:00", 10.80, 10.79, 400.0, 10.81, 500.0), ("14:55:00", 10.85, 10.84, 400.0, 10.86, 500.0)]
    q = (quote_rows("000001", "平安银行", 10.0, 11.0, A[:7], day=day) + quote_rows("600000", "浦发银行", 10.0, 11.0, B, day=day)
         + quote_rows("000002", "万科A", 10.0, 11.0, C, day=day) + quote_rows("600519", "测试酒业", 10.0, 11.0, E, day=day))
    hot = quote_rows("000001", "平安银行", 10.0, 11.0, A[7:], kind="hot", day=day)
    return q, hot


NEWS_COLS = {"source_id": pl.String, "item_id": pl.String, "revision_id": pl.String, "supersedes": pl.String, "content_hash": pl.String, "title": pl.String,
             "body": pl.String, "url": pl.String, "source_published_at": pl.Datetime("ms"), "source_modified_at": pl.Datetime("ms"),
             "received_at": pl.Datetime("ms"), "codes": pl.List(pl.String), "comments": pl.Int64, "level": pl.String}


def news_row(item: str, rev: str, title: str, body: str, pub: datetime, recv: datetime, codes: list[str], *, supersedes: str | None = None,
             source: str = "ths_724") -> dict:
    return {"source_id": source, "item_id": item, "revision_id": rev, "supersedes": supersedes, "content_hash": f"h-{item}-{rev}", "title": title,
            "body": body, "url": f"https://example.invalid/{item}", "source_published_at": pub, "source_modified_at": pub, "received_at": recv,
            "codes": codes, "comments": 0, "level": "1"}


def write_news(root: Path, part: str, rows: list[dict], day: date = DAY) -> Path:
    d = root / "news" / day.isoformat() / "messages"
    d.mkdir(parents=True, exist_ok=True)
    p = d / part
    pl.DataFrame(rows, schema=NEWS_COLS).write_parquet(p)
    return p


def ext_row(symbol: str, asset: str, price: float, t: str = "10:00:00", day: date = DAY) -> dict:
    return {"symbol": symbol, "name": symbol, "asset": asset, "host": "hq.sinajs.cn", "kind": "once", "poll_ts": _naive(day, t), "quote_ts": _naive(day, t),
            "price": price, "prev_close": price - 1, "prev_kind": "settle", "open": price, "high": price, "low": price, "volume": None, "amount": None,
            "bid1": None, "ask1": None}


def write_ext(root: Path, rows: list[dict], day: date = DAY, name: str = "quotes_ext.parquet") -> Path:
    d = root / "intraday_ext" / day.isoformat()
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    nums = ("price", "prev_close", "open", "high", "low", "volume", "amount", "bid1", "ask1")
    sch = {c: (pl.Datetime("ms") if c.endswith("_ts") else pl.Float64 if c in nums else pl.String) for c in LR.EXT_REQUIRED}
    pl.DataFrame(rows, schema=sch).write_parquet(p)
    return p


def tree_state(root: Path) -> dict[str, tuple[int, int, str]]:
    """{relative path: (size, mtime_ns, sha256)} of every file: proves the old tree is only read."""
    return {p.relative_to(root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(root.rglob("*")) if p.is_file()}
