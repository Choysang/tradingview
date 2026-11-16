"""Regime attribution of replayed playbook trades (pure: no IO, no clock).

Why: LEGACY_TRIAGE rows #18 / #24. A playbook's average trade return is shown per market regime so a user can see where the rule
worked or did not. The regime is the one of ``domain.market_state.regime`` (trend / liquidity / sentiment / style); nothing about it is
recomputed here.
Rules (versioned ``ATTRIBUTION_VERSION``):
  * point-in-time: a trade is attributed to the regime of the LAST COMPLETED session BEFORE its entry day. The entry fills at the open
    of the entry day, so the entry day's own regime (which needs that day's close) was not known when the order was decided.
    If no regime point lies within ``MAX_STALE_DAYS`` calendar days before the entry, the trade has no regime (it is counted as
    unclassified, never forced into a bucket).
  * a dimension whose ``basis`` is not ``ok`` (insufficient history / missing index) or whose label is ``unknown`` is not a measurement:
    the trade is unclassified FOR THAT DIMENSION, with the reason counted.
  * only CLOSED trades carry a return (exit price / entry price - 1, gross of costs: the replay trade record has no per-trade fee;
    fees are in the equity curve). Open trades are counted, never marked to a guess.
  * a cell with fewer than ``MIN_TRADES`` closed trades is flagged ``insufficient_sample`` (the page shows it faded as 样本不足); its average
    is still returned so nothing is hidden, but it must not be read as evidence.
  * an empty bucket has count 0 and average ``None`` (missing is missing, never 0).
Also here: ``reconstruct_exit_reason``, which names the exit rule that fired for a closed trade by re-running the same pure
``exit_decision`` over the trade's holding bars (the replay trade record keeps only a generic reason).
Not covered: net-of-fee trade returns, significance tests, regime transitions during the holding period.
"""
from __future__ import annotations

from bisect import bisect_left
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from pulse.domain.market_state.regime import LABELS

from .exits import ExitRules, HoldState, exit_decision
from .view import InstrumentView

ATTRIBUTION_VERSION = "regime-attribution-v1"
MIN_TRADES = 10
MAX_STALE_DAYS = 10
DIMENSIONS: tuple[str, ...] = ("trend", "liquidity", "sentiment", "style")
DIMENSION_TEXT = {"trend": "趋势", "liquidity": "流动性", "sentiment": "情绪", "style": "风格"}
UNMEASURED_LABELS = {"style": {"unknown"}}
PLACES = Decimal("1E-12")
D = Decimal


def q12(x: Decimal | None) -> Decimal | None:
    """Fixed 12-place decimal (the contract's Decimal allows at most 12 fraction digits)."""
    return None if x is None else x.quantize(PLACES, rounding=ROUND_HALF_EVEN)


# --------------------------------------------------------------------------------------------------------
# regime of the entry
# --------------------------------------------------------------------------------------------------------
def regime_at_entry(points: Sequence[Mapping[str, Any]], entry_day: str) -> Mapping[str, Any] | None:
    """The last regime point strictly before ``entry_day`` (ISO date), or None when none is recent enough.

    ``points`` are the regime timeline points (``date`` ISO string), ascending."""
    dates = [str(p["date"]) for p in points]
    k = bisect_left(dates, entry_day)               # first index with date >= entry_day
    if k == 0:
        return None
    p = points[k - 1]
    if (date.fromisoformat(entry_day) - date.fromisoformat(str(p["date"]))).days > MAX_STALE_DAYS:
        return None
    return p


def regime_tag(point: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Compact tag stored on a trade: the session it was read from, the four labels and which dimensions are real measurements."""
    if point is None:
        return None
    basis = point.get("basis") or {}
    return {"session": str(point["date"]), **{d: str(point[d]) for d in DIMENSIONS}, "basis": {d: str(basis.get(d, "missing")) for d in DIMENSIONS}}


def tag_trades(trades: Sequence[Mapping[str, Any]], points: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Copy of ``trades`` with ``regime_at_entry`` set from ``points`` (None when no regime is known for that entry)."""
    pts = sorted(points, key=lambda p: str(p["date"]))
    return [{**t, "regime_at_entry": regime_tag(regime_at_entry(pts, str(t["entry_day"])))} for t in trades]


# --------------------------------------------------------------------------------------------------------
# attribution
# --------------------------------------------------------------------------------------------------------
def _bucket_of(tag: Mapping[str, Any] | None, dim: str) -> tuple[str | None, str | None]:
    """(bucket, None) or (None, reason) for one trade and dimension."""
    if tag is None:
        return None, "no_regime"
    basis = (tag.get("basis") or {}).get(dim)
    if basis != "ok":
        return None, str(basis or "missing")            # insufficient_history / missing_index: the reason is the regime's own basis
    if str(tag[dim]) in UNMEASURED_LABELS.get(dim, ()):
        return None, "unmeasured_label"
    return str(tag[dim]), None


def attribute(trades: Sequence[Mapping[str, Any]], min_trades: int = MIN_TRADES) -> dict[str, Any]:
    """Average closed-trade return per regime bucket for every dimension.

    A trade mapping needs ``gross_return`` (Decimal-compatible or None for an open trade) and ``regime_at_entry`` (tag or None)."""
    closed = [t for t in trades if t.get("gross_return") is not None]
    out_dims = []
    for dim in DIMENSIONS:
        returns: dict[str, list[Decimal]] = {k: [] for k in LABELS[dim] if k not in UNMEASURED_LABELS.get(dim, ())}
        reasons: dict[str, int] = {}
        for t in closed:
            bucket, why = _bucket_of(t.get("regime_at_entry"), dim)
            if bucket is None:
                reasons[why or "no_regime"] = reasons.get(why or "no_regime", 0) + 1
            else:
                returns[bucket].append(D(str(t["gross_return"])))
        cells = []
        for key, rs in returns.items():
            n = len(rs)
            cells.append({"bucket": key, "label": LABELS[dim][key], "trade_count": n,
                          "avg_return": q12(sum(rs, D(0)) / n) if n else None,
                          "win_rate": q12(D(sum(1 for r in rs if r > 0)) / n) if n else None,
                          "insufficient_sample": n < min_trades})
        out_dims.append({"dimension": dim, "label": DIMENSION_TEXT[dim], "cells": cells,
                         "unclassified": {"count": sum(reasons.values()), "reasons": dict(sorted(reasons.items()))}})
    return {"version": ATTRIBUTION_VERSION, "min_trades": min_trades, "closed_trades": len(closed),
            "open_trades": len(trades) - len(closed), "dimensions": out_dims}


def heatmap(per_playbook: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Playbook x regime matrix from ``attribute`` results keyed by playbook id: the bucket columns of every dimension plus one
    row per playbook carrying its cells. Cells are the same objects as in ``attribute`` (faded/flagged by the same rule)."""
    columns = [{"dimension": d, "label": DIMENSION_TEXT[d],
                "buckets": [{"bucket": k, "label": v} for k, v in LABELS[d].items() if k not in UNMEASURED_LABELS.get(d, ())]}
               for d in DIMENSIONS]
    rows = [{"playbook": pid, "closed_trades": a["closed_trades"], "dimensions": a["dimensions"]} for pid, a in sorted(per_playbook.items())]
    return {"version": ATTRIBUTION_VERSION, "columns": columns, "rows": rows}


# --------------------------------------------------------------------------------------------------------
# which exit rule fired
# --------------------------------------------------------------------------------------------------------
def reconstruct_exit_reason(rules: ExitRules, view: InstrumentView, entry_day: str, exit_day: str, entry_price: float) -> str:
    """Name of the rule that first fired on a completed bar from the entry session up to (excluding) the exit session.

    Mirrors the replay: ``HoldState`` starts at the entry bar with the fill price and ``exit_decision`` is evaluated at every close
    from the entry day on. ``unknown`` when no rule fires in that range (it cannot happen for a replay trade; reported, not guessed)."""
    try:
        i0 = view.dates.index(entry_day)
    except ValueError:
        return "unknown"
    st = HoldState(i0, entry_price, entry_price)
    for i in range(i0, view.n):
        if view.dates[i] >= exit_day:
            break
        dec = exit_decision(rules, view, i, st)
        if dec is not None:
            return dec[0]
    return "unknown"
