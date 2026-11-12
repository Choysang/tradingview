"""Holding baseline versus active (T / grid) comparison on one account definition.

Why: M2-E05 / FINAL_SYSTEM_SPEC section 7.2 - improvement is judged by cost-after
total NAV against a hold account with the same initial cash, inventory, cost,
external flows and the same terminal prices. A lower displayed cost basis, a single
profitable leg or summed independent strategy NAVs is not evidence.
Invariants: baseline = replay of the same initial state and only the passive events
(session advance, external capital flows, pure splits); the NAV difference is
bridged exactly into cash, receivable/payable, dividend and inventory-value deltas
(asserted); fees and remaining inventory are reported; an unfinished plan shows up
as an inventory shortfall that is priced at the terminal price.
Not covered: dividends/tax change entitlement per account, so a stream containing
them needs an explicit ``baseline_events``; TWR of both accounts is available via
``performance.twr_by_flow_segments``.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from .ledger import LedgerReducer
from .numeric import D, ZERO

PASSIVE_KINDS = {'session.open', 'capital.deposit', 'capital.withdraw', 'corporate.split_effective'}
ENTITLEMENT_KINDS = {'dividend.accrued', 'dividend.paid', 'tax.withheld'}


@dataclass(frozen=True)
class HoldComparison:
    baseline_nav: Decimal
    active_nav: Decimal
    incremental_pnl: Decimal
    baseline_fees: Decimal
    active_fees: Decimal
    cash_delta: Decimal  # settled cash + receivable - payable + dividend receivable
    inventory_value_delta: Decimal
    baseline_inventory: Mapping[str, int]
    active_inventory: Mapping[str, int]
    inventory_shortfall: Mapping[str, int]  # baseline - active (positive: active holds less)

    def bridge_holds(self) -> bool:
        return self.incremental_pnl == self.cash_delta + self.inventory_value_delta


def passive_events(events: list[dict]) -> list[dict]:
    """Passive subset of an event stream, renumbered, for the hold baseline."""
    if any(e['kind'] in ENTITLEMENT_KINDS for e in events):
        raise ValueError('dividend/tax entitlement differs per account: pass explicit baseline_events')
    out = []
    for e in events:
        if e['kind'] in PASSIVE_KINDS:
            c = dict(e)
            c['sequence'] = len(out) + 1
            out.append(c)
    return out


def _position_value(book: LedgerReducer, marks: Mapping[str, str | Decimal]) -> Decimal:
    return sum((h.quantity * D(marks[s]) for s, h in book.holdings.items() if h.quantity), ZERO)


def compare_with_hold(initial: Mapping, active_events: list[dict], marks: Mapping[str, str | Decimal],
                      baseline_events: list[dict] | None = None) -> HoldComparison:
    active = LedgerReducer(**initial)
    for e in active_events:
        active.apply(e)
    base = LedgerReducer(**initial)
    for e in (passive_events(active_events) if baseline_events is None else baseline_events):
        base.apply(e)
    a, b = active.snapshot(marks), base.snapshot(marks)

    def liquid(s: dict) -> Decimal:
        return (D(s['cash_settled']) + D(s['trade_receivable']) - D(s['trade_payable'])
                + D(s['dividend_receivable']))

    names = set(active.holdings) | set(base.holdings)
    qa = {s: active.holdings[s].quantity if s in active.holdings else 0 for s in names}
    qb = {s: base.holdings[s].quantity if s in base.holdings else 0 for s in names}
    return HoldComparison(
        baseline_nav=D(b['nav']), active_nav=D(a['nav']), incremental_pnl=D(a['nav']) - D(b['nav']),
        baseline_fees=D(b['fees']), active_fees=D(a['fees']),
        cash_delta=liquid(a) - liquid(b),
        inventory_value_delta=_position_value(active, marks) - _position_value(base, marks),
        baseline_inventory=qb, active_inventory=qa, inventory_shortfall={s: qb[s] - qa[s] for s in names})
