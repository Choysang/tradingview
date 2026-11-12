"""Tencent quote and kline adapter (qt.gtimg.cn, web.ifzq.gtimg.cn, ifzq.gtimg.cn), M1-E01.

Why: second raw-price route and the minute path. Units verified on the recorded
endpoints: qt/kline volume is LOTS (hand, 100 shares) and is converted by the
profile; the combined qt field ``price/lots/amount`` carries amount in CNY yuan;
kline rows are ``[date, open, CLOSE, high, low, volume_lots]`` (close before
high/low); mkline rows are ``[bar CLOSE time yyyymmddhhmm, open, close, high,
low, volume_lots, {}, turnover_pct]``; ``minute/query`` rows are cumulative
(``hhmm price cum_lots cum_amount``) - one price per minute, so it is only a
sampled close-only series (capability ``minute_close_sampled``, estimated).

Invariants: positional rows are validated by length (drift -> SchemaChanged);
``qfq`` (adjusted) klines are never mapped into raw profiles - they are only
parsed by :func:`parse_kline_adjusted_closes` for factor inference.
Not covered: tick data, Level-2, HK/US markets.
"""
from __future__ import annotations

import json

from pulse.domain.evidence.announcements import NotData, SchemaChanged

from .http import Capture, RateLimitedClient

SOURCE_ID = "tencent"
QT_URL = "https://qt.gtimg.cn/q={symbols}"
DAY_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,{count},{adjust}&_var=x"
MKLINE_URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={symbol},{period},,{count}"
MINUTE_URL = "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={symbol}"
SCHEMA_VERSION = "tencent-recorded-20261111"
MAPPING_VERSION = "1.0.0"


def _op(source: str, target: str, op: str, **kw) -> dict:
    return {"source": source, "target": target, "op": op, **kw}


QT_PROFILE = {
    "profile_id": "tencent_qt_quote_20261111_v1", "source_id": SOURCE_ID, "endpoint_id": "qt_quote",
    "source_schema_version": SCHEMA_VERSION, "mapping_version": MAPPING_VERSION, "record_kind": "quote",
    "source_semantics": {"volume": "lots (x100 shares), cumulative", "amount": "CNY yuan from the combined field",
                         "quote_time": "yyyymmddhhmmss CST"},
    "operations": [
        _op("symbol", "instrument_id", "resolve_identifier_at_observation_time"),
        _op("quote_time", "event_time", "parse_datetime_cst_compact14"),
        _op("last", "last_price_raw", "decimal_exact", zero_is_missing=True),
        _op("open", "open_price_raw", "decimal_exact", zero_is_missing=True),
        _op("high", "high_price_raw", "decimal_exact", zero_is_missing=True),
        _op("low", "low_price_raw", "decimal_exact", zero_is_missing=True),
        _op("prev_close", "prev_close_price_raw", "decimal_exact", zero_is_missing=True),
        _op("bid1_price", "bid_price_raw", "decimal_exact", zero_is_missing=True),
        _op("ask1_price", "ask_price_raw", "decimal_exact", zero_is_missing=True),
        _op("bid1_lots", "bid_volume_shares", "decimal_multiply_then_require_integer", factor="100", raw_unit="lots"),
        _op("ask1_lots", "ask_volume_shares", "decimal_multiply_then_require_integer", factor="100", raw_unit="lots"),
        _op("volume_lots", "cumulative_volume_shares", "decimal_multiply_then_require_integer", factor="100", raw_unit="lots"),
        _op("amount_yuan", "cumulative_turnover_cny", "decimal_exact", raw_unit="CNY"),
    ],
    "recognized_unmapped": ["name", "turnover_pct", "combo_volume_lots"],
}

DAY_PROFILE = {
    "profile_id": "tencent_kline_day_20261111_v1", "source_id": SOURCE_ID, "endpoint_id": "kline_day",
    "source_schema_version": SCHEMA_VERSION, "mapping_version": MAPPING_VERSION, "record_kind": "bar",
    "source_semantics": {"row": "[date, open, close, high, low, volume_lots]", "volume": "lots", "prices": "unadjusted (empty adjust)"},
    "operations": [
        _op("symbol", "instrument_id", "resolve_identifier_at_observation_time"),
        _op("date", "trade_date", "parse_date_iso"),
        _op("open", "open_price_raw", "decimal_exact"), _op("high", "high_price_raw", "decimal_exact"),
        _op("low", "low_price_raw", "decimal_exact"), _op("close", "close_price_raw", "decimal_exact"),
        _op("volume_lots", "volume_shares", "decimal_multiply_then_require_integer", factor="100", raw_unit="lots"),
    ],
    "absent_fields": {"turnover_amount_cny": "endpoint_does_not_provide_turnover"},
}

MKLINE_PROFILE = {
    "profile_id": "tencent_mkline_20261111_v1", "source_id": SOURCE_ID, "endpoint_id": "kline_minute",
    "source_schema_version": SCHEMA_VERSION, "mapping_version": MAPPING_VERSION, "record_kind": "bar",
    "source_semantics": {"row": "[close_time, open, close, high, low, volume_lots, {}, turnover_pct]", "volume": "lots per interval (fractional lots possible)"},
    "operations": [
        _op("symbol", "instrument_id", "resolve_identifier_at_observation_time"),
        _op("time", "event_time", "parse_datetime_cst_compact12"),
        _op("open", "open_price_raw", "decimal_exact"), _op("high", "high_price_raw", "decimal_exact"),
        _op("low", "low_price_raw", "decimal_exact"), _op("close", "close_price_raw", "decimal_exact"),
        _op("volume_lots", "volume_shares", "decimal_multiply_then_require_integer", factor="100", raw_unit="lots"),
    ],
    "absent_fields": {"turnover_amount_cny": "endpoint_does_not_provide_turnover"},
    "recognized_unmapped": ["turnover_pct", "extra"],
}

MINUTE_QUERY_PROFILE = {
    "profile_id": "tencent_minute_query_20261111_v1", "source_id": SOURCE_ID, "endpoint_id": "minute_query",
    "source_schema_version": SCHEMA_VERSION, "mapping_version": MAPPING_VERSION, "record_kind": "cumulative_sample",
    "source_semantics": {"row": "hhmm price cumulative_lots cumulative_amount_yuan",
                         "note": "one price per minute: high/low unobservable; includes after-hours fixed-price trades after 15:05"},
    "operations": [
        _op("symbol", "instrument_id", "resolve_identifier_at_observation_time"),
        _op("time", "event_time", "parse_datetime_cst_compact12"),
        _op("price", "last_price_raw", "decimal_exact"),
        _op("cum_lots", "cumulative_volume_shares", "decimal_multiply_then_require_integer", factor="100", raw_unit="lots"),
        _op("cum_amount", "cumulative_turnover_cny", "decimal_exact", raw_unit="CNY"),
    ],
}
PROFILES = {"qt_quote": QT_PROFILE, "kline_day": DAY_PROFILE, "kline_minute": MKLINE_PROFILE,
            "minute_query": MINUTE_QUERY_PROFILE}


def _json_after_var(body: bytes) -> dict:
    text = body.decode("utf-8", errors="replace").strip()
    if text.startswith("x="):
        text = text[2:]
    if not text or text[0] != "{":
        raise NotData("tencent_body_not_json_object")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise NotData("tencent_body_invalid_json") from exc
    if doc.get("code") not in (0, "0", None):
        raise NotData(f"tencent_api_code_{doc.get('code')}")
    return doc


def parse_qt(body: bytes) -> tuple[list[dict], list[str]]:
    text = body.decode("gbk", errors="replace")
    if "v_" not in text:
        raise NotData("qt_body_has_no_quote_variables")
    records, empty = [], []
    for line in text.splitlines():
        line = line.strip().rstrip(";")
        if not line.startswith("v_"):
            continue
        var, _, payload = line.partition("=")
        symbol = var[2:]
        payload = payload.strip().strip('"')
        if not payload or payload.startswith("pv_none"):
            empty.append(symbol)
            continue
        f = payload.split("~")
        if len(f) < 45:
            raise SchemaChanged(f"qt_field_count_{len(f)}_lt_45")
        combo = f[35].split("/")
        if len(combo) != 3:
            raise SchemaChanged("qt_field_35_not_price_lots_amount")
        records.append({
            "symbol": symbol, "name": f[1], "last": f[3], "prev_close": f[4], "open": f[5], "volume_lots": f[6],
            "bid1_price": f[9], "bid1_lots": f[10], "ask1_price": f[19], "ask1_lots": f[20], "quote_time": f[30],
            "high": f[33], "low": f[34], "combo_volume_lots": combo[1], "amount_yuan": combo[2], "turnover_pct": f[38],
        })
    return records, empty


def parse_kline_day(body: bytes, symbol: str) -> list[dict]:
    doc = _json_after_var(body)
    node = (doc.get("data") or {}).get(symbol)
    if not isinstance(node, dict):
        raise SchemaChanged("tencent_day_missing_symbol_node")
    rows = node.get("day")
    if rows is None:
        if "qfqday" in node or "hfqday" in node:
            raise SchemaChanged("adjusted_kline_returned_for_raw_request")
        return []
    out = []
    for r in rows:
        if len(r) < 6:
            raise SchemaChanged(f"tencent_day_row_len_{len(r)}")
        out.append({"symbol": symbol, "date": r[0], "open": r[1], "close": r[2], "high": r[3], "low": r[4], "volume_lots": r[5]})
    return out


def parse_kline_adjusted_closes(body: bytes, symbol: str) -> dict[str, str]:
    """{date: forward-adjusted close} for factor inference only (never a raw bar)."""
    doc = _json_after_var(body)
    node = (doc.get("data") or {}).get(symbol) or {}
    rows = node.get("qfqday")
    if rows is None:
        raise SchemaChanged("tencent_qfq_missing")
    return {r[0]: r[2] for r in rows}


def parse_mkline(body: bytes, symbol: str, period: str) -> list[dict]:
    doc = _json_after_var(body)
    node = (doc.get("data") or {}).get(symbol)
    if not isinstance(node, dict) or period not in node:
        raise SchemaChanged("tencent_mkline_missing_period")
    out = []
    for r in node[period]:
        if len(r) < 6:
            raise SchemaChanged(f"tencent_mkline_row_len_{len(r)}")
        out.append({"symbol": symbol, "time": r[0], "open": r[1], "close": r[2], "high": r[3], "low": r[4],
                    "volume_lots": r[5], "turnover_pct": r[7] if len(r) > 7 else None})
    return out


def parse_minute_query(body: bytes, symbol: str) -> list[dict]:
    doc = _json_after_var(body)
    node = ((doc.get("data") or {}).get(symbol) or {}).get("data")
    if not isinstance(node, dict) or "data" not in node or "date" not in node:
        raise SchemaChanged("tencent_minute_query_shape")
    day = str(node["date"])
    out = []
    for line in node["data"]:
        parts = line.split()
        if len(parts) != 4:
            raise SchemaChanged("tencent_minute_query_row")
        out.append({"symbol": symbol, "time": day + parts[0], "price": parts[1], "cum_lots": parts[2], "cum_amount": parts[3]})
    return out


class TencentAdapter:
    def __init__(self, client: RateLimitedClient):
        self.client = client

    def quotes(self, symbols: list[str]) -> Capture:
        if not symbols or len(symbols) > 50:
            raise ValueError("1..50 symbols per request")
        joined = ",".join(symbols)
        return self.client.request(source_id=SOURCE_ID, endpoint_id="qt_quote", request_key=f"qt:{joined}",
                                   url=QT_URL.format(symbols=joined))

    def daily(self, symbol: str, count: int = 320, adjust: str = "") -> Capture:
        if adjust not in ("", "qfq"):
            raise ValueError("adjust must be '' (raw) or 'qfq' (factor inference only)")
        ep = "kline_day" if adjust == "" else "kline_day_qfq"
        return self.client.request(source_id=SOURCE_ID, endpoint_id=ep, request_key=f"day:{symbol}:{count}:{adjust or 'raw'}",
                                   url=DAY_URL.format(symbol=symbol, count=count, adjust=adjust))

    def minute(self, symbol: str, period: str = "m1", count: int = 240) -> Capture:
        if period not in ("m1", "m5", "m15", "m30", "m60"):
            raise ValueError("unsupported minute period")
        return self.client.request(source_id=SOURCE_ID, endpoint_id="kline_minute", request_key=f"mkline:{symbol}:{period}:{count}",
                                   url=MKLINE_URL.format(symbol=symbol, period=period, count=count))

    def minute_query(self, symbol: str) -> Capture:
        return self.client.request(source_id=SOURCE_ID, endpoint_id="minute_query", request_key=f"minq:{symbol}",
                                   url=MINUTE_URL.format(symbol=symbol))
