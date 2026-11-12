"""ActionabilitySnapshot: the account/quote overlay on a frozen ranking.

Why: SPEC §6. The published ranking never changes its action_rank; "can I act on this
now?" is a separate immutable snapshot bound to release_id, account_version,
quote_snapshot_id, policy and validity, paged by a fixed actionability_id.
Invariants:
  * buy: needs permission and cash for at least one lot; an empty position is NOT a reason
    to reject it; a limit-up with no counterparty is ``blocked`` (the research rank stays);
  * sell: needs sellable inventory (sellable - reserved), else ``blocked``;
  * T: each leg is checked on its own (entries carry ``LEG:<name>`` in reason_codes);
  * unknown inputs (no quote / no account for a sell / unknown trigger) => ``unknown`` -
    never silently ``eligible``; untriggered => ``not_triggered``;
  * ``account_action_rank`` is a 1..M order over ``eligible`` entries only, by frozen
    action rank (then alpha order given by the caller); it never rewrites the ranking;
  * ``actionability_id`` is a deterministic UUID of its binding inputs (redelivery-safe).
Not covered: the quote/account IO (ports), order placement, release/ranking storage.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Any
from uuid import UUID

from pulse.contracts import to_wire
from pulse.domain.plans.conditions import Tri
from pulse.domain.portfolio.coordinator import AccountSnapshot
from pulse.domain.strategy.canonical import dec_ctx, stable_uuid


@dataclass(frozen=True)
class QuoteView:
    instrument_id: UUID
    last_price_raw: Decimal | None
    #: False when e.g. limit-up with no sellers (cannot buy); None = unknown
    buy_available: bool | None = None
    sell_available: bool | None = None


@dataclass(frozen=True)
class PlanLeg:
    plan_ref: tuple[UUID, int]
    side: str  # buy | sell
    #: None = sizing could not be determined (e.g. NAV unknown): the leg is ``unknown``; <= 0 = ``blocked`` (insufficient size)
    quantity_shares: int | None
    trigger: Tri  # TRUE / FALSE / UNKNOWN from the condition runner
    leg: str | None = None  # e.g. "sell_leg" / "buyback_leg" for T plans
    limit_price_raw: Decimal | None = None


@dataclass(frozen=True)
class ActionabilityInput:
    release_id: UUID
    ranking_id: UUID | None
    quote_snapshot_id: UUID
    policy_version: str
    checked_at: datetime
    valid_until: datetime
    #: instrument -> frozen action rank (None if the ranking gave none), in alpha order
    ranking_order: Sequence[tuple[UUID, int | None]]
    plans: Mapping[UUID, Sequence[PlanLeg]]
    quotes: Mapping[UUID, QuoteView]
    account: AccountSnapshot | None = None
    lot_size: int = 100
    fee_buffer_ratio: Decimal = Decimal("0.003")


def _check_leg(inp: ActionabilityInput, iid: UUID, leg: PlanLeg) -> tuple[str, list[str], int | None]:
    reasons = [f"LEG:{leg.leg}"] if leg.leg else []
    quote = inp.quotes.get(iid)
    if leg.trigger == Tri.UNKNOWN:
        return "unknown", reasons + ["TRIGGER_UNKNOWN"], None
    if leg.trigger == Tri.FALSE:
        return "not_triggered", reasons + ["CONDITION_NOT_MET"], None
    if quote is None or quote.last_price_raw is None:
        return "unknown", reasons + ["QUOTE_MISSING"], None
    if leg.quantity_shares is None:
        return "unknown", reasons + ["SIZING_UNKNOWN"], None
    if leg.quantity_shares <= 0:
        return "blocked", reasons + ["INSUFFICIENT_SIZE"], None
    acct = inp.account
    if leg.side == "buy":
        if quote.buy_available is False:
            return "blocked", reasons + ["NO_COUNTERPARTY_LIMIT_UP"], None
        if quote.buy_available is None:
            return "unknown", reasons + ["BUY_AVAILABILITY_UNKNOWN"], None
        if acct is None:
            return "eligible", reasons + ["PUBLIC_OVERLAY_NO_ACCOUNT_CHECK"], leg.quantity_shares
        if acct.buy_permitted is not None and iid not in acct.buy_permitted:
            return "blocked", reasons + ["NO_BUY_PERMISSION"], None
        price = leg.limit_price_raw or quote.last_price_raw
        with localcontext(dec_ctx()):
            affordable = int(acct.cash_available_cny // (price * (1 + inp.fee_buffer_ratio))) // inp.lot_size * inp.lot_size
        qty = min(leg.quantity_shares // inp.lot_size * inp.lot_size, affordable)
        if qty <= 0:
            return "blocked", reasons + ["INSUFFICIENT_CASH"], None
        if qty < leg.quantity_shares:
            reasons.append("REDUCED_TO_AVAILABLE_CASH")
        return "eligible", reasons, qty
    # sell
    if quote.sell_available is False:
        return "blocked", reasons + ["NO_COUNTERPARTY_LIMIT_DOWN"], None
    if acct is None:
        return "unknown", reasons + ["ACCOUNT_REQUIRED_FOR_SELL"], None
    if acct.sell_permitted is not None and iid not in acct.sell_permitted:
        return "blocked", reasons + ["NO_SELL_PERMISSION"], None
    pos = acct.positions.get(iid)
    avail = 0 if pos is None else max(0, pos.sellable_shares - pos.reserved_sell_shares)
    qty = min(leg.quantity_shares, avail)
    if qty <= 0:
        return "blocked", reasons + ["NO_SELLABLE_INVENTORY"], None
    if qty < leg.quantity_shares:
        reasons.append("REDUCED_TO_SELLABLE")
    return "eligible", reasons, qty


def evaluate_actionability(inp: ActionabilityInput) -> dict[str, Any]:
    """Build the ``ActionabilitySnapshot`` wire payload."""
    if inp.valid_until <= inp.checked_at:
        raise ValueError("valid_until must be after checked_at")
    frozen_rank = {iid: r for iid, r in inp.ranking_order}
    alpha_pos = {iid: i for i, (iid, _) in enumerate(inp.ranking_order)}
    rows: list[dict[str, Any]] = []
    for iid, _ in inp.ranking_order:
        legs = inp.plans.get(iid) or []
        if not legs:
            rows.append(
                {"instrument_id": iid, "plan_ref": None, "frozen_action_rank": frozen_rank[iid],
                 "account_action_rank": None, "actionability_status": "unknown", "side": None,
                 "reason_codes": ["NO_PLAN"], "checked_quantity_shares": None, "_order": alpha_pos[iid]}
            )
            continue
        for leg in legs:
            status, reasons, qty = _check_leg(inp, iid, leg)
            rows.append(
                {"instrument_id": iid, "plan_ref": {"plan_id": leg.plan_ref[0], "plan_version": leg.plan_ref[1]},
                 "frozen_action_rank": frozen_rank[iid], "account_action_rank": None,
                 "actionability_status": status, "side": leg.side, "reason_codes": reasons or ["OK"],
                 "checked_quantity_shares": qty, "_order": alpha_pos[iid]}
            )
    eligible = sorted(
        (r for r in rows if r["actionability_status"] == "eligible"),
        key=lambda r: (r["frozen_action_rank"] is None, r["frozen_action_rank"] or 0, r["_order"]),
    )
    for n, r in enumerate(eligible, start=1):
        r["account_action_rank"] = n
    for r in rows:
        r.pop("_order")
    acct = inp.account
    actionability_id = stable_uuid(
        "actionability", inp.release_id, inp.ranking_id, acct.account_id if acct else None,
        acct.account_version if acct else None, inp.quote_snapshot_id, inp.policy_version, inp.checked_at.isoformat(),
    )
    return to_wire(
        {
            "actionability_id": actionability_id,
            "release_id": inp.release_id,
            "ranking_id": inp.ranking_id,
            "account_id": acct.account_id if acct else None,
            "account_version": acct.account_version if acct else None,
            "quote_snapshot_id": inp.quote_snapshot_id,
            "actionability_policy_version": inp.policy_version,
            "checked_at": inp.checked_at,
            "valid_until": inp.valid_until,
            "entries": rows,
        }
    )
