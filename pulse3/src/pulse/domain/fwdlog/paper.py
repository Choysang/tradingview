"""Intraday paper replay of ONE recorded session, triggers v2.1: shadow buys B1 / B3, sells S1 / S3 / M3 / policy v2, E059 box shadow (E053-E059).

Why: the old system replays every recorded session on a hypothetical book. Nothing is traded and nothing is booked: this is an EXPERIMENT RECORD (a forward log
dataset), not an account. The book is one block (about 10,000 yuan) of every main-board non-ST name that closed at the limit-up price in the previous
session, bought at that close, so it is sellable today (T+1). Every holding runs every sell rule side by side (S0 open, S1 seal decay, S3 under VWAP, M3 exit,
break-and-sell in three versions, hold to the close, policy v2, policy v3 shadow, and the E059 box shadow). Each rule is paper-filled at the bid of the row that
fired it (at the limit for S1). The shadow buys B1 / B3 keep every fired intent with three queue scenarios; ``triggers.ACTIVE`` is False for both so no fill touches
the book, and a shadow fill is acquired today, hence NOT sellable today (T+1: eligible on the next trading day).
Why a log and not the one ledger: ten sell rules sell the SAME shares of the same hypothetical book; a ledger can book one sale per share. Booking would change
the experiment, so the pre-registered record is kept as rows (``RULES_VERSION``) and the T+1 rule is checked as a rule on the dates.
Invariants: point in time (every decision at clock t reads only snapshots with known_at <= t); Decimal everywhere; missing stays None; returns are D0 only
(fill to today's close; the overnight and next-day legs need the next recorded session).
Not covered: B1 / B3 booking (shadow by pre-registration), the author-consensus modules, the next-day legs.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Callable, Mapping, Sequence

from . import triggers as T
from .board import BreakState, CalEvent, break_step, catalyst_pending, SECTOR_DUMP_PCT
from .box5 import Bar, bar_start, first_signal, idx_label
from .common import CST, D, HUNDRED, ZERO, median
from .heat import MarketState
from .queue import QueueRow, Scenario, simulate

BLOCK_YUAN = D(10_000)
LOT = D(100)
MAIN = ("600", "601", "603", "605", "000", "001", "002", "003")
ONE_POINT_ONE = D("1.1")
PAPER_CHECK_AT = 20                    # recorded sessions before the first comparison (E055 / E056 / E057)


def limit_price(prev_close: Decimal) -> Decimal:
    """previous close x 1.1 rounded half up to the cent."""
    return (prev_close * ONE_POINT_ONE).quantize(D("0.01"), rounding=ROUND_HALF_UP)


def previous_limit_ups(rows: Mapping[str, tuple[Decimal | None, Decimal | None]], names: Mapping[str, str | None]) -> list[tuple[str, Decimal]]:
    """Main-board non-ST names that closed at the limit-up price: ``rows`` code -> (close of the last session, close before it). Code order."""
    out = []
    for code in sorted(rows):
        close, pc = rows[code]
        if close is None or pc is None or not code.startswith(MAIN) or pc <= ZERO:
            continue
        if "ST" in (names.get(code) or ""):
            continue
        if close >= limit_price(pc) - T.EPS:
            out.append((code, close))
    return out


@dataclass(frozen=True)
class SectorContext:
    """Per full-snapshot known time: sealed names per industry, each industry's median move since the previous full snapshot, live failed-board rate."""
    times: tuple[datetime, ...] = ()
    counts: tuple[Mapping[str, int], ...] = ()
    moves: tuple[Mapping[str, Decimal], ...] = ()
    fail: tuple[Decimal | None, ...] = ()

    def at(self, t: datetime) -> tuple[Mapping[str, int], Mapping[str, Decimal], Decimal | None]:
        i = bisect.bisect_right(self.times, t) - 1
        return (self.counts[i], self.moves[i], self.fail[i]) if i >= 0 else ({}, {}, None)


@dataclass(frozen=True)
class PaperInputs:
    day: date
    names: Mapping[str, str | None]
    book: Sequence[tuple[str, Decimal]]                       # previous-session limit-ups (code, close), code order
    session: Mapping[str, Sequence[T.Snap]]                   # per code: continuous-session snapshots in known-time order (book + B3 candidates)
    finals: Mapping[str, T.Snap]                              # 09:25 final auction rows
    close_px: Mapping[str, Decimal]
    prev_amount: Mapping[str, Decimal]
    market: MarketState
    sector: SectorContext
    sw2: Mapping[str, str]
    events: Sequence[CalEvent] | None                         # already linked to stocks; None = no calendar
    minute_bars: Mapping[str, Sequence[Bar]]                  # box shadow bars of the book names
    t_open: datetime
    t_close: datetime
    t_final: datetime
    instrument_id: Callable[[str], str]
    next_session: date                                        # T+1: the first trading day after ``day``
    book_day: date | None = None


def _pct(a: Decimal, b: Decimal) -> Decimal:
    return (a / b - 1) * HUNDRED


def _buy_fill(rows: Sequence[T.Snap], arrive: datetime, lots: Decimal, close_t: datetime, limit: Decimal) -> dict[Scenario, tuple[Decimal, Decimal | None]]:
    """(filled lots, price) per scenario: below the limit at arrival -> the ask 1 of the first row after; else the queue model."""
    first = next((r for r in rows if r.known_at >= arrive), None)
    if first is not None and first.price is not None and first.ask1_px and first.ask1_px < limit - T.EPS:
        return {sc: (lots, first.ask1_px) for sc in Scenario}
    snaps = [QueueRow(r.quote_at, r.price, r.limit_up, r.bid1_px, r.bid1_lots, r.volume_lots) for r in rows if r.quote_at is not None]
    sim = simulate(snaps, arrive, lots, close_t)
    return {sc: (f.filled_lots, limit if f.filled_lots else None) for sc, f in sim.items()}


def _qty(price: Decimal) -> int:
    return int(max(LOT, (BLOCK_YUAN / price / LOT).to_integral_value(rounding="ROUND_DOWN") * LOT))


def buys(inp: PaperInputs) -> list[dict]:
    """Every fired B1 / B3 intent with its three queue scenarios (shadow records)."""
    out: list[dict] = []

    def record(rule: str, code: str, t: datetime, limit: Decimal, fills: Mapping[Scenario, tuple[Decimal, Decimal | None]], extra: dict | None = None) -> None:
        qty = _qty(limit)
        cp = inp.close_px.get(code)
        for sc, (lots, px) in fills.items():
            filled = int(min(lots * LOT, D(qty)) // LOT * LOT)
            status = "FILLED" if filled == qty else "PARTIALLY_FILLED" if filled else "UNFILLED"
            out.append({"opportunity_id": f"{inp.day}-{rule}-{code}-{sc.value}", "intent_id": f"{inp.day}-{rule}-{code}", "instrument_id": inp.instrument_id(code),
                        "code": code, "name": inp.names.get(code), "rule": rule, "strategy_ref": f"{rule}/{T.TRIGGERS_VERSION}", "decision_time": t, "status": status,
                        "scenario": sc.value, "target_quantity": qty, "filled_quantity": filled, "avg_fill_price": px if filled and px else None,
                        "reference_price": limit, "unfilled_reason": None if filled else "LIMIT_UP_QUEUE", "booked": bool(T.ACTIVE.get(rule, False)),
                        "d0_return_pct": _pct(cp, px) if filled and px and cp else None, **(extra or {})})

    for code, _ in inp.book:                                   # B1
        f = inp.finals.get(code)
        rows = inp.session.get(code)
        if f is None or rows is None or not T.b1(f, inp.prev_amount.get(code)) or not f.limit_up:
            continue
        record("B1", code, inp.t_final, f.limit_up, _buy_fill(rows, inp.t_final, max(D(1), BLOCK_YUAN / f.limit_up // LOT), inp.t_close, f.limit_up))
    for code, rows in inp.session.items():                      # B3
        if not code.startswith(MAIN) or "ST" in (inp.names.get(code) or ""):
            continue
        trig = T.B3()
        for r in rows:
            if trig.feed(r):
                record("B3", code, r.known_at, r.limit_up, _buy_fill(rows, r.known_at, max(D(1), BLOCK_YUAN / r.limit_up // LOT), inp.t_close, r.limit_up),
                       {"live_fail_rate": inp.sector.at(r.known_at)[2]})
                break
    return out


def sells(inp: PaperInputs) -> tuple[list[dict], dict]:
    """Every sell rule on every book holding. Returns (rows, counters)."""
    out: list[dict] = []
    held_total = {"tape": 0, "strict": 0}
    no_minute = 0
    for code, cost in inp.book:
        qty = _qty(cost)
        rs = inp.session.get(code)
        if not rs:
            out.append({"code": code, "name": inp.names.get(code), "rule": "ALL", "status": "SUSPENDED", "qty": qty, "cost": cost})
            continue
        first = rs[0]
        exits: dict[str, tuple[datetime, Decimal, str]] = {"S0_open": (first.known_at, first.bid1_px or first.price, "open")}
        s1, s3 = T.S1(), T.S3()
        held = {"tape": 0, "strict": 0}
        m3 = {"tape": BreakState.SEALED, "strict": BreakState.SEALED}
        was_sealed = False
        ind = inp.sw2.get(code)
        for r in rs:
            t = r.known_at
            bid = r.bid1_px or r.price
            if "S1_decay" not in exits and s1.feed(r):
                exits["S1_decay"] = (t, r.limit_up, f"bid at limit {r.bid1_lots} lots, peak {s1.peak:.0f}")
            below = s3.below_s(r)
            if "S3_vwap" not in exits and below >= T.VWAP_GRACE_S:
                exits["S3_vwap"] = (t, bid, f"under VWAP {below:.0f} s")
            sealed = T.sealed(r)
            was_sealed |= sealed
            if was_sealed:
                counts, moves, _ = inp.sector.at(t)
                peers = None if ind is None else counts.get(ind, 0) - (1 if sealed else 0)
                dump = None if ind is None or ind not in moves else moves[ind] <= D(SECTOR_DUMP_PCT)
                cat = catalyst_pending(inp.events, code, t) if inp.events else None
                above = None if not r.vwap or r.price is None else r.price >= r.vwap
                for variant in m3:
                    if m3[variant] is BreakState.EXIT:
                        continue
                    chk = break_step(m3[variant], t, sealed, above, below, peers, cat, dump, variant=variant)
                    m3[variant] = chk.state
                    if chk.state is BreakState.EXIT:             # the exit rule does not depend on the variant
                        exits.setdefault("M3_exit", (t, bid, chk.reason))
            if was_sealed and not sealed:                        # break-and-sell and its M3 versions
                exits.setdefault("Sbreak_open", (t, bid, "first break"))
                for variant, state_now in m3.items():
                    key = f"Sbreak_{variant}"
                    if key in exits:
                        continue
                    if state_now is BreakState.BENIGN:
                        held[variant] += 1
                    else:
                        exits[key] = (t, bid, f"break, M3 {state_now.value}")
        cp = inp.close_px.get(code) or rs[-1].price
        exits["HOLD_close"] = (inp.t_close, cp, "close")
        intraday = sorted((exits[k] for k in ("S1_decay", "M3_exit") if k in exits), key=lambda x: x[0])
        first_intraday = intraday[0] if intraday else None
        exits["POLICY_v2"] = T.sell_policy(True, inp.market.heat, first_intraday, exits["S0_open"][:2], exits["HOLD_close"][:2])
        exits["POLICY_v3_shadow"] = T.sell_policy_v3(T.sealed(first), first_intraday, exits["S0_open"][:2], exits["HOLD_close"][:2])
        bars = inp.minute_bars.get(code)
        shadow = {"S_Box5m"}
        if bars:
            sig = first_signal(bars, -1)
            if sig is not None:
                exits["S_Box5m"] = (bar_start(inp.day, sig[1]), sig[2],
                                    f"box break down, signal bar {idx_label(sig[0].idx)}, box {sig[0].box_low}-{sig[0].box_high}, redraw {sig[0].redraw_count}")
        else:
            no_minute += 1
        for rule, (t, px, why) in exits.items():
            out.append({"code": code, "name": inp.names.get(code), "rule": rule, "status": "FILLED", "qty": qty, "cost": cost, "time": t, "price": px, "why": why,
                        "return_pct": _pct(px, cost), "vs_close_pct": _pct(px, cp), "sellable_qty": qty, "t1_status": "SELLABLE_TODAY",
                        "held_back_tape": held["tape"], "held_back_strict": held["strict"], "shadow": rule in shadow})
        held_total["tape"] += held["tape"]
        held_total["strict"] += held["strict"]
    return out, {"held_back": held_total, "no_minute_bars": no_minute}


def t1_checks(buy_rows: Sequence[dict], inp: PaperInputs) -> list[dict]:
    """T+1 on the shadow buys: a BASE-scenario fill is acquired today and is eligible to sell on the next trading day, never today."""
    return [{"code": b["code"], "rule": b["rule"], "filled_quantity": b["filled_quantity"], "eligible_on": inp.next_session,
             "sellable_today": inp.next_session <= inp.day, "t1_status": "NOT_SELLABLE_TODAY" if not inp.next_session <= inp.day else "SELLABLE (bug)"}
            for b in buy_rows if b["scenario"] == "BASE" and b["filled_quantity"]]


def _stats(values: Sequence[Decimal]) -> tuple[Decimal | None, Decimal | None]:
    if not values:
        return None, None
    return sum(values, ZERO) / D(len(values)), median(values)


def day_summary(inp: PaperInputs, buy_rows: Sequence[dict], sell_rows: Sequence[dict], t1: Sequence[dict], counters: Mapping) -> dict:
    """The per-rule tables the old script prints (D0, no cost)."""
    btab = []
    keys = sorted({(b["rule"], b["scenario"]) for b in buy_rows}, key=lambda k: (k[0], ("OPTIMISTIC", "BASE", "PESSIMISTIC").index(k[1])))
    for rule, sc in keys:
        g = [b for b in buy_rows if b["rule"] == rule and b["scenario"] == sc]
        mean_, med = _stats([b["d0_return_pct"] for b in g if b["d0_return_pct"] is not None])
        btab.append({"rule": rule, "scenario": sc, "triggered": len(g), "filled": sum(1 for b in g if b["status"] == "FILLED"),
                     "partial": sum(1 for b in g if b["status"] == "PARTIALLY_FILLED"), "unfilled": sum(1 for b in g if b["status"] == "UNFILLED"),
                     "d0_mean_pct": mean_, "d0_median_pct": med})
    stab = []
    done = [s for s in sell_rows if s["status"] == "FILLED"]
    for rule in dict.fromkeys(s["rule"] for s in done):
        g = [s for s in done if s["rule"] == rule]
        vs = [s["vs_close_pct"] for s in g]
        stab.append({"rule": rule, "triggered": len(g), "mean_return_pct": _stats([s["return_pct"] for s in g])[0], "mean_vs_close_pct": _stats(vs)[0],
                     "median_vs_close_pct": _stats(vs)[1], "better_than_close_share": D(sum(1 for v in vs if v > ZERO)) / D(len(vs)), "shadow": g[0]["shadow"]})
    ms = inp.market
    return {"day": inp.day.isoformat(), "triggers_version": T.TRIGGERS_VERSION, "book_day": None if inp.book_day is None else inp.book_day.isoformat(),
            "book_size": len(inp.book), "events_linked": len(inp.events or ()), "booked": dict(T.ACTIVE),
            "market": {"day": ms.day.isoformat(), "limit_ups": ms.limit_ups, "failed_rate": None if ms.failed_rate is None else D(ms.failed_rate.numerator) / D(ms.failed_rate.denominator),
                       "heat": ms.heat, "failed": ms.failed},
            "buys": btab, "sells": stab, "t1": {"booked_buys": len(t1), "not_sellable_today": sum(1 for x in t1 if x["t1_status"] == "NOT_SELLABLE_TODAY")},
            "held_back": dict(counters["held_back"]), "no_minute_bars": counters["no_minute_bars"]}


def replay_day(inp: PaperInputs) -> dict:
    """One session: ``{"buys": [...], "sells": [...], "t1": [...], "summary": {...}}`` (rows keep Decimals and aware datetimes)."""
    buy_rows = buys(inp)
    sell_rows, counters = sells(inp)
    t1 = t1_checks(buy_rows, inp)
    return {"buys": buy_rows, "sells": sell_rows, "t1": t1, "summary": day_summary(inp, buy_rows, sell_rows, t1, counters)}
