"""The three entry points into the one ExecutionKernel.

Why: M2-E01 / FINAL_SYSTEM_SPEC section 8 - backtest replay, online paper and
normalized broker reports differ only in where fills come from; all reduce through
``ExecutionKernel`` into one ``LedgerReducer``.
* ``BacktestReplay`` / ``PaperSession``: bars -> MatcherPort -> proposed fills ->
  ``kernel.ingest_execution`` (paper receives bars one at a time, replay iterates);
  both call the single ``process_bar`` function, so they cannot diverge.
* ``BrokerReportAdapter``: already-normalized actual fills -> the same
  ``kernel.ingest_execution`` (never re-simulated), at-least-once safe.
* ``replay_events``: the reference's three transport harnesses over canonical
  events (historical / paper JSON round trip / broker redelivery).
Invariants: a fill id is the idempotency key; a session day is advanced exactly
once through the calendar before the first bar of that day.
Not covered: broker message parsing, out-of-order/missing-fill recovery (the
adapter must reconcile before calling), calendar holidays (calendar port).
"""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Iterable, Mapping, Protocol

from .events import ExecutionReport, IngestResult
from .kernel import ExecutionKernel
from .ledger import LedgerReducer
from .matcher import NO_FILL_QUEUE, Bar, MatcherPort, NoFill, PathScenario, ProposedFill, QueueScenario
from .numeric import MARKET_TIMEZONE, canonical

MAX_ROUNDS_PER_BAR = 16


class BarStrategy(Protocol):
    def before_bar(self, bar: Bar, kernel: ExecutionKernel) -> None: ...

    def on_fill(self, fill: ProposedFill, bar: Bar, kernel: ExecutionKernel) -> None: ...


class NullStrategy:
    def before_bar(self, bar, kernel): ...

    def on_fill(self, fill, bar, kernel): ...


@dataclass
class RunLog:
    fills: list[ProposedFill] = field(default_factory=list)
    no_fills: list[tuple[datetime, NoFill]] = field(default_factory=list)
    sessions: list[str] = field(default_factory=list)


def report_from(fill: ProposedFill) -> ExecutionReport:
    return ExecutionReport(fill.fill_id, fill.order_id, fill.quantity, fill.price, fill.at,
                           None, fill.model_id, fill.scenario_id)


def process_bar(kernel: ExecutionKernel, matcher: MatcherPort, bar: Bar, *, path: PathScenario,
                queue: QueueScenario, strategy: BarStrategy, log: RunLog) -> None:
    day = bar.start.astimezone(MARKET_TIMEZONE).date()
    if day > kernel.ledger.day:
        kernel.advance_session(day, datetime.combine(day, time(9, 0), MARKET_TIMEZONE))
        log.sessions.append(day.isoformat())
    strategy.before_bar(bar, kernel)
    matched: set[str] = set()
    used = 0
    for _ in range(MAX_ROUNDS_PER_BAR):
        orders = [o for o in kernel.open_orders(bar.instrument_id) if o.order_id not in matched]
        if not orders:
            return
        result = matcher.match(orders, bar, path=path, queue=queue, volume_used=used)
        used += result.volume_used
        log.no_fills.extend((bar.start, m) for m in result.no_fills)
        if not result.fills:
            return
        for fill in result.fills:
            matched.add(fill.order_id)
            kernel.ingest_execution(report_from(fill))
            log.fills.append(fill)
            strategy.on_fill(fill, bar, kernel)


class BacktestReplay:
    def __init__(self, kernel: ExecutionKernel, matcher: MatcherPort, *, path: PathScenario = PathScenario.UNKNOWN,
                 queue: QueueScenario = NO_FILL_QUEUE, strategy: BarStrategy | None = None):
        self.kernel, self.matcher, self.path, self.queue = kernel, matcher, path, queue
        self.strategy = strategy or NullStrategy()
        self.log = RunLog()

    def run(self, bars: Iterable[Bar]) -> RunLog:
        for bar in bars:
            process_bar(self.kernel, self.matcher, bar, path=self.path, queue=self.queue,
                        strategy=self.strategy, log=self.log)
        return self.log


class PaperSession(BacktestReplay):
    """Online paper: identical processing, bars arrive one at a time."""

    def on_bar(self, bar: Bar) -> RunLog:
        process_bar(self.kernel, self.matcher, bar, path=self.path, queue=self.queue,
                    strategy=self.strategy, log=self.log)
        return self.log


class BrokerReportAdapter:
    """Normalized actual fills into the kernel; redelivery is an exact no-op."""

    def __init__(self, kernel: ExecutionKernel):
        self.kernel = kernel

    def ingest(self, report: ExecutionReport) -> IngestResult:
        return self.kernel.ingest_execution(report)

    def ingest_many(self, reports: Iterable[ExecutionReport]) -> list[IngestResult]:
        return [self.ingest(r) for r in reports]


def replay_events(initial: Mapping, events: list[dict], transport: str = 'historical') -> LedgerReducer:
    """Reference transport harnesses (one reducer): historical, paper, broker_report_fixture."""
    ledger = LedgerReducer(**initial)
    if transport == 'historical':
        stream = iter(events)
    elif transport == 'paper':
        stream = (json.loads(canonical(e)) for e in events)
    elif transport == 'broker_report_fixture':
        # at-least-once delivery after normalization and sequencing
        stream = (e for original in events for e in (original, deepcopy(original)))
    else:
        raise ValueError('unknown fixture transport')
    for event in stream:
        ledger.apply(event)
    return ledger


replay_fixture = replay_events  # name used by the package reference tests
