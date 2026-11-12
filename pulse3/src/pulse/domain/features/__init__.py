"""Shared feature layer (W04, spec FINAL §4.1/§5/§8, FEATURE_STRATEGY_PROTOCOLS).

Charts, ranking, plans and backtests all call these functions so one indicator
has exactly one definition, one FeatureKey and one cache identity.
"""
from .definitions import DEFINITIONS, FeatureDefinition, compute, definition, make_key
from .indicators import atr, boll, ema, hhv, kdj, llv, ma, macd, relative_strength, ret_std, roc, rsi, volume_ratio
from .key import FeatureKey, key_hash, parameter_digest
from .store import SingleFlightFeatureStore

__all__ = [
    "DEFINITIONS", "FeatureDefinition", "FeatureKey", "SingleFlightFeatureStore", "atr", "compute", "definition",
    "ema", "key_hash", "ma", "macd", "make_key", "parameter_digest", "relative_strength", "rsi", "volume_ratio",
    "boll", "hhv", "kdj", "llv", "ret_std", "roc",
]
