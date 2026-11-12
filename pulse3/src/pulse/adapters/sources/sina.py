"""Sina quote and kline adapter (hq.sinajs.cn, money.finance.sina.com.cn), M1-E01.

Why: a residential-PC and cloud reachable raw-price source. Units verified against
the recorded endpoints (fixtures/data/recorded): ``hq`` volume is SHARES and
amount is CNY yuan (cumulative for the day); ``kline`` volume is SHARES per
interval; minute bar labels are the bar CLOSE time. Prices are unadjusted.

Invariants: hq requires the Referer header; a body that is not the expected
shape is ``NotData``/``MappingDrift`` (not "empty"); kline has no turnover
amount, so profiles declare ``absent_fields`` (partial quality, never 0).
Not covered: Level-2, adjusted klines (``ma=no`` and no qfq endpoint is used).
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from pulse.domain.evidence.announcements import NotData, SchemaChanged

from .http import Capture, RateLimitedClient

SOURCE_ID = "sina"
HQ_URL = "https://hq.sinajs.cn/list={symbols}"
KLINE_URL = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
             "?symbol={symbol}&scale={scale}&ma=no&datalen={datalen}")
REFERER = {"Referer": "https://finance.sina.com.cn"}
KLINE_SCALES = (5, 15, 30, 60, 240)
SCHEMA_VERSION = "sina-recorded-20261111"
MAPPING_VERSION = "1.0.0"

_COMMON = ("source_id", "endpoint_id", "source_schema_version", "mapping_version")


def _op(source: str, target: str, op: str, **kw) -> dict:
    return {"source": source, "target": target, "op": op, **kw}


HQ_PROFILE = {
    "profile_id": "sina_hq_quote_20261111_v1", "source_id": SOURCE_ID, "endpoint_id": "hq_quote",
    "source_schema_version": SCHEMA_VERSION, "mapping_version": MAPPING_VERSION, "record_kind": "quote",
    "source_semantics": {"volume": "shares, cumulative for the day", "amount": "CNY yuan, cumulative for the day",
                         "quote_time": "exchange time of the snapshot (CST)"},
    "operations": [
        _op("symbol", "instrument_id", "resolve_identifier_at_observation_time"),
        _op("quote_time", "event_time", "parse_datetime_cst_iso_seconds"),
        _op("last", "last_price_raw", "decimal_exact", zero_is_missing=True),
        _op("open", "open_price_raw", "decimal_exact", zero_is_missing=True),
        _op("high", "high_price_raw", "decimal_exact", zero_is_missing=True),
        _op("low", "low_price_raw", "decimal_exact", zero_is_missing=True),
        _op("prev_close", "prev_close_price_raw", "decimal_exact", zero_is_missing=True),
        _op("bid1_price", "bid_price_raw", "decimal_exact", zero_is_missing=True),
        _op("ask1_price", "ask_price_raw", "decimal_exact", zero_is_missing=True),
        _op("bid1_volume", "bid_volume_shares", "require_integer", raw_unit="shares"),
        _op("ask1_volume", "ask_volume_shares", "require_integer", raw_unit="shares"),
        _op("volume", "cumulative_volume_shares", "require_integer", raw_unit="shares"),
        _op("amount", "cumulative_turnover_cny", "decimal_exact", raw_unit="CNY"),
    ],
    "recognized_unmapped": ["name", "status", "trailer"],
}


def kline_profile(scale: int) -> dict:
    daily = scale == 240
    when = _op("day", "trade_date", "parse_date_iso") if daily else _op("day", "event_time", "parse_datetime_cst_iso_seconds")
    return {
        "profile_id": f"sina_kline_{scale}_20261111_v1", "source_id": SOURCE_ID,
        "endpoint_id": "kline_daily" if daily else "kline_minute", "source_schema_version": SCHEMA_VERSION,
        "mapping_version": MAPPING_VERSION, "record_kind": "bar",
        "source_semantics": {"volume": "shares per interval", "day": "bar date" if daily else "bar CLOSE time (CST)",
                             "prices": "unadjusted (ma=no)"},
        "operations": [
            _op("symbol", "instrument_id", "resolve_identifier_at_observation_time"), when,
            _op("open", "open_price_raw", "decimal_exact"), _op("high", "high_price_raw", "decimal_exact"),
            _op("low", "low_price_raw", "decimal_exact"), _op("close", "close_price_raw", "decimal_exact"),
            _op("volume", "volume_shares", "require_integer", raw_unit="shares"),
        ],
        "absent_fields": {"turnover_amount_cny": "endpoint_does_not_provide_turnover"},
    }


KLINE_DAILY_PROFILE = kline_profile(240)
KLINE_MINUTE_PROFILE = kline_profile(5)  # mapping is identical for every minute scale
PROFILES = {"hq_quote": HQ_PROFILE, "kline_daily": KLINE_DAILY_PROFILE, "kline_minute": KLINE_MINUTE_PROFILE}

_HQ_LINE = re.compile(r'var hq_str_(\w+)="(.*?)";?\s*$')


def _decode(body: bytes) -> str:
    try:
        return body.decode("gbk")
    except UnicodeDecodeError as exc:
        raise NotData("hq_body_not_gbk") from exc


def parse_hq(body: bytes) -> tuple[list[dict], list[str]]:
    """Return (records, empty_symbols). An empty quoted string means the symbol is unknown to Sina."""
    text = _decode(body)
    if "var hq_str_" not in text:
        raise NotData("hq_body_has_no_quote_variables")
    records, empty = [], []
    for line in text.splitlines():
        m = _HQ_LINE.match(line.strip())
        if not m:
            continue
        symbol, payload = m.group(1), m.group(2)
        if not payload:
            empty.append(symbol)
            continue
        f = payload.split(",")
        if len(f) < 32:
            raise SchemaChanged(f"hq_field_count_{len(f)}_lt_32")
        records.append({
            "symbol": symbol, "name": f[0], "open": f[1], "prev_close": f[2], "last": f[3], "high": f[4], "low": f[5],
            "bid1_price": f[11], "bid1_volume": f[10], "ask1_price": f[21], "ask1_volume": f[20],
            "volume": f[8], "amount": f[9], "quote_time": f"{f[30]} {f[31]}", "status": f[32] if len(f) > 32 else None,
            "trailer": ",".join(f[33:]) or None,
        })
    return records, empty


def parse_kline(body: bytes, symbol: str) -> list[dict]:
    text = body.decode("utf-8", errors="replace").strip()
    if not text or text[0] not in "[n":
        raise NotData("kline_body_not_json_array")
    if text == "null":
        return []
    try:
        rows = json.loads(text)
    except json.JSONDecodeError as exc:
        raise NotData("kline_body_invalid_json") from exc
    if not isinstance(rows, list):
        raise SchemaChanged("kline_not_a_list")
    out = []
    for r in rows:
        if not isinstance(r, dict):
            raise SchemaChanged("kline_row_not_object")
        out.append({"symbol": symbol, **r})
    return out


class SinaAdapter:
    def __init__(self, client: RateLimitedClient):
        self.client = client

    def quotes(self, symbols: list[str]) -> Capture:
        if not symbols or len(symbols) > 50:
            raise ValueError("1..50 symbols per request")
        joined = ",".join(symbols)
        return self.client.request(source_id=SOURCE_ID, endpoint_id="hq_quote", request_key=f"hq:{joined}",
                                   url=HQ_URL.format(symbols=joined), headers=REFERER)

    def kline(self, symbol: str, scale: int = 240, datalen: int = 30) -> Capture:
        if scale not in KLINE_SCALES:
            raise ValueError(f"scale must be one of {KLINE_SCALES}")
        endpoint = "kline_daily" if scale == 240 else "kline_minute"
        return self.client.request(
            source_id=SOURCE_ID, endpoint_id=endpoint, request_key=f"kline:{symbol}:{scale}:{datalen}",
            url=KLINE_URL.format(symbol=symbol, scale=scale, datalen=datalen), headers=REFERER)


def bar_close_time(day: str) -> datetime:
    return datetime.strptime(day, "%Y-%m-%d %H:%M:%S")
