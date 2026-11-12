"""PortfolioCoordinator: the single synthesiser of one physical account's targets.

Why: SPEC §7.1 / RECONCILIATION §9. Six strategy roles (selection, allocation, entry,
holding, execution, exit) propose; ONE coordinator turns proposals into approved
quantities for ONE physical account. Sleeves are virtual attribution only - they own no
cash and no inventory. Long base holdings and intraday T share the same inventory and the
same sellable quantity, so two modules can never both sell the same shares.
Invariants:
  * approval order is the declared ``PriorityPolicy.order`` (exit before new buy etc.), not
    "whichever model sounds stronger"; ties break by proposal_id;
  * sell approvals consume one shared sellable pool per instrument (sellable - reserved);
    buying today restores nothing, sale proceeds are not reusable cash this cycle
    (``cash_reuse_policy = next_session``);
  * a buy needs cash and permission - an EMPTY POSITION is never a reason to reject a
    normal buy; a sell needs sellable inventory;
  * approved quantities are lot multiples; the sum over sleeves equals the approved total.
Not covered: order placement, fees beyond the declared ``fee_buffer_ratio``, margin.
The account is read through the ``AccountSnapshotPort`` protocol (bound by the integrator
to the ledger package; this module imports nothing from it).
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Protocol
from uuid import UUID

from pulse.domain.strategy.canonical import dec_ctx

ROLES = ("selection", "allocation", "entry", "holding", "execution", "exit")
ORDER_ROLES = ("allocation", "entry", "holding", "execution", "exit")
KINDS = ("exit", "trim", "t_sell", "t_buy", "new_buy", "add_buy")
KIND_SIDE = {"exit": "sell", "trim": "sell", "t_sell": "sell", "t_buy": "buy", "new_buy": "buy", "add_buy": "buy"}


@dataclass(frozen=True)
class PositionView:
    quantity_shares: int
    sellable_shares: int
    reserved_sell_shares: int = 0


@dataclass(frozen=True)
class AccountSnapshot:
    account_id: UUID
    account_version: int
    cash_available_cny: Decimal
    positions: Mapping[UUID, PositionView]
    #: None = unrestricted; otherwise the instruments this account may buy.
    buy_permitted: frozenset[UUID] | None = None
    sell_permitted: frozenset[UUID] | None = None


class AccountSnapshotPort(Protocol):
    def snapshot(self, account_id: UUID) -> AccountSnapshot: ...


@dataclass(frozen=True)
class PriorityPolicy:
    version: str = "priority-v1"
    order: tuple[str, ...] = KINDS
    block_buy_when_exit_same_instrument: bool = True
    cash_reuse_policy: str = "next_session"
    lot_size: int = 100
    fee_buffer_ratio: Decimal = Decimal("0.003")

    def __post_init__(self) -> None:
        if sorted(self.order) != sorted(KINDS):
            raise ValueError("order must list every proposal kind exactly once")
        if self.cash_reuse_policy != "next_session":
            raise ValueError("only next_session cash reuse is supported (sale proceeds not reused today)")


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    sleeve_id: str  # virtual attribution only
    role: str
    kind: str
    instrument_id: UUID
    quantity_shares: int
    limit_price: Decimal | None = None  # buys need a price to be cash-checked
    #: "approved" (deterministic constraint or validated policy) | "shadow" (a predictive veto/trim
    #: policy without approval: recorded, never executed - RECONCILIATION §6)
    authority: str = "approved"

    def __post_init__(self) -> None:
        if self.role not in ORDER_ROLES:
            raise ValueError(f"role {self.role!r} cannot place orders (selection only ranks)")
        if self.kind not in KINDS:
            raise ValueError(f"unknown proposal kind {self.kind!r}")
        if self.quantity_shares <= 0:
            raise ValueError("quantity must be positive")
        if self.authority not in ("approved", "shadow"):
            raise ValueError("authority must be approved or shadow")


@dataclass(frozen=True)
class Decision:
    proposal_id: str
    sleeve_id: str
    instrument_id: UUID
    side: str
    requested_shares: int
    approved_shares: int
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class CoordinationResult:
    policy_version: str
    account_id: UUID
    account_version: int
    decisions: tuple[Decision, ...]
    #: sleeve -> (instrument, side) -> approved shares; purely descriptive attribution
    sleeve_attribution: Mapping[str, Mapping[tuple[UUID, str], int]]
    cash_reserved_cny: Decimal
    remaining_cash_cny: Decimal
    remaining_sellable: Mapping[UUID, int]

    def approved(self) -> list[Decision]:
        return [d for d in self.decisions if d.approved_shares > 0]


def coordinate(
    proposals: Sequence[Proposal], account: AccountSnapshot, policy: PriorityPolicy = PriorityPolicy()
) -> CoordinationResult:
    rank = {k: i for i, k in enumerate(policy.order)}
    ordered = sorted(proposals, key=lambda p: (rank[p.kind], p.proposal_id))
    sellable = {i: max(0, v.sellable_shares - v.reserved_sell_shares) for i, v in account.positions.items()}
    cash = account.cash_available_cny
    exit_approved: set[UUID] = set()
    decisions: list[Decision] = []
    attribution: dict[str, dict[tuple[UUID, str], int]] = defaultdict(lambda: defaultdict(int))
    reserved = Decimal(0)
    lot = policy.lot_size
    with localcontext(dec_ctx()):
        for p in ordered:
            side = KIND_SIDE[p.kind]
            reasons: list[str] = []
            approved = 0
            if p.authority == "shadow":  # shadow results consume no cash, inventory or sleeve attribution
                decisions.append(Decision(p.proposal_id, p.sleeve_id, p.instrument_id, side, p.quantity_shares, 0,
                                          ("SHADOW_ONLY_UNAPPROVED_POLICY",)))
                continue
            if side == "sell":
                if account.sell_permitted is not None and p.instrument_id not in account.sell_permitted:
                    reasons.append("NO_SELL_PERMISSION")
                else:
                    avail = sellable.get(p.instrument_id, 0)
                    approved = min(p.quantity_shares, avail)
                    if approved < p.quantity_shares and p.kind != "exit":
                        approved = approved // lot * lot  # partial non-exit sells stay lot-sized
                    if approved <= 0:
                        approved = 0
                        reasons.append("NO_SELLABLE_INVENTORY")
                    else:
                        if approved < p.quantity_shares:
                            reasons.append("REDUCED_TO_SHARED_SELLABLE")
                        sellable[p.instrument_id] = avail - approved
                        if p.kind == "exit":
                            exit_approved.add(p.instrument_id)
            else:
                if account.buy_permitted is not None and p.instrument_id not in account.buy_permitted:
                    reasons.append("NO_BUY_PERMISSION")
                elif p.limit_price is None or p.limit_price <= 0:
                    reasons.append("PRICE_REQUIRED_FOR_CASH_CHECK")
                elif policy.block_buy_when_exit_same_instrument and p.instrument_id in exit_approved:
                    reasons.append("EXIT_PRIORITY_CONFLICT")
                else:
                    unit_cost = p.limit_price * (Decimal(1) + policy.fee_buffer_ratio)
                    affordable = int(cash // unit_cost) // lot * lot
                    approved = min(p.quantity_shares // lot * lot, affordable)
                    if approved <= 0:
                        approved = 0
                        reasons.append("INSUFFICIENT_CASH")
                    else:
                        if approved < p.quantity_shares:
                            reasons.append("REDUCED_TO_AVAILABLE_CASH")
                        spend = approved * unit_cost
                        cash -= spend
                        reserved += spend
            if approved:
                attribution[p.sleeve_id][(p.instrument_id, side)] += approved
            decisions.append(Decision(p.proposal_id, p.sleeve_id, p.instrument_id, side, p.quantity_shares, approved, tuple(reasons)))
    return CoordinationResult(
        policy_version=policy.version,
        account_id=account.account_id,
        account_version=account.account_version,
        decisions=tuple(decisions),
        sleeve_attribution={s: dict(m) for s, m in attribution.items()},
        cash_reserved_cny=reserved,
        remaining_cash_cny=cash,
        remaining_sellable=sellable,
    )
