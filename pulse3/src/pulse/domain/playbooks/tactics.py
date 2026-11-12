"""Tactic replay: exit rules, scale-out and intraday T (reverse / forward) measured against a same-inventory HOLD baseline.

Why: LEGACY inventory "Exit lab" + PEX06 scheduleTacticReplay. A tactic is judged only by the cost-after total NAV of an
ACTIVE account minus a HOLD account that started from the same cash, the same opening lots and sees the same terminal
prices (`execution.comparison.compare_with_hold`): the NAV difference is bridged exactly into cash, inventory-value and
fee deltas and an unfinished leg shows up as an inventory shortfall priced at the terminal mark. Everything runs through the
ExecutionKernel (fees from the cost profile, T+1 from the lot rules, price bands, volume-capped partial fills).
Invariants: opening floating profit is NOT platform income (it is in both accounts); daily bars cannot prove the order of the
intraday high and low, so T legs only complete under a DECLARED path scenario - the result is reported for both OHLC and OLHC
and the spread between them is the path sensitivity; a leg that did not fill stays unfilled (never assumed); a parameter sweep
returns ALL variants (the caller stores all, not the best).
Not covered: minute-level fills (the data plane has minute bars only for a few codes), auctions, queue position.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

from pulse.domain.execution import (NO_FILL_QUEUE, BacktestReplay, DailyBarMatcher, ExecutionKernel, HoldComparison, Leg, LedgerReducer,
                                    LegPlan, LegPlanStrategy, PathScenario, compare_with_hold)
from pulse.domain.execution.numeric import MARKET_TIMEZONE
from pulse.domain.market_rules import CostProfile, CostProfileRegistry, ExplicitCalendar, InstrumentSpec

from .exits import ExitRules
from .replay import MATCHER_PARTICIPATION, ReplayConfig, ReplayResult, _bar, d4, replay
from .view import InstrumentView

D = Decimal
TICK = D("0.01")


@dataclass(frozen=True)
class TacticResult:
    kind: str
    label: str
    path: str
    comparison: HoldComparison
    legs_completed: int
    legs_attempted: int
    fees: Decimal
    detail: dict


def _tick(x: Decimal) -> Decimal:
    return (x / TICK).to_integral_value(ROUND_HALF_UP) * TICK


def _marks(views: Mapping[str, InstrumentView], sessions: Sequence[str], end: int) -> dict[str, Decimal]:
    last = sessions[end - 1]
    out: dict[str, Decimal] = {}
    for iid, v in views.items():
        idx = max((k for k, d in enumerate(v.dates) if d <= last and v.close[k] is not None), default=None)
        if idx is not None:
            out[iid] = d4(v.close[idx])
    return out


def opening_positions(views: Mapping[str, InstrumentView], sessions: Sequence[str], start: int,
                      lots: Mapping[str, tuple[int, Decimal]]) -> dict[str, dict]:
    """{iid: {"quantity", "cost"}} from (quantity, recorded cost CNY) per instrument."""
    return {iid: {"quantity": q, "cost": str(cost)} for iid, (q, cost) in lots.items()}


def run_exit_rules(*, rules: ExitRules, label: str, views: Mapping[str, InstrumentView], specs: Mapping[str, InstrumentSpec],
                   sessions: Sequence[str], start: int, end: int, cash: Decimal, lots: Mapping[str, tuple[int, Decimal]],
                   profile: CostProfile, run_id: str = "tx") -> tuple[TacticResult, ReplayResult]:
    cfg = ReplayConfig(cash, profile, start, end, max(len(lots), 1), 0, rules)
    r = replay(signals={}, views=views, specs=specs, sessions=sessions, cfg=cfg,
               opening_positions=opening_positions(views, sessions, start, lots), run_id=run_id)
    cmp = compare_with_hold(r.initial, r.events, _marks(views, sessions, end))
    sold = r.counters.get("exit_filled", 0)
    return TacticResult("exit_rules", label, "n/a", cmp, sold, sold, r.fees, {"rules": rules.to_json(), "counters": r.counters}), r


def run_intraday_t(*, mode: str, up_pct: float, rebuy_pct: float, share: float, path: PathScenario, views: Mapping[str, InstrumentView],
                   specs: Mapping[str, InstrumentSpec], sessions: Sequence[str], start: int, end: int, cash: Decimal,
                   lots: Mapping[str, tuple[int, Decimal]], profile: CostProfile, label: str | None = None) -> TacticResult:
    """mode 'reverse' (倒T: sell ``share`` of the base at prev_close*(1+up), buy back ``rebuy`` lower) or 'forward' (正T: buy at
    prev_close*(1-up), sell the same quantity of OLD base shares ``rebuy`` higher). One plan per instrument per session."""
    if mode not in ("reverse", "forward"):
        raise ValueError("mode must be reverse or forward")
    days = [date.fromisoformat(s) for s in sessions]
    pos = opening_positions(views, sessions, start, lots)
    initial = {"trading_day": sessions[start], "opening_cash": str(cash), "positions": pos, "sale_proceeds_reusable": True}
    ledger = LedgerReducer(**initial)
    tail = [days[-1] + timedelta(days=k) for k in range(1, 11) if (days[-1] + timedelta(days=k)).weekday() < 5]
    kernel = ExecutionKernel(ledger, dict(specs), ExplicitCalendar(days + tail), CostProfileRegistry((profile,)))
    matcher = DailyBarMatcher(MATCHER_PARTICIPATION)
    index = {iid: {d: k for k, d in enumerate(v.dates)} for iid, v in views.items()}
    attempted = completed = 0
    unfinished: dict[str, int] = {}
    for s in range(start, end):
        day, ds = days[s], sessions[s]
        if s > start:
            kernel.advance_session(day, datetime.combine(day, time(9, 0), MARKET_TIMEZONE))
        for iid in sorted(lots):
            i = index[iid].get(ds)
            built = None if i is None else _bar(iid, views[iid], i, day, specs[iid], kernel)
            if built is None:
                continue
            bar, prev = built
            base = ledger.sellable(iid) - ledger.reserved_sell(iid)
            q = (int(base * share) // 100) * 100
            if q <= 0:
                continue
            u, r = D(str(up_pct)) / 100, D(str(rebuy_pct)) / 100
            if mode == "reverse":
                p1 = _tick(prev * (1 + u))
                legs = (Leg("sell", "sell", q, p1), Leg("buy", "buy", q, _tick(p1 * (1 - r)), after="sell"))
            else:
                p1 = _tick(prev * (1 - u))
                legs = (Leg("buy", "buy", q, p1), Leg("sell", "sell", q, _tick(p1 * (1 + r)), after="buy"))
            plan = LegPlan(f"t:{mode}:{iid}:{ds}", iid, legs, prev_close=prev)
            strat = LegPlanStrategy(plan)
            BacktestReplay(kernel, matcher, path=path, queue=NO_FILL_QUEUE, strategy=strat).run([bar])
            strat.expire(kernel, bar.end)
            rep = strat.report(kernel)
            attempted += 1
            completed += 1 if rep.complete else 0
            if not rep.complete and rep.net_share_delta:
                unfinished[iid] = unfinished.get(iid, 0) + rep.net_share_delta
    events = [ledger.recorded_event(eid) for eid in ledger._events]
    cmp = compare_with_hold(initial, events, _marks(views, sessions, end))
    return TacticResult("intraday_t_" + mode, label or f"{'倒T' if mode == 'reverse' else '正T'} ±{up_pct:g}%/{rebuy_pct:g}%", path.value, cmp,
                        completed, attempted, D(str(ledger.snapshot_partial({k: v for k, v in _marks(views, sessions, end).items()})["fees"])),
                        {"mode": mode, "up_pct": up_pct, "rebuy_pct": rebuy_pct, "share": share, "unfinished_shares": unfinished})
