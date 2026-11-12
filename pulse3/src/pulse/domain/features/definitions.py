"""Feature definition registry: id, version, parameters, outputs, warm-up, digest.

Why: ``implementation_digest`` identifies the code that produced a value (the
sha256 of the kernel's source), ``definition_version`` the semantics (EMA seed,
histogram multiplier, null policy), ``parameter_digest`` the canonical parameter
JSON. Consumers call :func:`compute` and :func:`make_key` instead of calling
kernels by name, so chart/ranking/plan/backtest share one identity.

Invariants: unknown feature ids and unknown parameters are errors (no silent
defaults); outputs are the same length as the input series.
Not covered: dependency graph between features (declared via ``depends_on``,
folded into ``dependency_digest``).
"""
from __future__ import annotations

import hashlib
import inspect
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

from . import indicators as ind
from .key import FeatureKey, parameter_digest

NUMERIC_POLICY = "float64-v1"


@dataclass(frozen=True)
class Series:
    """Aligned float series of one instrument (None = missing, never 0)."""
    dates: tuple[str, ...]
    open: tuple[float | None, ...]
    high: tuple[float | None, ...]
    low: tuple[float | None, ...]
    close: tuple[float | None, ...]
    volume: tuple[float | None, ...]
    benchmark_close: tuple[float | None, ...] | None = None

    def __post_init__(self) -> None:
        n = len(self.dates)
        for name in ("open", "high", "low", "close", "volume"):
            if len(getattr(self, name)) != n:
                raise ValueError(f"{name} length differs from dates")
        if self.benchmark_close is not None and len(self.benchmark_close) != n:
            raise ValueError("benchmark_close must be aligned to dates")


@dataclass(frozen=True)
class FeatureDefinition:
    feature_id: str
    definition_version: str
    defaults: Mapping[str, Any]
    outputs: tuple[str, ...]
    run: Callable[[Series, dict], dict[str, list]]
    warmup: Callable[[dict], int]
    kernel: Callable
    depends_on: tuple[str, ...] = ()

    @property
    def implementation_digest(self) -> str:
        src = inspect.getsource(self.kernel) + inspect.getsource(self.run) + self.definition_version
        return hashlib.sha256(src.encode("utf-8")).hexdigest()

    def resolve_params(self, params: Mapping[str, Any] | None) -> dict:
        params = dict(params or {})
        unknown = set(params) - set(self.defaults)
        if unknown:
            raise ValueError(f"unknown parameters for {self.feature_id}: {sorted(unknown)}")
        return {**self.defaults, **params}

    @property
    def dependency_digest(self) -> str:
        return hashlib.sha256("|".join(sorted(self.depends_on) or ["none"]).encode()).hexdigest()


def _defs() -> dict[str, FeatureDefinition]:
    d: dict[str, FeatureDefinition] = {}

    def add(fd: FeatureDefinition) -> None:
        d[fd.feature_id] = fd

    add(FeatureDefinition("ma", "v1", {"n": 20}, ("ma",),
                          lambda s, p: {"ma": ind.ma(s.close, p["n"])}, lambda p: p["n"] - 1, ind.ma))
    add(FeatureDefinition("ema", "v1", {"n": 12, "seed": "sma_seed"}, ("ema",),
                          lambda s, p: {"ema": ind.ema(s.close, p["n"], p["seed"])},
                          lambda p: p["n"] - 1 if p["seed"] == "sma_seed" else 0, ind.ema))
    add(FeatureDefinition("macd", "v1", {"fast": 12, "slow": 26, "signal": 9, "ema_seed": "sma_seed", "hist_multiplier": 1},
                          ("macd", "signal", "hist"),
                          lambda s, p: ind.macd(s.close, p["fast"], p["slow"], p["signal"], p["ema_seed"], p["hist_multiplier"]),
                          lambda p: p["slow"] + p["signal"] - 2, ind.macd, ("ema",)))
    add(FeatureDefinition("rsi", "v1", {"n": 14}, ("rsi",),
                          lambda s, p: {"rsi": ind.rsi(s.close, p["n"])}, lambda p: p["n"], ind.rsi))
    add(FeatureDefinition("atr", "v1", {"n": 14}, ("atr",),
                          lambda s, p: {"atr": ind.atr(s.high, s.low, s.close, p["n"])}, lambda p: p["n"], ind.atr))
    add(FeatureDefinition("volume_ratio", "v1", {"n": 5}, ("volume_ratio",),
                          lambda s, p: {"volume_ratio": ind.volume_ratio(s.volume, p["n"])}, lambda p: p["n"], ind.volume_ratio))
    def run_rs(s: Series, p: dict) -> dict[str, list]:
        if s.benchmark_close is None:
            raise ValueError("relative_strength needs benchmark_close")
        return {"relative_strength": ind.relative_strength(s.close, s.benchmark_close, p["n"])}

    add(FeatureDefinition("relative_strength", "v1", {"n": 20}, ("relative_strength",), run_rs,
                          lambda p: p["n"], ind.relative_strength))

    # ---- ext group B shared kernels (BOLL/KDJ + rolling helpers; definition_version v1) ----
    add(FeatureDefinition("boll", "v1", {"n": 20, "k": 2.0}, ("mid", "upper", "lower"),
                          lambda s, p: ind.boll(s.close, p["n"], p["k"]), lambda p: p["n"] - 1, ind.boll))
    add(FeatureDefinition("kdj", "v1", {"n": 9}, ("k", "d", "j"),
                          lambda s, p: ind.kdj(s.high, s.low, s.close, p["n"]), lambda p: p["n"] - 1, ind.kdj))
    add(FeatureDefinition("roc", "v1", {"n": 5}, ("roc",),
                          lambda s, p: {"roc": ind.roc(s.close, p["n"])}, lambda p: p["n"], ind.roc))
    add(FeatureDefinition("hhv", "v1", {"n": 20, "field": "high"}, ("hhv",),
                          lambda s, p: {"hhv": ind.hhv(getattr(s, p["field"]), p["n"])}, lambda p: p["n"] - 1, ind.hhv))
    add(FeatureDefinition("llv", "v1", {"n": 20, "field": "low"}, ("llv",),
                          lambda s, p: {"llv": ind.llv(getattr(s, p["field"]), p["n"])}, lambda p: p["n"] - 1, ind.llv))
    add(FeatureDefinition("ret_std", "v1", {"n": 20}, ("ret_std",),
                          lambda s, p: {"ret_std": ind.ret_std(s.close, p["n"])}, lambda p: p["n"], ind.ret_std))
    add(FeatureDefinition("up_streak", "v1", {}, ("up_streak",),
                          lambda s, p: {"up_streak": ind.up_streak(s.close)}, lambda p: 1, ind.up_streak))
    add(FeatureDefinition("limit_streak", "v1", {"limit_pct": 10}, ("limit_streak",),
                          lambda s, p: {"limit_streak": ind.limit_streak(s.close, p["limit_pct"])}, lambda p: 1, ind.limit_streak))
    add(FeatureDefinition("limit_count", "v1", {"limit_pct": 10, "n": 250}, ("limit_count",),
                          lambda s, p: {"limit_count": ind.limit_count(s.close, p["limit_pct"], p["n"])}, lambda p: p["n"], ind.limit_count))
    return d


DEFINITIONS: dict[str, FeatureDefinition] = _defs()


def definition(feature_id: str) -> FeatureDefinition:
    try:
        return DEFINITIONS[feature_id]
    except KeyError as exc:
        raise KeyError(f"unknown feature {feature_id!r}") from exc


def compute(feature_id: str, series: Series, params: Mapping[str, Any] | None = None) -> dict[str, list]:
    fd = definition(feature_id)
    resolved = fd.resolve_params(params)
    out = fd.run(series, resolved)
    for name, values in out.items():
        if len(values) != len(series.dates):
            raise AssertionError(f"{feature_id}.{name} length mismatch")
    return out


def make_key(feature_id: str, params: Mapping[str, Any] | None, *, instrument_ids: tuple[str, ...], timeframe: str,
             adjustment_mode: str, snapshot_id: str, decision_as_of: datetime, availability_policy_version: str,
             session_policy_version: str, input_revision: str, input_schema_version: str,
             universe_policy_version: str, calendar_version: str, adjustment_version: str,
             market_rules_version: str) -> FeatureKey:
    """Build the full 19-dimension key; unused dimensions must be passed as an explicit ``not-applicable``."""
    fd = definition(feature_id)
    return FeatureKey(
        feature_id=feature_id, definition_version=fd.definition_version,
        implementation_digest=fd.implementation_digest, parameter_digest=parameter_digest(fd.resolve_params(params)),
        instrument_ids=instrument_ids, timeframe=timeframe, adjustment_mode=adjustment_mode, snapshot_id=snapshot_id,
        decision_as_of=decision_as_of, availability_policy_version=availability_policy_version,
        session_policy_version=session_policy_version, input_revision=input_revision,
        input_schema_version=input_schema_version, dependency_digest=fd.dependency_digest,
        universe_policy_version=universe_policy_version, calendar_version=calendar_version,
        adjustment_version=adjustment_version, market_rules_version=market_rules_version,
        numeric_policy_version=NUMERIC_POLICY)
