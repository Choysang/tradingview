"""Versioned A-share trading rules: price limits, lot sizes, T+1 dates, fees.

Why: execution_standard.md section 3 - rules live in one registry with effective
dates, not as strategy constants. Pure domain code (no IO).
Invariants: every lookup is deterministic for (instrument, date); unverified
combinations raise instead of guessing. Not covered: see each module docstring.
"""
from .calendar import ExplicitCalendar, TradingCalendar, WeekdayOnlyCalendar
from .costs import (CostEngine, CostProfile, CostProfileRegistry, FeeIncrement, FeeRule,
                    a_share_documented_partial, synthetic_fixed_fee)
from .instruments import Board, Exchange, InstrumentKind, InstrumentSpec, UnsupportedInstrument
from .lots import LOT_RULES_VERSION, eligible_on_for_buy, lot_step, validate_quantity
from .price_limits import (PRICE_LIMIT_RULES_VERSION, PriceBand, PriceLimitRegistry,
                           PriceLimitRule, RuleNotVerified)

MARKET_RULES_VERSION = f"{PRICE_LIMIT_RULES_VERSION}+{LOT_RULES_VERSION}"

__all__ = [
    "Board", "CostEngine", "CostProfile", "CostProfileRegistry", "Exchange", "ExplicitCalendar",
    "FeeIncrement", "FeeRule", "InstrumentKind", "InstrumentSpec", "LOT_RULES_VERSION",
    "MARKET_RULES_VERSION", "PRICE_LIMIT_RULES_VERSION", "PriceBand", "PriceLimitRegistry",
    "PriceLimitRule", "RuleNotVerified", "TradingCalendar", "UnsupportedInstrument",
    "WeekdayOnlyCalendar", "a_share_documented_partial", "eligible_on_for_buy", "lot_step",
    "synthetic_fixed_fee", "validate_quantity",
]
