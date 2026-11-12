"""Exit rules (close-based) shared by the playbook portfolio replay and the exit lab / tactic replay.

Why: LEGACY inventory "Exit lab (playbooks/exits)": 10 exit rules, 2 entry rules, scale-out, intraday T (reverse and forward).
The old file (read for rule SEMANTICS only) varied one practitioner rule at a time on top of a playbook's defaults.
Here a rule set is an ``ExitRules`` value (a strategy INSTANCE parameter set, role ``exit``); it is evaluated on the
COMPLETED bar, and a hit sells at the NEXT session's open through the ExecutionKernel (never at the close it was seen on).
Invariants: every rule reads only indexes <= i; a rule whose input is missing does not fire (it never fires on a guess);
``scale_out_pct`` sells half the position (rounded down to the lot step) once; ``holding_days`` counts sessions from the entry
session (entry session = day 0) and sells at the open of session ``entry + holding_days``.
Not covered: intraday stops/targets (daily bars cannot place them), weak-open and sentiment-ebb rules (need the next open /
a sentiment series), buy-side variants (the replay enters at the next open, or abandons on a one-word board).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

from .spec import ExitSpec
from .view import InstrumentView, ok


@dataclass(frozen=True)
class ExitRules:
    holding_days: int
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    trailing_stop_pct: float | None = None
    scale_out_pct: float | None = None
    sell_below_ma5: bool = False
    sell_on_stall: bool = False
    sell_on_shadow: bool = False
    bias_exit_pct: float | None = None
    chandelier_atr: float | None = None

    @classmethod
    def from_spec(cls, e: ExitSpec) -> "ExitRules":
        return cls(e.holding_days, e.stop_loss_pct, e.take_profit_pct, e.trailing_stop_pct)

    def to_json(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, False)}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "ExitRules":
        allowed = set(cls.__dataclass_fields__)
        unknown = set(d) - allowed
        if unknown:
            raise ValueError(f"unknown exit rule keys {sorted(unknown)}")
        return cls(**{"holding_days": int(d["holding_days"]), **{k: v for k, v in d.items() if k != "holding_days"}})


#: exit-lab variants (key, label, changes) - each changes ONE rule on top of the defaults (plus scale-out)
VARIANTS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("default", "默认卖法", {}),
    ("stop_tight", "紧止损 5%", {"stop_loss_pct": 5.0}),
    ("stop_wide", "宽止损 15%", {"stop_loss_pct": 15.0}),
    ("below_ma5", "跌破 5 日线卖", {"sell_below_ma5": True}),
    ("stall", "放量滞涨卖", {"sell_on_stall": True}),
    ("chandelier", "吊灯止损（3×ATR）", {"chandelier_atr": 3.0}),
    ("chandelier_tight", "紧吊灯止损（2×ATR）", {"chandelier_atr": 2.0}),
    ("chandelier_wide", "宽吊灯止损（5×ATR）", {"chandelier_atr": 5.0}),
    ("bias", "乖离过大卖（高于 20 日线 15%）", {"bias_exit_pct": 15.0}),
    ("shadow", "高位放量长上影卖", {"sell_on_shadow": True}),
    ("scale_half", "涨 5% 先卖一半", {"scale_out_pct": 5.0}),
)
VARIANT_BY_KEY = {k: (label, ch) for k, label, ch in VARIANTS}


def variant(base: ExitRules, key: str) -> ExitRules:
    return replace(base, **VARIANT_BY_KEY[key][1])


@dataclass
class HoldState:
    entry_index: int           # view index of the entry session (day 0)
    entry_price: float
    peak_close: float
    scaled_out: bool = False


def exit_decision(rules: ExitRules, v: InstrumentView, i: int, st: HoldState) -> tuple[str, float] | None:
    """(reason, fraction) when the position should be sold at the NEXT open given the completed bar ``i``; fraction 0.5 = half.
    Updates ``st.peak_close``."""
    c = v.close[i]
    if not ok(c):
        return None
    st.peak_close = max(st.peak_close, c)
    pnl = (c / st.entry_price - 1.0) * 100.0
    if rules.stop_loss_pct is not None and pnl <= -rules.stop_loss_pct:
        return "stop_loss", 1.0
    if rules.take_profit_pct is not None and pnl >= rules.take_profit_pct:
        return "take_profit", 1.0
    if rules.trailing_stop_pct is not None and c <= st.peak_close * (1 - rules.trailing_stop_pct / 100.0):
        return "trailing_stop", 1.0
    if rules.chandelier_atr is not None:
        atr = v.atr(14)[i]
        if ok(atr) and c < st.peak_close - rules.chandelier_atr * atr:
            return "chandelier", 1.0
    if rules.sell_below_ma5:
        m5 = v.ma(5)[i]
        if ok(m5) and c < m5 and i > st.entry_index:
            return "below_ma5", 1.0
    if rules.sell_on_stall:
        vma5, pc = v.vol_ma(5)[i - 1] if i >= 1 else None, v.pct()[i]
        if ok(vma5, pc, v.volume[i]) and v.volume[i] > 2 * vma5 and pc < 2:
            return "stall", 1.0
    if rules.bias_exit_pct is not None:
        m20 = v.ma(20)[i]
        if ok(m20) and m20 > 0 and (c / m20 - 1) * 100 >= rules.bias_exit_pct:
            return "bias", 1.0
    if rules.sell_on_shadow and pnl > 0:
        h, o, low, vma5 = v.high[i], v.open[i], v.low[i], v.vol_ma(5)[i - 1] if i >= 1 else None
        if ok(h, o, low, vma5, v.volume[i]) and h > low and (h - max(o, c)) > 0.5 * (h - low) and v.volume[i] > 2 * vma5:
            return "shadow", 1.0
    if rules.scale_out_pct is not None and not st.scaled_out and pnl >= rules.scale_out_pct:
        return "scale_out", 0.5
    if i - st.entry_index + 1 >= rules.holding_days:
        return "holding_days", 1.0
    return None
