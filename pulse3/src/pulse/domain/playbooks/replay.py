"""Portfolio replay of playbook signals THROUGH the one ExecutionKernel and ledger.

Why: M2 / FINAL_SYSTEM_SPEC §8 - a strategy earns status only from a result produced by ExecutionKernel + LedgerReducer with
the user's cost profile, never from a private backtest loop. The old backtester (playbooks/backtest.py) is historical
evidence of the SEMANTICS only (next-open fills, one-word board skipped, T+1, costs, lots); this module contains no such loop:
it only decides WHAT to order at each close and feeds one daily bar at a time to ``process_bar`` (the same function the
paper session uses).
Rules: a signal seen on the completed bar of session s is ordered for the open of the next session of THAT stock; a buy limit
is the day's limit-up price and fills at the open unless the open is at the limit (a one-word board is NOT assumed filled:
the default queue scenario fills nothing); exits are close-based (see exits.py) and sell at the next open, a blocked sell
(limit-down, suspension) is retried every session; sizing is equal weight of marks-to-market NAV / max_positions rounded down to
100-share lots; unfilled day orders are cancelled at the end of the session; positions left at the end are marked, not liquidated.
Invariants: all money is Decimal; prices are the series' own (the research series is forward adjusted - research-grade, see
``ReplayResult.price_basis``); deterministic (no randomness); every rejected / unfilled order is counted by reason.
Not covered: auctions, queue priority, impact; the matcher participation cap (10% of the bar volume) is a declared parameter.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from pulse.domain.execution import (NO_FILL_QUEUE, Bar, DailyBarMatcher, ExecutionKernel, LedgerReducer, NullStrategy, OrderIntent,
                                    PathScenario)
from pulse.domain.execution.entrypoints import RunLog, process_bar
from pulse.domain.execution.numeric import MARKET_TIMEZONE
from pulse.domain.market_rules import CostProfile, CostProfileRegistry, ExplicitCalendar, InstrumentSpec, RuleNotVerified

from .exits import ExitRules, HoldState, exit_decision
from .view import InstrumentView, ok

D = Decimal
PRICE_BASIS = "legacy_forward_adjusted_research_only"
LOT = 100
MATCHER_PARTICIPATION = D("0.10")


def d4(x: float) -> Decimal:
    return D(str(round(float(x), 4)))


@dataclass
class ReplayConfig:
    cash: Decimal
    profile: CostProfile
    start: int                      # session index (inclusive)
    end: int                        # session index (exclusive)
    max_positions: int
    max_new_per_day: int
    exits: ExitRules
    path: PathScenario = PathScenario.UNKNOWN
    min_close: float = 0.5


@dataclass
class Trade:
    instrument_id: str
    entry_day: str
    exit_day: str | None
    quantity: int
    entry_price: Decimal
    exit_price: Decimal | None
    reason: str | None

    @property
    def gross_return(self) -> Decimal | None:
        return None if self.exit_price is None else self.exit_price / self.entry_price - 1


@dataclass
class ReplayResult:
    nav: list[tuple[str, Decimal | None]]
    trades: list[Trade]
    counters: dict[str, int]
    fees: Decimal
    final: dict
    events: list[dict]
    initial: dict
    price_basis: str = PRICE_BASIS
    cost_policy_version: str = ""


def _bar(iid: str, v: InstrumentView, i: int, day: date, spec: InstrumentSpec, kernel: ExecutionKernel) -> tuple[Bar, Decimal] | None:
    """Daily Bar with the day's limit prices; the limit equals the open when the open sits at the band (one-word/at-limit)."""
    if i is None or i < 1 or not ok(v.open[i], v.high[i], v.low[i], v.close[i], v.volume[i], v.close[i - 1]) or v.close[i - 1] <= 0:
        return None
    o, h, l_, c = d4(v.open[i]), d4(v.high[i]), d4(v.low[i]), d4(v.close[i])
    prev = d4(v.close[i - 1])
    if prev <= 0 or o <= 0 or l_ <= 0:
        return None
    try:
        band = kernel.price_limits.band(spec, day, prev)
    except RuleNotVerified:
        return None                                   # e.g. SZSE ST after 2026-07-06: never guess a band
    lu, ld = band.limit_up, band.limit_down
    lu_eff = o if o >= lu * D("0.9995") else lu
    ld_eff = o if o <= ld * D("1.0005") else ld
    start = datetime.combine(day, time(9, 30), MARKET_TIMEZONE)
    end = datetime.combine(day, time(15, 0), MARKET_TIMEZONE)
    lo, hi = min(l_, o, c), max(h, o, c)
    return Bar(iid, start, end, o, hi, lo, c, int(v.volume[i]), lu_eff, ld_eff), prev


def replay(*, signals: Mapping[str, Sequence], views: Mapping[str, InstrumentView], specs: Mapping[str, InstrumentSpec],
           sessions: Sequence[str], cfg: ReplayConfig, opening_positions: Mapping[str, Mapping] | None = None,
           run_id: str = "pb") -> ReplayResult:
    days = [date.fromisoformat(s) for s in sessions]
    tail = [days[-1] + timedelta(days=k) for k in range(1, 11) if (days[-1] + timedelta(days=k)).weekday() < 5]
    cal = ExplicitCalendar(days + tail)          # approximate weekdays after the window, only for T+1 dates of the last buys
    initial = {"trading_day": sessions[cfg.start], "opening_cash": str(cfg.cash), "positions": dict(opening_positions or {}),
               "sale_proceeds_reusable": True}
    ledger = LedgerReducer(**initial)
    kernel = ExecutionKernel(ledger, dict(specs), cal, CostProfileRegistry((cfg.profile,)))
    matcher = DailyBarMatcher(MATCHER_PARTICIPATION)
    log = RunLog()
    pos = {iid: {d: k for k, d in enumerate(v.dates)} for iid, v in views.items()}
    counters: dict[str, int] = {}

    def count(key: str, n: int = 1) -> None:
        counters[key] = counters.get(key, 0) + n

    state: dict[str, HoldState] = {}
    trade_open: dict[str, Trade] = {}
    trades: list[Trade] = []
    pending_entries: list[tuple[float, str]] = []  # (score, iid) decided at the previous close
    exit_requests: dict[str, float] = {}           # iid -> fraction of the position to sell
    last_close: dict[str, Decimal] = {}
    nav_series: list[tuple[str, Decimal | None]] = []

    for s in range(cfg.start, cfg.end):
        day, ds = days[s], sessions[s]
        if s > cfg.start:
            kernel.advance_session(day, datetime.combine(day, time(9, 0), MARKET_TIMEZONE))
        # ---- morning: exits first (funds), then entries, one bar each
        work: list[tuple[str, str, float]] = [("exit", iid, frac) for iid, frac in sorted(exit_requests.items())]
        work += [("entry", iid, score) for score, iid in sorted(pending_entries, key=lambda t: (-t[0], t[1]))]
        pending_entries = []
        submitted: list[tuple[str, str, float, str, Bar, int]] = []
        for kind, iid, x in work:                       # phase 1: every order is reserved at the open (events stay time-ordered)
            v, spec = views[iid], specs[iid]
            i = pos[iid].get(ds)
            built = _bar(iid, v, i, day, spec, kernel) if i is not None else None
            if built is None:
                count(f"{kind}_no_bar")
                continue
            bar, prev = built
            band = kernel.price_limits.band(spec, day, prev)
            if kind == "exit":
                qty = ledger.sellable(iid) - ledger.reserved_sell(iid)
                if qty <= 0:
                    continue
                frac_qty = qty if x >= 1.0 else (int(qty * x) // LOT) * LOT
                if frac_qty <= 0:
                    exit_requests.pop(iid, None)
                    continue
                oid = f"{run_id}:x:{iid}:{ds}"
                res = kernel.submit(OrderIntent(oid, iid, "sell", frac_qty, band.limit_down), expected_account_version=ledger.account_version,
                                    at=bar.start, prev_close=prev)
            else:
                if iid in ledger.holdings and ledger.holdings[iid].quantity:
                    continue
                snap = ledger.snapshot_partial({k: last_close[k] for k in last_close})
                nav = snap["nav"]
                if nav is None:
                    count("entry_skipped_missing_marks")
                    continue
                px = band.limit_up * D("1.003")
                qty = (int(min(nav / cfg.max_positions, ledger.cash_available()) / px) // LOT) * LOT
                if qty <= 0:
                    count("entry_skipped_no_cash")
                    continue
                oid = f"{run_id}:e:{iid}:{ds}"
                res = kernel.submit(OrderIntent(oid, iid, "buy", qty, band.limit_up), expected_account_version=ledger.account_version,
                                    at=bar.start, prev_close=prev)
            if not res.accepted:
                count(f"{kind}_rejected:{(res.reason or 'unknown')[:40]}")
                continue
            submitted.append((kind, iid, x, oid, bar, i))
        for kind, iid, x, oid, bar, i in submitted:     # phase 2: match each bar, cancel what is left at the close
            process_bar(kernel, matcher, bar, path=cfg.path, queue=NO_FILL_QUEUE, strategy=NullStrategy(), log=log)
            order = ledger.orders[oid]
            if order.remaining > 0:
                kernel.request_cancel(oid)
                kernel.confirm_cancel(oid, order.filled, bar.end)
            if kind == "entry":
                if order.filled:
                    count("entry_filled")
                    avg = _avg_fill(log, oid)
                    state[iid] = HoldState(i, float(avg), float(avg))
                    t = Trade(iid, ds, None, order.filled, avg, None, None)
                    trade_open[iid] = t
                    trades.append(t)
                else:
                    count("entry_unfilled")
            else:
                if order.filled:
                    still = ledger.holdings.get(iid)
                    left = still.quantity if still else 0
                    if left == 0:
                        exit_requests.pop(iid, None)
                        t = trade_open.pop(iid, None)
                        if t is not None:
                            t.exit_day, t.exit_price, t.reason = ds, _avg_fill(log, oid), "exit"
                        state.pop(iid, None)
                    elif x < 1.0:
                        exit_requests.pop(iid, None)
                        if iid in state:
                            state[iid].scaled_out = True
                    count("exit_filled")
                else:
                    count("exit_unfilled")
        # ---- close: marks, NAV
        for iid, v in views.items():
            i = pos[iid].get(ds)
            if i is not None and ok(v.close[i]):
                last_close[iid] = d4(v.close[i])
        held = {iid: h for iid, h in ledger.holdings.items() if h.quantity}
        snap = ledger.snapshot_partial({iid: last_close.get(iid) for iid in held})
        nav_series.append((ds, snap["nav"]))
        # ---- close: decide tomorrow
        for iid in held:
            st = state.get(iid)
            v, i = views[iid], pos[iid].get(ds)
            if st is None:                                    # opening position: reference = its recorded cost
                h = held[iid]
                st = state[iid] = HoldState(cfg.start - 1 if i is None else min(i, cfg.start), float(h.cost / h.quantity), float(h.cost / h.quantity))
            if i is None:
                continue
            dec = exit_decision(cfg.exits, v, i, st)
            if dec is not None and iid not in exit_requests:
                exit_requests[iid] = dec[1]
                count(f"exit_signal:{dec[0]}")
        free_slots = cfg.max_positions - len(held) - 0
        if free_slots > 0 and s + 1 < cfg.end:
            cands: list[tuple[float, str]] = []
            for iid, sig in signals.items():
                i = pos[iid].get(ds)
                if i is None or sig[i] is None or iid in held or iid in exit_requests:
                    continue
                if not ok(views[iid].close[i]) or views[iid].close[i] < cfg.min_close:
                    continue
                cands.append((float(sig[i]), iid))
            cands.sort(key=lambda t: (-t[0], t[1]))
            pending_entries = cands[:min(free_slots, cfg.max_new_per_day)]
            count("signals", len(cands))
    events = [ledger.recorded_event(eid) for eid in ledger._events]
    final = ledger.snapshot_partial({iid: last_close.get(iid) for iid, h in ledger.holdings.items() if h.quantity})
    return ReplayResult(nav_series, trades, counters, D(str(final["fees"])), final, events, initial, cost_policy_version=cfg.profile.version)


def _avg_fill(log: RunLog, order_id: str) -> Decimal:
    fs = [f for f in log.fills if f.order_id == order_id]
    q = sum(f.quantity for f in fs)
    return sum((f.price * f.quantity for f in fs), D(0)) / q if q else D(0)
