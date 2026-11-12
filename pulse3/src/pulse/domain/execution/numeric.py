"""Numeric and identity primitives of the one economic ledger.

Why: execution/unified_ledger.py (v3.2 package) is the sole ledger reference;
these helpers are its numeric policy ``decimal50-scale12-half-even-v1``, ported
unchanged. Invariants: money is Decimal (never float), at most 12 decimal places
and inside numeric(38,12); quantities are integer shares in the bigint range.
Not covered: currencies other than CNY, fee schedules (see market_rules).
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN
import json
from zoneinfo import ZoneInfo

D = Decimal
ZERO = D('0')
SCALE = D('0.000000000001')
POLICY_VERSION = 'pulse-economic-cny-weighted-average-gross-v1'
NUMERIC_POLICY_VERSION = 'decimal50-scale12-half-even-v1'
MARKET_TIMEZONE = ZoneInfo('Asia/Shanghai')


def decimal(value: str | Decimal, name: str = 'amount', *, positive=False,
            nonnegative=True) -> Decimal:
    if not isinstance(value, (str, Decimal)):
        raise TypeError(f'{name}: require decimal string or Decimal, never float')
    result = D(value)
    if not result.is_finite():
        raise ValueError(f'{name}: finite value required')
    if (positive and result <= 0) or (nonnegative and result < 0):
        raise ValueError(f'{name}: invalid sign')
    if result.as_tuple().exponent < -12:
        raise ValueError(f'{name}: at most 12 decimal places')
    if result != 0 and result.adjusted() >= 26:
        raise ValueError(f'{name}: outside numeric(38,12) range')
    return result


def quantity(value: int, name='quantity', *, positive=False) -> int:
    if type(value) is not int or value < 0 or value > 2**63-1 or (positive and value == 0):
        raise ValueError(f'{name}: positive/nonnegative integer shares required')
    return value


def identity(value: str, name='id') -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name}: nonempty string required')
    return value


def quantize(value: Decimal) -> Decimal:
    return value.quantize(SCALE, rounding=ROUND_HALF_EVEN)


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def display(value: Decimal) -> str:
    return format(value, 'f')

