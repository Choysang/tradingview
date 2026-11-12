"""Interval statistics over a user-chosen window of ONE price series (queryIntervalStatistics).

Why: LEGACY inventory "interval statistics" / PEX04: the chart's range statistics must use the same price definition as the
chart (one adjustment mode, one snapshot), and must never be mixed with account TWR. The window is chosen by the user after
seeing the data, so the result is always labelled exploratory (``exploratory = true``).
Invariants: prices are Decimal; ratios use ROUND_HALF_EVEN at 12 places; a metric that cannot be computed has ``value`` None
and a ``missing_reason`` (never 0); ``period_return`` uses the last close BEFORE the window as base when one exists (so the
first day's change is included) and otherwise is missing (``period_return_from_first_open`` is always offered as the
separately named alternative); sample_count is the number of sessions inside the window. Window is half-open [start, end).
Not covered: dividends/corporate actions (the series' own adjustment mode decides), account performance.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

DEFINITION_VERSION = "interval-v1"
POLICY = "interval-stats-v1"


@dataclass(frozen=True)
class Bar:
    day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None


def _q(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.000000000001"), rounding=ROUND_HALF_EVEN)


def _metric(name: str, value: Decimal | None, unit: str, n: int, reason: str | None = None) -> dict:
    if value is None and not reason:
        raise ValueError("a missing metric needs a reason")
    return {"name": name, "definition_version": DEFINITION_VERSION, "value": None if value is None else _q(value),
            "unit": unit, "sample_count": n, "missing_reason": None if value is not None else reason}


def compute(bars: Sequence[Bar], start: date, end: date) -> list[dict]:
    """Metrics for sessions with start <= day < end; ``bars`` must contain at least the bars before ``start`` if wanted."""
    ordered = sorted(bars, key=lambda b: b.day)
    inside = [b for b in ordered if start <= b.day < end]
    before = [b for b in ordered if b.day < start]
    n = len(inside)
    if n == 0:
        reason = "no_session_in_window"
        return [_metric(x, None, u, 0, reason) for x, u in (
            ("period_return", "ratio"), ("period_return_from_first_open", "ratio"), ("max_drawdown", "ratio"),
            ("daily_volatility", "ratio"), ("up_day_share", "ratio"), ("best_day", "ratio"), ("worst_day", "ratio"),
            ("high_low_range", "ratio"), ("avg_volume_shares", "shares"), ("sessions", "count"))]
    with localcontext() as ctx:
        ctx.prec = 50
        base = before[-1].close if before else None
        last = inside[-1]
        out = [
            _metric("period_return", (last.close / base - 1) if base and base > 0 else None, "ratio", n,
                    "no_base_bar_before_window"),
            _metric("period_return_from_first_open", last.close / inside[0].open - 1 if inside[0].open > 0 else None, "ratio",
                    n, "first_open_not_positive"),
        ]
        peak, mdd = inside[0].close, Decimal(0)
        for b in inside:
            peak = max(peak, b.close)
            if peak > 0:
                mdd = min(mdd, b.close / peak - 1)
        out.append(_metric("max_drawdown", mdd, "ratio", n))
        seq = ([before[-1]] if before else []) + inside
        rets = [seq[i].close / seq[i - 1].close - 1 for i in range(1, len(seq)) if seq[i - 1].close > 0]
        if len(rets) >= 2:
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
            out.append(_metric("daily_volatility", var.sqrt(), "ratio", len(rets)))
        else:
            out.append(_metric("daily_volatility", None, "ratio", len(rets), "fewer_than_two_daily_returns"))
        if rets:
            out += [_metric("up_day_share", Decimal(sum(1 for r in rets if r > 0)) / len(rets), "ratio", len(rets)),
                    _metric("best_day", max(rets), "ratio", len(rets)), _metric("worst_day", min(rets), "ratio", len(rets))]
        else:
            out += [_metric(x, None, "ratio", 0, "no_daily_return") for x in ("up_day_share", "best_day", "worst_day")]
        lo = min(b.low for b in inside)
        hi = max(b.high for b in inside)
        out.append(_metric("high_low_range", hi / lo - 1 if lo > 0 else None, "ratio", n, "low_not_positive"))
        vols = [b.volume for b in inside if b.volume is not None]
        out.append(_metric("avg_volume_shares", sum(vols) / len(vols) if vols else None, "shares", len(vols), "volume_missing"))
        out.append(_metric("sessions", Decimal(n), "count", n))
    return out
