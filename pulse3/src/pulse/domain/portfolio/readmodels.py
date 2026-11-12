"""Account read models derived from the ONE ledger's canonical events: order views and time-weighted performance (PEX12, D27).

Why: orders and performance are not new books. They are pure functions of (account events up to ``account_version``, the
knowledge cutoff, explicit valuation marks, declared policies); every number comes from the ledger reducer.
Invariants:
* an event after ``knowledge_as_of`` is unknown to the projection; an order seen only through imported broker fills is
  ``partial`` (its original quantity and limit are inferred, not observed);
* order states from events: ``working`` / ``partially_filled`` / ``filled`` / ``cancelled``. A cancel REQUEST that is still in
  flight leaves no event, so it is not recoverable and the coverage says so; repeated reports never raise ``CumQty`` (the
  reducer rejects a duplicated fill id);
* TWR cuts segments at every external capital flow (``capital.deposit/withdraw``), income and costs stay inside NAV; NAV with
  a missing mark is None (never zero) and then the return is None with the missing instruments named;
* cost stress (x2, x3): fills are re-costed with the scaled profile order by order (cumulative fee rule) and replayed, so the
  result is the account under that cost level, not a post-hoc subtraction; x1 keeps the recorded fees.
Not covered: annualisation, benchmark-relative metrics (the caller supplies the benchmark window), money-weighted return.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Callable, Mapping
from zoneinfo import ZoneInfo

from pulse.domain.execution import LedgerReducer
from pulse.domain.execution.numeric import display
from pulse.domain.market_rules.costs import CostEngine, CostProfile

CST = ZoneInfo("Asia/Shanghai")
ORDER_POLICY = "order-view-events-v1"
FLOW_KINDS = ("capital.deposit", "capital.withdraw")
ZERO = Decimal(0)


def _t(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _day(e: dict) -> date:
    return _t(e["effective_at"]).astimezone(CST).date()


# ---- orders ---------------------------------------------------------------------------------------------------------------------
def orders_from_events(events: list[dict], *, until_sequence: int, knowledge_as_of: datetime, window_start: datetime,
                       window_end: datetime, id_of: Callable[[str, str], uuid.UUID]) -> tuple[list[dict], dict]:
    orders: dict[str, dict] = {}
    for e in events:
        if e["sequence"] > until_sequence or _t(e["effective_at"]) > knowledge_as_of:
            continue
        k, p = e["kind"], e["payload"]
        if k == "order.reserve":
            orders[p["order_id"]] = {"order_id": p["order_id"], "instrument_id": p["instrument_id"], "side": p["side"], "orig": p["quantity"],
                                     "limit": p.get("limit_price"), "filled": 0, "cancelled": False, "first": _t(e["effective_at"]),
                                     "last": _t(e["effective_at"]), "events": [id_of("event", e["event_id"])]}
        elif k == "fill.booked" and p["order_id"] in orders:
            o = orders[p["order_id"]]
            o["filled"] += p["quantity"]
            o["last"] = max(o["last"], _t(e["effective_at"]))
            o["events"].append(id_of("event", e["event_id"]))
        elif k == "order.cancel_confirmed" and p["order_id"] in orders:
            o = orders[p["order_id"]]
            o["cancelled"] = True
            o["last"] = max(o["last"], _t(e["effective_at"]))
            o["events"].append(id_of("event", e["event_id"]))
    out = []
    for o in sorted(orders.values(), key=lambda x: (x["first"], x["order_id"])):
        if not (window_start <= o["first"] < window_end):
            continue
        leaves = max(o["orig"] - o["filled"], 0)
        state = "cancelled" if o["cancelled"] else "filled" if leaves == 0 else "partially_filled" if o["filled"] else "working"
        out.append({"order_id": id_of("order", o["order_id"]), "instrument_id": uuid.UUID(o["instrument_id"]) if _is_uuid(o["instrument_id"]) else id_of("instrument", o["instrument_id"]),
                    "side": o["side"], "state": state, "original_quantity": o["orig"], "cumulative_filled_quantity": o["filled"],
                    "leaves_quantity": 0 if state == "cancelled" else leaves,
                    "limit_price_raw": o["limit"] if o["limit"] and Decimal(o["limit"]) > 0 else None,
                    "account_event_ids": o["events"], "state_observed_at": o["last"],
                    "quality_status": "partial" if o["order_id"].startswith("broker:") else "valid"})
    coverage = {"policy_version": ORDER_POLICY, "expected_count": None, "observed_count": len(out), "missing_count": None,
                "unverifiable_reasons": ["cancel_request_in_flight_leaves_no_event", "order_acknowledgements_not_modelled"]}
    return out, coverage


def _is_uuid(s: str) -> bool:
    try:
        uuid.UUID(s)
        return True
    except ValueError:
        return False


# ---- performance ----------------------------------------------------------------------------------------------------------------
@dataclass
class Perf:
    twr: Decimal | None = None
    segments: int = 0
    nav_end: Decimal | None = None
    fees: Decimal | None = None
    realized: Decimal | None = None
    net_capital: Decimal | None = None
    trades: int = 0
    held_at_end: int = 0
    missing: set = field(default_factory=set)
    reasons: list = field(default_factory=list)


def recost(events: list[dict], profile: CostProfile) -> list[dict]:
    """Fills re-costed under ``profile`` order by order (cumulative-notional rule). Other events untouched."""
    side: dict[str, str] = {}
    notional: dict[str, Decimal] = {}
    charged: dict[str, Decimal] = {}
    out = []
    for e in events:
        if e["kind"] == "order.reserve":
            side[e["payload"]["order_id"]] = e["payload"]["side"]
        elif e["kind"] == "fill.booked":
            p = e["payload"]
            oid = p["order_id"]
            notional[oid] = notional.get(oid, ZERO) + p["quantity"] * Decimal(p["price"])
            cum, _unknown = CostEngine.cumulative_fee(profile, side[oid], notional[oid])
            inc = cum - charged.get(oid, ZERO)
            charged[oid] = cum
            e = {**e, "payload": {**p, "fee": display(inc if inc > 0 else ZERO)}}
        out.append(e)
    return out


def compute_performance(initial: Mapping, events: list[dict], *, start: date, end: date,
                        marks_at: Callable[[date, set[str]], Mapping[str, Decimal | None]], profile: CostProfile | None = None) -> Perf:
    """TWR over [start, end] with flows cut out. ``events`` are canonical, ascending, already limited to the account version."""
    ev = recost(events, profile) if profile is not None else list(events)
    ledger = LedgerReducer(**initial)
    res = Perf()

    def nav(day: date) -> Decimal | None:
        held = {s for s, h in ledger.holdings.items() if h.quantity}
        marks = marks_at(day, held)
        absent = {s for s in held if marks.get(s) is None}
        res.missing |= absent
        return ledger.snapshot_partial({s: marks.get(s) for s in held})["nav"]

    segments: list[tuple[Decimal, Decimal]] = []
    begin: Decimal | None = None
    started = False
    broken = False
    for e in ev:
        d = _day(e)
        if d > end:
            break
        if not started and d >= start:
            begin, started = nav(start), True
        if started and e["kind"] in FLOW_KINDS:
            pre = nav(d)
            ledger.apply(e)
            post = nav(d)
            if begin is None or pre is None or post is None:
                broken = True
            elif not (begin == 0 and pre == 0):
                segments.append((begin, pre))
            begin = post
        else:
            ledger.apply(e)
        if e["kind"] == "fill.booked":
            res.trades += 1
    if not started:
        begin = nav(start)
    final = nav(end)
    res.nav_end = final
    snap = ledger.snapshot_partial({s: marks_at(end, {s}).get(s) for s, h in ledger.holdings.items() if h.quantity})
    res.fees, res.realized, res.net_capital = snap["fees"], snap["realized_gross"], snap["net_external_capital"]
    res.held_at_end = sum(1 for h in ledger.holdings.values() if h.quantity)
    if begin is None or final is None or broken:
        res.reasons.append("mark_missing" if res.missing else "nav_unavailable")
        return res
    if not (begin == 0 and final == 0 and not segments):
        segments.append((begin, final))
    live = [(b, v) for b, v in segments if b != 0]
    if len(live) != len(segments) or not live:
        res.reasons.append("no_capital_in_window")
        return res
    from pulse.domain.execution.performance import true_time_weighted_return
    res.twr = true_time_weighted_return([(str(b), str(v)) for b, v in live])
    res.segments = len(live)
    return res
