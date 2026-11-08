"""Intraday recorder for the 沪深主板: auction, level-1 book, limit-board queues, minute bars, limit pools.

Daily bars cannot show what an intraday trader sees: the opening auction, how long a board stays sealed,
the queue (封单) in front of a limit order, the moment a board breaks. This job records them forward, so
research can later replay limit orders against the real queue instead of assuming a fill rate.

One process per trading day, started by a scheduled task at 09:10 Asia/Shanghai, exits by 17:00:
* before the open, the static pool: main-board names that touched either limit (|move| >= 9.7% at the
  high or low) in the last 3 sessions of the live warehouse, typically 100~400;
* 09:15~09:25  full main-board snapshot every 60 s (the auction), plus one at 09:25:10 (its result);
  09:19:30~09:25:10 the static pool every 5 s, so the 09:19:50 -> 09:20:05 step (orders can no longer be
  cancelled after 09:20) and the last auction minute 09:24 -> 09:25 are both on the grid;
* 09:30~11:30, 13:00~15:00  full snapshot every 60 s; the hot set every 10 s - the static pool plus
  names within 2% of either limit or that touched one today (sticky for the day, at most 800; when full,
  the names nearest a limit get the free places);
* 15:01:30  closing snapshot;
* 15:05~15:30  盘后固定价格交易 (all main-board A shares since the 2026-07-06 rules): the static pool and the
  hot set every 60 s, and the whole board at 15:05:05, 15:15:00 and 15:30:05 (kind after). The quote fields
  after 15:00 are not verified yet, so these polls always keep their raw text;
* 15:25  Eastmoney limit-up, broken-board and limit-down pools (first / last seal time, opens, seal money);
* 16:20  minute bars of the hot set, names moving 5% or more, the day's pools and news-linked names, at
  1 request a second, after Pulse's 15:35 daily sync, most important first (`minute_order`: limit-up,
  broken board, limit-down, news-linked, the rest), so a block late in the run loses only the least wanted.
  At the end of the day every code gets 1-minute bars built from the snapshots (`minute_synth.parquet`). The rest of the board is not fetched: on 2026-11-02 ~300 minute requests at 2/s from
  15:31 drew a WAF block, and the 60 s full snapshots already cover every code.
Convertible bonds (codes 11xxxx Shanghai, 12xxxx Shenzhen) of main-board stocks ride along: listed bonds
from the Eastmoney bond list at start-up (`cb_map.json`: bond -> stock), in every full snapshot, and in the
10 s / 5 s polls whenever their stock is in the hot set or static pool. A bond trades T+0 without the 10%
limit, so its move can lead the stock's. If the list cannot be fetched the day runs without bonds.
News-linked names: the message recorder (news.py, a separate process) writes `watch.json` with an expiry
per code; each 10 s poll adds the unexpired main-board ones (at most 80, one extra request batch) to the hot
poll. A missing or broken file means no extra names, never an error.

Output, under `$PULSE_INTRADAY_DIR/<YYYY-MM-DD>/` (default `<data_dir>/intraday`; never the warehouse, the
server stays its only writer, and the warehouse is only read for the code list and the static pool):
* `raw/*.txt.gz` - every response as received (gzip members appended), the original evidence;
* `quotes.parquet` (kind auction / auction_final / full / close / after), `hot.parquet` (kind auction_hot / hot /
  after_hot),
  `minute.parquet`, `pools.parquet` - merged and sorted at the end of the day from 15-minute parts;
* `static_pool.json`, `done.json` (steps already run, so a restart does not repeat them), `run.log`,
  `summary.json` (counts, missed grid slots, errors, blocked hosts, the warehouse's last bar date).
Snapshot rows: code, name, kind, poll_ts and quote_ts (CST, naive), price, prev_close, open, high, low,
volume / outer / inner in lots, amount in yuan, bid1~5 and ask1~5 price and lots (a sealed board's bid1
lots is its queue; a level price of 0 means no order there), limit_up, limit_down, turnover %, float /
total cap in 亿, volume ratio, vwap, and the after-hours fixed-price session's cumulative amount (万元) and
volume (lots), fields 58 / 59 (checked on 2026-11-02: lots x 100 x close = amount; the main volume field 6
does not include them). Unknown fields are null, never 0. price and limit_up are parsed from
the same text, so `price == limit_up` is exact.

Politeness and failure: requests go through `tencent._get` (per-host pacing: quotes 10/s, minute 2/s with
a rest after 600). A WAF answer (HTTP 403 / 501, waf.tencent.com) or 20 failures in a row stops that host
for the rest of the day, no retry loops; Eastmoney answering 403 / 429 / 501 or HTML stops the pools.
Each poll has a time budget and every step checks the 17:00 stop, so a slow host cannot push the process
into the next morning. An error in one step is logged and the day goes on; the files are always closed.
`reparse <dir>` rebuilds a day's quotes / hot tables from its raw text with the current parser (the
original files are kept as `*.v0.parquet`); 2026-11-02 was recorded before fields 58 / 59 were parsed.
Usage: python -m backend.app.warehouse.intraday run | once [--codes N] | reparse <day dir>
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
import gzip
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sys
import time
from typing import Any

import httpx
import polars as pl

from ..config import settings
from ..market.calendar import load_calendar
from . import minute_synth
from .store import warehouse
from .news import WATCH_CAP, output_root as news_root, read_watch, watch_path
from .tencent import QUOTE_BATCH, QUOTE_URL, ProviderBlocked, TencentError, _get, _number, _ticker, new_client

LOG = logging.getLogger("pulse.warehouse.intraday")

CST = timezone(timedelta(hours=8))
MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")
QUOTE_HOST, MINUTE_HOST, POOL_HOST = "qt.gtimg.cn", "web.ifzq.gtimg.cn", "push2ex.eastmoney.com"
MINUTE_URL = f"https://{MINUTE_HOST}/appstock/app/minute/query"
POOL_URL = f"https://{POOL_HOST}/{{name}}"
POOL_UT = "7eea3edcaed734bea9cbfc24409ed989"
POOLS = {"zt": ("getTopicZTPool", "fbt:asc"), "zb": ("getTopicZBPool", "fbt:asc"), "dt": ("getTopicDTPool", "fund:asc")}
POOL_PACE = 1.0
CB_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
CB_PAGES = 6

AUCTION = (dtime(9, 15), dtime(9, 25))
AUCTION_DENSE = (dtime(9, 19, 30), dtime(9, 25, 10))
SESSIONS = ((dtime(9, 30), dtime(11, 30, 30)), (dtime(13, 0), dtime(15, 0, 30)))
AFTER_HOURS = (dtime(15, 5), dtime(15, 30, 30))  # 盘后固定价格交易
AFTER_EVERY, AFTER_OFFSET = 60, 5
FULL_EVERY, HOT_EVERY, AUCTION_EVERY = 60, 10, 5
STATIC_DAYS = 3
STATIC_MOVE = 0.097  # a high or low this far from the previous close counts as touching the 10% limit
FULL_OFFSET = 2  # seconds into each minute, so the exchange's minute has printed
# One-shot steps: (name, not before, not after). A late start skips steps whose window has passed.
ONE_SHOTS = (
    ("auction_final", dtime(9, 25, 10), dtime(9, 27)),
    ("close", dtime(15, 1, 30), dtime(15, 4)),
    ("after_open", dtime(15, 5, 5), dtime(15, 7)),
    ("after_mid", dtime(15, 15), dtime(15, 17)),
    ("after_end", dtime(15, 30, 5), dtime(15, 33)),
    ("pools", dtime(15, 25), dtime(15, 50)),
    ("minute_hot", dtime(16, 20), dtime(16, 50)),
)
MINUTE_PACE = 1.0  # seconds between minute requests, on top of the host pacer
STOP_AT = dtime(17, 0)
# Seconds a poll may take before its remaining batches are dropped (normal: full ~4 s, hot ~1 s).
POLL_BUDGET = {"auction": 40.0, "full": 40.0, "auction_final": 40.0, "close": 40.0, "after": 40.0, "hot": 8.0,
               "auction_hot": 4.0, "after_hot": 8.0}
AFTER_SHOTS = ("after_open", "after_mid", "after_end")  # whole-board snapshots of kind "after"
MAX_FAILS_IN_ROW = 20
HOT_BAND = 0.02
HOT_MOVE = 5.0
HOT_CAP = 800
FLUSH_EVERY = 900
MINUTE_PART = 200  # codes per minute-bar part file
MIN_FREE_GB = 5.0  # stop recording below this
RAW_FULL_FREE_GB = 15.0  # below this, full snapshots are kept as parquet only (hot polls keep raw)
RECENT_DAYS = 15
TRADING_SHARE = 0.5  # share of quotes stamped today, below which the day is not a trading day
MAX_ERRORS = 50

_QUOTE_LINE = re.compile(r'v_(?:sh|sz|bj)(\d{6})="([^"]*)"')
BOOK = [(f"bid{i}_px", 9 + 2 * (i - 1)) for i in range(1, 6)] + [(f"bid{i}_lots", 10 + 2 * (i - 1)) for i in range(1, 6)] \
    + [(f"ask{i}_px", 19 + 2 * (i - 1)) for i in range(1, 6)] + [(f"ask{i}_lots", 20 + 2 * (i - 1)) for i in range(1, 6)]
FIELDS = [("price", 3), ("prev_close", 4), ("open", 5), ("volume_lots", 6), ("outer_lots", 7), ("inner_lots", 8),
          *BOOK, ("high", 33), ("low", 34), ("turnover", 38), ("float_cap", 44), ("total_cap", 45),
          ("limit_up", 47), ("limit_down", 48), ("volume_ratio", 49), ("vwap", 51),
          ("after_amount_wan", 58), ("after_lots", 59)]  # 盘后固定价格交易: amount in 万元, volume in lots
SNAPSHOT_SCHEMA: dict[str, Any] = {"code": pl.Utf8, "name": pl.Utf8, "kind": pl.Utf8, "poll_ts": pl.Datetime("ms"),
                                   "quote_ts": pl.Datetime("ms"), "amount_yuan": pl.Float64,
                                   **{name: pl.Float64 for name, _ in FIELDS}}
MINUTE_SCHEMA: dict[str, Any] = {"code": pl.Utf8, "date": pl.Date, "minute": pl.Utf8, "price": pl.Float64,
                                 "cum_lots": pl.Float64, "cum_amount_yuan": pl.Float64}
TABLES = {"quotes": ("code", "poll_ts"), "hot": ("code", "poll_ts"), "minute": ("code", "minute")}


# --- parsing -------------------------------------------------------------------

def _stamp(text: str) -> datetime | None:
    try:
        return datetime.strptime(text.strip(), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def parse_snapshot(text: str, kind: str, poll_ts: datetime) -> list[dict[str, Any]]:
    """One row per quote line with a name; zero prices (suspended, no trade yet) stay null."""
    rows: list[dict[str, Any]] = []
    for match in _QUOTE_LINE.finditer(text):
        f = match.group(2).split("~")
        if len(f) < 52 or not f[1].strip():
            continue
        row: dict[str, Any] = {"code": match.group(1), "name": f[1].strip(), "kind": kind, "poll_ts": poll_ts,
                               "quote_ts": _stamp(f[30])}
        for name, index in FIELDS:
            value = _number(f[index]) if index < len(f) else None  # older or shorter lines lack the after-hours fields
            row[name] = None if value is None or (value == 0 and name in ("price", "open", "high", "low", "vwap")) else value
        parts = f[35].split("/")
        row["amount_yuan"] = _number(parts[2]) if len(parts) == 3 else None
        rows.append(row)
    return rows


def qticker(code: str) -> str:
    """Quote ticker: bonds 11xxxx trade in Shanghai and 12xxxx in Shenzhen (the stock rule maps 1xxxxx to sz)."""
    if code.startswith("11"):
        return f"sh{code}"
    if code.startswith("12"):
        return f"sz{code}"
    return _ticker(code)


def bonds_of(rows: list[dict[str, Any]], today: date) -> dict[str, str]:
    """Listed, not yet delisted convertible bonds of main-board stocks: bond code -> stock code."""
    out: dict[str, str] = {}
    for r in rows:
        bond, stock = str(r.get("SECURITY_CODE") or ""), str(r.get("CONVERT_STOCK_CODE") or "")
        listed, gone = str(r.get("LISTING_DATE") or "")[:10], str(r.get("DELIST_DATE") or "")[:10]
        if not (bond.startswith(("11", "12")) and stock.startswith(MAIN_PREFIX) and listed):
            continue
        if listed <= today.isoformat() and (not gone or gone > today.isoformat()):
            out[bond] = stock
    return out


def fetch_bonds(client: httpx.Client, today: date) -> dict[str, str]:
    rows: list[dict[str, Any]] = []
    for page in range(1, CB_PAGES + 1):
        params = {"reportName": "RPT_BOND_CB_LIST", "columns": "SECURITY_CODE,CONVERT_STOCK_CODE,LISTING_DATE,DELIST_DATE",
                  "pageNumber": str(page), "pageSize": "500", "sortColumns": "PUBLIC_START_DATE", "sortTypes": "-1",
                  "source": "WEB", "client": "WEB"}
        result = _dict(_dict(client.get(CB_URL, params=params).json()).get("result"))
        rows += [r for r in (result.get("data") or []) if isinstance(r, dict)]
        if page >= int(result.get("pages") or 1):
            break
        time.sleep(POOL_PACE)
    return bonds_of(rows, today)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def parse_minute(payload: Any, code: str) -> list[dict[str, Any]]:
    node = _dict(_dict(_dict(_dict(payload).get("data")).get(_ticker(code))).get("data"))
    try:
        when = datetime.strptime(str(node.get("date")), "%Y%m%d").date()
    except ValueError:
        return []
    lines = node.get("data")
    rows = []
    for line in lines if isinstance(lines, list) else []:
        parts = str(line).split()
        if len(parts) != 4:
            continue
        rows.append({"code": code, "date": when, "minute": parts[0], "price": _number(parts[1]),
                     "cum_lots": _number(parts[2]), "cum_amount_yuan": _number(parts[3])})
    return rows


def _near() -> pl.Expr:
    up, down = pl.col("limit_up"), pl.col("limit_down")
    return ((pl.col("price") >= up * (1 - HOT_BAND)) | (pl.col("price") <= down * (1 + HOT_BAND))
            | (pl.col("high") >= up) | (pl.col("low") <= down))


def hot_codes(frame: pl.DataFrame) -> set[str]:
    """Names within HOT_BAND of either limit, or that touched one today."""
    if frame.is_empty():
        return set()
    return set(frame.filter(_near().fill_null(False))["code"].to_list())


def nearest(frame: pl.DataFrame, codes: set[str], n: int) -> set[str]:
    """The `n` of `codes` whose price is closest to either limit."""
    if n <= 0 or not codes:
        return set()
    dist = pl.min_horizontal((pl.col("price") / pl.col("limit_up") - 1).abs(), (pl.col("price") / pl.col("limit_down") - 1).abs())
    ranked = frame.filter(pl.col("code").is_in(list(codes))).with_columns(dist.fill_null(1.0).alias("_d")).sort("_d", "code")
    return set(ranked["code"].head(n).to_list())


def movers(frame: pl.DataFrame) -> set[str]:
    move = ((pl.col("price") / pl.col("prev_close") - 1) * 100).abs()
    return set(frame.filter((move >= HOT_MOVE).fill_null(False))["code"].to_list())


def minute_order(codes: set[str], latest: pl.DataFrame, pools: dict[str, set[str]], watched: set[str]) -> list[str]:
    """Minute-bar fetch order: limit-up, broken board, limit-down, news-linked, then the rest. The pools come
    from Eastmoney; the last snapshot adds names the pools miss (or all of them when the pools failed)."""
    tiers = [set(pools.get(key, ())) for key in ("zt", "zb", "dt")]
    if not latest.is_empty():
        price, up, down = pl.col("price"), pl.col("limit_up"), pl.col("limit_down")
        for tier, cond in zip(tiers, (price >= up, (pl.col("high") >= up) & (price < up), price <= down)):
            tier |= set(latest.filter(cond.fill_null(False))["code"].to_list())
    order: list[str] = []
    seen: set[str] = set()
    for tier in (*tiers, watched, codes):
        fresh = sorted((tier & codes) - seen)
        order += fresh
        seen |= set(fresh)
    return order


# --- schedule ------------------------------------------------------------------

@dataclass
class Clock:
    """What the loop has already done today; `due` is pure so the schedule can be tested."""
    last_full: datetime | None = None
    last_hot: datetime | None = None
    last_auction: datetime | None = None
    last_after: datetime | None = None
    done: set[str] = field(default_factory=set)


def _inside(t: dtime, window: tuple[dtime, dtime]) -> bool:
    return window[0] <= t <= window[1]


def slot(now: datetime, every: int) -> datetime:
    """Start of the `every`-second grid cell that holds `now`."""
    return now.replace(second=now.second - now.second % every, microsecond=0)


def due(now: datetime, clock: Clock) -> list[str]:
    """Steps to run now, the fast grids first so a slow full snapshot cannot delay them."""
    t = now.time()
    out: list[str] = []
    in_session = any(_inside(t, w) for w in SESSIONS)
    if _inside(t, AUCTION_DENSE) and clock.last_auction != slot(now, AUCTION_EVERY):
        out.append("auction_hot")
    if in_session and clock.last_hot != slot(now, HOT_EVERY):
        out.append("hot")
    if _inside(t, AFTER_HOURS) and now.second >= AFTER_OFFSET and clock.last_after != slot(now, AFTER_EVERY):
        out.append("after_hot")
    if (in_session or _inside(t, AUCTION)) and now.second >= FULL_OFFSET and clock.last_full != slot(now, FULL_EVERY):
        out.append("full")
    for name, start, end in ONE_SHOTS:
        if name not in clock.done and start <= t <= end:
            out.append(name)
    return out


def _window(t: dtime) -> int:
    for i, w in enumerate((AUCTION, *SESSIONS, AFTER_HOURS)):
        if _inside(t, w):
            return i
    return -1


def missed(previous: datetime | None, current: datetime, every: int) -> int:
    """Grid slots skipped between two polls of one grid in the same window (auction, morning, afternoon)."""
    if previous is None or _window(previous.time()) != _window(current.time()):
        return 0
    gap = (current - previous).total_seconds()
    return int(gap // every) - 1 if gap > every else 0


# --- recorder ------------------------------------------------------------------

class Recorder:
    def __init__(self, day: date, codes: list[str], root: Path, client: httpx.Client, static: set[str] | None = None,
                 stop_at: dtime | None = None, bonds: dict[str, str] | None = None) -> None:
        self.day, self.codes, self.client, self.stop_at = day, codes, client, stop_at
        self.bonds = {b: s for b, s in (bonds or {}).items() if s in set(codes)}
        self.dir = root / day.isoformat()
        for sub in ("raw", *TABLES):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        self.frames: dict[str, list[pl.DataFrame]] = {"quotes": [], "hot": []}
        self.minute_rows: list[dict[str, Any]] = []
        self.minute_seen: set[str] = set()
        self.pool_codes: dict[str, set[str]] = {}
        self.watch_seen: set[str] = set()  # every news-linked name of the day, for the minute-bar order
        self.latest = pl.DataFrame(schema=SNAPSHOT_SCHEMA)
        self.static = set(static or ()) & set(codes)
        self.hot: set[str] = set(self.static)
        (self.dir / "static_pool.json").write_text(json.dumps(sorted(self.static)), encoding="utf-8")
        (self.dir / "cb_map.json").write_text(json.dumps(self.bonds, ensure_ascii=False), encoding="utf-8")
        self.blocked: dict[str, str] = {}
        self.fails: dict[str, int] = {}
        self.seq = 0
        self.stats: dict[str, Any] = {"polls": {}, "rows": {}, "missed": {}, "truncated": {}, "failed_requests": 0,
                                      "minute_codes": 0, "minute_missing": 0, "pools": {}, "errors": [], "notes": []}
        self.last_flush = time.monotonic()
        self.disk_low = False
        self.finished = False
        self.watch_file: Path | None = None  # set by run(); None in tests and the smoke run
        self.raw_full = self._free_gb() >= RAW_FULL_FREE_GB
        if not self.raw_full:
            self.note(f"free disk below {RAW_FULL_FREE_GB} GB: full snapshots kept as parquet only")

    def late(self) -> bool:
        """Past the stop time (the `once` smoke test has none)."""
        return self.stop_at is not None and datetime.now(CST).time() >= self.stop_at

    def _free_gb(self) -> float:
        return shutil.disk_usage(self.dir).free / 1e9

    def note(self, text: str) -> None:
        if text not in self.stats["notes"]:
            self.stats["notes"].append(text)

    def error(self, text: str) -> None:
        if len(self.stats["errors"]) < MAX_ERRORS:
            self.stats["errors"].append(f"{datetime.now(CST):%H:%M:%S} {text}"[:300])

    def _raw(self, name: str, header: str, body: str) -> None:
        with gzip.open(self.dir / "raw" / f"{name}.txt.gz", "at", encoding="utf-8") as fh:
            fh.write(f"# {header}\n{body}\n")

    def _count(self, key: str, kind: str, n: int) -> None:
        self.stats[key][kind] = self.stats[key].get(kind, 0) + n

    def _block(self, host: str, exc: Exception | str) -> None:
        self.blocked[host] = f"{datetime.now(CST):%H:%M:%S} {exc}"
        LOG.error("intraday: %s stopped for today: %s", host, exc)

    def _failed(self, host: str, exc: Exception) -> None:
        self.stats["failed_requests"] += 1
        self.fails[host] = self.fails.get(host, 0) + 1
        LOG.warning("intraday: %s request failed: %s", host, exc)
        if self.fails[host] >= MAX_FAILS_IN_ROW:
            self._block(host, f"{MAX_FAILS_IN_ROW} failures in a row, last: {exc}")

    def _part(self, sub: str) -> Path:
        self.seq += 1
        return self.dir / sub / f"part-{datetime.now(CST):%H%M%S}-{self.seq:05d}.parquet"

    # quotes
    def snapshot(self, kind: str, codes: list[str]) -> pl.DataFrame:
        if QUOTE_HOST in self.blocked or not codes:
            return pl.DataFrame(schema=SNAPSHOT_SCHEMA)
        poll_ts = datetime.now(CST).replace(tzinfo=None)
        start, budget = time.monotonic(), POLL_BUDGET.get(kind, 40.0)
        tickers = [qticker(code) for code in codes]
        rows: list[dict[str, Any]] = []
        bodies: list[str] = []
        for index in range(0, len(tickers), QUOTE_BATCH):
            if QUOTE_HOST in self.blocked or self.late():
                break
            if time.monotonic() - start > budget:
                self._count("truncated", kind, 1)
                break
            try:
                response = _get(self.client, QUOTE_URL + ",".join(tickers[index:index + QUOTE_BATCH]))
            except ProviderBlocked as exc:
                self._block(QUOTE_HOST, exc)
                break
            except TencentError as exc:
                self._failed(QUOTE_HOST, exc)
                continue
            self.fails[QUOTE_HOST] = 0
            text = response.content.decode("gbk", errors="replace")
            bodies.append(text)
            rows.extend(parse_snapshot(text, kind, poll_ts))
        sub = "hot" if kind in ("hot", "auction_hot", "after_hot") else "quotes"
        if bodies and (sub == "hot" or self.raw_full or kind == "after"):
            self._raw(sub, f"{kind} {poll_ts:%H:%M:%S}", "\n".join(bodies))
        frame = pl.DataFrame(rows, schema=SNAPSHOT_SCHEMA)
        self.frames[sub].append(frame)
        self._count("polls", kind, 1)
        self._count("rows", kind, frame.height)
        return frame

    def with_bonds(self, stocks: set[str]) -> list[str]:
        return sorted(stocks | {b for b, s in self.bonds.items() if s in stocks})

    def full(self, kind: str) -> None:
        frame = self.snapshot(kind, self.codes + sorted(self.bonds))
        if frame.is_empty():
            return
        self.latest = frame
        if kind in ("full", "close") and datetime.now(CST).time() >= SESSIONS[0][0]:
            new = hot_codes(frame.filter(~pl.col("code").is_in(list(self.bonds)))) - self.hot
            room = HOT_CAP - len(self.hot)
            if len(new) > room:  # the static pool and names already in stay; the nearest new ones get the room
                self.note(f"hot set capped at {HOT_CAP}")
                new = nearest(frame, new, room)
            self.hot |= new

    def watched(self) -> set[str]:
        if self.watch_file is None:
            return set()
        live = read_watch(self.watch_file, datetime.now(CST), WATCH_CAP) & set(self.codes)
        self.watch_seen |= live
        self.stats["watch_max"] = max(self.stats.get("watch_max", 0), len(live))
        return live

    def poll_hot(self, kind: str = "hot") -> None:
        codes = self.static if kind == "auction_hot" else self.static | self.hot if kind == "after_hot" else self.hot
        if kind == "hot":
            codes = codes | self.watched()
        if codes:
            self.snapshot(kind, self.with_bonds(codes))

    def trading_today(self) -> bool:
        """Quotes stamped today: false on an unlisted holiday, when the feed repeats the last session."""
        stamps = self.latest["quote_ts"].drop_nulls()
        if stamps.is_empty():
            return False
        return float((stamps.dt.date() == self.day).mean()) >= TRADING_SHARE

    # minute bars
    def minutes(self, codes: list[str], label: str) -> None:
        got = 0
        todo = [c for c in codes if c not in self.minute_seen]
        for i, code in enumerate(todo):
            if MINUTE_HOST in self.blocked or self.late():
                break
            if i:
                time.sleep(MINUTE_PACE)
            try:
                response = _get(self.client, MINUTE_URL, params={"code": _ticker(code)})
                payload = response.json()
            except ProviderBlocked as exc:
                self._block(MINUTE_HOST, exc)
                break
            except (TencentError, ValueError) as exc:
                self._failed(MINUTE_HOST, exc)
                continue
            self.fails[MINUTE_HOST] = 0
            self._raw("minute", f"{label} {code}", json.dumps(payload, ensure_ascii=False))
            rows = parse_minute(payload, code)
            if rows and rows[0]["date"] == self.day:
                self.minute_rows.extend(rows)
                self.minute_seen.add(code)
                got += 1
            if (i + 1) % MINUTE_PART == 0:
                self._flush_minute()
        self._flush_minute()
        self.stats["minute_codes"] += got
        self.stats["minute_missing"] += len(todo) - got
        LOG.info("intraday: minute %s %d/%d codes", label, got, len(todo))

    def _flush_minute(self) -> None:
        if self.minute_rows:
            pl.DataFrame(self.minute_rows, schema=MINUTE_SCHEMA).write_parquet(self._part("minute"), compression="zstd")
            self.minute_rows = []

    # Eastmoney pools
    def _pool_get(self, name: str, params: dict[str, str]) -> httpx.Response:
        try:
            return self.client.get(POOL_URL.format(name=name), params=params)
        except httpx.TimeoutException:
            time.sleep(2.0)
            return self.client.get(POOL_URL.format(name=name), params=params)

    def pools(self) -> None:
        frames = []
        for key, (name, sort) in POOLS.items():
            if POOL_HOST in self.blocked:
                break
            params = {"ut": POOL_UT, "dpt": "wz.ztzt", "Pageindex": "0", "pagesize": "10000", "sort": sort,
                      "date": f"{self.day:%Y%m%d}"}
            try:
                response = self._pool_get(name, params)
            except httpx.HTTPError as exc:
                self.error(f"pool {key}: {exc!r}")
                continue
            if response.status_code in (403, 429, 501):
                self._block(POOL_HOST, f"HTTP {response.status_code}")
                break
            try:
                payload = response.json()
            except ValueError:
                self._block(POOL_HOST, f"not JSON (HTTP {response.status_code})")  # an HTML page is a refusal
                break
            self._raw("pools", key, json.dumps(payload, ensure_ascii=False))
            data = _dict(_dict(payload).get("data"))
            if str(data.get("qdate", "")) != f"{self.day:%Y%m%d}":  # the feed answers a holiday with the last session
                self.note(f"pool {key} is for {data.get('qdate')}, not today: not stored")
                continue
            pool = [r for r in (data.get("pool") or []) if isinstance(r, dict)]
            try:
                if int(data.get("tc") or 0) > len(pool):
                    self.note(f"pool {key} truncated: {len(pool)} of {data['tc']}")
            except (TypeError, ValueError):
                pass
            self.stats["pools"][key] = len(pool)
            self.pool_codes[key] = {str(r.get("c")) for r in pool if r.get("c")}
            if pool:
                frames.append(pl.DataFrame([{k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                                             for k, v in r.items()} for r in pool], infer_schema_length=None)
                              .with_columns(pl.lit(key).alias("pool")))
            time.sleep(POOL_PACE)
        if frames:
            pl.concat(frames, how="diagonal_relaxed").write_parquet(self.dir / "pools.parquet")

    # files
    def flush(self, force: bool = False) -> None:
        if not force and time.monotonic() - self.last_flush < FLUSH_EVERY:
            return
        for sub, frames in self.frames.items():
            if frames:
                pl.concat(frames).write_parquet(self._part(sub), compression="zstd")
                self.frames[sub] = []
        self.last_flush = time.monotonic()
        if self._free_gb() < MIN_FREE_GB:
            self.disk_low = True
            LOG.error("intraday: free disk below %s GB, stopping", MIN_FREE_GB)

    def compact(self) -> None:
        """One file per table, sorted (compresses far better than the parts), streamed so a whole day never
        sits in memory. The parts are removed only after the merged file is written."""
        for sub, keys in TABLES.items():
            parts = sorted((self.dir / sub).glob("part-*.parquet"))
            if not parts:
                continue
            target = self.dir / f"{sub}.parquet"
            temporary = self.dir / f".{sub}.parquet.tmp"
            sources = ([str(target)] if target.exists() else []) + [str(p) for p in parts]
            try:
                lazy = pl.scan_parquet(sources)
                if sub == "minute":  # a restart may fetch a code twice
                    lazy = lazy.unique(subset=list(keys), keep="last")
                lazy.sort(list(keys)).sink_parquet(temporary, compression="zstd", compression_level=9)
                os.replace(temporary, target)
            except (OSError, pl.exceptions.PolarsError) as exc:
                self.note(f"compact {sub} failed, parts kept: {exc}")
                continue
            for p in parts:
                p.unlink(missing_ok=True)

    def synth(self) -> None:
        bars = minute_synth.from_day(self.dir)
        bars.write_parquet(self.dir / "minute_synth.parquet", compression="zstd")
        self.stats["minute_synth_codes"] = bars["code"].n_unique()

    def finish(self, reason: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.finished:
            return {}
        self.finished = True
        steps = (("flush", lambda: self.flush(force=True)), ("minute", self._flush_minute), ("compact", self.compact),
                 ("synth", self.synth))
        for step_name, fn in steps:
            try:
                fn()
            except Exception as exc:  # the summary must still be written
                self.error(f"finish {step_name}: {exc!r}")
        size = sum(p.stat().st_size for p in self.dir.rglob("*") if p.is_file())
        summary = {"day": self.day.isoformat(), "codes": len(self.codes), "bonds": len(self.bonds), "static_pool": len(self.static),
                   "hot_codes": len(self.hot), "blocked": self.blocked, "end": reason, "bytes": size,
                   **(extra or {}), **self.stats}
        (self.dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        LOG.info("intraday: %s", json.dumps(summary, ensure_ascii=False, default=str))
        return summary


def recent_bars(today: date) -> pl.DataFrame:
    """Main-board bars of the last RECENT_DAYS calendar days before `today`, from the live warehouse (read only)."""
    lazy = warehouse("live").scan("bars")
    if lazy is None:
        return pl.DataFrame(schema={"code": pl.Utf8, "date": pl.Date, "high": pl.Float64, "low": pl.Float64, "close": pl.Float64})
    return (lazy.filter((pl.col("date") >= today - timedelta(days=RECENT_DAYS)) & (pl.col("date") < today)
                        & pl.col("code").str.slice(0, 3).is_in(MAIN_PREFIX))
            .select("code", "date", "high", "low", "close").collect())


def main_codes(bars: pl.DataFrame) -> list[str]:
    return sorted(bars["code"].unique().to_list())


def static_pool(bars: pl.DataFrame, days: int = STATIC_DAYS) -> set[str]:
    """Names whose high or low reached about the 10% limit in the last `days` sessions."""
    if bars.is_empty():
        return set()
    bars = bars.sort("code", "date").with_columns(pl.col("close").shift(1).over("code").alias("prev"))
    last = sorted(bars["date"].unique().to_list())[-days:]
    touched = (pl.col("high") / pl.col("prev") - 1 >= STATIC_MOVE) | (pl.col("low") / pl.col("prev") - 1 <= -STATIC_MOVE)
    return set(bars.filter(pl.col("date").is_in(last) & touched.fill_null(False))["code"].to_list())


def output_root() -> Path:
    override = os.getenv("PULSE_INTRADAY_DIR")
    return Path(override) if override else settings.data_dir / "intraday"


def step(rec: Recorder, action: str) -> None:
    if action in ("full", "auction_final", "close"):
        kind = "auction" if action == "full" and datetime.now(CST).time() < SESSIONS[0][0] else action
        rec.full(kind)
    elif action in AFTER_SHOTS:
        rec.full("after")
    elif action in ("hot", "auction_hot", "after_hot"):
        rec.poll_hot(action)
    elif action == "minute_hot":
        wanted = (rec.hot | movers(rec.latest) | rec.watch_seen | set().union(*rec.pool_codes.values())) & set(rec.codes)
        rec.minutes(minute_order(wanted, rec.latest, rec.pool_codes, rec.watch_seen), "hot")
    elif action == "pools":
        rec.pools()


def _log_to(day_dir: Path) -> None:
    handler = logging.FileHandler(day_dir / "run.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(handler)


def _advance(clock: Clock, rec: Recorder, action: str, now: datetime) -> None:
    """Mark a step as taken (before running it, so a failing step is not retried in a tight loop)."""
    grids = {"full": ("last_full", FULL_EVERY), "hot": ("last_hot", HOT_EVERY), "auction_hot": ("last_auction", AUCTION_EVERY),
             "after_hot": ("last_after", AFTER_EVERY)}
    if action in grids:
        attr, every = grids[action]
        current = slot(now, every)
        lost = missed(getattr(clock, attr), current, every)
        if lost:
            rec._count("missed", action, lost)
        setattr(clock, attr, current)
    else:
        clock.done.add(action)
        (rec.dir / "done.json").write_text(json.dumps(sorted(clock.done)), encoding="utf-8")


def run() -> int:
    today = datetime.now(CST).date()
    if not load_calendar().is_trading_day(today):
        LOG.info("intraday: %s is not a trading day", today)
        return 0
    day_dir = output_root() / today.isoformat()
    summary = day_dir / "summary.json"
    if summary.exists() and json.loads(summary.read_text(encoding="utf-8")).get("end") == "done":
        LOG.info("intraday: %s already recorded", today)
        return 0
    bars = recent_bars(today)
    codes = main_codes(bars)
    if not codes:
        LOG.error("intraday: no main-board codes in the live warehouse")
        return 1
    clock = Clock()
    done_file = day_dir / "done.json"
    if done_file.exists():
        clock.done = set(json.loads(done_file.read_text(encoding="utf-8")))
    extra = {"bars_last_date": bars["date"].max(), "restarted": bool(clock.done)}
    checked, reason = False, "stop time"
    with new_client() as client:
        try:
            bonds = fetch_bonds(client, today)
        except Exception as exc:  # the day runs without bonds
            LOG.warning("intraday: bond list failed: %r", exc)
            bonds = {}
        rec = Recorder(today, codes, output_root(), client, static_pool(bars), stop_at=STOP_AT, bonds=bonds)
        if not bonds:
            rec.note("no convertible bond list: bonds not recorded")
        rec.watch_file = watch_path(news_root())
        _log_to(rec.dir)
        LOG.info("intraday: %s, %d codes, static pool %d, bars to %s", today, len(codes), len(rec.static), extra["bars_last_date"])
        try:
            while True:
                now = datetime.now(CST)
                if now.time() >= STOP_AT or "minute_hot" in clock.done or rec.disk_low:
                    reason = "disk low" if rec.disk_low else "done" if "minute_hot" in clock.done else "stop time"
                    break
                for action in due(now, clock):
                    _advance(clock, rec, action, now)
                    try:
                        step(rec, action)
                    except Exception as exc:  # one bad answer must not end the day
                        LOG.exception("intraday: step %s failed", action)
                        rec.error(f"{action}: {exc!r}")
                    if action == "full" and not checked and now.time() >= SESSIONS[0][0] and not rec.latest.is_empty():
                        checked = True
                        if not rec.trading_today():
                            reason = "quotes not stamped today: not a trading day"
                            return 0
                try:
                    rec.flush()
                except Exception as exc:
                    LOG.exception("intraday: flush failed")
                    rec.error(f"flush: {exc!r}")
                time.sleep(0.5)
        finally:
            rec.finish(reason, extra)
    return 0


def once(n: int) -> int:
    """Smoke test outside the session: one full snapshot, minute bars and pools for a few codes."""
    today = datetime.now(CST).date()
    bars = recent_bars(today)
    codes = main_codes(bars)
    static = static_pool(bars)
    LOG.info("intraday once: %d codes, static pool %d", len(codes), len(static))
    codes = codes[:n] if n else codes
    with new_client() as client:
        try:
            bonds = fetch_bonds(client, today)
        except Exception as exc:
            LOG.warning("intraday: bond list failed: %r", exc)
            bonds = {}
        rec = Recorder(today, codes, output_root(), client, static | set(codes[:3]), bonds=bonds)
        _log_to(rec.dir)
        try:
            rec.full("full")
            rec.poll_hot("auction_hot")
            rec.hot |= hot_codes(rec.latest)
            rec.poll_hot()
            rec.minutes(codes[:5], "once")
            rec.pools()
        finally:
            rec.finish("once", {"bars_last_date": bars["date"].max() if bars.height else None})
    return 0


_RAW_HEADER = re.compile(r"^# (\S+) (\d\d:\d\d:\d\d)$", re.M)


def raw_polls(path: Path) -> list[tuple[str, str, str]]:
    """(kind, HH:MM:SS, body) of every poll in a raw snapshot file."""
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        text = fh.read()
    heads = list(_RAW_HEADER.finditer(text))
    return [(h.group(1), h.group(2), text[h.end():heads[i + 1].start() if i + 1 < len(heads) else len(text)])
            for i, h in enumerate(heads)]


def reparse(day_dir: Path) -> dict[str, int]:
    """Rebuild quotes / hot rows of the polls that kept raw text with the current parser. poll_ts is taken
    from the stored table (the raw header has whole seconds only); polls without raw text stay as they are."""
    day = date.fromisoformat(day_dir.name)
    out = {}
    for sub in ("quotes", "hot"):
        target, raw = day_dir / f"{sub}.parquet", day_dir / "raw" / f"{sub}.txt.gz"
        if not (target.exists() and raw.exists()):
            continue
        old = pl.read_parquet(target)
        stamps = (old.select("kind", "poll_ts").unique()
                  .with_columns(pl.col("poll_ts").dt.strftime("%H:%M:%S").alias("_s")))
        rows = []
        for kind, hms, body in raw_polls(raw):
            ts = stamps.filter((pl.col("kind") == kind) & (pl.col("_s") == hms))["poll_ts"]
            poll_ts = ts[0] if ts.len() else datetime.combine(day, dtime.fromisoformat(hms))
            rows += parse_snapshot(body, kind, poll_ts)
        new = pl.DataFrame(rows, schema=SNAPSHOT_SCHEMA)
        keys = new.select("kind", "poll_ts").unique()
        kept = old.join(keys, on=["kind", "poll_ts"], how="anti")
        merged = pl.concat([kept, new], how="diagonal_relaxed").select(list(SNAPSHOT_SCHEMA)).sort("code", "poll_ts")
        backup = day_dir / f"{sub}.v0.parquet"
        if not backup.exists():
            shutil.copy2(target, backup)
        temporary = day_dir / f".{sub}.parquet.tmp"
        merged.write_parquet(temporary, compression="zstd", compression_level=9)
        os.replace(temporary, target)
        out[sub] = new.height
        LOG.info("intraday: reparse %s: %d rows from raw, %d kept, %d before", sub, new.height, kept.height, old.height)
    return out


def cli(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request would bury the log
    parser = argparse.ArgumentParser(prog="intraday")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    smoke = sub.add_parser("once")
    smoke.add_argument("--codes", type=int, default=0)
    again = sub.add_parser("reparse")
    again.add_argument("day_dir", type=Path)
    args = parser.parse_args(argv)
    if args.cmd == "reparse":
        print(reparse(args.day_dir))
        return 0
    return run() if args.cmd == "run" else once(args.codes)


if __name__ == "__main__":
    sys.exit(cli())
