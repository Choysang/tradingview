"""Application service: ExecutionKernel actions persisted atomically per account.

Why: execution_standard.md section 2 - one short PostgreSQL transaction locks the
account head, runs the pure kernel against the current ledger, and writes event,
journal, lots, reservations, projection and the new account version together.
Network calls, model inference and market data waits stay outside this service.
Invariants: each public method opens its own connection and transaction (safe for
concurrent strategies); the kernel works on a copy of the locked ledger and every
accepted event is written through ``AccountSession.append`` (a persistence failure
rolls everything back); a stale ``expected_version`` is reported as the explicit
rejection ``account_version_conflict`` (never a silent retry), while
``submit_order`` without an expected version simply serializes behind the lock and
is judged on the then-current cash/inventory.
Not covered: outbox publication, raw broker fact storage (separate packages).
"""
from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from typing import Mapping

from pulse.adapters.postgres import db
from pulse.adapters.postgres.ledger_repo import (AccountVersionConflict, EventContext, LedgerRepo)
from pulse.domain.execution import (KERNEL_VERSION, ExecutionKernel, ExecutionReport, IngestResult, OrderIntent,
                                    SubmitResult)
from pulse.domain.market_rules import (MARKET_RULES_VERSION, CostProfileRegistry, InstrumentSpec,
                                       PriceLimitRegistry, TradingCalendar)


class ExecutionService:
    def __init__(self, dsn: str, instruments: Mapping[str, InstrumentSpec], calendar: TradingCalendar,
                 costs: CostProfileRegistry, *, source_id: str = "kernel", input_manifest_id: str = "none",
                 price_limits: PriceLimitRegistry | None = None, require_price_band: bool = True):
        self.dsn, self.instruments, self.calendar, self.costs = dsn, instruments, calendar, costs
        self.price_limits, self.require_price_band = price_limits, require_price_band
        self.source_id, self.input_manifest_id = source_id, input_manifest_id

    def _ctx(self, day: date) -> EventContext:
        return EventContext(self.source_id, MARKET_RULES_VERSION, self.costs.resolve(day).version,
                            KERNEL_VERSION, self.input_manifest_id)

    def _repo(self):
        return LedgerRepo(db.connect(self.dsn, autocommit=True))

    def open_account(self, account_id: uuid.UUID, initial: Mapping) -> None:
        conn = db.connect(self.dsn, autocommit=True)
        try:
            LedgerRepo(conn).create_account(account_id, initial, self._ctx(date.fromisoformat(initial["trading_day"])))
        finally:
            conn.close()

    def _run(self, account_id: uuid.UUID, expected_version: int | None, action):
        repo = self._repo()
        try:
            with repo.locked(account_id, expected_version) as session:
                ctx = self._ctx(session.ledger.day)
                kernel = ExecutionKernel(deepcopy(session.ledger), self.instruments, self.calendar, self.costs,
                                         self.price_limits, sink=lambda e: session.append(e, ctx),
                                         require_price_band=self.require_price_band)
                return action(kernel, session.version)
        finally:
            repo.conn.close()

    def submit_order(self, account_id: uuid.UUID, intent: OrderIntent, at: datetime, *,
                     prev_close: Decimal | None = None, suspended: bool = False,
                     expected_version: int | None = None) -> SubmitResult:
        try:
            return self._run(account_id, expected_version, lambda k, v: k.submit(
                intent, expected_account_version=v, at=at, prev_close=prev_close, suspended=suspended))
        except AccountVersionConflict:
            return SubmitResult(False, "account_version_conflict")

    def ingest_report(self, account_id: uuid.UUID, report: ExecutionReport) -> IngestResult:
        return self._run(account_id, None, lambda k, v: k.ingest_execution(report))

    def advance_session(self, account_id: uuid.UUID, day: date, at: datetime) -> bool:
        return self._run(account_id, None, lambda k, v: k.advance_session(day, at))

    def apply_event(self, account_id: uuid.UUID, event: dict) -> bool:
        return self._run(account_id, None, lambda k, v: k.apply_event(event))

    def confirm_cancel(self, account_id: uuid.UUID, order_id: str, cumulative_filled: int, at: datetime) -> bool:
        return self._run(account_id, None, lambda k, v: k.confirm_cancel(order_id, cumulative_filled, at))

    def snapshot(self, account_id: uuid.UUID, marks: Mapping[str, str | Decimal]) -> dict:
        repo = self._repo()
        try:
            return repo.load(account_id).snapshot(marks)
        finally:
            repo.conn.close()
