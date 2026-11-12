"""Playbook definition types (pure data + one pure signal function per playbook).

Why: LEGACY inventory §5 - "One playbook = rule text plus pure signal function over a research panel; no-look-ahead
test". A pulse3 playbook is a STRATEGY DEFINITION candidate (role selection) whose evidence label is ``未检验`` until it
passes the unified experiment path; the old backtest numbers are NOT carried over (the old files are read only as historical
evidence of the rule semantics).
Invariants: ``signal(view, params)`` returns one value per bar of the view - a positive score where the rule fires on the
COMPLETED bar, else None; it reads indexes <= i only; entry is the next session's open (the replay's job, not the signal's);
``requires`` lists data this playbook needs that the platform may not have - an experiment on a playbook whose required data
is missing is recorded as a FAILED trial (``data_not_available:<name>``), never as a fabricated result.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .view import InstrumentView

SignalFn = Callable[[InstrumentView, dict[str, float]], list]

FAMILIES = {
    "limit_relay": "连板接力", "trend_breakout": "趋势与突破", "academic_factor": "学术因子", "oversold": "超跌与震荡",
    "sentiment_cycle": "情绪周期与择时", "crowd": "抱团与联动", "factor_composite": "因子复合", "technical": "技术分析",
}


@dataclass(frozen=True)
class Param:
    key: str
    label: str
    value: float
    low: float
    high: float
    step: float


@dataclass(frozen=True)
class ExitSpec:
    """Close-based exit rules; a rule that fires at a close sells at the NEXT session's open."""
    holding_days: int
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    trailing_stop_pct: float | None = None


@dataclass(frozen=True)
class XSpec:
    """Cross-sectional ranks per date among ELIGIBLE names. ``metrics`` value fn(view, i) -> float|None; a name is eligible
    when ``eligible(view, i)`` and every metric is present. Columns appear as ``view.xs[f"{key}.{metric}"]`` = (rank, n)
    with rank 1 = smallest metric. ``composite`` adds ``f"{key}.score"``: rank by descending mean percentile rank."""
    key: str
    metrics: dict[str, Callable[[InstrumentView, int], float | None]]
    eligible: Callable[[InstrumentView, int], bool]
    composite: tuple[str, ...] = ()
    min_names: int = 20
    #: optional per-date statistic over the eligible rows' metric dicts -> column f"{key}.flag" (bool|None)
    date_flag: Callable[[list[dict[str, float]]], bool | None] | None = None


@dataclass(frozen=True)
class Playbook:
    id: str
    name: str
    family: str
    style: str
    horizon: str                       # short | swing | long
    rule: str
    source: str
    params: tuple[Param, ...]
    exits: ExitSpec
    max_positions: int
    max_new_per_day: int
    signal: SignalFn
    xs: XSpec | None = None
    needs_market: bool = False
    requires: tuple[str, ...] = ()
    unavailable_reason: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def default_params(self, overrides: dict[str, float] | None = None) -> dict[str, float]:
        values = {p.key: p.value for p in self.params}
        for k, v in (overrides or {}).items():
            if k not in values:
                raise ValueError(f"unknown parameter {k!r} for {self.id}")
            values[k] = float(v)
        return values
