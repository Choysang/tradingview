"""Tencent quote and kline provider for the historical warehouse.

Two endpoints, both public and unauthenticated:
* `qt.gtimg.cn/q=sh600000,sz000001` - one `~`-separated line per ticker, used
  to enumerate the listed universe and read caps / PE / PB.
* `web.ifzq.gtimg.cn/appstock/app/fqkline/get` - daily klines. Stocks return at
  most 640 bars per request, so long windows are split into <= 2-year chunks.

Everything here is a plain blocking function over a shared `httpx.Client` so
the sync job can fan out with a thread pool. Network failures are retried a
couple of times and then raised as `TencentError`; an empty answer for a code
(not listed in that window) is a normal empty frame, not an error.

Politeness: each host has a pacer (requests per second across all threads).
A burst of whole-market kline requests trips Tencent's WAF, which then answers
HTTP 501 with a redirect page for every request; that is raised at once as
`TencentBlocked` (no retries) so the sync job can stop instead of hammering.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import logging
import re
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
import polars as pl

from ..research.tencent_history import market

LOG = logging.getLogger("pulse.warehouse.tencent")

KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
QUOTE_URL = "https://qt.gtimg.cn/q="

TIMEOUT = 8.0
RETRIES = 2
BACKOFF = 0.4
QUOTE_BATCH = 80
QUOTE_WORKERS = 8
CHUNK_DAYS = 730  # <= 2 years, comfortably under the 640-bar cap per request
MAX_BARS = 640
FAILED_BATCH_LIMIT = 0.25
# Per host: (requests per second, requests per burst, rest seconds after a burst),
# shared by every thread. The kline WAF acts on request count more than on rate
# (see sync.PROBE_EVERY); pacing keeps bursts polite, `--wait-hours` resumes.
HOST_PACE = {"web.ifzq.gtimg.cn": (2.0, 600, 60.0), "qt.gtimg.cn": (10.0, 0, 0.0)}

# (market prefix, first code, last code) - candidate ranges probed through the quote endpoint.
CODE_RANGES: tuple[tuple[str, int, int], ...] = (
    ("sh", 600000, 605999),
    ("sh", 688000, 689999),
    ("sz", 1, 3999),
    ("sz", 300000, 301999),
    ("bj", 920000, 920999),
    ("bj", 430000, 430999),
    ("bj", 830000, 839999),
    ("bj", 870000, 873999),
)

# Index code -> kline ticker. Codes are shared with stocks (000001), so the
# market prefix is fixed here instead of derived from the code.
INDEX_TICKERS: dict[str, str] = {
    "000985": "sh000985",  # 中证全指
    "000300": "sh000300",  # 沪深300
    "000852": "sh000852",  # 中证1000
    "000905": "sh000905",  # 中证500
    "399006": "sz399006",  # 创业板指
    "000001": "sh000001",  # 上证指数
}

SECURITY_SCHEMA: dict[str, Any] = {
    "code": pl.Utf8,
    "name": pl.Utf8,
    "board": pl.Utf8,
    "total_cap": pl.Float64,
    "float_cap": pl.Float64,
    "pe_ttm": pl.Float64,
    "pb": pl.Float64,
    "as_of": pl.Utf8,
    "list_date": pl.Utf8,
}
BAR_SCHEMA: dict[str, Any] = {
    "code": pl.Utf8,
    "date": pl.Date,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
}
INDEX_SCHEMA: dict[str, Any] = {
    "symbol": pl.Utf8,
    "date": pl.Date,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
}

_QUOTE_LINE = re.compile(r'v_(?:sh|sz|bj)(\d{6})="([^"]*)"')


class TencentError(RuntimeError):
    """A request kept failing after its retries."""


class ProviderBlocked(TencentError):
    """A data host refuses us (rate limit); retrying only extends the block. Sync jobs
    stop at once on any provider's refusal."""


class TencentBlocked(ProviderBlocked):
    """Tencent's WAF refuses us."""


class _Pacer:
    """Minimum spacing between requests to one host, across threads, with a
    rest after every `burst` requests (0 = never)."""

    def __init__(self, per_second: float, burst: int = 0, rest: float = 0.0) -> None:
        self._gap = 1.0 / per_second
        self._burst = burst
        self._rest = rest
        self._count = 0
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._count += 1
            pause = self._rest if self._burst and self._count % self._burst == 0 else 0.0
            self._next = slot + self._gap + pause
        if slot > now:
            time.sleep(slot - now)


_PACERS = {host: _Pacer(*pace) for host, pace in HOST_PACE.items()}


def _blocked(response: httpx.Response) -> bool:
    return response.status_code in (403, 501) or b"waf.tencent.com" in response.content[:600]


def kline_open(client: httpx.Client) -> bool:
    """One tiny kline request, unpaced: False while the WAF still refuses us."""
    try:
        response = client.get(KLINE_URL, params={"param": "sh000001,day,,,1,qfq"})
    except httpx.HTTPError:
        return False
    return response.status_code == 200 and not _blocked(response)


def new_client() -> httpx.Client:
    limits = httpx.Limits(max_connections=16, max_keepalive_connections=8)
    return httpx.Client(timeout=TIMEOUT, limits=limits, headers={"User-Agent": "Mozilla/5.0 Pulse"})


# --- code helpers ------------------------------------------------------------

def market_of(code: str) -> str:
    """Tencent market prefix (sh / sz / bj)."""
    return market(code)


def board_of(code: str) -> str:
    if code.startswith(("688", "689", "300", "301")):
        return "20cm"
    if market_of(code) == "bj":
        return "30cm"
    return "10cm"


def _ticker(code: str) -> str:
    return f"{market_of(code)}{code}"


def candidate_codes() -> list[tuple[str, str]]:
    """(market, code) pairs to probe, in a stable order."""
    out: list[tuple[str, str]] = []
    for prefix, first, last in CODE_RANGES:
        out.extend((prefix, f"{number:06d}") for number in range(first, last + 1))
    return out


# --- http ----------------------------------------------------------------------

def _get(client: httpx.Client, url: str, params: dict[str, str] | None = None) -> httpx.Response:
    host = urlsplit(url).hostname or ""
    pacer = _PACERS.get(host)
    last: Exception | None = None
    for attempt in range(RETRIES + 1):
        try:
            if pacer is not None:
                pacer.wait()
            response = client.get(url, params=params)
            if _blocked(response):
                raise TencentBlocked(f"{host} 拒绝访问（HTTP {response.status_code}，腾讯 WAF 限流），已停止请求，稍后再试")
            if response.status_code == 200:
                return response
            last = TencentError(f"HTTP {response.status_code}")
        except httpx.HTTPError as exc:
            last = exc
        if attempt < RETRIES:
            time.sleep(BACKOFF * (attempt + 1))
    raise TencentError(f"{url.split('//')[-1].split('/')[0]} 请求失败：{last}") from last


def _number(text: str | None) -> float | None:
    try:
        value = float(text) if text not in (None, "", "-") else None
    except ValueError:
        return None
    return value if value is not None and value == value and abs(value) != float("inf") else None


def _positive(text: str | None) -> float | None:
    value = _number(text)
    return value if value is not None and value > 0 else None


# --- securities ----------------------------------------------------------------

def parse_quotes(text: str, as_of: str) -> list[dict[str, Any]]:
    """Rows for tickers that have a name and a positive price. Unknown PE / PB /
    caps are None, never 0."""
    rows: list[dict[str, Any]] = []
    for match in _QUOTE_LINE.finditer(text):
        fields = match.group(2).split("~")
        if len(fields) < 4:
            continue
        name = fields[1].strip()
        price = _positive(fields[3])
        if not name or price is None:
            continue
        code = fields[2].strip() if re.fullmatch(r"\d{6}", fields[2].strip() or "") else match.group(1)
        pe = _number(fields[39]) if len(fields) > 39 else None
        rows.append(
            {
                "code": code,
                "name": name,
                "board": board_of(code),
                "total_cap": _positive(fields[45]) if len(fields) > 45 else None,
                "float_cap": _positive(fields[44]) if len(fields) > 44 else None,
                "pe_ttm": pe if pe not in (None, 0.0) else None,
                "pb": _positive(fields[46]) if len(fields) > 46 else None,
                "as_of": as_of,
                "list_date": None,
            }
        )
    return rows


QUOTE_BAR_SCHEMA: dict[str, Any] = {**BAR_SCHEMA, "prev_close": pl.Float64}


def parse_quote_bars(text: str) -> list[dict[str, Any]]:
    """Today's daily bar from each quote line. Fields: [3] last, [4] previous
    close as published (on an ex-right day this is the exchange's reference
    price, which is what reveals a dividend or split), [5] open, [30] time
    YYYYMMDDhhmmss, [33] high, [34] low, [36] volume in lots, [37] amount in
    万元. Rows without a positive price, time or OHLC are skipped."""
    rows: list[dict[str, Any]] = []
    for match in _QUOTE_LINE.finditer(text):
        fields = match.group(2).split("~")
        if len(fields) < 38 or not re.fullmatch(r"\d{14}", fields[30].strip()):
            continue
        stamp = fields[30].strip()
        close, opening = _positive(fields[3]), _positive(fields[5])
        high, low = _positive(fields[33]), _positive(fields[34])
        if None in (close, opening, high, low):
            continue
        lots = _number(fields[36])
        amount = _number(fields[37])
        rows.append({
            "code": match.group(1),
            "date": date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8])),
            "open": opening,
            "high": high,
            "low": low,
            "close": close,
            "volume": lots * 100 if lots is not None else None,
            "amount": amount / 1e4 if amount is not None else None,
            "prev_close": _positive(fields[4]),
        })
    return rows


def fetch_quote_bars(codes: list[str], client: httpx.Client) -> pl.DataFrame:
    """Today's bar for every code in ~len/80 requests (vs one kline request per code)."""
    tickers = [_ticker(code) for code in codes]
    rows: list[dict[str, Any]] = []
    for index in range(0, len(tickers), QUOTE_BATCH):
        response = _get(client, QUOTE_URL + ",".join(tickers[index : index + QUOTE_BATCH]))
        rows.extend(parse_quote_bars(response.content.decode("gbk", errors="replace")))
    return pl.DataFrame(rows, schema=QUOTE_BAR_SCHEMA).unique(subset=["code"], keep="last").sort("code")


def _quote_batch(client: httpx.Client, tickers: list[str], as_of: str) -> list[dict[str, Any]]:
    response = _get(client, QUOTE_URL + ",".join(tickers))
    return parse_quotes(response.content.decode("gbk", errors="replace"), as_of)


def _securities_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=SECURITY_SCHEMA).unique(subset=["code"], keep="last").sort("code")


def fetch_quotes(codes: list[str], client: httpx.Client) -> pl.DataFrame:
    """Securities rows for explicit codes."""
    as_of = date.today().isoformat()
    tickers = [_ticker(code) for code in codes]
    batches = [tickers[i : i + QUOTE_BATCH] for i in range(0, len(tickers), QUOTE_BATCH)]
    rows: list[dict[str, Any]] = []
    for batch in batches:
        rows.extend(_quote_batch(client, batch, as_of))
    return _securities_frame(rows)


def list_securities(client: httpx.Client) -> pl.DataFrame:
    """Enumerate the listed A-share universe (SH, SZ and BJ) via the quote
    endpoint. Raises TencentError when too many batches failed to trust the list."""
    as_of = date.today().isoformat()
    tickers = [f"{prefix}{code}" for prefix, code in candidate_codes()]
    batches = [tickers[i : i + QUOTE_BATCH] for i in range(0, len(tickers), QUOTE_BATCH)]
    rows: list[dict[str, Any]] = []
    failed = 0

    def run(batch: list[str]) -> list[dict[str, Any]] | None:
        try:
            return _quote_batch(client, batch, as_of)
        except TencentBlocked:
            raise
        except TencentError as exc:
            LOG.warning("quote batch failed: %s", exc)
            return None

    with ThreadPoolExecutor(max_workers=QUOTE_WORKERS) as pool:
        for result in pool.map(run, batches):
            if result is None:
                failed += 1
            else:
                rows.extend(result)
    if failed > len(batches) * FAILED_BATCH_LIMIT:
        raise TencentError(f"行情接口失败批次过多（{failed}/{len(batches)}），证券列表不可信")
    if failed:
        LOG.warning("%s of %s quote batches failed; securities list is partial", failed, len(batches))
    return _drop_legacy_bse_aliases(_securities_frame(rows))


def _drop_legacy_bse_aliases(frame: pl.DataFrame) -> pl.DataFrame:
    """Tencent still answers for a BSE stock's pre-migration code (4xxxxx / 8xxxxx)
    next to its new 920xxx code. Keep the 920xxx row when both exist, otherwise
    every migrated stock would be counted twice."""
    migrated = frame.filter(pl.col("code").str.starts_with("92"))["name"].to_list()
    legacy = pl.col("code").str.starts_with("4") | pl.col("code").str.starts_with("8")
    return frame.filter(~(legacy & pl.col("name").is_in(migrated)))


# --- klines --------------------------------------------------------------------

def windows(start: date, end: date, days: int = CHUNK_DAYS) -> list[tuple[date, date]]:
    """Consecutive inclusive windows of at most `days` days covering [start, end]."""
    out: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        stop = min(cursor + timedelta(days=days - 1), end)
        out.append((cursor, stop))
        cursor = stop + timedelta(days=1)
    return out


def parse_kline(payload: dict[str, Any], ticker: str) -> list[tuple[date, float, float, float, float, float]]:
    """Rows are [date, open, close, high, low, volume(手), ...]. Returned as
    (date, open, high, low, close, volume in shares)."""
    node = (payload.get("data") or {})
    node = node.get(ticker) if isinstance(node, dict) else None
    if not isinstance(node, dict):
        return []
    rows = node.get("qfqday") or node.get("day") or []
    out: list[tuple[date, float, float, float, float, float]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        try:
            day = date.fromisoformat(str(row[0])[:10])
            opened, close, high, low, lots = (float(row[i]) for i in range(1, 6))
        except (TypeError, ValueError):
            continue
        if min(opened, close, high, low) <= 0:
            continue
        out.append((day, opened, high, low, close, lots * 100))
    return out


def _kline(client: httpx.Client, ticker: str, start: date, end: date) -> list[tuple]:
    param = f"{ticker},day,{start.isoformat()},{end.isoformat()},{MAX_BARS},qfq"
    response = _get(client, KLINE_URL, {"param": param})
    try:
        payload = response.json()
    except ValueError as exc:
        raise TencentError(f"{ticker} 返回了无法解析的数据") from exc
    return parse_kline(payload, ticker)


def fetch_bars(code: str, start: date, end: date, client: httpx.Client) -> pl.DataFrame:
    """Forward-adjusted daily bars for one stock; volume is in shares, amount is
    unknown (null) because the kline carries no turnover value."""
    ticker = _ticker(code)
    rows: list[tuple] = []
    for first, last in windows(start, end):
        rows.extend(_kline(client, ticker, first, last))
    if not rows:
        return pl.DataFrame(schema=BAR_SCHEMA)
    frame = pl.DataFrame(
        rows, schema=["date", "open", "high", "low", "close", "volume"], orient="row"
    ).with_columns(pl.lit(code).alias("code"), pl.lit(None, dtype=pl.Float64).alias("amount"))
    return (
        frame.unique(subset=["date"], keep="last").sort("date").select(list(BAR_SCHEMA))
        .cast(BAR_SCHEMA)  # type: ignore[arg-type]
    )


def fetch_index(symbol: str, start: date, end: date, client: httpx.Client) -> pl.DataFrame:
    """Daily bars of one of INDEX_TICKERS, keyed by the 6-digit index code."""
    ticker = INDEX_TICKERS.get(symbol)
    if ticker is None:
        raise ValueError(f"unknown index {symbol}")
    rows: list[tuple] = []
    for first, last in windows(start, end):
        rows.extend(_kline(client, ticker, first, last))
    if not rows:
        return pl.DataFrame(schema=INDEX_SCHEMA)
    frame = pl.DataFrame(
        rows, schema=["date", "open", "high", "low", "close", "volume"], orient="row"
    ).with_columns(pl.lit(symbol).alias("symbol"))
    return (
        frame.unique(subset=["date"], keep="last").sort("date").select(list(INDEX_SCHEMA))
        .cast(INDEX_SCHEMA)  # type: ignore[arg-type]
    )
