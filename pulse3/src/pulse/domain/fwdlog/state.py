"""E089 market-state forward log with the extended recorder's daily health (pre-registration ``2026-11-09-batch5-preregistration.md`` section 2).

Why: E074 left the hypothesis "a crash day with a broad-ETF volume surge is followed by an above-average 5 days"; this log records the state of
every session so it can be tested when 10 independent episodes exist. It is DESCRIPTIVE: it changes no buy or sell rule.
Per session one row: ratio = the 7 broad ETFs' summed amount / median of the previous 20 daily sums; surge = ratio >= 2.0; panic_support = surge AND
main-board equal-weight change <= -3 percent; late_share = share of the ETF amount after 14:30; late_heavy = late_share >= 0.30 (no historical
calibration); futures basis of the main contract (largest open interest) of IF/IH/IC/IM against its index; cross-market changes; recorder health.
Invariants: an unknown value is None, never 0 or False; an ETF sum needs ALL seven ETFs polled after the close (a recorder that died at 10:00
must not report a quiet day) and only rows stamped with the session date count (a holiday feed repeats the last session); a session with no
extended recording still gets a row (``ext_missing``), so a gap shows; a day a recorder reported as "not a trading day" gets none (caller).
Not covered: reading files (``application.bf_fwdlog_inputs``), the thresholds' change (a change is a new experiment).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Mapping, Sequence

from .common import CST, D, HUNDRED, ZERO, UTC, at_clock, mean, median, q

ETFS = ("sh510300", "sh510310", "sh510050", "sz159919", "sh510500", "sh512100", "sz159845")
VWAP_ETF, LOT = "sh510300", D(100)
INDEX_OF = {"IF": "sh000300", "IH": "sh000016", "IC": "sh000905", "IM": "sh000852"}
CONTEXT = {"hsi": "hf_HSI", "gold": "hf_GC", "oil": "hf_CL", "a50": "em_CN00Y", "cnh": "em_USDCNH"}
MINUTE_SYMBOLS = ("em_CN00Y", "em_USDCNH")
POLL_KINDS = ("grid", "close")           # not "once" (a smoke test) and not "minute" (bars)
CONTRACT = re.compile(r"^(IF|IH|IC|IM)\d{4}$")
WINDOW, RATIO, PANIC_PCT, LATE_SHARE = 20, D(2), D(-3), D("0.30")
LATE_FROM, DAY_END, SESSION_OPEN, SESSION_CLOSE = time(14, 30), time(15, 5), time(9, 30), time(15, 0)
EPISODE_GAP, H1_EPISODES, NEXT = 10, 10, (1, 5, 20)
SOURCE_FAIL_DAYS, CFFEX_SLOW_DAYS, CFFEX_SLOW_MIN, LOST_LOOKBACK = 5, 3, D(4), 5
START = date(2026, 11, 10)               # the first session recorded end to end by the fixed recorder (E087)
NOT_TRADING = "not a trading day"
NO_ETF_FLAGS = {"etf_amount": None, "n_etf": 0, "late_share": None, "vwap_gap_pct": None, "ratio": None, "surge": None,
                "panic_support": None, "late_heavy": None}


@dataclass(frozen=True)
class ExtRow:
    """One recorded extended-intraday row (ETF, index, future, A50, CNH ...)."""
    symbol: str
    kind: str
    poll_at: datetime
    quote_at: datetime | None
    price: Decimal | None
    prev_close: Decimal | None = None
    amount: Decimal | None = None
    volume: Decimal | None = None
    open_interest: Decimal | None = None
    open: Decimal | None = None


def _pct(price: Decimal | None, base: Decimal | None) -> Decimal | None:
    """(price / base - 1) * 100 to 4 places; None when either is missing or zero (as the old log: falsy means unknown)."""
    if not price or not base:
        return None
    return q((price / base - 1) * HUNDRED, 4)


def _clock(t: datetime) -> time:
    return t.astimezone(CST).time()


def last_of(rows: Sequence[ExtRow], day: date, end: time) -> dict[str, ExtRow]:
    """Per symbol, the last row with a price polled up to ``end`` today (poll time order; later input order wins a tie)."""
    lo, hi = at_clock(day, time(0, 0)), at_clock(day, end)
    best: dict[str, tuple[datetime, int, ExtRow]] = {}
    for i, r in enumerate(rows):
        if r.price is None or not (lo <= r.poll_at <= hi):
            continue
        cur = best.get(r.symbol)
        if cur is None or (r.poll_at, i) >= (cur[0], cur[1]):
            best[r.symbol] = (r.poll_at, i, r)
    return {s: v[2] for s, v in best.items()}


def first_in(rows: Sequence[ExtRow], day: date, start: time, slack_min: int, kinds: tuple[str, ...]) -> dict[str, ExtRow]:
    """Per symbol, the first row of ``kinds`` polled in [start, start + slack] today (the old ``at``: slack ends at most at minute 59)."""
    lo = at_clock(day, start)
    hi = at_clock(day, time(start.hour, min(59, start.minute + slack_min)))
    out: dict[str, tuple[datetime, int, ExtRow]] = {}
    for i, r in enumerate(rows):
        if r.kind not in kinds or not (lo <= r.poll_at <= hi):
            continue
        cur = out.get(r.symbol)
        if cur is None or (r.poll_at, i) < (cur[0], cur[1]):
            out[r.symbol] = (r.poll_at, i, r)
    return {s: v[2] for s, v in out.items()}


def fresh_last(rows: Sequence[ExtRow], day: date, end: time) -> dict[str, ExtRow]:
    """As ``last_of`` without a symbol whose last row is from before 14:30 (the recorder died, or the source did)."""
    return {s: r for s, r in last_of(rows, day, end).items() if _clock(r.poll_at) >= LATE_FROM}


def etf_state(rows: Sequence[ExtRow], day: date) -> dict:
    """The seven ETFs' cumulative amount at the close and at 14:30, and 510300's close against its day's average price."""
    cum = [r for r in rows if r.symbol in ETFS and r.amount is not None and r.kind in POLL_KINDS and r.quote_at is not None
           and r.quote_at.astimezone(CST).date() == day]
    if not cum:
        return {"etf_amount": None, "n_etf": 0, "late_share": None, "vwap_gap_pct": None}
    end = {s: r for s, r in last_of(cum, day, DAY_END).items() if _clock(r.poll_at) >= SESSION_CLOSE}
    state = {"etf_amount": None, "n_etf": len(end), "late_share": None, "vwap_gap_pct": None}
    v = end.get(VWAP_ETF)
    if v is not None and v.volume:
        state["vwap_gap_pct"] = _pct(v.price, v.amount / (v.volume * LOT))
    if len(end) < len(ETFS):
        return state
    total = sum((r.amount for r in end.values()), ZERO)
    state["etf_amount"] = q(total, 4)
    late = {s: r.amount for s, r in first_in(cum, day, LATE_FROM, 3, POLL_KINDS).items()}
    if set(late) == set(end) and total > ZERO:
        state["late_share"] = q((total - sum(late.values(), ZERO)) / total, 4)
    return state


def market_state(closing: Mapping[str, tuple[Decimal | None, Decimal | None]]) -> dict:
    """Main-board equal-weight change and up-share from the closing snapshots (code -> (price, prev_close)); price and prev_close must be > 0."""
    r = [p / c - 1 for p, c in closing.values() if p is not None and c is not None and p > ZERO and c > ZERO]
    if not r:
        return {"ew_pct": None, "up_share": None, "n_ew": 0}
    return {"ew_pct": q(mean(r) * HUNDRED, 4), "up_share": q(D(sum(1 for x in r if x > ZERO)) / D(len(r)), 4), "n_ew": len(r)}


def context_state(rows: Sequence[ExtRow], day: date) -> dict:
    last = fresh_last(rows, day, SESSION_CLOSE)
    out = {}
    for name, symbol in CONTEXT.items():
        r = last.get(symbol)
        out[f"{name}_pct"] = _pct(r.price, r.prev_close) if r else None
    idx = fresh_last(rows, day, DAY_END).get("sh000001")
    out["sh_pct"] = _pct(idx.price, idx.prev_close) if idx else None
    return out


def futures_state(rows: Sequence[ExtRow], day: date) -> dict[str, dict | None]:
    """The main contract (largest open interest at the last poll) of each product against its index at the close."""
    last = fresh_last(rows, day, DAY_END)
    out: dict[str, dict | None] = {}
    for product, index in INDEX_OF.items():
        cands = [r for s, r in last.items() if (m := CONTRACT.match(s)) and m.group(1) == product and r.open_interest is not None]
        if not cands or index not in last:
            out[product] = None
            continue
        main = max(cands, key=lambda r: (r.open_interest, r.symbol))
        out[product] = {"contract": main.symbol, "basis_pct": _pct(main.price, last[index].price), "quote_at": main.quote_at}
    return out


def cffex_gap(rows: Sequence[ExtRow], day: date) -> Decimal | None:
    """Median minutes between changes of the IF main contract's quote time during the session."""
    main = futures_state(rows, day).get("IF")
    if not main:
        return None
    a, b = at_clock(day, SESSION_OPEN), at_clock(day, SESSION_CLOSE)
    stamps = sorted({r.quote_at for r in rows if r.symbol == main["contract"] and r.quote_at is not None and a <= r.quote_at <= b})
    gaps = [D((u - v).total_seconds()) / D(60) for v, u in zip(stamps, stamps[1:])]
    return q(median(gaps), 4) if gaps else None


def health_state(summary: Mapping, rows: Sequence[ExtRow] | None, day: date, *, merged: bool) -> dict:
    """Recorder health from the ext recorder's own summary (native coverage or legacy ``summary.json``; ``{"unreadable": True}`` when it could not be read; ``{}`` when none exists) and the rows.

    A field the summary does not carry is None (never 0); ``source`` says where the figures came from and ``note`` says why they are missing when there is no record."""
    unreadable = bool(summary.get("unreadable"))
    present = bool(summary) and not unreadable
    source = summary.get("source") or ("legacy_summary" if present else "unreadable" if unreadable else "none")
    note = summary.get("note") or (None if present else "recorder summary unreadable" if unreadable else "no recorder health record for this session")
    minutes = {s: sum(1 for r in rows if r.symbol == s and r.kind == "minute") if rows is not None else 0 for s in MINUTE_SYMBOLS}
    return {"end": summary.get("end"), "missed": summary.get("missed"), "failed_requests": summary.get("failed_requests"),
            "never_returned": len(summary["never_returned"]) if "never_returned" in summary else None, "blocked": sorted(summary.get("blocked", {})), "merged": merged,
            "a50_bars": minutes["em_CN00Y"], "cnh_bars": minutes["em_USDCNH"],
            "cffex_gap_min": cffex_gap(rows, day) if rows is not None else None, "source": source, "note": note}


def baseline(sums: Mapping[str, Decimal], day: date) -> list[Decimal]:
    """Up to ``WINDOW`` complete daily ETF sums before ``day`` (seed and logged days merged by date), oldest first."""
    keys = sorted(k for k in sums if k < day.isoformat())
    return [sums[k] for k in keys][-WINDOW:]


def state_row(day: date, *, closing: Mapping[str, tuple[Decimal | None, Decimal | None]] | None, ext: Sequence[ExtRow] | None,
              summary: Mapping, merged: bool, sums: Mapping[str, Decimal]) -> dict:
    """The row of one session. ``closing`` None = no main-board recording; ``ext`` None or empty = no extended recording."""
    row: dict = {"date": day.isoformat(), **(market_state(closing) if closing else {"ew_pct": None, "up_share": None, "n_ew": 0}),
                 "health": health_state(summary, ext, day, merged=merged)}
    if not ext:
        return {**row, "ext_missing": True, **NO_ETF_FLAGS}
    etf = etf_state(ext, day)
    base = baseline(sums, day)
    ratio = etf["etf_amount"] / median(base) if etf["etf_amount"] and len(base) >= WINDOW else None
    surge = None if ratio is None else ratio >= RATIO
    panic = None if surge is None or row["ew_pct"] is None else bool(surge and row["ew_pct"] <= PANIC_PCT)
    late = None if etf["late_share"] is None else etf["late_share"] >= LATE_SHARE
    return {**row, **etf, "ratio": q(ratio, 4), "surge": surge, "panic_support": panic, "late_heavy": late,
            **context_state(ext, day), "futures": futures_state(ext, day)}


# ---- decision statistics over the stored rows --------------------------------------------------------------------------------
def compound(pcts: Sequence[Decimal | None], i: int, h: int) -> Decimal | None:
    window = list(pcts[i + 1:i + 1 + h])
    if len(window) < h or any(p is None for p in window):
        return None
    prod = D(1)
    for p in window:
        prod *= 1 + p / HUNDRED
    return q((prod - 1) * HUNDRED, 4)


def episodes(flags: Sequence[bool | None]) -> list[int]:
    """Index of the first flagged day of each episode: flagged days within EPISODE_GAP rows are one episode."""
    starts, last = [], None
    for i, f in enumerate(flags):
        if f:
            if last is None or i - last > EPISODE_GAP:
                starts.append(i)
            last = i
    return starts


def _streak(rows: Sequence[dict], failed) -> int:
    n = 0
    for r in reversed(rows):
        if not failed(r):
            break
        n += 1
    return n


def actions(rows: Sequence[dict]) -> dict:
    """E087's two pending decisions, from days the recorder ran; a lost day is another problem and gets its own line."""
    lost = [r["date"] for r in rows[-LOST_LOOKBACK:] if r.get("ext_missing") or r.get("error")]
    ran = [r for r in rows if not r.get("ext_missing") and not r.get("error")]
    miss = _streak(ran, lambda r: not r["health"]["a50_bars"] or not r["health"]["cnh_bars"])
    slow = _streak(ran, lambda r: (r["health"]["cffex_gap_min"] or ZERO) >= CFFEX_SLOW_MIN)
    todo = []
    if lost:
        todo.append(f"extended recording or state row lost on {', '.join(lost)}: read that day's recorder log")
    if miss >= SOURCE_FAIL_DAYS:
        todo.append("A50 / CNH missing: fetch them from the PC pipeline and push to the server")
    if slow >= CFFEX_SLOW_DAYS:
        todo.append("CFFEX files update slowly in session: lower requests to one per 5 minutes")
    return {"a50_cnh_missing_days": miss, "cffex_slow_days": slow, "lost_days": lost, "todo": todo}


def summary(rows: Sequence[dict]) -> dict:
    """The pre-registered progress of E089: counts, the late-share distribution, panic episodes (H1 needs ``H1_EPISODES``) and recorder actions."""
    ew = [r.get("ew_pct") for r in rows]
    panic = episodes([r.get("panic_support") for r in rows])
    all5 = [v for i in range(len(rows)) if (v := compound(ew, i, 5)) is not None]
    late = [r["late_share"] for r in rows if r.get("late_share") is not None]
    return {"days": len(rows), "ext_missing": sum(1 for r in rows if r.get("ext_missing")), "errors": sum(1 for r in rows if r.get("error")),
            "with_ratio": sum(1 for r in rows if r.get("ratio") is not None),
            "surge_days": sum(1 for r in rows if r.get("surge")), "late_heavy_days": sum(1 for r in rows if r.get("late_heavy")),
            "late_share_median": q(median(late), 4) if late else None,
            "panic_episodes": len(panic), "h1_check_at": H1_EPISODES,
            "panic_next": [{"date": rows[i]["date"], **{f"ew_{h}": compound(ew, i, h) for h in NEXT}} for i in panic],
            "all_days_mean_next5": q(mean(all5), 4) if all5 else None, **actions(rows)}
