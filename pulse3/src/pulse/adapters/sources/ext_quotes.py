"""Native extended-intraday quotes: instruments, parsers, request plan and the guarded fetcher (step 2, LEGACY_TRIAGE #104/#186).

Why: until the M6 cutover the old recorder (``backend/app/warehouse/intraday_ext.py``) records what the main-board recorder does not:
broad ETFs, indices, the CFFEX index futures, the FTSE China A50, Hang Seng futures, offshore CNH, COMEX gold and WTI crude. Research
state signals (E089) need them. pulse3 imports the old files today (``legacy_recordings``, endpoint ``intraday_ext``); this module is the
native replacement, ported by rewriting, producing the SAME rows (symbol, asset, kind, prev_kind, ... see :data:`EXT_FRAME_SCHEMA`).

Instruments (the old probe of 2026-11-09, 38 symbols):
* ``qt.gtimg.cn/q=<comma list>`` one request: ETFs sh510300 sh510310 sh510050 sz159919 sh510500 sh512100 sz159845, indices sh000001 sz399001
  sz399006 sh000300 sh000016 sh000905 sh000852 sh000688 sh000985 sz399303 (``~`` format: volume in lots, amount in yuan from field 35, index
  bid/ask 0.00 -> NULL) and ``hf_HSI`` / ``hf_GC`` / ``hf_CL`` (comma format: last, high, low, previous settlement, open, date, time, name).
  The hf_HSI bid/ask did not agree with the last price and the hf volume is unverified, so bid1, ask1, volume and amount stay NULL for hf rows.
* ``www.cffex.com.cn/quote_<IF|IH|IC|IM>.txt`` one request per product (plain http: https timed out from the server): one line per listed
  contract with open, high, low, last, change, bid, ask, volume (contracts), open interest (contracts). ``prev_close`` = last - change is the
  previous SETTLEMENT (``prev_kind`` "settle", checked against Eastmoney's settlement field); the file has no amount (NULL) and no time, so
  ``quote_ts`` is the response's Last-Modified (kept as a ``#last-modified:`` first line of the stored raw body; read ``quote_ts``, not
  ``poll_ts``, for the age of a futures quote).
* ``push2his.eastmoney.com/api/qt/stock/trends2/get`` one request per symbol: ``em_CN00Y`` (A50 futures) and ``em_USDCNH``. Every answer holds
  all 1-minute bars of the session (the A50 session starts the evening before), so one good answer per fetch is enough. Rows are kind
  "minute"; ``poll_ts`` and ``quote_ts`` are the bar's minute. ``prev_close`` is the settlement for the A50 and the previous close for CNH.
Dropped by the old recorder and not asked again: hq.sinajs.cn (HTTP 403 from the server), push2/push2delay ulist (302 / 502), Eastmoney's
COMEX gold (10 minutes behind Tencent's hf_GC).

Invariants:
* unknown fields are NULL, never 0 (a zero price is NULL); the raw answer is kept by the caller before parsing;
* refusals are detected here exactly like the old recorder: HTTP 403/429/456/501 (``RateLimitedClient``), any redirect, a waf.tencent.com
  mention, or an HTML page answered with a success status -> :class:`RefusedAnswer` (a ``SourceBlocked``); the host is then closed for the run;
* at most one request per second per host (one per 3 s for Eastmoney, which drops this server after quick requests): one client per host;
* an unexpected CFFEX header or an unknown Eastmoney symbol yields NO rows (the format changed), which the recorder counts as a failure.
Not covered: scheduling and publishing (``application.bf_ext_record``), the legacy file layout (``legacy_recordings``), tick data.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
import polars as pl

from .http import Capture, RateLimitedClient, SourceBlocked

CST = timezone(timedelta(hours=8))
SCHEMA_VERSION = "native-ext-quotes-20261119"
MAPPING_VERSION = "1.0.0"

# --- hosts, sources, endpoints ------------------------------------------------------------------------------
QUOTE_HOST, CFFEX_HOST, EM_HOST = "qt.gtimg.cn", "www.cffex.com.cn", "push2his.eastmoney.com"
HOSTS = (QUOTE_HOST, CFFEX_HOST, EM_HOST)
SRC_TENCENT, SRC_CFFEX, SRC_EASTMONEY = "native_ext_tencent", "native_ext_cffex", "native_ext_eastmoney"
EP_QUOTE, EP_FILE, EP_TRENDS = "quote", "quote_file", "trends_minute"
HOST_ENDPOINT: dict[str, tuple[str, str]] = {QUOTE_HOST: (SRC_TENCENT, EP_QUOTE), CFFEX_HOST: (SRC_CFFEX, EP_FILE), EM_HOST: (SRC_EASTMONEY, EP_TRENDS)}
RECORDER_ID = "pulse_native_ext"

QUOTE_URL = f"https://{QUOTE_HOST}/q="
CFFEX_URL = f"http://{CFFEX_HOST}/quote_{{}}.txt"
EM_URL = (f"https://{EM_HOST}/api/qt/stock/trends2/get?fields1=f1,f2,f3,f5,f6,f7,f8"
          "&fields2=f51,f52,f53,f54,f55,f56,f57,f58&iscr=0&ndays=1&secid=")
HEADERS: dict[str, dict[str, str]] = {EM_HOST: {"Referer": "https://quote.eastmoney.com/"}}
ENCODINGS = {QUOTE_HOST: "gbk", CFFEX_HOST: "gbk", EM_HOST: "utf-8"}
#: minimum seconds between two requests to one host
HOST_INTERVAL = {QUOTE_HOST: 1.0, CFFEX_HOST: 1.0, EM_HOST: 3.0}
REQUEST_TIMEOUT = 6.0
LAST_MODIFIED = "#last-modified: "
CFFEX_COLUMNS = "instrument,openprice,highest,lowest,lastprice,updown,bprice,bamount,sprice,samount,volume,position"

# --- instruments ----------------------------------------------------------------------------------------------
ETFS = ("sh510300", "sh510310", "sh510050", "sz159919", "sh510500", "sh512100", "sz159845")
INDICES = ("sh000001", "sz399001", "sz399006", "sh000300", "sh000016", "sh000905", "sh000852", "sh000688", "sh000985", "sz399303")
FUTURE_PRODUCTS = ("IF", "IH", "IC", "IM")
TENCENT_OVERSEAS = {"hf_HSI": "future", "hf_GC": "commodity", "hf_CL": "commodity"}
EM_SYMBOLS = {"em_CN00Y": ("104.CN00Y", "future"), "em_USDCNH": ("133.USDCNH", "fx")}      # symbol: (secid, asset)

#: the old recorder's row layout (naive Asia/Shanghai times); the frame builder converts it to the dataset shape
ROW_SCHEMA: dict[str, Any] = {
    "symbol": pl.Utf8, "name": pl.Utf8, "asset": pl.Utf8, "host": pl.Utf8, "kind": pl.Utf8,
    "poll_ts": pl.Datetime("ms"), "quote_ts": pl.Datetime("ms"),
    "price": pl.Float64, "prev_close": pl.Float64, "prev_kind": pl.Utf8, "open": pl.Float64, "high": pl.Float64,
    "low": pl.Float64, "volume": pl.Float64, "amount": pl.Float64, "bid1": pl.Float64, "ask1": pl.Float64,
    "open_interest": pl.Float64, "contract": pl.Utf8,
}
_DEC_COLUMNS = ("price", "prev_close", "open", "high", "low", "bid1", "ask1", "open_interest")
#: the dataset frame ``ext_quote_snapshots`` (identical to what ``LegacyRecordings.read_ext`` returns; tests compare the two)
EXT_FRAME_SCHEMA: dict[str, Any] = {
    "source_part": pl.String, "session_date": pl.Date,
    **{c: pl.String for c in ("symbol", "name", "asset", "host", "kind", "prev_kind", "contract")},
    "poll_at": pl.Datetime("ms", "UTC"), "quote_at": pl.Datetime("ms", "UTC"),
    **{c: pl.Decimal(24, 6) for c in _DEC_COLUMNS}, "volume": pl.Float64, "amount": pl.Float64,
}
SORT_KEYS = ["symbol", "poll_at", "kind"]

_TENCENT_LINE = re.compile(r'v_((?:sh|sz|bj)\d{6}|hf_\w+)="([^"]*)"')
_CONTRACT = re.compile(r"(?:IF|IH|IC|IM)\d{4}")

_COMMON_POLICY = {"timezone": "Asia/Shanghai", "calendar_id": "xshg_xshe_sessions", "rate_limit": "<=1 request/second per host",
                  "on_block": "stop that host at once for the run (never retried), circuit OPEN",
                  "entitlement_status": "public_unauthenticated_endpoint", "recorder": RECORDER_ID,
                  "dataset": "ext_quote_snapshots", "replaces": "legacy_recordings/intraday_ext (LEGACY_TRIAGE #104/#186)",
                  "unknown_fields": "NULL, never 0 (a zero price is NULL)"}
ENDPOINT_POLICIES: dict[str, dict] = {
    EP_QUOTE: {**_COMMON_POLICY, "endpoint": "qt.gtimg.cn/q=<17 ETF/index symbols,hf_HSI,hf_GC,hf_CL>", "encoding": "gbk",
               "units": "ETF/index: price in yuan/points, volume in lots (vendor field 36), amount in yuan (field 35); hf_*: last/high/low/open/prev "
                        "SETTLEMENT in the contract's own units, bid1/ask1/volume/amount NULL (unverified at the source)",
               "time": "ETF/index quote_ts = vendor field 30 (CST); hf_* quote_ts = vendor date + time (Beijing time)"},
    EP_FILE: {**_COMMON_POLICY, "endpoint": "www.cffex.com.cn/quote_<IF|IH|IC|IM>.txt", "encoding": "gbk", "transport": "plain http (https times out)",
              "units": "price in index points, volume and open_interest in contracts, amount NULL; prev_close = last - updown = previous settlement",
              "time": "quote_ts = response Last-Modified (CST), stored as a '#last-modified:' first line of the raw body",
              "cadence_note": "the file changed every 5 min 15 s after the close on 2026-11-09; the in-session cadence is unverified"},
    EP_TRENDS: {**_COMMON_POLICY, "endpoint": "push2his.eastmoney.com/api/qt/stock/trends2/get", "encoding": "utf-8", "referer_required": True,
                "rate_limit": "<=1 request per 3 seconds (Eastmoney drops this server after quick requests)",
                "units": "1-minute bars: open/high/low/price(close) in points (A50) or CNH per USD; volume contracts (A50, unverified), NULL for CNH",
                "time": "kind 'minute': poll_ts = quote_ts = the bar's minute (CST); the A50 session starts the evening before"},
}


class ExtParseError(RuntimeError):
    """An answer that cannot be read at all (not JSON, wrong shape); a well-formed answer without our rows is just empty."""


class RefusedAnswer(SourceBlocked):
    """A success status that is really a refusal (redirect, WAF page, HTML instead of quote text); carries the answer as evidence."""

    def __init__(self, message: str, status: int, capture: Capture):
        super().__init__(message, status)
        self.capture = capture


# --- instruments -------------------------------------------------------------------------------------------------
def third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(4 - first.weekday()) % 7 + 14)


def _add_months(year: int, month: int, n: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + n
    return index // 12, index % 12 + 1


def contract_months(day: date) -> list[str]:
    """yymm of the listed CFFEX index-futures contracts: current month, next month, then the next two quarter months;
    the current month is dropped once it is past its third Friday (expiry)."""
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
    """The contracts expected to be listed on ``day`` (for the summary only: every contract in a file is recorded)."""
    return [f"{p}{m}" for p in FUTURE_PRODUCTS for m in contract_months(day)]


def all_symbols(day: date) -> list[str]:
    return [*tencent_symbols(), *future_symbols(day), *EM_SYMBOLS]


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


@dataclass(frozen=True)
class Request:
    host: str
    url: str
    key: str                     # stable label of the request (becomes part of the raw object key)


def requests_for(host: str, symbols: list[str]) -> list[Request]:
    """One batched request for Tencent, one file per product for the exchange, one series per symbol for Eastmoney."""
    if host == CFFEX_HOST:
        return [Request(host, CFFEX_URL.format(p), f"cffex:{p}") for p in FUTURE_PRODUCTS if any(s.startswith(p) for s in symbols)]
    if host == EM_HOST:
        return [Request(host, EM_URL + EM_SYMBOLS[s][0], f"em:{s}") for s in symbols if s in EM_SYMBOLS]
    return [Request(host, QUOTE_URL + ",".join(symbols), "qt:batch")] if symbols else []


def request_plan(symbols: list[str]) -> dict[str, list[Request]]:
    return {h: reqs for h in HOSTS if (reqs := requests_for(h, [s for s in symbols if host_of(s) == h]))}


# --- parsing ------------------------------------------------------------------------------------------------------
def _number(text: str | None) -> float | None:
    try:
        value = float(text) if text not in (None, "", "-") else None
    except ValueError:
        return None
    return value if value is not None and value == value and abs(value) != float("inf") else None


def _positive(text: str | None) -> float | None:
    value = _number(text)
    return value if value is not None and value > 0 else None


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


def _minute(text: str) -> datetime | None:
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def _http_stamp(text: str) -> datetime | None:
    try:
        return parsedate_to_datetime(text.strip()).astimezone(CST).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _row(symbol: str, name: str, host: str, kind: str, poll_ts: datetime, quote_ts: datetime | None, **fields: Any) -> dict[str, Any]:
    row: dict[str, Any] = {column: None for column in ROW_SCHEMA}
    row.update(symbol=symbol, name=name.strip(), asset=asset_of(symbol), host=host, kind=kind, poll_ts=poll_ts, quote_ts=quote_ts, **fields)
    return row


def parse_quote(symbol: str, f: list[str], kind: str, poll_ts: datetime) -> dict[str, Any] | None:
    """ETF / index line (``~`` separated): [3] last, [4] previous close, [5] open, [9] bid1, [19] ask1, [30] time, [33] high, [34] low,
    [35] ``last/lots/yuan``, [36] volume in lots."""
    if len(f) < 38 or not f[1].strip():
        return None
    parts = f[35].split("/")
    return _row(symbol, f[1], QUOTE_HOST, kind, poll_ts, _stamp(f[30]),
                price=_positive(f[3]), prev_close=_positive(f[4]), prev_kind="close", open=_positive(f[5]),
                high=_positive(f[33]), low=_positive(f[34]), volume=_number(f[36]),
                amount=_number(parts[2]) if len(parts) == 3 else None, bid1=_positive(f[9]), ask1=_positive(f[19]))


def parse_hf(symbol: str, f: list[str], kind: str, poll_ts: datetime) -> dict[str, Any] | None:
    """Overseas future (comma separated): [0] last, [4] high, [5] low, [6] time, [7] previous settlement, [8] open, [12] date, [13] name.
    Bid / ask / volume are not trusted (module docstring) and stay NULL."""
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
        row = parse_hf(symbol, body.split(","), kind, poll_ts) if symbol.startswith("hf_") else parse_quote(symbol, body.split("~"), kind, poll_ts)
        if row is not None:
            rows.append(row)
    return rows


def parse_cffex(text: str, kind: str, poll_ts: datetime) -> list[dict[str, Any]]:
    """One product file. An unexpected header means the format changed: no rows (a failed request).
    Columns: [0] contract, [1] open, [2] high, [3] low, [4] last, [5] last minus previous settlement, [6] bid, [8] ask, [10] volume, [11] open interest."""
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
                         volume=_number(f[10]), bid1=_positive(f[6]), ask1=_positive(f[8]), open_interest=_number(f[11]), contract=f[0]))
    return rows


def parse_minutes(text: str, kind: str, poll_ts: datetime) -> list[dict[str, Any]]:
    """Eastmoney's minute series of one symbol: ``trends`` lines of time, open, close, high, low, volume, amount, average. Every row is
    kind "minute" stamped with its bar (two fetches of a day merge into one series); any other symbol or a reply without data gives no rows.
    ``kind`` and ``poll_ts`` are accepted for the common parser signature and not used."""
    try:
        data = (json.loads(text) or {}).get("data") or {}
    except (ValueError, AttributeError) as exc:
        raise ExtParseError(f"not an Eastmoney trends answer: {exc}") from exc
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
        rows.append(_row(symbol, str(data.get("name") or ""), EM_HOST, "minute", stamp, stamp, price=_positive(f[2]), prev_close=prev_close,
                         prev_kind="settle" if is_future else "close", open=_positive(f[1]), high=_positive(f[3]), low=_positive(f[4]),
                         volume=_number(f[5]) if is_future else None))
    return rows


PARSERS: dict[str, Callable[[str, str, datetime], list[dict[str, Any]]]] = {QUOTE_HOST: parse_tencent, CFFEX_HOST: parse_cffex, EM_HOST: parse_minutes}


def parse_capture(host: str, body: bytes, kind: str, poll_ts: datetime) -> list[dict[str, Any]]:
    return PARSERS[host](body.decode(ENCODINGS[host], errors="replace"), kind, poll_ts)


# --- dataset frame -------------------------------------------------------------------------------------------------
def _utc(col: str) -> pl.Expr:
    return pl.col(col).cast(pl.Datetime("ms")).dt.replace_time_zone("Asia/Shanghai").dt.convert_time_zone("UTC").cast(pl.Datetime("ms", "UTC"))


def ext_frame(rows: list[dict[str, Any]], day: date, part_name: str) -> pl.DataFrame:
    """Rows (naive CST times, floats) -> the ``ext_quote_snapshots`` frame: UTC instants, decimal(24,6) prices, float volume / amount."""
    df = pl.DataFrame(rows, schema=ROW_SCHEMA)
    return df.select(
        pl.lit(part_name).alias("source_part"), pl.lit(day).alias("session_date"),
        *[pl.col(c).cast(pl.String) for c in ("symbol", "name", "asset", "host", "kind", "prev_kind", "contract")],
        _utc("poll_ts").alias("poll_at"), _utc("quote_ts").alias("quote_at"),
        *[pl.col(c).cast(pl.Float64).fill_nan(None).cast(pl.Decimal(24, 6)).alias(c) for c in _DEC_COLUMNS],
        pl.col("volume").cast(pl.Float64).fill_nan(None), pl.col("amount").cast(pl.Float64).fill_nan(None))


# --- http ----------------------------------------------------------------------------------------------------------
def check_not_refused(cap: Capture) -> None:
    """A refusal that arrives with a success-looking status: any redirect (these endpoints never redirect), the Tencent WAF, or an HTML page
    where quote text should be. A plain 404 / 502 is NOT a refusal (the client raises a transport failure; the next poll is the retry)."""
    if 300 <= cap.status < 400:
        raise RefusedAnswer(f"redirect_{cap.status}", cap.status, cap)
    if b"waf.tencent.com" in cap.body[:600]:
        raise RefusedAnswer("waf_tencent", cap.status, cap)
    if 200 <= cap.status < 300 and cap.body.lstrip()[:1] == b"<":
        raise RefusedAnswer("html_answer", cap.status, cap)


class HeaderTap(httpx.BaseTransport):
    """Transport wrapper that remembers the headers of the last response (the shared HTTP client does not return them)."""

    def __init__(self, inner: httpx.BaseTransport | None = None):
        self.inner = inner or httpx.HTTPTransport()
        self.last_headers: dict[str, str] = {}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self.inner.handle_request(request)
        self.last_headers = {k.lower(): v for k, v in response.headers.items()}
        return response

    def close(self) -> None:
        self.inner.close()


class ExtFetcher:
    """One ``RateLimitedClient`` per host (own pacing, own circuit), the CFFEX one behind a :class:`HeaderTap`.

    ``fetch`` returns the answer as a ``Capture`` (the CFFEX body gets the ``#last-modified:`` line in front: that is also what is stored as
    raw evidence); raises ``SourceBlocked`` (circuit of that host OPEN, nothing more is sent to it) or ``TransportFailure``."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None, interval: Mapping[str, float] | None = None, timeout: float = REQUEST_TIMEOUT,
                 clock: Callable[[], float] | None = None, sleep: Callable[[float], None] | None = None, now: Callable[[], datetime] | None = None):
        gaps = {**HOST_INTERVAL, **(interval or {})}
        extra: dict[str, Any] = {k: v for k, v in (("clock", clock), ("sleep", sleep), ("now", now)) if v is not None}
        self.tap = HeaderTap(transport)
        self.clients = {h: RateLimitedClient(min_interval_seconds=gaps[h], timeout=timeout,
                                             transport=self.tap if h == CFFEX_HOST else transport, **extra) for h in HOSTS}

    def blocked(self, host: str) -> str | None:
        c = self.clients[host]
        return c.block_reason if c.circuit_open else None

    def close_host(self, host: str, reason: str) -> None:
        self.clients[host].circuit_open, self.clients[host].block_reason = True, reason

    @property
    def requests_made(self) -> int:
        return sum(c.requests_made for c in self.clients.values())

    def fetch(self, req: Request) -> Capture:
        source_id, endpoint_id = HOST_ENDPOINT[req.host]
        client = self.clients[req.host]
        cap = client.request(source_id=source_id, endpoint_id=endpoint_id, request_key=req.key, url=req.url, headers=HEADERS.get(req.host))
        try:
            check_not_refused(cap)
        except RefusedAnswer as exc:
            self.close_host(req.host, str(exc))
            raise
        if req.host == CFFEX_HOST and self.tap.last_headers.get("last-modified"):
            cap = replace(cap, body=f"{LAST_MODIFIED}{self.tap.last_headers['last-modified']}\n".encode("ascii", "replace") + cap.body)
        return cap

    def close(self) -> None:
        for c in self.clients.values():
            c.close()
