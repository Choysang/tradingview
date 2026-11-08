"""Intraday recorder for what the main-board recorder (intraday.py) does not cover: broad ETFs, indices,
CFFEX index futures, the FTSE China A50, Hang Seng futures, USD/CNH, COMEX gold and WTI crude.

Research needs these for state signals ("national team buying broad ETFs", the index's first 30 minutes against
its last 30, overnight shocks). This is a separate process: it never imports or touches intraday.py, news.py
or the warehouse, and writes only under its own directory.

One process per day, started around 09:00 Asia/Shanghai:
* one poll every 60 s, 5 s into the minute, from 09:00 to 16:00 (the A-share quotes only move 09:15~15:00, but
  index futures trade 09:30~15:00 and the A50, Hang Seng futures, CNH and gold run longer);
* one extra "close" snapshot at 15:01:30;
* the process ends after the 16:00 poll and in any case by 16:10.
A weekend or a holiday of the trading calendar ends the run at once; a day whose index quotes carry no
today's timestamp after 09:31 (an unlisted holiday) ends it as "not a trading day".

Instruments (probed 2026-11-08 against the live hosts; 42 symbols, 2 requests per poll):
* qt.gtimg.cn/q=<comma list>: ETFs sh510300 sh510310 sh510050 sz159919 sh510500 sh512100 sz159845 and indices
  sh000001 sz399001 sz399006 sh000300 sh000016 sh000905 sh000852 sh000688 sh000985 sz399303. Same `~` format
  as stocks; volume in lots, amount in yuan (field 35), index bid/ask are 0.00 and become null.
* hq.sinajs.cn/list=<comma list> (needs `Referer: https://finance.sina.com.cn`):
  - nf_IF0 nf_IH0 nf_IC0 nf_IM0: Sina's continuous series. It is the MAIN contract (highest open interest),
    not the front month: on 2026-11-06 nf_IF0 equalled nf_IF2612 field for field while IF2611 was the front
    month. The name carries no month, so the recorder also polls the listed dated contracts (nf_IF2611, ...:
    current month, next month and the next two quarter months, see `contract_months`) and fills `contract` of a
    continuous row with the dated contract whose price, open, high, low, volume and amount match exactly
    (null when none or several match, e.g. before the open). Dated rows carry their own `contract`.
    Futures prev_close is the previous SETTLEMENT (field 14: the exchange's +-10% limits equal 1.1 / 0.9 times
    it, checked on all four products); field 13 is the previous close. Volume is in contracts; Sina's amount
    field is the sum of price x volume without the contract multiplier (field 5 / field 4 equals the average
    price in field 48), so amount in yuan = field 5 x 300 (IF, IH) or x 200 (IC, IM).
  - hf_CHA50CFD (FTSE China A50 futures), hf_HSI (Hang Seng index futures), hf_GC (COMEX gold), hf_CL (WTI
    crude): last, high, low, previous settlement, open, date and time (all Beijing time). Their bid / ask
    fields did not agree with the last price in the fixtures and the volume field looks unreliable, so bid1,
    ask1, volume and amount stay null; the raw text keeps them. These are continuous quotes: contract is null.
  - fx_susdcnh: USD/CNH offshore yuan. prev_close is derived as last minus the published change (prev_kind
    "derived"), bid / ask are the first two fields.
Tried and dropped: nothing. Sina also lists expired dated contracts with stale quotes (nf_IF2609 answered with
2026-09-18 data): `contract_months` never asks for a month past its third Friday, quote_ts shows any stale row.

Output, under `$PULSE_INTRADAY_EXT_DIR/<YYYY-MM-DD>/` (default `<data_dir>/intraday_ext`):
* `raw/<host>.txt.gz` - every response as received (GBK bytes, one gzip member per poll, a `# kind HH:MM:SS status`
  header line before each), including the body of a refusal;
* `parts/part-*.parquet` every 15 minutes, merged at the end into `quotes_ext.parquet` (sorted by symbol, poll_ts),
  so a crash keeps most of the day; `python -m backend.app.warehouse.intraday_ext compact <day dir>` merges
  leftovers;
* `done.json`, `run.log`, `summary.json` (polls, rows, missed grid slots, errors, blocked hosts, the symbols that
  never returned a price).
Rows: symbol (as requested), name, asset (etf / index / future / fx / commodity), host, kind (grid / close / once),
poll_ts and quote_ts (CST, naive), price, prev_close and prev_kind (close / settle / derived), open, high, low,
volume, amount (yuan), bid1, ask1, contract. Unknown fields are null, never 0 (a zero price is null).

Politeness: one batched request per host per poll, at most one request a second per host, no retries (the next
poll is the retry). HTTP 403 / 429 / 456 / 501, any redirect, a waf.tencent.com mention or an HTML page stops that
host for the rest of the day; so do 20 failures in a row. Each poll runs the hosts in parallel with a time budget,
so a slow host cannot delay the grid. Recording stops below 5 GB free disk.
Usage: python -m backend.app.warehouse.intraday_ext run | once | compact <day dir>
"""
from __future__ import annotations

import argparse
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
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
from .tencent import ProviderBlocked, _number, _Pacer, _positive, new_client

LOG = logging.getLogger("pulse.warehouse.intraday_ext")

CST = timezone(timedelta(hours=8))
QUOTE_HOST, SINA_HOST = "qt.gtimg.cn", "hq.sinajs.cn"
HOSTS = (QUOTE_HOST, SINA_HOST)
QUOTE_URL = f"https://{QUOTE_HOST}/q="
SINA_URL = f"https://{SINA_HOST}/list="
HEADERS = {SINA_HOST: {"Referer": "https://finance.sina.com.cn"}}
BLOCK_STATUS = (403, 429, 456, 501)
HOST_PER_SECOND = 1.0

ETFS = ("sh510300", "sh510310", "sh510050", "sz159919", "sh510500", "sh512100", "sz159845")
INDICES = ("sh000001", "sz399001", "sz399006", "sh000300", "sh000016", "sh000905", "sh000852", "sh000688",
           "sh000985", "sz399303")
FUTURE_MULTIPLIER = {"IF": 300, "IH": 300, "IC": 200, "IM": 200}  # yuan per index point
OVERSEAS = {"hf_CHA50CFD": "future", "hf_HSI": "future", "hf_GC": "commodity", "hf_CL": "commodity",
            "fx_susdcnh": "fx"}

GRID_WINDOW = (dtime(9, 0), dtime(16, 0, 59))
GRID_OFFSET = 5  # seconds into each minute
GRID_EVERY = 60
CLOSE_WINDOW = (dtime(15, 1, 30), dtime(15, 4))
TRADING_CHECK_AFTER = dtime(9, 31)
STOP_AT = dtime(16, 10)
POLL_BUDGET = 12.0  # seconds a poll may wait for its hosts
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
    "contract": pl.Utf8,
}
SORT_KEYS = ["symbol", "poll_ts", "kind"]
MATCH_KEYS = ("price", "open", "high", "low", "volume", "amount")

_TENCENT_LINE = re.compile(r'v_((?:sh|sz|bj)\d{6})="([^"]*)"')
_SINA_LINE = re.compile(r'var hq_str_(\w+)="([^"]*)"')
_RAW_HEADER = re.compile(rb"^# (\S+) (\d\d:\d\d:\d\d) (\d+)$", re.M)


class Blocked(ProviderBlocked):
    """A host refuses us; carries the status and the start of the page as evidence."""

    def __init__(self, message: str, status: int = 0, body: bytes = b"") -> None:
        super().__init__(message)
        self.status, self.body = status, body


class HostError(RuntimeError):
    """A host answered with an unusable status."""


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
    return [*ETFS, *INDICES]


def sina_symbols(day: date) -> list[str]:
    """Per product the continuous series first, then the dated contracts, then the overseas quotes."""
    months = contract_months(day)
    futures = [f"nf_{p}{tail}" for p in FUTURE_MULTIPLIER for tail in ("0", *months)]
    return [*futures, *OVERSEAS]


def host_of(symbol: str) -> str:
    return SINA_HOST if symbol.startswith(("nf_", "hf_", "fx_")) else QUOTE_HOST


def asset_of(symbol: str) -> str:
    if symbol in ETFS:
        return "etf"
    if symbol in INDICES:
        return "index"
    if symbol.startswith("nf_"):
        return "future"
    return OVERSEAS.get(symbol, "unknown")


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


def parse_tencent(text: str, kind: str, poll_ts: datetime) -> list[dict[str, Any]]:
    """ETF and index lines (`~` separated): [3] last, [4] previous close, [5] open, [9] bid1, [19] ask1, [30] time,
    [33] high, [34] low, [35] last/lots/yuan, [36] volume in lots."""
    rows: list[dict[str, Any]] = []
    for match in _TENCENT_LINE.finditer(text):
        f = match.group(2).split("~")
        if len(f) < 38 or not f[1].strip():
            continue
        parts = f[35].split("/")
        rows.append(_row(match.group(1), f[1], QUOTE_HOST, kind, poll_ts, _stamp(f[30]),
                         price=_positive(f[3]), prev_close=_positive(f[4]), prev_kind="close", open=_positive(f[5]),
                         high=_positive(f[33]), low=_positive(f[34]), volume=_number(f[36]),
                         amount=_number(parts[2]) if len(parts) == 3 else None,
                         bid1=_positive(f[9]), ask1=_positive(f[19])))
    return rows


def parse_nf(symbol: str, f: list[str], kind: str, poll_ts: datetime) -> dict[str, Any] | None:
    """CFFEX index future: [0] open, [1] high, [2] low, [3] last, [4] volume, [5] sum(price x volume),
    [14] previous settlement, [16] bid1, [26] ask1, [36] date, [37] time, last field the name."""
    if len(f) < 38:
        return None
    tail = symbol[5:]
    multiplier = FUTURE_MULTIPLIER.get(symbol[3:5])
    points = _number(f[5])
    return _row(symbol, f[-1], SINA_HOST, kind, poll_ts, _stamp_parts(f[36], f[37]),
                price=_positive(f[3]), prev_close=_positive(f[14]), prev_kind="settle", open=_positive(f[0]),
                high=_positive(f[1]), low=_positive(f[2]), volume=_number(f[4]),
                amount=points * multiplier if points is not None and multiplier else None,
                bid1=_positive(f[16]), ask1=_positive(f[26]),
                contract=symbol[3:] if tail.isdigit() and len(tail) == 4 else None)


def parse_hf(symbol: str, f: list[str], kind: str, poll_ts: datetime) -> dict[str, Any] | None:
    """Overseas future: [0] last, [4] high, [5] low, [6] time, [7] previous settlement, [8] open, [12] date,
    [13] name. Bid / ask / volume are not trusted (see the module docstring)."""
    if len(f) < 14:
        return None
    return _row(symbol, f[13], SINA_HOST, kind, poll_ts, _stamp_parts(f[12], f[6]),
                price=_positive(f[0]), prev_close=_positive(f[7]), prev_kind="settle", open=_positive(f[8]),
                high=_positive(f[4]), low=_positive(f[5]))


def parse_fx(symbol: str, f: list[str], kind: str, poll_ts: datetime) -> dict[str, Any] | None:
    """USD/CNH: [0] time, [1] bid, [2] ask, [5] open, [6] high, [7] low, [8] last, [9] name, [11] change,
    [17] date. The previous close is not published; it is last minus the change."""
    if len(f) < 18:
        return None
    price = _positive(f[8]) or _positive(f[1])
    change = _number(f[11])
    prev = round(price - change, 6) if price is not None and change is not None else None
    return _row(symbol, f[9], SINA_HOST, kind, poll_ts, _stamp_parts(f[17], f[0]),
                price=price, prev_close=prev if prev and prev > 0 else None, prev_kind="derived",
                open=_positive(f[5]), high=_positive(f[6]), low=_positive(f[7]),
                bid1=_positive(f[1]), ask1=_positive(f[2]))


def parse_sina(text: str, kind: str, poll_ts: datetime) -> list[dict[str, Any]]:
    """One row per non-empty quote; a symbol Sina does not know comes back as an empty string and is skipped."""
    parsers = {"nf_": parse_nf, "hf_": parse_hf, "fx_": parse_fx}
    rows: list[dict[str, Any]] = []
    for match in _SINA_LINE.finditer(text):
        symbol, body = match.group(1), match.group(2)
        parser = parsers.get(symbol[:3])
        row = parser(symbol, body.split(","), kind, poll_ts) if parser and body else None
        if row is not None and row["name"]:
            rows.append(row)
    return rows


def with_main_contracts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Name the dated contract behind each continuous row (nf_IF0): the one dated row of the same product whose
    price, open, high, low, volume and amount are identical. No match or several leaves `contract` null."""
    dated = [r for r in rows if r["asset"] == "future" and r["contract"]]
    out: list[dict[str, Any]] = []
    for row in rows:
        if row["symbol"].startswith("nf_") and row["symbol"][5:] == "0" and row["price"] is not None:
            product = row["symbol"][3:5]
            hits = [d["contract"] for d in dated if d["contract"][:2] == product
                    and all(d[k] == row[k] for k in MATCH_KEYS)]
            row = {**row, "contract": hits[0] if len(hits) == 1 else None}
        out.append(row)
    return out


PARSERS: dict[str, Callable[[str, str, datetime], list[dict[str, Any]]]] = {
    QUOTE_HOST: parse_tencent, SINA_HOST: parse_sina}


# --- http ------------------------------------------------------------------------

def looks_blocked(status: int, body: bytes, location: str = "") -> bool:
    """A refusal: a rate-limit status, any redirect (these endpoints never redirect), the Tencent WAF or an
    HTML page where quote text should be."""
    return (status in BLOCK_STATUS or 300 <= status < 400 or "waf.tencent.com" in location
            or b"waf.tencent.com" in body[:600] or body.lstrip()[:1] == b"<")


Fetcher = Callable[[str, str], bytes]
_PACERS = {host: _Pacer(HOST_PER_SECOND) for host in HOSTS}


class HttpFetcher:
    """One paced GET per call; raises Blocked on a refusal and HostError on any other bad status."""

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def __call__(self, host: str, url: str) -> bytes:
        _PACERS[host].wait()
        response = self.client.get(url, headers=HEADERS.get(host))
        body = response.content
        if looks_blocked(response.status_code, body, response.headers.get("location", "")):
            raise Blocked(f"{host} refused: HTTP {response.status_code}", response.status_code, body[:BODY_KEEP])
        if response.status_code != 200:
            raise HostError(f"{host} HTTP {response.status_code}")
        return body


# --- schedule --------------------------------------------------------------------

@dataclass
class Clock:
    """What the loop has already done today; `due` is pure so the schedule can be tested."""
    last_grid: datetime | None = None
    done: set[str] = field(default_factory=set)


def slot(now: datetime) -> datetime:
    """Start of the minute that holds `now`."""
    return now.replace(second=0, microsecond=0)


def due(now: datetime, clock: Clock) -> list[str]:
    """Steps to run now: the minute poll once per minute from GRID_OFFSET s in, and the closing snapshot."""
    t = now.time()
    out: list[str] = []
    if GRID_WINDOW[0] <= t <= GRID_WINDOW[1] and now.second >= GRID_OFFSET and clock.last_grid != slot(now):
        out.append("grid")
    if "close" not in clock.done and CLOSE_WINDOW[0] <= t <= CLOSE_WINDOW[1]:
        out.append("close")
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
        self.symbols = symbols if symbols is not None else [*tencent_symbols(), *sina_symbols(day)]
        self.by_host = {host: [s for s in self.symbols if host_of(s) == host] for host in HOSTS}
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
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ext")

    def late(self) -> bool:
        return self.stop_at is not None and datetime.now(CST).time() >= self.stop_at

    def _free_gb(self) -> float:
        return shutil.disk_usage(self.dir).free / 1e9

    def note(self, text: str) -> None:
        if text not in self.stats["notes"]:
            self.stats["notes"].append(text)

    def error(self, text: str) -> None:
        self.stats["error_count"] += 1
        if len(self.stats["errors"]) < MAX_ERRORS:
            self.stats["errors"].append(f"{datetime.now(CST):%H:%M:%S} {text}"[:300])

    def _count(self, key: str, name: str, n: int = 1) -> None:
        self.stats[key][name] = self.stats[key].get(name, 0) + n

    def _block(self, host: str, exc: Exception | str) -> None:
        self.blocked[host] = f"{datetime.now(CST):%H:%M:%S} {exc}"
        LOG.error("intraday_ext: %s stopped for today: %s", host, exc)

    def _failed(self, host: str, exc: Exception | str) -> None:
        self.stats["failed_requests"] += 1
        self.fails[host] = self.fails.get(host, 0) + 1
        self.error(f"{host}: {exc}")
        LOG.warning("intraday_ext: %s request failed: %s", host, exc)
        if self.fails[host] >= MAX_FAILS_IN_ROW:
            self._block(host, f"{MAX_FAILS_IN_ROW} failures in a row, last: {exc}")

    def _raw(self, host: str, kind: str, poll_ts: datetime, status: int, body: bytes) -> None:
        header = f"# {kind} {poll_ts:%H:%M:%S} {status}\n".encode("ascii")
        with gzip.open(self.dir / "raw" / f"{host}.txt.gz", "ab") as fh:
            fh.write(header + body + b"\n")

    def check_disk(self) -> bool:
        """False (and sticky) once free space is below the floor."""
        if not self.disk_low and self._free_gb() < MIN_FREE_GB:
            self.disk_low = True
            LOG.error("intraday_ext: free disk below %s GB, stopping", MIN_FREE_GB)
        return not self.disk_low

    def _handle(self, host: str, future: Future[bytes], kind: str, poll_ts: datetime) -> list[dict[str, Any]]:
        try:
            body = future.result()
        except Blocked as exc:
            self._raw(host, kind, poll_ts, exc.status, exc.body)
            self._block(host, exc)
            return []
        except Exception as exc:  # one failure must not end the day
            self._failed(host, exc)
            return []
        self._raw(host, kind, poll_ts, 200, body)
        rows = PARSERS[host](body.decode("gbk", errors="replace"), kind, poll_ts)
        if not rows:
            self._failed(host, "no quotes parsed")
            return []
        self.fails[host] = 0
        return rows

    def _urls(self) -> dict[str, str]:
        prefix = {QUOTE_HOST: QUOTE_URL, SINA_HOST: SINA_URL}
        return {h: prefix[h] + ",".join(s) for h, s in self.by_host.items() if s and h not in self.blocked}

    def poll(self, kind: str, poll_ts: datetime | None = None) -> list[dict[str, Any]]:
        """One batched request per host, run in parallel under POLL_BUDGET; returns the rows recorded."""
        poll_ts = poll_ts or datetime.now(CST).replace(tzinfo=None)
        if not self.check_disk() or self.late():
            return []
        futures = {host: self.pool.submit(self.fetcher, host, url) for host, url in self._urls().items()}
        wait(list(futures.values()), timeout=POLL_BUDGET)
        rows: list[dict[str, Any]] = []
        for host, future in futures.items():
            if not future.done():
                future.cancel()
                self._count("truncated", host)
                continue
            got = self._handle(host, future, kind, poll_ts)
            self._count("rows_by_host", host, len(got))
            if host == QUOTE_HOST and got:
                self.latest_a_share = got
            rows += got
        rows = with_main_contracts(rows)
        self.buffer.extend(rows)
        self.seen |= {r["symbol"] for r in rows if r["price"] is not None}
        self._count("polls", kind)
        self._count("rows", kind, len(rows))
        return rows

    def trading_today(self) -> bool:
        """A-share quotes stamped today: false on an unlisted holiday, when the feed repeats the last session."""
        stamps = [r["quote_ts"].date() for r in self.latest_a_share if r["quote_ts"] is not None]
        return bool(stamps) and sum(d == self.day for d in stamps) / len(stamps) >= TRADING_SHARE

    # files
    def flush(self, force: bool = False) -> None:
        if not force and time.monotonic() - self.last_flush < FLUSH_EVERY:
            return
        if self.buffer:
            self.seq += 1
            part = self.dir / "parts" / f"part-{datetime.now(CST):%H%M%S}-{self.seq:05d}.parquet"
            pl.DataFrame(self.buffer, schema=SCHEMA).write_parquet(part, compression="zstd")
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
        self.pool.shutdown(wait=False, cancel_futures=True)
        for step_name, fn in (("flush", lambda: self.flush(force=True)), ("compact", lambda: compact(self.dir))):
            try:
                fn()
            except Exception as exc:  # the summary must still be written
                self.error(f"finish {step_name}: {exc!r}")
        summary = self.summary(reason, extra)
        (self.dir / "summary.json").write_bytes(
            json.dumps(summary, ensure_ascii=False, indent=1, default=str).encode("utf-8"))
        LOG.info("intraday_ext: %s", json.dumps(summary, ensure_ascii=False, default=str))
        return summary


def compact(day_dir: Path) -> int:
    """Merge the part files (and an earlier merged file, after a restart) into `quotes_ext.parquet`, sorted by
    symbol and poll time. The parts are removed only after the merged file is in place. Returns the row count."""
    target = day_dir / "quotes_ext.parquet"
    parts = sorted((day_dir / "parts").glob("part-*.parquet"))
    if not parts:
        return pl.read_parquet(target).height if target.exists() else 0
    sources = ([target] if target.exists() else []) + parts
    merged = (pl.concat([pl.read_parquet(p) for p in sources], how="vertical_relaxed")
              .unique(subset=SORT_KEYS, keep="last").sort(SORT_KEYS))
    temporary = day_dir / ".quotes_ext.parquet.tmp"
    merged.write_parquet(temporary, compression="zstd", compression_level=9)
    os.replace(temporary, target)
    for p in parts:
        p.unlink(missing_ok=True)
    return merged.height


def raw_polls(path: Path) -> list[tuple[str, str, int, str]]:
    """(kind, HH:MM:SS, status, text) of every poll stored in a raw file."""
    with gzip.open(path, "rb") as fh:
        data = fh.read()
    heads = list(_RAW_HEADER.finditer(data))
    out = []
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(data)
        out.append((h.group(1).decode(), h.group(2).decode(), int(h.group(3)),
                    data[h.end():end].decode("gbk", errors="replace")))
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
        current = slot(now)
        rec.stats["missed"] += missed(clock.last_grid, current)
        clock.last_grid = current
    else:
        clock.done.add(action)
        (rec.dir / "done.json").write_bytes(json.dumps(sorted(clock.done)).encode("utf-8"))


def _end_reason(rec: Recorder, now: datetime) -> str | None:
    if rec.disk_low:
        return "disk low"
    if now.time() >= STOP_AT:
        return "stop time"
    return "done" if finished(now) else None


def _loop(rec: Recorder, clock: Clock) -> str:
    checked = False
    while True:
        now = datetime.now(CST)
        reason = _end_reason(rec, now)
        if reason:
            return reason
        for action in due(now, clock):
            _advance(clock, rec, action, now)
            try:
                rows = rec.poll(action)
            except Exception as exc:  # one bad answer must not end the day
                LOG.exception("intraday_ext: poll %s failed", action)
                rec.error(f"{action}: {exc!r}")
                rows = []
            if action == "grid" and rows and not checked and now.time() >= TRADING_CHECK_AFTER:
                checked = True
                if not rec.trading_today():
                    LOG.info("intraday_ext: %s not a trading day (quotes not stamped today)", rec.day)
                    return "quotes not stamped today: not a trading day"
        try:
            rec.flush()
        except Exception as exc:
            LOG.exception("intraday_ext: flush failed")
            rec.error(f"flush: {exc!r}")
        time.sleep(0.5)


def run() -> int:
    today = datetime.now(CST).date()
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
    if done_file.exists():
        clock.done = set(json.loads(done_file.read_text(encoding="utf-8")))
    reason = "stop time"
    with new_client() as client:
        rec = Recorder(today, output_root(), HttpFetcher(client), stop_at=STOP_AT)
        _log_to(rec.dir)
        LOG.info("intraday_ext: %s, %d symbols, dated contracts %s", today, len(rec.symbols), contract_months(today))
        try:
            reason = _loop(rec, clock)
        finally:
            rec.finish(reason, {"restarted": bool(clock.done)})
    return 0


def once() -> int:
    """Smoke test: one poll now, saved under the output root and printed."""
    today = datetime.now(CST).date()
    with new_client() as client:
        rec = Recorder(today, output_root(), HttpFetcher(client))
        _log_to(rec.dir)
        try:
            rows = rec.poll("once")
        finally:
            summary = rec.finish("once")
    for r in rows:
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
