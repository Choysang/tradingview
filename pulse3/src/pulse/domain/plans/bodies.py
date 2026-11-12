"""Typed plan bodies for intraday T (做T) and grid plans.

Why: SPEC §7.2 / RECONCILIATION §9. T is a grouped operation on EXISTING sellable inventory
(sell leg, buy-back leg, leftover, expiry); a grid has an anchor, step, levels, per-level
size, max inventory, cash budget, daily sellable quota, fee threshold, re-anchor rule, gap
and multi-level-per-bar handling. The contract TradePlan carries the entry/exit condition
trees; these bodies carry the instrument-specific mechanics and are referenced from
``validation.result_ref`` as ``planbody:<kind>:<sha256>`` (storage table proposed in
migrations/proposals/decision_plan_body.sql - not applied here).
Invariants:
  * T quantities are lot multiples and never exceed the sellable base at plan time; a
    buy-back/new buy NEVER restores sellable quantity that was already used (A-share
    same-day-buy is not sellable): sellable_after = base_sellable - sold;
  * leftover (sold - bought back, or bought - sold) is reported with its exposure side and
    must be counted by evaluation (a missed buy-back is lost holdings, not a gain);
  * a grid whose step does not clear the round-trip fee ratio is rejected;
  * re-anchoring creates a NEW body version (frozen dataclasses, ``replace``); history is
    never moved to flatter a backtest.
Not covered: placing/cancelling orders, fee model (caller supplies the ratio), sizing from
the live account.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from pulse.contracts import to_wire
from pulse.domain.strategy.canonical import canonical_hash

TICK = Decimal("0.01")


class BodyError(ValueError):
    pass


def body_hash(body: Any) -> str:
    return canonical_hash(to_wire(dataclasses.asdict(body)))


def body_ref(kind: str, body: Any) -> str:
    return f"planbody:{kind}:{body_hash(body)}"


# ------------------------------------------------------------------ T plan


@dataclass(frozen=True)
class TLeg:
    side: str  # buy | sell
    quantity_shares: int
    lower_price_raw: Decimal
    upper_price_raw: Decimal
    condition_ref: str  # hash/ref of the condition tree used to trigger this leg


@dataclass(frozen=True)
class TPlanBody:
    body_version: int
    direction: str  # sell_first (sell old inventory, buy back lower) | buy_first (buy, sell old inventory higher)
    base_sellable_shares: int  # sellable OLD inventory when the plan was compiled
    lot_size: int
    first_leg: TLeg
    second_leg: TLeg
    leftover_policy: str  # carry | flatten_at_expiry_if_marketable
    expires_at: datetime

    def __post_init__(self) -> None:
        if self.direction not in ("sell_first", "buy_first"):
            raise BodyError("direction must be sell_first or buy_first")
        sides = (self.first_leg.side, self.second_leg.side)
        if sides != (("sell", "buy") if self.direction == "sell_first" else ("buy", "sell")):
            raise BodyError(f"legs {sides} do not match direction {self.direction}")
        for leg in (self.first_leg, self.second_leg):
            if leg.quantity_shares <= 0 or leg.quantity_shares % self.lot_size:
                raise BodyError("leg quantity must be a positive multiple of the lot size")
            if leg.lower_price_raw > leg.upper_price_raw:
                raise BodyError("leg price zone inverted")
        sold = self.first_leg if self.direction == "sell_first" else self.second_leg
        if sold.quantity_shares > self.base_sellable_shares:
            raise BodyError("sell leg exceeds the sellable old inventory at plan time")
        if self.first_leg.quantity_shares != self.second_leg.quantity_shares:
            raise BodyError("T legs are a pair: equal planned quantities (leftover arises only from fills)")
        if self.leftover_policy not in ("carry", "flatten_at_expiry_if_marketable"):
            raise BodyError("unknown leftover_policy")


@dataclass(frozen=True)
class TProgress:
    sold_shares: int = 0
    bought_shares: int = 0


@dataclass(frozen=True)
class TOutcome:
    status: str  # in_progress | complete | expired_flat | expired_with_leftover
    leftover_shares: int
    exposure: str | None  # underweight (sold, not bought back) | overweight (bought, not sold) | None
    sellable_after_shares: int
    actions: tuple[str, ...]


def apply_t_fills(body: TPlanBody, progress: TProgress, *, sold: int = 0, bought: int = 0) -> TProgress:
    new = TProgress(progress.sold_shares + sold, progress.bought_shares + bought)
    if new.sold_shares > body.base_sellable_shares:
        raise BodyError("sold more than the sellable old inventory")
    planned = body.first_leg.quantity_shares
    if new.sold_shares > planned or new.bought_shares > planned:
        raise BodyError("fills exceed the planned leg quantity")
    if body.direction == "sell_first" and new.bought_shares > new.sold_shares:
        raise BodyError("buy-back cannot exceed what was sold")
    return new


def t_outcome(body: TPlanBody, progress: TProgress, *, now: datetime) -> TOutcome:
    sellable_after = body.base_sellable_shares - progress.sold_shares  # buying never restores this
    sold, bought = progress.sold_shares, progress.bought_shares
    planned = body.first_leg.quantity_shares
    if body.direction == "sell_first":
        leftover, exposure = sold - bought, "underweight"
    else:
        leftover, exposure = bought - sold, "overweight"
    done = sold == planned and bought == planned
    if done:
        return TOutcome("complete", 0, None, sellable_after, ())
    if now < body.expires_at:
        return TOutcome("in_progress", leftover, exposure if leftover else None, sellable_after, ())
    actions = ("reconcile_then_cancel_unfilled_orders",)
    if leftover == 0:
        return TOutcome("expired_flat", 0, None, sellable_after, actions)
    if body.leftover_policy == "flatten_at_expiry_if_marketable":
        actions += ("flatten_leftover_if_marketable",)
    return TOutcome("expired_with_leftover", leftover, exposure, sellable_after, actions)


# ------------------------------------------------------------------ grid plan


@dataclass(frozen=True)
class GridBody:
    body_version: int
    anchor_source: str  # last_close | vwap | atr_mid | manual
    anchor_price_raw: Decimal
    step_unit: str  # price | ratio | atr
    step_value: Decimal
    atr_value: Decimal | None
    levels_below: int
    levels_above: int
    qty_per_level: int
    lot_size: int
    max_inventory_shares: int
    cash_budget_cny: Decimal
    daily_sellable_quota_shares: int
    round_trip_fee_ratio: Decimal
    min_net_edge_ratio: Decimal
    gap_policy: str  # fill_at_open | skip_gapped_levels
    multi_level_policy: str  # one_level_per_bar | all_crossed_levels
    intrabar_path: str  # open_low_high_close | open_high_low_close (declared assumption)
    re_anchor_trigger: str  # price_outside_band | manual | time
    supersedes_hash: str | None = None

    def __post_init__(self) -> None:
        if self.step_unit not in ("price", "ratio", "atr"):
            raise BodyError("bad step_unit")
        if self.step_unit == "atr" and (self.atr_value is None or self.atr_value <= 0):
            raise BodyError("atr step needs a positive atr_value (no guessed ATR)")
        if self.gap_policy not in ("fill_at_open", "skip_gapped_levels"):
            raise BodyError("bad gap_policy")
        if self.multi_level_policy not in ("one_level_per_bar", "all_crossed_levels"):
            raise BodyError("bad multi_level_policy")
        if self.intrabar_path not in ("open_low_high_close", "open_high_low_close"):
            raise BodyError("an explicit intrabar path assumption is required")
        if self.qty_per_level <= 0 or self.qty_per_level % self.lot_size:
            raise BodyError("qty_per_level must be a positive lot multiple")
        if self.anchor_price_raw <= 0 or self.step_value <= 0:
            raise BodyError("anchor and step must be positive")
        if self.levels_below < 1 or self.levels_above < 1:
            raise BodyError("need at least one level on each side")
        step = self.step_price()
        if step / self.anchor_price_raw <= self.round_trip_fee_ratio + self.min_net_edge_ratio:
            raise BodyError("grid step does not clear the round-trip fee threshold")
        if self.qty_per_level * self.levels_below > self.max_inventory_shares:
            raise BodyError("buy levels would exceed max_inventory_shares")
        need_cash = sum((p * self.qty_per_level for p in self.buy_prices()), Decimal(0))
        if need_cash > self.cash_budget_cny:
            raise BodyError("cash budget cannot fund every buy level")

    def step_price(self) -> Decimal:
        if self.step_unit == "price":
            return self.step_value
        if self.step_unit == "ratio":
            return self.anchor_price_raw * self.step_value
        return self.atr_value * self.step_value  # type: ignore[operator]

    def _q(self, p: Decimal) -> Decimal:
        return p.quantize(TICK, rounding=ROUND_HALF_EVEN)

    def buy_prices(self) -> list[Decimal]:
        return [self._q(self.anchor_price_raw - i * self.step_price()) for i in range(1, self.levels_below + 1)]

    def sell_prices(self) -> list[Decimal]:
        return [self._q(self.anchor_price_raw + i * self.step_price()) for i in range(1, self.levels_above + 1)]


def re_anchor(body: GridBody, *, new_anchor_price_raw: Decimal, anchor_source: str, atr_value: Decimal | None = None) -> GridBody:
    """Re-anchoring is a NEW version; the previous body (and its levels) stay untouched."""
    return replace(
        body,
        body_version=body.body_version + 1,
        anchor_price_raw=new_anchor_price_raw,
        anchor_source=anchor_source,
        atr_value=atr_value if atr_value is not None else body.atr_value,
        supersedes_hash=body_hash(body),
    )


@dataclass(frozen=True)
class GridState:
    inventory_shares: int
    sellable_shares: int  # OLD inventory only; shares bought today are not sellable
    cash_available_cny: Decimal
    sold_today_shares: int
    filled_buy_levels: tuple[int, ...] = ()
    filled_sell_levels: tuple[int, ...] = ()


@dataclass(frozen=True)
class GridIntent:
    side: str
    level_index: int  # 1-based distance from the anchor
    price_raw: Decimal
    quantity_shares: int
    gapped: bool


def levels_crossed(body: GridBody, state: GridState, *, open_: Decimal, low: Decimal, high: Decimal) -> list[GridIntent]:
    """Intents for one bar under the body's declared path / gap / multi-level policies."""
    buys: list[GridIntent] = []
    sells: list[GridIntent] = []
    for i, p in enumerate(body.buy_prices(), start=1):
        if i in state.filled_buy_levels or p < low:
            continue
        gapped = p > open_  # price jumped below the level before it could trade there
        if gapped and body.gap_policy == "skip_gapped_levels":
            continue
        buys.append(GridIntent("buy", i, open_ if gapped else p, body.qty_per_level, gapped))
    for i, p in enumerate(body.sell_prices(), start=1):
        if i in state.filled_sell_levels or p > high:
            continue
        gapped = p < open_
        if gapped and body.gap_policy == "skip_gapped_levels":
            continue
        sells.append(GridIntent("sell", i, open_ if gapped else p, body.qty_per_level, gapped))
    buys.sort(key=lambda g: g.level_index)  # nearest level first
    sells.sort(key=lambda g: g.level_index)
    ordered = (buys + sells) if body.intrabar_path == "open_low_high_close" else (sells + buys)
    out: list[GridIntent] = []
    inv, cash, sellable, sold_today = state.inventory_shares, state.cash_available_cny, state.sellable_shares, state.sold_today_shares
    budget_left = body.cash_budget_cny
    for g in ordered:
        if g.side == "buy":
            cost = g.price_raw * g.quantity_shares
            if inv + g.quantity_shares > body.max_inventory_shares or cost > cash or cost > budget_left:
                continue
            inv, cash, budget_left = inv + g.quantity_shares, cash - cost, budget_left - cost
        else:
            if (g.quantity_shares > sellable
                    or sold_today + g.quantity_shares > body.daily_sellable_quota_shares):
                continue
            sellable, sold_today, inv = sellable - g.quantity_shares, sold_today + g.quantity_shares, inv - g.quantity_shares
        out.append(g)
        if body.multi_level_policy == "one_level_per_bar":
            break
    return out
