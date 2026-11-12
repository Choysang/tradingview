"""Intraday-T and grid leg plans on top of the ExecutionKernel.

Why: FINAL_SYSTEM_SPEC section 7.2 - T/grid is a group of dependent legs (sell leg,
buy-back leg, remaining leg, expiry). They use the same account, reservations and
T+1 rules as everything else: selling base inventory and buying it back the same
day does NOT restore the sellable quantity used by the sale (the reducer's buy lot
becomes eligible only on a later trading day).
Invariants: a leg with ``after`` is submitted only once its predecessor is fully
filled; with an unknown intrabar path it is released at the fill instant and so
can only match from the next bar (no favourable intrabar order is chosen); with a
declared path it may match in the same bar, after the predecessor's path time.
``expire`` requests and confirms cancellation with cumulative fills, then reports
what is left open. An incomplete plan (e.g. sold, not bought back) is reported,
never hidden, and keeps the genuine inventory shortfall.
Not covered: re-anchoring/versioned grids, multi-bar gap handling policy,
automatic repricing; one leg = one order.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .events import OrderIntent
from .kernel import ExecutionKernel
from .matcher import Bar, ProposedFill


@dataclass(frozen=True)
class Leg:
    leg_id: str
    side: str
    quantity: int
    limit_price: Decimal
    after: str | None = None  # leg_id that must be fully filled first


@dataclass(frozen=True)
class LegPlan:
    plan_id: str
    instrument_id: str
    legs: tuple[Leg, ...]
    prev_close: Decimal | None = None  # reference price for the price-band check


@dataclass(frozen=True)
class LegState:
    leg_id: str
    status: str  # pending | open | partial | filled | cancelled | rejected | expired
    filled: int
    note: str | None = None


@dataclass(frozen=True)
class PlanReport:
    plan_id: str
    legs: tuple[LegState, ...]
    net_share_delta: int  # bought minus sold through this plan
    complete: bool


class LegPlanStrategy:
    """A BarStrategy driving one ``LegPlan``."""

    def __init__(self, plan: LegPlan):
        self.plan = plan
        self._legs = {leg.leg_id: leg for leg in plan.legs}
        self._submitted: dict[str, str] = {}  # leg_id -> order_id
        self._rejected: dict[str, str] = {}

    def order_id(self, leg: Leg) -> str:
        return f"{self.plan.plan_id}:{leg.leg_id}"

    def _submit(self, leg: Leg, kernel: ExecutionKernel, at: datetime, floor=None) -> None:
        res = kernel.submit(
            OrderIntent(self.order_id(leg), self.plan.instrument_id, leg.side, leg.quantity, leg.limit_price,
                        strategy_id=self.plan.plan_id),
            expected_account_version=kernel.ledger.account_version, at=at,
            prev_close=self.plan.prev_close, path_floor=floor)
        if res.accepted:
            self._submitted[leg.leg_id] = self.order_id(leg)
        else:
            self._rejected[leg.leg_id] = res.reason or "rejected"

    def before_bar(self, bar: Bar, kernel: ExecutionKernel) -> None:
        for leg in self.plan.legs:
            if (leg.after is None and leg.leg_id not in self._submitted
                    and leg.leg_id not in self._rejected and bar.instrument_id == self.plan.instrument_id):
                self._submit(leg, kernel, bar.start)

    def on_fill(self, fill: ProposedFill, bar: Bar, kernel: ExecutionKernel) -> None:
        done = {lid for lid, oid in self._submitted.items() if kernel.ledger.orders[oid].remaining == 0
                and not kernel.ledger.orders[oid].cancelled}
        for leg in self.plan.legs:
            if (leg.after in done and leg.leg_id not in self._submitted and leg.leg_id not in self._rejected):
                floor = (bar.start, fill.path_time) if fill.path_time is not None else None
                self._submit(leg, kernel, fill.at, floor)

    def expire(self, kernel: ExecutionKernel, at: datetime) -> None:
        for oid in self._submitted.values():
            order = kernel.ledger.orders[oid]
            if order.remaining > 0:
                kernel.request_cancel(oid)
                kernel.confirm_cancel(oid, order.filled, at)

    def report(self, kernel: ExecutionKernel) -> PlanReport:
        states, delta = [], 0
        for leg in self.plan.legs:
            oid = self._submitted.get(leg.leg_id)
            if oid is None:
                reason = self._rejected.get(leg.leg_id)
                states.append(LegState(leg.leg_id, "rejected" if reason else "pending", 0, reason))
                continue
            o = kernel.ledger.orders[oid]
            if o.filled == o.quantity:
                status = "filled"
            elif o.cancelled:
                status = "cancelled"
            else:
                status = "partial" if o.filled else "open"
            delta += o.filled if leg.side == "buy" else -o.filled
            states.append(LegState(leg.leg_id, status, o.filled))
        complete = all(s.status == "filled" for s in states) and delta == 0
        return PlanReport(self.plan.plan_id, tuple(states), delta, complete)
