"""Intraday recorder for what the main-board recorder (intraday.py) does not cover: broad ETFs, indices,
CFFEX index futures, the FTSE China A50, Hang Seng futures, USD/CNH, COMEX gold and WTI crude.

Research needs these for state signals ("national team buying broad ETFs", the index's first 30 minutes against
its last 30, overnight shocks). This is a separate process: it never imports or touches intraday.py, news.py
or the warehouse, and writes only under its own directory.

One process per day, started around 09:00 Asia/Shanghai:
* one poll every 60 s, 5 s into the minute, from 09:00 to 16:00 (the A-share quotes only move 09:15~15:00, but
  index futures trade 09:30~15:00 and the A50, Hang Seng futures, CNH and gold run longer);
* one extra "close" snapshot at 15:01:30, which also fetches the A50 and CNH minute bars;
* one more fetch of those minute bars at 16:00:30 (kind "end");
* the process ends after that and in any case by 16:10.
A weekend or a holiday of the trading calendar ends the run at once; a day whose index quotes carry no
today's timestamp on five polls after 09:31 (an unlisted holiday) ends it as "not a trading day".

Instruments (probed from the server on 2026-11-09; 38 symbols, 5 requests per minute, 2 more at each minute-bar fetch):
* qt.gtimg.cn/q=<comma list> (one request):
  - ETFs sh510300 sh510310 sh510050 sz159919 sh510500 sh512100 sz159845 and indices sh000001 sz399001 sz399006
    sh000300 sh000016 sh000905 sh000852 sh000688 sh000985 sz399303. Same `~` format as stocks; volume in lots,
    amount in yuan (field 35), index bid/ask are 0.00 and become null.
  - hf_HSI (Hang Seng index futures), hf_GC (COMEX gold), hf_CL (WTI crude): last, high, low, previous settlement,
    open, date and time (Beijing time). The hf_HSI bid / ask did not agree with the last price and the volume
    field is unverified, so bid1, ask1, volume and amount stay null; the raw text keeps them. These are continuous
    quotes: contract is null.
* www.cffex.com.cn/quote_<IF|IH|IC|IM>.txt (one request per product): the exchange's own files, one line per listed
  contract with open, high, low, last, change, bid, ask, volume (contracts) and open interest. Plain http: https
  timed out from the server, and only public quotes pass here. The symbol is the contract code (IF2612). The main
  contract of a poll is the one with the highest open_interest, worked out when the data is read.
  The file has no previous settlement, but `updown` is last minus it (IF2611 on 2026-11-09: 4048.4 - 3.6 = 4044.8,
  Eastmoney's settlement field), so prev_close is derived with prev_kind "settle". It has no amount (null) and no
  time: quote_ts is the response's Last-Modified, which the fetcher keeps as a `#last-modified:` first line.
  After the close on 2026-11-09 the files changed every 5 min 15 s; the cadence during a session is not known yet,
  so read quote_ts, not poll_ts, for the age of a futures quote.
  `contract_months` only says which contracts to expect, for the summary; every contract in the file is recorded.
* push2his.eastmoney.com/api/qt/stock/trends2/get (one request per symbol, twice a day): em_CN00Y (FTSE China A50
  futures, front-month continuous, so it jumps at the monthly roll; contract is null) and em_USDCNH (offshore yuan).
  Each answer holds every 1-minute bar of the current session, so one good answer a day is enough, and the A50 bars
  start the evening before (2026-11-06 16:45 for the Monday session): the overnight move is in them. These rows are
  kind "minute": poll_ts and quote_ts are the bar's minute, and open, high, low are the bar's. prev_close is the
  settlement for the A50 and the previous close for CNH. A50 volume is contracts as published (unverified); CNH has
  none. The endpoint is not always there for this server (it dropped a connection on 2026-11-09), hence two tries.
Tried and dropped:
* hq.sinajs.cn answers this server with HTTP 403 (2026-11-08); it is not asked again.
* push2.eastmoney.com answers 302 to push2delay.eastmoney.com, whose ulist.np gives 60 s quotes but answered this
  server with HTTP 502 on 10 of 11 requests (2026-11-09). It answers from another address.
* Eastmoney's COMEX gold (101.GC00Y) stood 10 minutes behind Tencent's hf_GC, and its CFFEX market (8) is delayed.

Output, under `$PULSE_INTRADAY_EXT_DIR/<YYYY-MM-DD>/` (default `<data_dir>/intraday_ext`):
* `raw/<host>.txt.gz` - every response as received (GBK bytes, one gzip member per poll, a `# kind HH:MM:SS status`
  header line before each), including the body of a refusal;
* `parts/part-*.parquet` every 15 minutes, merged at the end into `quotes_ext.parquet` (sorted by symbol, poll_ts),
  so a crash keeps most of the day; `python -m backend.app.warehouse.intraday_ext compact <day dir>` merges
  leftovers;
* `done.json`, `run.log`, `summary.json` (polls, rows, missed grid slots, errors, blocked hosts, the symbols that
  never returned a price; after a restart the counts are the second process's own, and missed counts from 09:00).
Rows: symbol, name, asset (etf / index / future / fx / commodity), host, kind (grid / close / once / minute), poll_ts and
quote_ts (CST, naive), price, prev_close and prev_kind (close / settle), open, high, low, volume, amount (yuan), bid1,
ask1, open_interest, contract. Unknown fields are null, never 0 (a zero price is null).

Politeness: at most one request a second per host (one in 3 s for Eastmoney), one at a time per host, no retries (the
next poll is the retry).
HTTP 403 / 429 / 456 / 501, any redirect, a waf.tencent.com mention or an HTML page answered with a success status
stops that host for the rest of the day, and so do 20 polls' worth of failures in a row. Each poll runs the hosts in
parallel with a time budget; a request over the budget counts as a failure, so a slow host cannot delay the grid or
starve the others. Recording stops below 5 GB free disk.
Usage: python -m backend.app.warehouse.intraday_ext run | once | compact <day dir>
"""
from __future__ import annotations

import argparse
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from email.utils import parsedate_to_datetime
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
from .tencent import ProviderBlocked, _number, _Pacer, _positive, new_client

LOG = logging.getLogger("pulse.warehouse.intraday_ext")

CST = timezone(timedelta(hours=8))
QUOTE_HOST, CFFEX_HOST, EM_HOST = "qt.gtimg.cn", "www.cffex.com.cn", "push2his.eastmoney.com"
HOSTS = (QUOTE_HOST, CFFEX_HOST, EM_HOST)
QUOTE_URL = f"https://{QUOTE_HOST}/q="
CFFEX_URL = f"http://{CFFEX_HOST}/quote_{{}}.txt"
EM_URL = (f"https://{EM_HOST}/api/qt/stock/trends2/get?fields1=f1,f2,f3,f5,f6,f7,f8"
          "&fields2=f51,f52,f53,f54,f55,f56,f57,f58&iscr=0&ndays=1&secid=")
HEADERS = {EM_HOST: {"Referer": "https://quote.eastmoney.com/"}}
ENCODINGS = {EM_HOST: "utf-8"}  # the others answer in GBK
LAST_MODIFIED = "#last-modified: "
CFFEX_COLUMNS = "instrument,openprice,highest,lowest,lastprice,updown,bprice,bamount,sprice,samount,volume,position"
BLOCK_STATUS = (403, 429, 456, 501)
HOST_PER_SECOND = {QUOTE_HOST: 1.0, CFFEX_HOST: 1.0, EM_HOST: 1 / 3}  # Eastmoney drops this server after quick requests

ETFS = ("sh510300", "sh510310", "sh510050", "sz159919", "sh510500", "sh512100", "sz159845")
INDICES = ("sh000001", "sz399001", "sz399006", "sh000300", "sh000016", "sh000905", "sh000852", "sh000688",
           "sh000985", "sz399303")
FUTURE_PRODUCTS = ("IF", "IH", "IC", "IM")
TENCENT_OVERSEAS = {"hf_HSI": "future", "hf_GC": "commodity", "hf_CL": "commodity"}
EM_SYMBOLS = {"em_CN00Y": ("104.CN00Y", "future"), "em_USDCNH": ("133.USDCNH", "fx")}  # symbol: secid, asset

GRID_WINDOW = (dtime(9, 0), dtime(16, 0, 59))
GRID_OFFSET = 5  # seconds into each minute
GRID_EVERY = 60
CLOSE_WINDOW = (dtime(15, 1, 30), dtime(15, 4))
END_WINDOW = (dtime(16, 0, 30), GRID_WINDOW[1])
TRADING_CHECK_AFTER = dtime(9, 31)
STALE_POLLS = 5  # polls with yesterday's stamps before the day counts as not a trading day
STOP_AT = dtime(16, 10)
POLL_BUDGET = 12.0  # seconds a poll may wait for its hosts
LOOP_PAUSE = 0.5  # seconds between looks at the schedule
MAX_FAILS_IN_ROW = 20
FLUSH_EVERY = 900
MIN_FREE_GB = 5.0
TRADING_SHARE = 0.5  # share of A-share quotes stamped today, below which the day is not a trading day
MAX_ERRORS = 50
BODY_KEEP = 2000  # bytes of a refusal page kept in the raw file

SCHEMA: dict[str, Any] = {
    "symbol": pl.Utf8, "name": pl.Utf8, "asset": pl.Utf8, "host": pl.Utf8, "kind": pl.Utf8,
    "poll_ts": pl.Datetime("ms"), "quote_ts": pl.Datetime("ms"),
    "price": pl.Float64, "prev_close": pl.Float64, "prev_kind": pl.Utf8, "open": pl.Float64, "high": pl.Float64,
    "low": pl.Float64, "volume": pl.Float64, "amount": pl.Float64, "bid1": pl.Float64, "ask1": pl.Float64,
    "open_interest": pl.Float64, "contract": pl.Utf8,
}
SORT_KEYS = ["symbol", "poll_ts", "kind"]

_TENCENT_LINE = re.compile(r'v_((?:sh|sz|bj)\d{6}|hf_\w+)="([^"]*)"')
_CONTRACT = re.compile(r"(?:IF|IH|IC|IM)\d{4}")
_RAW_HEADER = re.compile(rb"^# (\S+) (\d\d:\d\d:\d\d) (\d+)$", re.M)


class Blocked(ProviderBlocked):
    """A host refuses us; carries the status and the start of the page as evidence."""

    def __init__(self, message: str, status: int = 0, body: bytes = b"") -> None:
        super().__init__(message)
        self.status, self.body = status, body


class HostError(RuntimeError):
    """A host answered with an unusable status."""


class HostStopped(RuntimeError):
    """A queued request of a host that refused us earlier in the same poll: it is not sent."""


# --- instruments -----------------------------------------------------------------

def third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(4 - first.weekday()) % 7 + 14)


def _add_months(year: int, month: int, n: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + n
    return index // 12, index % 12 + 1


def contract_months(day: date) -> list[str]:
    """yymm of the listed CFFEX index-futures contracts: current month, next month, then the next two quarter
    months. Once the current month is past its third Friday (expiry) it is dropped from the list."""
    year, month = day.year, day.month
    if day > third_friday(year, month):
        year, month = _add_months(year, month, 1)
    months = [(year, month), _add_months(year, month, 1)]
    cursor = months[-1]
    while len(months) < 4:
        cursor = _add_months(*cursor, 1)
        if cursor[1] % 3 == 0:
            months.append(cursor)
    return [f"{y % 100:02d}{m:02d}" for y, m in months]


def tencent_symbols() -> list[str]:
    return [*ETFS, *INDICES, *TENCENT_OVERSEAS]


def future_symbols(day: date) -> list[str]:
    """The contracts expected to be listed on `day`."""
    return [f"{p}{m}" for p in FUTURE_PRODUCTS for m in contract_months(day)]


def host_of(symbol: str) -> str:
    if symbol in EM_SYMBOLS:
        return EM_HOST
    return CFFEX_HOST if _CONTRACT.fullmatch(symbol) else QUOTE_HOST


def asset_of(symbol: str) -> str:
    if symbol in ETFS:
        return "etf"
    if symbol in INDICES:
        return "index"
    if _CONTRACT.fullmatch(symbol):
        return "future"
    if symbol in EM_SYMBOLS:
        return EM_SYMBOLS[symbol][1]
    return TENCENT_OVERSEAS.get(symbol, "unknown")


def urls_for(host: str, symbols: list[str]) -> list[str]:
    """A batched request for Tencent, one file per product for the exchange, one series per symbol for Eastmoney."""
    if host == CFFEX_HOST:
        return [CFFEX_URL.format(p) for p in FUTURE_PRODUCTS if any(s.startswith(p) for s in symbols)]
    if host == EM_HOST:
        return [EM_URL + EM_SYMBOLS[s][0] for s in symbols]
    return [QUOTE_URL + ",".join(symbols)]


# --- parsing ---------------------------------------------------------------------

def _stamp(text: str) -> datetime | None:
    try:
        return datetime.strptime(text.strip(), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _stamp_parts(day_text: str, time_text: str) -> datetime | None:
    try:
        return datetime.strptime(f"{day_text.strip()} {time_text.strip()}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _row(symbol: str, name: str, host: str, kind: str, poll_ts: datetime, quote_ts: datetime | None,
         **fields: Any) -> dict[str, Any]:
    row: dict[str, Any] = {column: None for column in SCHEMA}
    row.update(symbol=symbol, name=name.strip(), asset=asset_of(symbol), host=host, kind=kind, poll_ts=poll_ts,
               quote_ts=quote_ts, **fields)
    return row


def _http_stamp(text: str) -> datetime | None:
    try:
        return parsedate_to_datetime(text.strip()).astimezone(CST).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _minute(text: str) -> datetime | None:
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def parse_quote(symbol: str, f: list[str], kind: str, poll_ts: datetime) -> dict[str, Any] | None:
    """ETF and index line (`~` separated): [3] last, [4] previous close, [5] open, [9] bid1, [19] ask1, [30] time,
    [33] high, [34] low, [35] last/lots/yuan, [36] volume in lots."""
    if len(f) < 38 or not f[1].strip():
        return None
    parts = f[35].split("/")
    return _row(symbol, f[1], QUOTE_HOST, kind, poll_ts, _stamp(f[30]),
                price=_positive(f[3]), prev_close=_positive(f[4]), prev_kind="close", open=_positive(f[5]),
                high=_positive(f[33]), low=_positive(f[34]), volume=_number(f[36]),
                amount=_number(parts[2]) if len(parts) == 3 else None,
                bid1=_positive(f[9]), ask1=_positive(f[19]))


def parse_hf(symbol: str, f: list[str], kind: str, poll_ts: datetime) -> dict[str, Any] | None:
    """Overseas future (comma separated): [0] last, [4] high, [5] low, [6] time, [7] previous settlement, [8] open,
    [12] date, [13] name. Bid / ask / volume are not trusted (see the module docstring)."""
    if len(f) < 14 or not f[13].strip():
        return None
    return _row(symbol, f[13], QUOTE_HOST, kind, poll_ts, _stamp_parts(f[12], f[6]),
                price=_positive(f[0]), prev_close=_positive(f[7]), prev_kind="settle", open=_positive(f[8]),
                high=_positive(f[4]), low=_positive(f[5]))


def parse_tencent(text: str, kind: str, poll_ts: datetime) -> list[dict[str, Any]]:
    """One row per known line; Tencent leaves out what it does not know."""
    rows: list[dict[str, Any]] = []
    for match in _TENCENT_LINE.finditer(text):
        symbol, body = match.group(1), match.group(2)
        row = (parse_hf(symbol, body.split(","), kind, poll_ts) if symbol.startswith("hf_")
               else parse_quote(symbol, body.split("~"), kind, poll_ts))
        if row is not None:
            rows.append(row)
    return rows


def parse_cffex(text: str, kind: str, poll_ts: datetime) -> list[dict[str, Any]]:
    """One product file. An unexpected header means the format changed: no rows, which counts as a failed request.
    Columns: [0] contract, [1] open, [2] high, [3] low, [4] last, [5] last minus previous settlement, [6] bid,
    [8] ask, [10] volume, [11] open interest."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    stamp = None
    if lines and lines[0].startswith(LAST_MODIFIED):
        stamp = _http_stamp(lines[0][len(LAST_MODIFIED):])
        lines = lines[1:]
    if not lines or lines[0] != CFFEX_COLUMNS:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines[1:]:
        f = line.split(",")
        if len(f) != 12 or not _CONTRACT.fullmatch(f[0]):
            continue
        price, change = _positive(f[4]), _number(f[5])
        rows.append(_row(f[0], f[0], CFFEX_HOST, kind, poll_ts, stamp, price=price,
                         prev_close=round(price - change, 6) if price is not None and change is not None else None,
                         prev_kind="settle", open=_positive(f[1]), high=_positive(f[2]), low=_positive(f[3]),
                         volume=_number(f[10]), bid1=_positive(f[6]), ask1=_positive(f[8]),
                         open_interest=_number(f[11]), contract=f[0]))
    return rows


def parse_minutes(text: str, kind: str, poll_ts: datetime) -> list[dict[str, Any]]:
    """Eastmoney's minute series of one symbol: `trends` lines of time, open, close, high, low, volume, amount, average.
    Every row is kind "minute" and stamped with its bar, so two fetches of a day merge into one series. Anything but
    the expected symbols, or a reply without data, gives no rows."""
    data = (json.loads(text) or {}).get("data") or {}
    symbol = f"em_{data.get('code')}"
    if symbol not in EM_SYMBOLS:
        return []
    is_future = EM_SYMBOLS[symbol][1] == "future"
    prev_close = _positive(data.get("preSettlement" if is_future else "preClose"))
    rows: list[dict[str, Any]] = []
    for line in data.get("trends") or []:
        f = str(line).split(",")
        stamp = _minute(f[0])
        if len(f) < 8 or stamp is None:
            continue
        rows.append(_row(symbol, str(data.get("name") or ""), EM_HOST, "minute", stamp, stamp, price=_positive(f[2]),
                         prev_close=prev_close, prev_kind="settle" if is_future else "close", open=_positive(f[1]),
                         high=_positive(f[3]), low=_positive(f[4]), volume=_number(f[5]) if is_future else None))
    return rows


PARSERS: dict[str, Callable[[str, str, datetime], list[dict[str, Any]]]] = {
    QUOTE_HOST: parse_tencent, CFFEX_HOST: parse_cffex, EM_HOST: parse_minutes}


# --- http ------------------------------------------------------------------------

def looks_blocked(status: int, body: bytes, location: str = "") -> bool:
    """A refusal: a rate-limit status, any redirect (these endpoints never redirect), the Tencent WAF or an HTML page
    where quote text should be. Any other error page (404, 502) is a plain failure: the next poll is the retry."""
    if status in BLOCK_STATUS or 300 <= status < 400 or "waf.tencent.com" in location or b"waf.tencent.com" in body[:600]:
        return True
    return 200 <= status < 300 and body.lstrip()[:1] == b"<"


Fetcher = Callable[[str, str], bytes]
_PACERS = {host: _Pacer(rate) for host, rate in HOST_PER_SECOND.items()}


class HttpFetcher:
    """One GET per call; raises Blocked on a refusal and HostError on any other bad status. The exchange's files
    carry no time, so their Last-Modified goes in front of the body, where the raw file keeps it too."""

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def __call__(self, host: str, url: str) -> bytes:
        response = self.client.get(url, headers=HEADERS.get(host))
        body = response.content
        if looks_blocked(response.status_code, body, response.headers.get("location", "")):
            raise Blocked(f"{host} refused: HTTP {response.status_code}", response.status_code, body[:BODY_KEEP])
        if response.status_code != 200:
            raise HostError(f"{host} HTTP {response.status_code}")
        stamp = response.headers.get("last-modified") if host == CFFEX_HOST else None
        return f"{LAST_MODIFIED}{stamp}\n".encode("ascii") + body if stamp else body


# --- schedule --------------------------------------------------------------------

def _now() -> datetime:
    """The wall clock in CST as a naive datetime: the only form the schedule and the row timestamps use."""
    return datetime.now(CST).replace(tzinfo=None)


def _tick() -> None:
    time.sleep(LOOP_PAUSE)


@dataclass
class Clock:
    """What the loop has already done today; `due` is pure so the schedule can be tested."""
    last_grid: datetime | None = None
    done: set[str] = field(default_factory=set)


def slot(now: datetime) -> datetime:
    """Start of the minute that holds `now`."""
    return now.replace(second=0, microsecond=0)


def due(now: datetime, clock: Clock) -> list[str]:
    """Steps to run now: the minute poll once per minute from GRID_OFFSET s in, the closing snapshot and the last
    fetch of the minute bars."""
    t = now.time()
    out: list[str] = []
    if GRID_WINDOW[0] <= t <= GRID_WINDOW[1] and now.second >= GRID_OFFSET and clock.last_grid != slot(now):
        out.append("grid")
    if "close" not in clock.done and CLOSE_WINDOW[0] <= t <= CLOSE_WINDOW[1]:
        out.append("close")
    if "end" not in clock.done and END_WINDOW[0] <= t <= END_WINDOW[1]:
        out.append("end")
    return out


def finished(now: datetime) -> bool:
    """The grid window is over: nothing more will become due."""
    return now.time() > GRID_WINDOW[1]


def grid_slots(day: date) -> list[datetime]:
    """Every minute poll of a day, 09:00 .. 16:00 inclusive."""
    first = datetime.combine(day, GRID_WINDOW[0]) + timedelta(seconds=GRID_OFFSET)
    count = int((datetime.combine(day, dtime(16, 0)) - datetime.combine(day, GRID_WINDOW[0])).total_seconds() // GRID_EVERY) + 1
    return [first + timedelta(seconds=GRID_EVERY * i) for i in range(count)]


def missed(previous: datetime | None, current: datetime) -> int:
    """Minute slots skipped before `current`; a first poll counts the minutes since the window opened."""
    base = previous or datetime.combine(current.date(), GRID_WINDOW[0]) - timedelta(minutes=1)
    gap = (current - base).total_seconds()
    return max(0, int(gap // GRID_EVERY) - 1)


# --- recorder --------------------------------------------------------------------

class Recorder:
    def __init__(self, day: date, root: Path, fetcher: Fetcher, stop_at: dtime | None = None,
                 symbols: list[str] | None = None) -> None:
        self.day, self.fetcher, self.stop_at = day, fetcher, stop_at
        self.symbols = symbols if symbols is not None else [*tencent_symbols(), *future_symbols(day), *EM_SYMBOLS]
        by_host = {host: [s for s in self.symbols if host_of(s) == host] for host in HOSTS}
        self.requests = [(host, url) for host, listed in by_host.items() if listed for url in urls_for(host, listed)]
        self.dir = root / day.isoformat()
        for sub in ("raw", "parts"):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        self.buffer: list[dict[str, Any]] = []
        self.latest_a_share: list[dict[str, Any]] = []
        self.seen: set[str] = set()
        self.blocked: dict[str, str] = {}
        self.fails: dict[str, int] = {}
        self.seq = 0
        self.stats: dict[str, Any] = {"polls": {}, "rows": {}, "rows_by_host": {}, "missed": 0, "truncated": {},
                                      "failed_requests": 0, "error_count": 0, "errors": [], "notes": []}
        self.last_flush = time.monotonic()
        self.disk_low = False
        self.finished = False
        # one worker per host: its requests run one after the other, and a stuck host cannot take the others' workers
        self.pools = {host: ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"ext-{host}") for host in HOSTS}

    def late(self) -> bool:
        return self.stop_at is not None and _now().time() >= self.stop_at

    def _free_gb(self) -> float:
        return shutil.disk_usage(self.dir).free / 1e9

    def note(self, text: str) -> None:
        if text not in self.stats["notes"]:
            self.stats["notes"].append(text)

    def error(self, text: str) -> None:
        self.stats["error_count"] += 1
        if len(self.stats["errors"]) < MAX_ERRORS:
            self.stats["errors"].append(f"{_now():%H:%M:%S} {text}"[:300])

    def _count(self, key: str, name: str, n: int = 1) -> None:
        self.stats[key][name] = self.stats[key].get(name, 0) + n

    def _block(self, host: str, exc: Exception | str) -> None:
        self.blocked[host] = f"{_now():%H:%M:%S} {exc}"
        LOG.error("intraday_ext: %s stopped for today: %s", host, exc)

    def _failed(self, host: str, exc: Exception | str) -> None:
        self.stats["failed_requests"] += 1
        self.fails[host] = self.fails.get(host, 0) + 1
        self.error(f"{host}: {exc}")
        LOG.warning("intraday_ext: %s request failed: %s", host, exc)
        limit = MAX_FAILS_IN_ROW * sum(h == host for h, _ in self.requests)
        if self.fails[host] >= limit:
            self._block(host, f"{limit} failures in a row, last: {exc}")

    def _raw(self, host: str, kind: str, poll_ts: datetime, status: int, body: bytes) -> None:
        header = f"# {kind} {poll_ts:%H:%M:%S} {status}\n".encode("ascii")
        try:
            with gzip.open(self.dir / "raw" / f"{host}.txt.gz", "ab") as fh:
                fh.write(header + body + b"\n")
        except OSError as exc:  # a locked file must not cost the poll its rows
            self.error(f"raw {host}: {exc!r}")

    def check_disk(self) -> bool:
        """False (and sticky) once free space is below the floor."""
        if not self.disk_low and self._free_gb() < MIN_FREE_GB:
            self.disk_low = True
            LOG.error("intraday_ext: free disk below %s GB, stopping", MIN_FREE_GB)
        return not self.disk_low

    def _fetch(self, host: str, url: str) -> bytes:
        """Paced; a refusal stops the host at once, so the rest of this poll's queue for it is not sent."""
        _PACERS[host].wait()
        if host in self.blocked:
            raise HostStopped(host)
        try:
            return self.fetcher(host, url)
        except Blocked as exc:
            self._block(host, exc)
            raise

    def _handle(self, host: str, future: Future[bytes], kind: str, poll_ts: datetime) -> list[dict[str, Any]]:
        try:
            body = future.result()
        except HostStopped:
            return []
        except Blocked as exc:
            self._raw(host, kind, poll_ts, exc.status, exc.body)
            return []
        except Exception as exc:  # one failure must not end the day
            self._failed(host, exc)
            return []
        self._raw(host, kind, poll_ts, 200, body)
        try:
            rows = PARSERS[host](body.decode(ENCODINGS.get(host, "gbk"), errors="replace"), kind, poll_ts)
        except Exception as exc:  # a malformed answer must not cost the other hosts their poll
            self._failed(host, f"parse: {exc!r}")
            return []
        if not rows:
            self._failed(host, "no quotes parsed")
            return []
        self.fails[host] = 0
        return rows

    def poll(self, kind: str, poll_ts: datetime | None = None) -> list[dict[str, Any]]:
        """This step's requests, hosts in parallel under POLL_BUDGET; returns the rows recorded. The minute bars of
        Eastmoney are not part of the grid; the "end" step fetches nothing else."""
        poll_ts = poll_ts or _now()
        if not self.check_disk() or self.late():
            return []
        planned = [(h, u) for h, u in self.requests if h not in self.blocked
                   and not (kind == "grid" and h == EM_HOST) and not (kind == "end" and h != EM_HOST)]
        futures = [(host, self.pools[host].submit(self._fetch, host, url)) for host, url in planned]
        wait([future for _, future in futures], timeout=POLL_BUDGET)
        rows: list[dict[str, Any]] = []
        for host, future in futures:
            if not future.done():
                future.cancel()
                self._count("truncated", host)
                self._failed(host, "over the poll budget")
                continue
            got = self._handle(host, future, kind, poll_ts)
            self._count("rows_by_host", host, len(got))
            rows += got
        self.latest_a_share = [r for r in rows if r["asset"] in ("etf", "index")]
        self.buffer.extend(rows)
        self.seen |= {r["symbol"] for r in rows if r["price"] is not None}
        self._count("polls", kind)
        self._count("rows", kind, len(rows))
        return rows

    def trading_today(self) -> bool | None:
        """Are this poll's A-share quotes stamped today? False on an unlisted holiday, when the feed repeats the last
        session; None when the poll holds no usable stamp. Overseas quotes do not count: they carry today's date on a
        Chinese holiday too."""
        stamps = [r["quote_ts"].date() for r in self.latest_a_share if r["quote_ts"] is not None]
        return sum(d == self.day for d in stamps) / len(stamps) >= TRADING_SHARE if stamps else None

    # files
    def flush(self, force: bool = False) -> None:
        if not force and time.monotonic() - self.last_flush < FLUSH_EVERY:
            return
        if self.buffer:
            self.seq += 1
            part = self.dir / "parts" / f"part-{_now():%H%M%S}-{self.seq:05d}.parquet"
            temporary = part.with_name(f"{part.name}.tmp")
            pl.DataFrame(self.buffer, schema=SCHEMA).write_parquet(temporary, compression="zstd")
            os.replace(temporary, part)
            self.buffer = []
        self.last_flush = time.monotonic()
        self.check_disk()

    def summary(self, reason: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        size = sum(p.stat().st_size for p in self.dir.rglob("*") if p.is_file())
        never = sorted(set(self.symbols) - self.seen)
        return {"day": self.day.isoformat(), "symbols": len(self.symbols), "never_returned": never,
                "blocked": self.blocked, "end": reason, "bytes": size, **(extra or {}), **self.stats}

    def finish(self, reason: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.finished:
            return {}
        self.finished = True
        for pool in self.pools.values():
            pool.shutdown(wait=False, cancel_futures=True)
        for step_name, fn in (("flush", lambda: self.flush(force=True)), ("compact", lambda: compact(self.dir))):
            try:
                fn()
            except Exception as exc:  # the summary must still be written
                self.error(f"finish {step_name}: {exc!r}")
        summary = self.summary(reason, extra)
        name = "summary_once.json" if reason == "once" else "summary.json"  # a smoke test leaves the day's record alone
        _write_atomic(self.dir / name, json.dumps(summary, ensure_ascii=False, indent=1, default=str).encode("utf-8"))
        LOG.info("intraday_ext: %s", json.dumps(summary, ensure_ascii=False, default=str))
        return summary


def _write_atomic(path: Path, data: bytes) -> None:
    """A process killed mid-write must not leave a half file that stops every later restart."""
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def compact(day_dir: Path) -> int:
    """Merge the part files (and an earlier merged file, after a restart) into `quotes_ext.parquet`, sorted by
    symbol and poll time. The parts are removed only after the merged file is in place. Returns the row count."""
    target = day_dir / "quotes_ext.parquet"
    parts = sorted((day_dir / "parts").glob("part-*.parquet"))
    if not parts:
        return pl.read_parquet(target).height if target.exists() else 0
    sources = ([target] if target.exists() else []) + parts
    merged = (pl.concat([pl.read_parquet(p) for p in sources], how="diagonal_relaxed")
              .unique(subset=SORT_KEYS, keep="last").sort(SORT_KEYS))
    temporary = day_dir / ".quotes_ext.parquet.tmp"
    merged.write_parquet(temporary, compression="zstd", compression_level=9)
    os.replace(temporary, target)
    for p in parts:
        p.unlink(missing_ok=True)
    return merged.height


def raw_polls(path: Path, encoding: str = "gbk") -> list[tuple[str, str, int, str]]:
    """(kind, HH:MM:SS, status, text) of every poll stored in a raw file."""
    with gzip.open(path, "rb") as fh:
        data = fh.read()
    heads = list(_RAW_HEADER.finditer(data))
    out = []
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(data)
        out.append((h.group(1).decode(), h.group(2).decode(), int(h.group(3)),
                    data[h.end():end].decode(encoding, errors="replace")))
    return out


def output_root() -> Path:
    override = os.getenv("PULSE_INTRADAY_EXT_DIR")
    return Path(override) if override else settings.data_dir / "intraday_ext"


def _log_to(day_dir: Path) -> None:
    handler = logging.FileHandler(day_dir / "run.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(handler)


def _advance(clock: Clock, rec: Recorder, action: str, now: datetime) -> None:
    """Mark a step as taken before running it, so a failing step is not retried in a tight loop."""
    if action == "grid":
        previous, clock.last_grid = clock.last_grid, slot(now)
        rec.stats["missed"] += missed(previous, clock.last_grid)
    else:
        clock.done.add(action)
        _write_atomic(rec.dir / "done.json", json.dumps(sorted(clock.done)).encode("utf-8"))


def _end_reason(rec: Recorder, now: datetime) -> str | None:
    if rec.disk_low:
        return "disk low"
    if now.time() >= STOP_AT:
        return "stop time"
    return "done" if finished(now) else None


def _loop(rec: Recorder, clock: Clock) -> str:
    verified, stale = False, 0
    while True:
        now = _now()
        reason = _end_reason(rec, now)
        if reason:
            return reason
        for action in due(now, clock):
            try:
                _advance(clock, rec, action, now)
            except Exception as exc:  # bookkeeping must not cost the poll: 2026-11-09 lost a whole day this way
                LOG.exception("intraday_ext: bookkeeping for %s failed", action)
                rec.error(f"advance {action}: {exc!r}")
            try:
                rec.poll(action)
            except Exception as exc:  # one bad answer must not end the day
                LOG.exception("intraday_ext: poll %s failed", action)
                rec.error(f"{action}: {exc!r}")
            if action == "grid" and not verified and now.time() >= TRADING_CHECK_AFTER:
                verdict = rec.trading_today()
                verified, stale = verdict is True, stale + (verdict is False)
                if stale >= STALE_POLLS:  # one stale answer proves nothing: a cache may repeat yesterday
                    LOG.info("intraday_ext: %s not a trading day (quotes not stamped today)", rec.day)
                    return "quotes not stamped today: not a trading day"
        try:
            rec.flush()
        except Exception as exc:
            LOG.exception("intraday_ext: flush failed")
            rec.error(f"flush: {exc!r}")
        _tick()


def run() -> int:
    today = _now().date()
    if not load_calendar().is_trading_day(today):
        LOG.info("intraday_ext: %s is not a trading day", today)
        return 0
    day_dir = output_root() / today.isoformat()
    summary = day_dir / "summary.json"
    if summary.exists() and json.loads(summary.read_text(encoding="utf-8")).get("end") == "done":
        LOG.info("intraday_ext: %s already recorded", today)
        return 0
    clock = Clock()
    done_file = day_dir / "done.json"
    restarted = done_file.exists()
    if restarted:
        clock.done = set(json.loads(done_file.read_text(encoding="utf-8")))
    reason = "crash"
    with new_client() as client:
        rec = Recorder(today, output_root(), HttpFetcher(client), stop_at=STOP_AT)
        _log_to(rec.dir)
        LOG.info("intraday_ext: %s, %d symbols, dated contracts %s", today, len(rec.symbols), contract_months(today))
        try:
            reason = _loop(rec, clock)
            if reason == "done" and not rec.stats["polls"]:
                reason = "nothing recorded: started after the window"
        finally:
            rec.finish(reason, {"restarted": restarted})
    return 0


def once() -> int:
    """Smoke test: one poll now, saved under the output root and printed."""
    today = _now().date()
    with new_client() as client:
        rec = Recorder(today, output_root(), HttpFetcher(client))
        _log_to(rec.dir)
        try:
            rows = rec.poll("once")
        finally:
            summary = rec.finish("once")
    latest_bar = {r["symbol"]: r for r in rows if r["kind"] == "minute"}
    for r in [*(r for r in rows if r["kind"] != "minute"), *latest_bar.values()]:
        print(f"{r['symbol']:<12}{r['asset']:<10}{str(r['contract'] or ''):<8}{r['price']}\t{r['prev_close']} ({r['prev_kind']})\t"
              f"{r['quote_ts']}\t{r['name']}")
    print(json.dumps({k: summary[k] for k in ("symbols", "never_returned", "blocked", "polls", "rows", "rows_by_host",
                                              "error_count")}, ensure_ascii=False))
    return 0


def cli(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request would bury the log
    parser = argparse.ArgumentParser(prog="intraday_ext")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("once")
    again = sub.add_parser("compact")
    again.add_argument("day_dir", type=Path)
    args = parser.parse_args(argv)
    if args.cmd == "compact":
        print(compact(args.day_dir))
        return 0
    return run() if args.cmd == "run" else once()


if __name__ == "__main__":
    sys.exit(cli())
