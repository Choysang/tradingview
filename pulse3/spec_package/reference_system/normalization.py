"""Executable examples for three documented source profiles; no network connector.

Raw storage must precede this function in production. The digest below describes
the supplied JSON object, not an assertion that original HTTP bytes were saved.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, Inexact, Rounded, localcontext
import hashlib
import json
from typing import Callable


class MappingDrift(ValueError):
    pass


def json_scalar(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError("unsupported_json_value")


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False,
                                    default=json_scalar).encode()).hexdigest()


def exact(value: object) -> Decimal:
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("precision_limited_or_invalid_type")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid_decimal") from exc
    if not result.is_finite():
        raise ValueError("non_finite")
    if len(result.as_tuple().digits) > 128 or abs(result.as_tuple().exponent) > 128:
        raise ValueError("source_numeric_precision_out_of_profile")
    return result


def transform_number(value, op):
    with localcontext() as ctx:
        ctx.prec = 160
        ctx.traps[Inexact] = True
        ctx.traps[Rounded] = True
        numeric = exact(value)
        kind = op["op"]
        if kind in ("decimal_multiply", "decimal_multiply_then_require_integer"):
            numeric *= exact(op["factor"])
        elif kind == "decimal_divide":
            numeric /= exact(op["factor"])
        elif kind not in ("decimal_exact", "require_integer"):
            raise MappingDrift("unsupported_transform")
        return numeric


def require_storage_precision(numeric, target):
    digits = numeric.as_tuple().digits
    trailing = 0
    for digit in reversed(digits):
        if digit != 0:
            break
        trailing += 1
    fraction = max(0, -numeric.as_tuple().exponent - trailing)
    integer = max(0, numeric.adjusted() + 1) if numeric else 0
    max_integer, max_fraction = 26, 12  # The production seed uses numeric(38,12).
    if integer > max_integer or fraction > max_fraction:
        raise ValueError("canonical_numeric_scale_or_precision_exceeded")


def aware(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ValueError("timezone_required")
    return result


def normalize(profile: dict, raw: dict, context: dict,
              resolve: Callable[[str, str, str | None], str]) -> dict:
    """Map one observation without inventing missing facts or market identity.

resolve is a supplied point-in-time identity lookup, not a ticker-prefix heuristic.
Null mapped values remain null; an absent mapped key is schema drift. Float input
is retained in raw but not treated as recovered exact source precision.
"""
    for key in ("source_id", "endpoint_id", "source_schema_version", "mapping_version"):
        if context.get(key) != profile[key]:
            raise MappingDrift(f"profile_mismatch:{key}")
    if context.get("adjustment_mode", "raw") != "raw" or context.get("adjust", ""):
        raise MappingDrift("adjusted_data_cannot_enter_raw_dataset")
    first, ingested, available = (aware(context[k]) for k in
                                  ("first_seen_at", "ingested_at", "available_at"))
    if not first <= ingested <= available:
        raise ValueError("invalid_availability_order")
    operations = profile["operations"]
    missing_keys = [op["source"] for op in operations if op["source"] not in raw]
    if missing_keys:
        raise MappingDrift("missing_mapped_keys:" + ",".join(missing_keys))
    payload_hash = digest(raw)
    result = {"currency": "CNY", "adjustment_mode": "raw", "is_final": False,
              "source_id": profile["source_id"], "endpoint_id": profile["endpoint_id"],
              "source_schema_version": profile["source_schema_version"],
              "mapping_version": profile["mapping_version"],
              "raw_payload_hash": payload_hash, "raw": deepcopy(raw),
              "first_seen_at": context["first_seen_at"], "ingested_at": context["ingested_at"],
              "available_at": context["available_at"],
              "published_at": context.get("published_at"),
              "event_time": context.get("event_time"),
              "field_status": {}, "field_lineage": {}, "issues": []}
    for key in ("published_at", "event_time"):
        if result[key] is not None:
            aware(result[key])
    if profile["endpoint_id"] == "rt_k" and raw.get("trade_time"):
        # Parsing provider-specific formats requires a versioned time adapter;
        # caller must supply the parsed event_time, never an invented timestamp.
        if result["event_time"] is None:
            result["issues"].append("trade_time_needs_time_adapter")
    result["time_quality"] = "observed" if result["event_time"] else "missing"
    trade_date = context.get("trade_date")
    for op in operations:
        if op["op"] in ("parse_date_yyyymmdd", "parse_date_iso") and raw[op["source"]] is not None:
            value = raw[op["source"]]
            if not isinstance(value, str):
                raise MappingDrift("date_must_be_source_string")
            parsed = (datetime.strptime(value, "%Y%m%d").date() if op["op"] == "parse_date_yyyymmdd"
                      else date.fromisoformat(value))
            if trade_date and parsed.isoformat() != trade_date:
                raise MappingDrift("trade_date_context_conflict")
            trade_date = parsed.isoformat()
    if not trade_date:
        raise MappingDrift("explicit_trade_date_required")
    date.fromisoformat(trade_date)
    result["trade_date"] = trade_date
    for op in operations:
        source, target, kind = op["source"], op["target"], op["op"]
        value = raw[source]
        result["field_lineage"][target] = {
            "source_id": profile["source_id"], "endpoint_id": profile["endpoint_id"],
            "raw_payload_hash": payload_hash, "source_field": source,
            "transform": kind, "factor": op.get("factor"),
            "mapping_version": profile["mapping_version"],
            "available_at": context["available_at"]}
        if value is None:
            result[target] = None
            result["field_status"][target] = "missing"
            continue
        try:
            if kind.startswith("resolve_identifier"):
                if not isinstance(value, str):
                    raise ValueError("identifier_must_preserve_string")
                if kind == "resolve_identifier_with_request_market_and_trade_date" and not context.get("market"):
                    raise ValueError("bare_identifier_requires_market")
                converted = resolve(value, trade_date, context.get("market"))
                if not converted:
                    raise ValueError("unresolved_identifier")
            elif kind.startswith("parse_date"):
                converted = trade_date
            else:
                numeric = transform_number(value, op)
                require_storage_precision(numeric, target)
                if target.endswith("price_raw") and numeric <= 0:
                    raise ValueError("non_positive_price")
                if target in ("volume_shares", "trade_count", "turnover_amount_cny", "turnover_ratio") and numeric < 0:
                    raise ValueError("negative_quantity_or_amount")
                if kind in ("require_integer", "decimal_multiply_then_require_integer"):
                    if numeric != numeric.to_integral_value():
                        raise ValueError("fractional_shares_or_count")
                    if numeric > 9007199254740991:
                        raise ValueError("json_safe_integer_exceeded")
                    converted = int(numeric)
                else:
                    converted = format(numeric, "f")
                    if target == "turnover_amount_cny" and numeric.as_tuple().exponent > -2:
                        converted = format(numeric, ".2f")
            result[target] = converted
            result["field_status"][target] = "observed"
        except (ValueError, KeyError, InvalidOperation, Inexact, Rounded) as exc:
            if isinstance(exc, MappingDrift):
                raise
            result[target] = None
            result["field_status"][target] = "invalid"
            result["issues"].append(f"{target}:{exc}")
    price_names = [k + "_price_raw" for k in ("open", "high", "low", "close")]
    if all(result.get(k) is not None for k in price_names):
        o, h, l, c = (Decimal(result[k]) for k in price_names)
        if not l <= min(o, c) <= max(o, c) <= h:
            result["issues"].append("ohlc_order_violation")
            for k in price_names:
                result["field_status"][k] = "invalid"
    recognized = {op["source"] for op in operations} | set(profile.get("endpoint_optional_fields", {}))
    if profile["endpoint_id"] == "rt_k":
        recognized |= {"name", "pre_close", "ask_price1", "ask_volume1", "bid_price1", "bid_volume1", "trade_time"}
    result["extra_fields"] = {k: deepcopy(v) for k, v in raw.items() if k not in recognized}
    result["schema_drift"] = "additive_fields" if result["extra_fields"] else "none"
    statuses = set(result["field_status"].values())
    volume, amount = result.get("volume_shares"), result.get("turnover_amount_cny")
    inconsistent_zero = volume is not None and amount is not None and ((volume == 0) != (Decimal(amount) == 0))
    if inconsistent_zero:
        result["issues"].append("zero_volume_amount_contradiction")
    result["quality_status"] = "invalid" if "invalid" in statuses or inconsistent_zero else ("partial" if "missing" in statuses else "valid")
    result["normalization_valid"] = result["quality_status"] == "valid"
    result["field_quality"] = {key: {"status": status, "lineage": result["field_lineage"][key]}
                               for key, status in result["field_status"].items()}
    del result["field_status"], result["field_lineage"]
    # Provider daily dates or successful HTTP requests do not establish finality.
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
