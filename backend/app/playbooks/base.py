"""The playbook interface: one shape for every 战法, from 打板 to 价值.

A playbook is a catalogue entry (`PlaybookSpec`, the contract) plus one pure
signal function. The function sees the whole research panel at once and marks
the (code, date) rows where the rule fires on a *completed* bar; the engine
enters at the next bar's open. Signals never look at future rows — shifting
forward inside a signal function is a bug, and `tests/test_playbook_library.py`
checks it by truncating the panel.

Research panel (built by `panel.research_panel`), one row per (code, date),
sorted by code then date. Columns every signal may rely on:
    code, date, open, high, low, close, volume, amount (亿元),
    prev_close, pct_chg (%), limit_ratio, limit_up_price, limit_down_price,
    is_limit_up, touched_limit, is_broken, is_limit_down, one_word,
    open_at_limit, open_at_floor, limit_streak,
    ma5 ma10 ma20 ma60 ma120 ma250, vol_ma5 vol_ma20,
    ret_5 ret_20 ret_60 ret_120 ret_250 (%), volatility_20 (% daily std),
    atr_14, high_20_prev high_55_prev high_60_prev high_250_prev (prior N bars, today excluded),
    low_20_prev, macd_dif macd_dea macd_hist, kdj_k kdj_d kdj_j, rsi_6 rsi_14,
    boll_mid boll_upper boll_lower, days_listed (sessions since listing; a stock already trading on the
    warehouse's first date counts as long listed, 10 000 + bars),
    name, board, total_cap, float_cap, pe_ttm, pb (latest securities snapshot; null when unknown),
    cap_est (total_cap scaled by price: the latest share count held constant),
    industry, ind_limit_ups, name_heat, name_char, lhb_on, smart_buyers, smart_score, trip_buyers,
    inst_net (crowd features, see playbooks/crowd.py; null when the source table is missing).

Regime frame (built by `regime.regime_frame`), one row per date:
    date, trend ("bull"|"bear"|"range"), liquidity ("expanding"|"steady"|"contracting"),
    sentiment ("freezing"|"repair"|"climax"|"ebb"), style ("large"|"small"|"balanced"),
    index_close, breadth_ma20, limit_up_count, broken_rate, max_height, amount_ratio, premium.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import polars as pl

from contracts.schema import PlaybookSpec

# (panel, params) -> LazyFrame[code, date, score]; score > 0, higher = stronger.
SignalFn = Callable[[pl.LazyFrame, dict[str, float]], pl.LazyFrame]


@dataclass(frozen=True)
class Playbook:
    spec: PlaybookSpec
    signal: SignalFn
    # Panel columns that must be non-null for the playbook to be testable at all,
    # e.g. ("pe_ttm",) for value: point-in-time fundamentals are not in the
    # warehouse, so such playbooks rank today's names but are not backtested.
    requires_history: tuple[str, ...] = field(default_factory=tuple)

    def params(self, overrides: dict[str, float] | None = None) -> dict[str, float]:
        values = {param.key: param.value for param in self.spec.params}
        for key, value in (overrides or {}).items():
            if key in values:
                values[key] = float(value)
        return values


REGISTRY: dict[str, Playbook] = {}


def register(playbook: Playbook) -> Playbook:
    if playbook.spec.id in REGISTRY:
        raise ValueError(f"duplicate playbook id: {playbook.spec.id}")
    REGISTRY[playbook.spec.id] = playbook
    return playbook


def catalog() -> list[Playbook]:
    from . import library, library_crowd, library_factor, library_sentiment, library_tech  # noqa: F401 - importing registers every playbook

    return list(REGISTRY.values())


def resolve(playbook_id: str) -> Playbook | None:
    catalog()
    return REGISTRY.get(playbook_id)
