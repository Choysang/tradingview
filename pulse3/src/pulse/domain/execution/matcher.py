"""Bar matchers (MatcherPort): daily-bar and minute-bar, one shared algorithm.

Why: execution_standard.md sections 2-3 and FINAL_SYSTEM_SPEC section 8 - matcher
granularity is replaceable, but both matchers emit proposed fills that go through
the same ExecutionKernel and ledger. The matcher never touches accounts.
Rules (all explicit, each fill records ``model_id`` and ``scenario_id``):
* an order is matchable on a bar only if it was active at the bar start; a leg
  released inside a bar is matchable in the same bar only under a *declared* path
  scenario (OHLC / OLHC) and only after its predecessor's path time;
* suspended or zero-volume bars never fill;
* limit queue: if the market is at the order's adverse limit when the order
  becomes active (buy at limit-up, sell at limit-down), or the bar has zero range
  while limits are unknown, the fill depends on an explicit ``QueueScenario``.
  The default fills nothing: an unknown queue is never assumed filled;
* otherwise a buy fills at ``min(limit, market)`` when the bar trades at or below
  the limit, a sell at ``max(limit, market)``; quantity is capped by
  ``max_participation`` x bar volume shared by all orders on the bar (partial
  fills), rounded down to the lot step unless it completes the order;
* without a declared path an intrabar round trip is never assumed (no favourable
  ordering is chosen).
Not covered: auctions, price/time priority against the real book, market impact,
latency; ``max_participation`` is a declared engineering parameter (default 0.10),
not an observed market fact.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Protocol, Sequence

D = Decimal
ZERO = D(0)


class PathScenario(str, Enum):
    UNKNOWN = "path_unknown"
    OHLC = "path_open_high_low_close"
    OLHC = "path_open_low_high_close"


@dataclass(frozen=True)
class QueueScenario:
    scenario_id: str
    fill_fraction: Decimal  # share of the remaining quantity filled at the limit price

    def __post_init__(self):
        if not (ZERO <= self.fill_fraction <= 1):
            raise ValueError("fill_fraction must be within [0, 1]")


NO_FILL_QUEUE = QueueScenario("limit_queue_unknown_no_fill", ZERO)


@dataclass(frozen=True)
class Bar:
    instrument_id: str
    start: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    limit_up: Decimal | None = None
    limit_down: Decimal | None = None
    suspended: bool = False

    def __post_init__(self):
        if self.start.tzinfo is None or self.end.tzinfo is None or self.end <= self.start:
            raise ValueError("bar needs tz-aware start < end")
        if not (self.low <= min(self.open, self.close) and max(self.open, self.close) <= self.high):
            raise ValueError("bar OHLC inconsistent")


@dataclass(frozen=True)
class MatchOrder:
    order_id: str
    instrument_id: str
    side: str
    remaining: int
    limit_price: Decimal
    active_from: datetime
    lot_step: int = 100
    path_floor_bar: datetime | None = None  # bar.start this floor refers to
    path_floor: Decimal = ZERO  # path time in [0, 3] at which the order became active


@dataclass(frozen=True)
class ProposedFill:
    order_id: str
    fill_id: str
    quantity: int
    price: Decimal
    at: datetime
    path_time: Decimal | None
    model_id: str
    scenario_id: str


@dataclass(frozen=True)
class NoFill:
    order_id: str
    reason: str


@dataclass(frozen=True)
class MatchResult:
    fills: tuple[ProposedFill, ...] = ()
    no_fills: tuple[NoFill, ...] = ()
    volume_used: int = 0
    path_scenario: PathScenario = PathScenario.UNKNOWN


class MatcherPort(Protocol):
    model_id: str

    def match(self, orders: Sequence[MatchOrder], bar: Bar, *, path: PathScenario = ...,
              queue: QueueScenario = ..., volume_used: int = ...) -> MatchResult: ...


class _Path:
    """Piecewise-linear intrabar price path, parameter t in [0, 3]."""

    def __init__(self, bar: Bar, scenario: PathScenario):
        x1, x2 = (bar.high, bar.low) if scenario is PathScenario.OHLC else (bar.low, bar.high)
        self.pts = [bar.open, x1, x2, bar.close]

    def price_at(self, t: Decimal) -> Decimal:
        i = min(int(t), 2)
        a, b = self.pts[i], self.pts[i + 1]
        return a + (b - a) * (t - i)

    def first_time(self, side: str, limit: Decimal, floor: Decimal) -> Decimal | None:
        """First t >= floor where a buy limit is marketable (price <= limit) / a sell (price >= limit)."""
        ok = (lambda p: p <= limit) if side == "buy" else (lambda p: p >= limit)
        for i in range(3):
            lo, hi = D(i), D(i + 1)
            if hi < floor:
                continue
            s = max(lo, floor)
            if ok(self.price_at(s)):
                return s
            a, b = self.pts[i], self.pts[i + 1]
            if ok(b):
                return lo + (a - limit) / (a - b)
        return None


class BarMatcher:
    model_id = "bar-v1"
    min_span = timedelta(0)
    max_span = timedelta(days=2)

    def __init__(self, max_participation: Decimal = D("0.10")):
        if not (ZERO < max_participation <= 1):
            raise ValueError("max_participation must be in (0, 1]")
        self.max_participation = max_participation

    def _accepts(self, bar: Bar) -> None:
        span = bar.end - bar.start
        if not (self.min_span <= span <= self.max_span):
            raise ValueError(f"{self.model_id}: bar span {span} outside this matcher's granularity")

    def match(self, orders: Sequence[MatchOrder], bar: Bar, *, path: PathScenario = PathScenario.UNKNOWN,
              queue: QueueScenario = NO_FILL_QUEUE, volume_used: int = 0) -> MatchResult:
        self._accepts(bar)
        fills: list[ProposedFill] = []
        misses: list[NoFill] = []
        used = volume_used
        route = None if path is PathScenario.UNKNOWN else _Path(bar, path)
        for o in orders:
            if o.instrument_id != bar.instrument_id:
                continue
            if bar.suspended:
                misses.append(NoFill(o.order_id, "suspended"))
                continue
            if bar.volume <= 0:
                misses.append(NoFill(o.order_id, "no_volume"))
                continue
            floor = ZERO
            if o.active_from > bar.start:
                if route is None or o.path_floor_bar != bar.start:
                    misses.append(NoFill(o.order_id, "order_not_active_at_bar_start"))
                    continue
                floor = o.path_floor
            hit = self._touch(o, bar, route, floor)
            if hit is None:
                misses.append(NoFill(o.order_id, "limit_not_touched"))
                continue
            t, market = hit
            qty, price, scenario, why = self._size(o, bar, market, queue, used)
            if qty == 0:
                misses.append(NoFill(o.order_id, why))
                continue
            used += qty
            fills.append(ProposedFill(o.order_id, f"{self.model_id}:{o.order_id}:{bar.start.isoformat()}:{o.remaining}",
                                      qty, price, bar.end, t, self.model_id, scenario))
        return MatchResult(tuple(fills), tuple(misses), used - volume_used, path)

    @staticmethod
    def _touch(o: MatchOrder, bar: Bar, route: _Path | None, floor: Decimal):
        """(path_time, market_price_when_marketable) or None."""
        if route is None:
            ok = bar.low <= o.limit_price if o.side == "buy" else bar.high >= o.limit_price
            return (None, bar.open) if ok else None
        t = route.first_time(o.side, o.limit_price, floor)
        return None if t is None else (t, route.price_at(t))

    def _size(self, o: MatchOrder, bar: Bar, market: Decimal, queue: QueueScenario, used: int):
        """(quantity, price, scenario_id, miss_reason)."""
        buy = o.side == "buy"
        adverse = bar.limit_up if buy else bar.limit_down
        if adverse is not None:
            # marketable only at the adverse limit: the order joins the limit queue
            queued = market == adverse and (o.limit_price >= adverse if buy else o.limit_price <= adverse)
        else:
            queued = bar.high == bar.low  # limits unknown and no range: possibly locked
        if queued:
            qty = int(o.remaining * queue.fill_fraction) // o.lot_step * o.lot_step
            if qty == 0:
                return 0, ZERO, "", "limit_queue_unknown"
            qty = self._cap(o, qty, bar, used)
            return (qty, market, queue.scenario_id, "") if qty else (0, ZERO, "", "insufficient_volume")
        price = min(o.limit_price, market) if buy else max(o.limit_price, market)
        qty = self._cap(o, o.remaining, bar, used)
        return (qty, price, "touch_at_limit_or_better", "") if qty else (0, ZERO, "", "insufficient_volume")

    def _cap(self, o: MatchOrder, want: int, bar: Bar, used: int) -> int:
        room = int(bar.volume * self.max_participation) - used
        if room >= want:
            return want
        return max(0, room // o.lot_step * o.lot_step)


class DailyBarMatcher(BarMatcher):
    model_id = "daily-bar-v1"
    min_span = timedelta(hours=1)


class MinuteBarMatcher(BarMatcher):
    model_id = "minute-bar-v1"
    max_span = timedelta(minutes=30)
