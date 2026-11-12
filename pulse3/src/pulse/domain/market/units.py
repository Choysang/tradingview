"""Profile-driven record normalization (spec FINAL §4, RECON §4, M1-E02).

Why: the same field name means different units on different endpoints (Tushare
``daily.vol`` is lots, ``rt_k.vol`` is shares). A mapping profile is selected by
the full tuple ``(source_id, endpoint_id, source_schema_version, mapping_version)``
and is the only place units are converted, by declared decimal operations.

Invariants: missing stays ``None`` with field status ``missing`` (never 0);
fractional shares, non-finite values, negative quantities, non-positive prices
and OHLC contradictions become ``invalid`` fields, never rounded or repaired;
an absent mapped key is schema drift (``MappingDrift``) that pauses canonical
publication for that endpoint while raw capture continues; adjusted prices
cannot enter a raw mapping; times and finality are never invented.
Not covered: HTTP, persistence, identity creation (``resolve`` is injected).
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .numeric import (
    NumericError,
    check_storage_precision,
    dec,
    dec_from_float,
    div,
    mul,
    to_int_exact,
)

CST = ZoneInfo("Asia/Shanghai")
PRICE_TARGETS = ("open_price_raw", "high_price_raw", "low_price_raw", "close_price_raw",
                 "last_price_raw", "bid_price_raw", "ask_price_raw", "exchange_reference_price_raw",
                 "prev_close_price_raw", "open_price", "high_price", "low_price", "close_price")
QUANTITY_TARGETS = ("volume_shares", "trade_count", "turnover_amount_cny", "turnover_ratio",
                    "bid_volume_shares", "ask_volume_shares", "cumulative_volume_shares", "cumulative_turnover_cny")


class MappingDrift(ValueError):
    """Source shape or unit documentation no longer matches the registered profile."""


def canonical_json(value: object) -> str:
    def default(v: object) -> str:
        if isinstance(v, Decimal):
            return str(v)
        raise TypeError("unsupported_json_value")

    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False, default=default)


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def profile_hash(profile: dict) -> str:
    """Mapping rule digest stored in core.source_endpoint.mapping_hash."""
    stable = {k: v for k, v in profile.items() if k not in ("synthetic_raw", "canonical_expected", "official_source")}
    return sha256_json(stable)


def aware(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ValueError("timezone_required")
    return result


def _parse_cst(text: str, fmt: str) -> datetime:
    return datetime.strptime(text, fmt).replace(tzinfo=CST).astimezone(timezone.utc)


_TIME_OPS = {
    "parse_datetime_cst_compact12": "%Y%m%d%H%M",
    "parse_datetime_cst_compact14": "%Y%m%d%H%M%S",
    "parse_datetime_cst_iso_seconds": "%Y-%m-%d %H:%M:%S",
    "parse_datetime_cst_iso_minutes": "%Y-%m-%d %H:%M",
}
_DATE_OPS = {"parse_date_yyyymmdd": "%Y%m%d", "parse_date_iso": "%Y-%m-%d"}


def _transform(value: Any, op: dict) -> tuple[Decimal, str | None]:
    """Return (numeric, precision_note)."""
    kind = op["op"]
    note = None
    if kind in ("decimal_from_float64", "float64_require_integer"):
        if isinstance(value, float):
            numeric = dec_from_float(value)
            note = "precision_limited: source value is IEEE float64; shortest round-trip decimal"
            scale = op.get("round_scale")
            if scale is not None and -numeric.as_tuple().exponent > scale:
                # declared rule: digits beyond the storage scale are float64 noise, not source precision
                numeric = numeric.quantize(Decimal(1).scaleb(-scale), rounding="ROUND_HALF_EVEN")
                note += f"; rounded half-even to {scale} decimals (storage scale)"
        else:
            numeric = dec(value)
    else:
        numeric = dec(value)
    if kind in ("decimal_multiply", "decimal_multiply_then_require_integer"):
        numeric = mul(numeric, dec(op["factor"]))
    elif kind == "decimal_divide":
        numeric = div(numeric, dec(op["factor"]))
    elif kind == "float64_multiply_then_require_integer":
        numeric = mul(dec_from_float(value) if isinstance(value, float) else dec(value), dec(op["factor"]))
        note = "precision_limited: source value is IEEE float64" if isinstance(value, float) else None
    elif kind not in ("decimal_exact", "require_integer", "decimal_from_float64", "float64_require_integer"):
        raise MappingDrift(f"unsupported_transform:{kind}")
    return numeric, note


_INTEGER_OPS = ("require_integer", "decimal_multiply_then_require_integer",
                "float64_multiply_then_require_integer", "float64_require_integer")


def normalize(profile: dict, raw: dict, context: dict,
              resolve: Callable[[str, str, str | None], str]) -> dict:
    """Map one source record to canonical fields without inventing facts.

    ``resolve(identifier, trade_date, market)`` is a supplied point-in-time
    identity lookup, never a ticker-prefix guess. Returns a dict with canonical
    values, per-field ``field_quality`` (contract FieldQualityEntry shape),
    ``quality_status`` and issues.
    """
    for key in ("source_id", "endpoint_id", "source_schema_version", "mapping_version"):
        if context.get(key) != profile[key]:
            raise MappingDrift(f"profile_mismatch:{key}")
    if context.get("adjustment_mode", "raw") != profile.get("adjustment_mode", "raw") or context.get("adjust", ""):
        raise MappingDrift("adjusted_data_cannot_enter_raw_dataset")
    first, ingested, available = (aware(context[k]) for k in ("first_seen_at", "ingested_at", "available_at"))
    if not first <= ingested <= available:
        raise ValueError("invalid_availability_order")
    operations = profile["operations"]
    missing_keys = [op["source"] for op in operations if op["source"] not in raw]
    if missing_keys:
        raise MappingDrift("missing_mapped_keys:" + ",".join(missing_keys))
    payload_hash = sha256_json(raw)
    raw_object_id = context.get("raw_object_id")
    result: dict[str, Any] = {
        "currency": "CNY", "adjustment_mode": profile.get("adjustment_mode", "raw"), "is_final": False,
        "source_id": profile["source_id"], "endpoint_id": profile["endpoint_id"],
        "source_schema_version": profile["source_schema_version"],
        "mapping_version": profile["mapping_version"],
        "raw_payload_hash": payload_hash,
        "first_seen_at": context["first_seen_at"], "ingested_at": context["ingested_at"],
        "available_at": context["available_at"],
        "published_at": context.get("published_at"),
        "event_time": context.get("event_time"),
        "issues": [],
    }
    statuses: dict[str, dict] = {}
    for key in ("published_at", "event_time"):
        if result[key] is not None:
            aware(result[key])

    trade_date = context.get("trade_date")
    # Time / date operations first: they define the trading date used by identity lookup.
    for op in operations:
        kind, src = op["op"], op["source"]
        value = raw[src]
        if value is None:
            continue
        if kind in _DATE_OPS:
            if not isinstance(value, str):
                raise MappingDrift("date_must_be_source_string")
            parsed = datetime.strptime(value, _DATE_OPS[kind]).date()
            if trade_date and parsed.isoformat() != trade_date:
                raise MappingDrift("trade_date_context_conflict")
            trade_date = parsed.isoformat()
        elif kind in _TIME_OPS:
            if not isinstance(value, str):
                raise MappingDrift("time_must_be_source_string")
            instant = _parse_cst(value, _TIME_OPS[kind])
            local_date = instant.astimezone(CST).date().isoformat()
            if trade_date and local_date != trade_date:
                raise MappingDrift("trade_date_context_conflict")
            trade_date = local_date
            result["event_time"] = instant.isoformat().replace("+00:00", "Z")
    if not trade_date:
        raise MappingDrift("explicit_trade_date_required")
    date.fromisoformat(trade_date)
    result["trade_date"] = trade_date
    result["time_quality"] = "observed" if result["event_time"] else "missing"

    def lineage(op: dict, status: str, reason: str | None, raw_value: Any, note: str | None) -> dict:
        shown = raw_value if isinstance(raw_value, (str, int, float, bool)) or raw_value is None else str(raw_value)
        return {
            "status": status, "reason": reason, "raw_object_id": raw_object_id,
            "source_id": profile["source_id"], "endpoint_id": profile["endpoint_id"],
            "mapping_version": profile["mapping_version"],
            "raw_value": shown,
            "raw_unit": op.get("raw_unit"),
            "conversion": op["op"] + (f"*{op['factor']}" if "factor" in op else ""),
            "value_origin": "primary_source" if status != "missing" else "not_available",
            "precision_note": note,
        }

    for op in operations:
        src, target, kind = op["source"], op["target"], op["op"]
        value = raw[src]
        if kind in _DATE_OPS or kind in _TIME_OPS:
            if value is None:
                result[target] = None
                statuses[target] = lineage(op, "missing", "source_value_null", value, None)
            else:
                result[target] = trade_date if target == "trade_date" else result["event_time"]
                statuses[target] = lineage(op, "observed", None, value, None)
            continue
        if value is None:
            result[target] = None
            statuses[target] = lineage(op, "missing", "source_value_null", value, None)
            continue
        if op.get("zero_is_missing") and not isinstance(value, bool) and str(value).strip() in ("0", "0.0", "0.000", "0.00", ""):
            result[target] = None
            statuses[target] = lineage(op, "missing", "zero_means_no_quote", value, None)
            continue
        note = None
        try:
            if kind.startswith("resolve_identifier"):
                if not isinstance(value, str):
                    raise NumericError("identifier_must_preserve_string")
                market = context.get("market")
                if kind == "resolve_identifier_with_request_market_and_trade_date" and not market:
                    raise NumericError("bare_identifier_requires_market")
                converted = resolve(value, trade_date, market)
                if not converted:
                    raise NumericError("unresolved_identifier")
            else:
                numeric, note = _transform(value, op)
                check_storage_precision(numeric)
                if target in PRICE_TARGETS and numeric <= 0:
                    raise NumericError("non_positive_price")
                if target in QUANTITY_TARGETS and numeric < 0:
                    raise NumericError("negative_quantity_or_amount")
                converted = to_int_exact(numeric) if kind in _INTEGER_OPS else numeric
            result[target] = converted
            statuses[target] = lineage(op, "observed", None, value, note)
        except (NumericError, ValueError) as exc:
            if isinstance(exc, MappingDrift):
                raise
            result[target] = None
            statuses[target] = lineage(op, "invalid", str(exc), value, note)
            result["issues"].append(f"{target}:{exc}")

    for target, reason in profile.get("absent_fields", {}).items():
        # The endpoint never carries this field: it is MISSING (never 0) and keeps the record partial.
        result[target] = None
        statuses[target] = {
            "status": "missing", "reason": reason, "raw_object_id": raw_object_id, "source_id": profile["source_id"],
            "endpoint_id": profile["endpoint_id"], "mapping_version": profile["mapping_version"], "raw_value": None,
            "raw_unit": None, "conversion": None, "value_origin": "not_available", "precision_note": None}
    for names in (("open_price_raw", "high_price_raw", "low_price_raw", "close_price_raw"),
                  ("open_price", "high_price", "low_price", "close_price")):
        prices = [result.get(k) for k in names]
        if all(p is not None for p in prices):
            o, h, l, c = prices
            if not l <= min(o, c) <= max(o, c) <= h:
                result["issues"].append("ohlc_order_violation")
                for k in names:
                    statuses[k] = {**statuses[k], "status": "invalid", "reason": "ohlc_order_violation"}
    recognized = ({op["source"] for op in operations} | set(profile.get("endpoint_optional_fields", {}))
                  | set(profile.get("recognized_unmapped", [])))
    result["extra_fields"] = {k: deepcopy(v) for k, v in raw.items() if k not in recognized}
    result["schema_drift"] = "additive_fields" if result["extra_fields"] else "none"
    volume, amount = result.get("volume_shares"), result.get("turnover_amount_cny")
    inconsistent_zero = volume is not None and amount is not None and ((volume == 0) != (amount == 0))
    if inconsistent_zero:
        result["issues"].append("zero_volume_amount_contradiction")
    status_values = {v["status"] for v in statuses.values()}
    result["quality_status"] = ("invalid" if "invalid" in status_values or inconsistent_zero
                                else "partial" if "missing" in status_values else "valid")
    result["normalization_valid"] = result["quality_status"] == "valid"
    result["field_quality"] = statuses
    if context.get("finalization_evidence_id") and context.get("finalization_confirmed") is True:
        result["is_final"] = True
        result["finalization_evidence_id"] = context["finalization_evidence_id"]
    return result


def comparable(left: dict, right: dict) -> bool:
    """Unit equality alone does not establish the same economic observation."""
    keys = ("instrument_id", "trade_date", "timeframe", "session_scope", "event_time",
            "adjustment_mode", "adjustment_version", "is_final")
    return all(k in left and k in right and left[k] is not None and right[k] is not None
               and left[k] == right[k] for k in keys)


def detect_unit_change(volume_shares: int, amount_cny: Decimal, reference_price: Decimal,
                       tolerance: Decimal = Decimal("0.35")) -> str | None:
    """Detect lots-vs-shares / yuan-vs-thousand-yuan mix-ups from implied VWAP.

    ``amount / volume`` must lie near the bar price. Ratios near 100 or 1/100
    (lots read as shares or the reverse) or 1000 are flagged
    ``unit_change_suspected:<label>``; ``None`` means plausible.
    """
    if volume_shares <= 0 or amount_cny <= 0 or reference_price <= 0:
        return None
    ratio = (amount_cny / Decimal(volume_shares)) / reference_price
    if abs(ratio - 1) <= tolerance:
        return None
    for label, scale in (("lots_as_shares", Decimal(100)), ("shares_as_lots", Decimal(1) / 100),
                         ("thousand_yuan_as_yuan", Decimal(1000)), ("yuan_as_thousand_yuan", Decimal(1) / 1000)):
        if abs(ratio / scale - 1) <= tolerance:
            return f"unit_change_suspected:{label}"
    return "unit_change_suspected:unknown_scale"
