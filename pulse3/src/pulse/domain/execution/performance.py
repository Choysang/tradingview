"""Time-weighted return with external cash flows split out.

Why: REFERENCE_RECONCILIATION_V32 section 9 and execution_standard.md section 5:
TWR must cut segments at every external capital flow and chain them; income and
transaction costs are inside NAV and are never external flows.
Invariants: each segment has no external flow inside it and exact boundary
valuations; begin NAV must be positive. Not covered: annualization, XIRR/MWR,
GIPS compliance claims.
"""
from __future__ import annotations

from decimal import Decimal, localcontext, ROUND_HALF_EVEN
from typing import Callable, Iterable, Mapping

from .ledger import LedgerReducer
from .numeric import D, decimal

def true_time_weighted_return(segments: Iterable[tuple[str, str]]) -> Decimal:
    """Each segment has no external flow inside; exact boundary valuations.

    Income and transaction costs are already inside NAV, never removed as
    capital flows. No annualization and no full GIPS compliance claim.
    """
    with localcontext() as ctx:
        ctx.prec = 50
        ctx.rounding = ROUND_HALF_EVEN
        product, n = D('1'), 0
        for begin, end in segments:
            b = decimal(begin, 'begin_nav', positive=True)
            v = decimal(end, 'end_nav')
            product *= v / b
            n += 1
        if not n:
            raise ValueError('at least one subperiod required')
        return product - 1



def twr_by_flow_segments(initial: Mapping, events: Iterable[dict],
                         marks_at: Callable[[dict | None], Mapping[str, str | Decimal]],
                         end_marks: Mapping[str, str | Decimal]) -> Decimal:
    """TWR of a canonical event stream, cut at every ``capital.deposit/withdraw``.

    ``marks_at(event)`` supplies raw prices at the instant of that event (None for
    the opening); the NAV just before and just after the flow use the same marks,
    so a flow never changes performance. A segment that begins with zero NAV and
    ends with zero NAV (an empty account before its first deposit) carries no capital
    and is skipped. Trades, fees, dividends and tax stay inside NAV.
    """
    ledger = LedgerReducer(**initial)
    begin = D(ledger.snapshot(marks_at(None))['nav'])
    segments: list[tuple[str, str]] = []
    for event in events:
        if event['kind'] in ('capital.deposit', 'capital.withdraw'):
            marks = marks_at(event)
            pre = D(ledger.snapshot(marks)['nav'])
            if not (begin == 0 and pre == 0):
                segments.append((str(begin), str(pre)))
            ledger.apply(event)
            begin = D(ledger.snapshot(marks)['nav'])
        else:
            ledger.apply(event)
    segments.append((str(begin), ledger.snapshot(end_marks)['nav']))
    return true_time_weighted_return(segments)
