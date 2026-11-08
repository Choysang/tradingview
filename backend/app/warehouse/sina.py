"""Sina daily bars, forward-adjusted by ratio: raw kline / Sina's qfq factor.

Why bars come from Sina: Tencent's fqkline forward adjustment subtracts the
cumulative dividends instead of scaling (verified 2026-10-31 on 000001: the
raw/adjusted ratio drifts inside a quarter with no ex-date in it). Subtraction
inflates the percentage moves of old bars of high-dividend stocks — a raw +7.7%
day of 平安银行 in 2023 reads about +10% and would be taken for a limit-up. Ratio
adjustment keeps every daily return equal to the raw one, which limit-up
detection and every backtest need. Checked against Tencent on 000002 (no
dividends since 2023): identical closes and volumes on all 727 shared days.

One kline request returns up to 1,023 bars (about four years); the factor file
is one more request. Both hosts are paced and a refusal stops the job like a
Tencent WAF block.
"""
from __future__ import annotations

from datetime import date
import json
import logging
import math
import time
from urllib.parse import urlsplit

import httpx
import polars as pl

from . import tencent
from .tencent import (
    BACKOFF, BAR_SCHEMA, INDEX_SCHEMA, INDEX_TICKERS, RETRIES, ProviderBlocked, TencentError, _Pacer, market_of,
)

KLINE_URL = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
FACTOR_URL = "https://finance.sina.com.cn/realstock/company/{symbol}/qfq.js"
MAX_BARS = 1023
MAX_YEARS = 4  # 1,023 bars is about 4.1 years at 250 a year; bars_needed adds a 20-bar margin
HEADERS = {"Referer": "https://finance.sina.com.cn"}
HOST_PACE = {"quotes.sina.cn": (2.0, 0, 0.0), "finance.sina.com.cn": (2.0, 0, 0.0)}
BLOCK_STATUS = (403, 429, 456, 503)
STALE_INDICES = {"000985"}  # Sina's 中证全指 series stops in 2016: Tencent keeps serving it

_PACERS = {host: _Pacer(*pace) for host, pace in HOST_PACE.items()}
LOG = logging.getLogger("pulse.warehouse.sina")


class SinaBlocked(ProviderBlocked):
    """Sina refuses us (rate limit or a block page)."""


def _get(client: httpx.Client, url: str, params: dict | None = None) -> httpx.Response:
    host = urlsplit(url).hostname or ""
    last: Exception | None = None
    for attempt in range(RETRIES + 1):
        try:
            _PACERS[host].wait()
            response = client.get(url, params=params, headers=HEADERS)
            head = response.content[:1].strip()
            if response.status_code in BLOCK_STATUS or head == b"<":
                raise SinaBlocked(f"{host} 拒绝访问（HTTP {response.status_code}，新浪限流），已停止请求，稍后再试")
            if response.status_code == 200:
                return response
            last = TencentError(f"HTTP {response.status_code}")
        except httpx.HTTPError as exc:
            last = exc
        if attempt < RETRIES:
            time.sleep(BACKOFF * (attempt + 1))
    raise TencentError(f"{urlsplit(url).hostname} 请求失败：{last}") from last


def bars_needed(start: date, today: date | None = None) -> int:
    """Trading bars from `start` to today, with margin, capped at what one request returns."""
    days = ((today or date.today()) - start).days
    return max(10, min(MAX_BARS, math.ceil(days * 250 / 365) + 20))


def parse_kline(text: str) -> pl.DataFrame:
    rows = json.loads(text) if text.strip() not in ("", "null") else []
    if not rows:
        return pl.DataFrame(schema={"date": pl.Date, "open": pl.Float64, "high": pl.Float64, "low": pl.Float64,
                                    "close": pl.Float64, "volume": pl.Float64})
    return pl.DataFrame(rows).select(
        pl.col("day").str.slice(0, 10).str.to_date().alias("date"),
        *(pl.col(k).cast(pl.Float64) for k in ("open", "high", "low", "close", "volume")),
    )


def parse_factors(text: str) -> pl.DataFrame:
    """(d, f) sorted by date: from `d` on, adjusted price = raw / f."""
    empty = pl.DataFrame(schema={"d": pl.Date, "f": pl.Float64})
    if "{" not in text:
        return empty
    data = json.loads(text[text.index("{"): text.rindex("}") + 1]).get("data") or []
    if not data:
        return empty
    return pl.DataFrame({"d": [x["d"] for x in data], "f": [float(x["f"]) for x in data]}).with_columns(
        pl.col("d").str.to_date()
    ).sort("d")


def adjust(raw: pl.DataFrame, factors: pl.DataFrame) -> pl.DataFrame:
    """Divide prices by the factor in force on each day; no factor file means no adjustment."""
    if raw.is_empty():
        return raw
    if factors.is_empty():
        return raw
    earliest = float(factors["f"][0])
    joined = raw.sort("date").join_asof(factors, left_on="date", right_on="d", strategy="backward")
    factor = pl.col("f").fill_null(earliest)
    return joined.with_columns(*(pl.col(k) / factor for k in ("open", "high", "low", "close"))).drop("d", "f")


def fetch_bars(code: str, start: date, end: date, client: httpx.Client) -> pl.DataFrame:
    """Ratio forward-adjusted daily bars for one stock; volume in shares, amount unknown (null)."""
    symbol = f"{market_of(code)}{code}"
    raw = parse_kline(_get(client, KLINE_URL, {"symbol": symbol, "scale": 240, "ma": "no", "datalen": bars_needed(start)}).text)
    raw = raw.filter(pl.col("date").is_between(start, end))
    if raw.is_empty():
        return pl.DataFrame(schema=BAR_SCHEMA)
    factors = parse_factors(_get(client, FACTOR_URL.format(symbol=symbol)).text)
    frame = adjust(raw, factors).with_columns(pl.lit(code).alias("code"), pl.lit(None, dtype=pl.Float64).alias("amount"))
    return frame.unique(subset=["date"], keep="last").sort("date").select(list(BAR_SCHEMA)).cast(BAR_SCHEMA)  # type: ignore[arg-type]


def fetch_index(symbol: str, start: date, end: date, client: httpx.Client) -> pl.DataFrame:
    """Daily bars of one of INDEX_TICKERS (indices need no adjustment)."""
    if symbol in STALE_INDICES:
        try:
            return tencent.fetch_index(symbol, start, end, client)
        except ProviderBlocked as exc:  # Tencent's block must not stop a Sina job: the stored series stays
            LOG.warning("index %s skipped: %s", symbol, exc)
            return pl.DataFrame(schema=INDEX_SCHEMA)
    ticker = INDEX_TICKERS.get(symbol)
    if ticker is None:
        raise ValueError(f"unknown index {symbol}")
    raw = parse_kline(_get(client, KLINE_URL, {"symbol": ticker, "scale": 240, "ma": "no", "datalen": bars_needed(start)}).text)
    raw = raw.filter(pl.col("date").is_between(start, end))
    if raw.is_empty():
        return pl.DataFrame(schema=INDEX_SCHEMA)
    return raw.with_columns(pl.lit(symbol).alias("symbol")).unique(subset=["date"], keep="last").sort("date").select(
        list(INDEX_SCHEMA)
    ).cast(INDEX_SCHEMA)  # type: ignore[arg-type]


def kline_open(client: httpx.Client) -> bool:
    """One tiny kline request, unpaced: False while Sina still refuses us."""
    try:
        response = client.get(KLINE_URL, params={"symbol": "sh000001", "scale": 240, "ma": "no", "datalen": 1}, headers=HEADERS)
    except httpx.HTTPError:
        return False
    return response.status_code == 200 and response.content[:1].strip() != b"<"

