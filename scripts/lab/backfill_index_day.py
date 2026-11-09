"""One-off repair: rebuild one lost day's index series for tail_forward (E078) from Tencent's minute endpoint.

The extended recorder (E076) crashed on 2026-11-09, so that day has no `quotes_ext.parquet` and E078 would log it
without its index side, for good. Tencent's minute endpoint still serves the latest session until the next one
opens, so one request gets the series back. These are published, never revised prices: no look-ahead is added.
The rows say what they are (kind "backfill", host web.ifzq.gtimg.cn). The minute price stands in for the snapshot
5 s into that minute. Only sh000001 is rebuilt; the other instruments of that day stay lost.

Usage: python scripts/lab/backfill_index_day.py <YYYY-MM-DD> [--symbol sh000001]
Refuses to overwrite an existing file and refuses a day the endpoint no longer serves.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path
import sys

import httpx
import polars as pl

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.app.warehouse import intraday_ext as X  # noqa: E402
from backend.app.warehouse.tencent import _number, _positive  # noqa: E402

MINUTE_HOST = "web.ifzq.gtimg.cn"
MINUTE_URL = f"https://{MINUTE_HOST}/appstock/app/minute/query"


def rows_from(payload: dict, symbol: str, day: date) -> list[dict]:
    """One row per minute of `day`; [] when the payload holds another day or no quote line."""
    node = payload["data"][symbol]
    if str(node["data"].get("date")) != day.strftime("%Y%m%d"):
        return []
    quote = node.get("qt", {}).get(symbol) or []
    if len(quote) < 6:
        return []
    name, prev_close, day_open = quote[1], _positive(quote[4]), _positive(quote[5])
    rows = []
    for line in node["data"].get("data") or []:
        parts = str(line).split()
        if len(parts) != 4:
            continue
        stamp = datetime.combine(day, datetime.strptime(parts[0], "%H%M").time())
        rows.append({**{c: None for c in X.SCHEMA}, "symbol": symbol, "name": name, "asset": X.asset_of(symbol),
                     "host": MINUTE_HOST, "kind": "backfill", "poll_ts": stamp, "quote_ts": stamp,
                     "price": _positive(parts[1]), "prev_close": prev_close, "prev_kind": "close", "open": day_open,
                     "volume": _number(parts[2]), "amount": _number(parts[3])})
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="backfill_index_day")
    parser.add_argument("day", type=date.fromisoformat)
    parser.add_argument("--symbol", default="sh000001")
    args = parser.parse_args(argv)
    target = X.output_root() / args.day.isoformat() / "quotes_ext.parquet"
    if target.exists():
        print(f"refused: {target} already exists")
        return 1
    with httpx.Client(timeout=10, headers={"User-Agent": "Mozilla/5.0 Pulse"}) as client:
        response = client.get(MINUTE_URL, params={"code": args.symbol})
    if X.looks_blocked(response.status_code, response.content, response.headers.get("location", "")):
        print(f"refused by {MINUTE_HOST}: HTTP {response.status_code}")
        return 1
    rows = rows_from(response.json(), args.symbol, args.day)
    if not rows:
        print(f"nothing usable for {args.day}: the endpoint serves another session or no quote line")
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    frame = pl.DataFrame(rows, schema=X.SCHEMA).sort(X.SORT_KEYS)
    frame.write_parquet(target, compression="zstd")
    print(f"{target}: {frame.height} rows, {frame['poll_ts'].min()} .. {frame['poll_ts'].max()}")
    print(frame.filter(pl.col("poll_ts").dt.strftime("%H%M").is_in(["0930", "1000", "1430", "1500"]))
          .select("poll_ts", "price", "prev_close", "open"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
