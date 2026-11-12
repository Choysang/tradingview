"""Corporate-action adjustment kept apart from raw bars (spec FINAL §4.1, RECON §4).

Why: execution and the ledger use raw prices; technical research uses versioned
forward/backward adjusted series. An ex-rights drop is not a strategy loss, so
the adjustment is a separate, versioned list of events, never written into raw.

Model: ``AdjustmentEvent(ex_date, ratio)`` with ``ratio = exchange_reference_price
/ previous_raw_close`` (<=1 for cash dividends). Forward adjusted price at date t
is ``raw * product(ratio of events with ex_date > t)``; backward is
``raw / product(ratio of events with ex_date <= t)``. Volume is not adjusted here.
Not covered: choosing the corporate-action source; events may be derived from
exchange reference prices, or inferred from a provider's adjusted/raw pair
(``derive_events_from_pair``, value origin ``calculation``).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import Sequence

from .bars import Bar
from .numeric import CONTEXT, div, mul, sub


@dataclass(frozen=True)
class AdjustmentEvent:
    ex_date: date
    ratio: Decimal
    origin: str = "exchange_reference_price"

    def __post_init__(self) -> None:
        if not (self.ratio > 0):
            raise ValueError("adjustment ratio must be positive")


def adjustment_version(events: Sequence[AdjustmentEvent], label: str = "adj") -> str:
    body = "|".join(f"{e.ex_date.isoformat()}:{e.ratio.normalize()}:{e.origin}" for e in sorted(events, key=lambda e: e.ex_date))
    return f"{label}-{hashlib.sha256(body.encode()).hexdigest()[:12]}"


def events_from_reference_prices(raw: Sequence[Bar], *, min_change: Decimal = Decimal("0.0005")) -> list[AdjustmentEvent]:
    """Events where a bar's exchange reference price differs from the prior raw close.

    Bars without a reference price contribute nothing: the absence is not an
    assertion that no corporate action happened.
    """
    out: list[AdjustmentEvent] = []
    ordered = sorted(raw, key=lambda b: b.trade_date)
    for prev, cur in zip(ordered, ordered[1:]):
        if cur.exchange_reference_price is None or prev.close is None:
            continue
        ratio = div(cur.exchange_reference_price, prev.close)
        if abs(ratio - 1) > min_change:
            out.append(AdjustmentEvent(cur.trade_date, ratio))
    return out


def derive_events_from_pair(raw: Sequence[Bar], adjusted_close: dict[date, Decimal], *,
                            offset_tol: Decimal = Decimal("0.0025")) -> list[AdjustmentEvent]:
    """Infer ex-dates and cash amounts from a provider's forward-adjusted closes versus raw closes.

    Verified on the recorded Tencent ``qfq`` series: that provider adjusts ADDITIVELY
    (``adj = raw - offset`` with ``offset`` piecewise constant, e.g. 2.172 on every day of a
    segment). A step in ``offset`` between consecutive common dates is a cash distribution
    ``d`` going ex on the later date. We express it in this module's multiplicative model as
    ``ratio = (previous raw close - d) / previous raw close`` (a cash-only reference price);
    stock dividends / splits are NOT recoverable this way. Rounding of both series bounds the
    smallest detectable event (``offset_tol``). Origin ``derived_provider_pair_cash_equivalent``.
    """
    common = [b for b in sorted(raw, key=lambda b: b.trade_date) if b.close and b.trade_date in adjusted_close]
    out: list[AdjustmentEvent] = []
    for prev, cur in zip(common, common[1:]):
        off_prev = prev.close - adjusted_close[prev.trade_date]
        off_cur = cur.close - adjusted_close[cur.trade_date]
        d = off_prev - off_cur
        if abs(d) > offset_tol:
            out.append(AdjustmentEvent(cur.trade_date, div(sub(prev.close, d), prev.close), "derived_provider_pair_cash_equivalent"))
    return out


def _factor_forward(events: Sequence[AdjustmentEvent], d: date) -> Decimal:
    f = Decimal(1)
    for e in events:
        if e.ex_date > d:
            f = CONTEXT.multiply(f, e.ratio)
    return f


def _factor_backward(events: Sequence[AdjustmentEvent], d: date) -> Decimal:
    f = Decimal(1)
    for e in events:
        if e.ex_date <= d:
            f = CONTEXT.divide(f, e.ratio)
    return f


def adjust(bars: Sequence[Bar], events: Sequence[AdjustmentEvent], mode: str, version: str | None = None) -> list[Bar]:
    """Return adjusted copies; ``mode`` is forward_adjusted or backward_adjusted."""
    if mode not in ("forward_adjusted", "backward_adjusted"):
        raise ValueError("mode must be forward_adjusted or backward_adjusted")
    if any(b.adjustment_mode != "raw" for b in bars):
        raise ValueError("adjustment starts from raw bars only")
    fn = _factor_forward if mode == "forward_adjusted" else _factor_backward
    ver = version or adjustment_version(events)
    out = []
    for b in bars:
        f = fn(events, b.trade_date)

        def scale(x: Decimal | None) -> Decimal | None:
            return None if x is None else mul(x, f)

        out.append(replace(b, open=scale(b.open), high=scale(b.high), low=scale(b.low), close=scale(b.close),
                           exchange_reference_price=None, adjustment_mode=mode, adjustment_version=ver))
    return out


def to_raw(adjusted: Bar, events: Sequence[AdjustmentEvent]) -> Bar:
    """Inverse mapping so chart price zones can be mapped back to tradable raw prices."""
    if adjusted.adjustment_mode == "raw":
        return adjusted
    fn = _factor_forward if adjusted.adjustment_mode == "forward_adjusted" else _factor_backward
    f = fn(events, adjusted.trade_date)

    def unscale(x: Decimal | None) -> Decimal | None:
        return None if x is None else div(x, f)

    return replace(adjusted, open=unscale(adjusted.open), high=unscale(adjusted.high), low=unscale(adjusted.low),
                   close=unscale(adjusted.close), adjustment_mode="raw", adjustment_version=None)
