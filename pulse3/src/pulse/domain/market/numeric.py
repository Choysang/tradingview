"""Exact numeric helpers shared by the market domain (spec FINAL §4, RECON §4).

Why: prices, amounts and ratios are Decimal (context precision 50, ROUND_HALF_EVEN);
quantities are integer shares. Floats from a source are never silently promoted
to "exact": they are converted through their shortest round-trip repr and the
result is flagged ``precision_limited`` by the caller.

Invariants: no function here returns a float; ``None`` stays ``None``.
Not covered: currency conversion, rounding to tick size (market_rules package).
"""
from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation

CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)
MAX_SAFE_INTEGER = 9007199254740991
STORAGE_MAX_INTEGER_DIGITS = 26  # numeric(38,12)
STORAGE_MAX_FRACTION_DIGITS = 12


class NumericError(ValueError):
    pass


def dec(value: object) -> Decimal:
    """Exact Decimal from str/int/Decimal. Floats and bools are rejected."""
    if isinstance(value, bool) or isinstance(value, float) or not isinstance(value, (str, int, Decimal)):
        raise NumericError("precision_limited_or_invalid_type")
    try:
        result = Decimal(value) if not isinstance(value, Decimal) else value
    except InvalidOperation as exc:
        raise NumericError("invalid_decimal") from exc
    if not result.is_finite():
        raise NumericError("non_finite")
    t = result.as_tuple()
    if len(t.digits) > 128 or abs(int(t.exponent)) > 128:
        raise NumericError("source_numeric_precision_out_of_profile")
    return result


def dec_from_float(value: float) -> Decimal:
    """Float64 -> Decimal via shortest round-trip repr (precision is source-limited)."""
    if isinstance(value, bool) or not isinstance(value, float):
        raise NumericError("not_a_float")
    if value != value or value in (float("inf"), float("-inf")):
        raise NumericError("non_finite")
    return Decimal(repr(value))


def check_storage_precision(value: Decimal) -> None:
    t = value.as_tuple()
    digits = t.digits
    trailing = 0
    for d in reversed(digits):
        if d != 0:
            break
        trailing += 1
    fraction = max(0, -int(t.exponent) - trailing)
    integer = max(0, value.adjusted() + 1) if value else 0
    if integer > STORAGE_MAX_INTEGER_DIGITS or fraction > STORAGE_MAX_FRACTION_DIGITS:
        raise NumericError("canonical_numeric_scale_or_precision_exceeded")


def to_int_exact(value: Decimal) -> int:
    if value != value.to_integral_value():
        raise NumericError("fractional_shares_or_count")
    n = int(value)
    if abs(n) > MAX_SAFE_INTEGER:
        raise NumericError("json_safe_integer_exceeded")
    return n


def mul(a: Decimal, b: Decimal) -> Decimal:
    return CONTEXT.multiply(a, b)


def div(a: Decimal, b: Decimal) -> Decimal:
    return CONTEXT.divide(a, b)


def add(a: Decimal, b: Decimal) -> Decimal:
    return CONTEXT.add(a, b)


def sub(a: Decimal, b: Decimal) -> Decimal:
    return CONTEXT.subtract(a, b)


def quantize_money(value: Decimal, places: int = 6) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN, context=CONTEXT)
