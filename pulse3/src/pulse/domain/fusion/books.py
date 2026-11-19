"""Forward books of the fusion lane: frozen main book (5-day hold), 10-day shadow, equity-curve gate (legacy triage batch 3).

Why: E036/E040 (curve gate), E065 (10-day shadow) and the frozen main book are *forward records*: their rules were
pre-registered and must not change. This module keeps exactly those rules in one pure place.
Rules, unchanged:
* signals: the top ``offer`` (40) names of each day's score; a signal of day ``d`` (known at the close) is bought at the open of
  the next session when the name has a bar, does not open sealed at the limit-up and its open is positive;
* book: ``positions`` (20) slots, equal weight (equity / slots at the open, whole 100-share lots), at most
  ``round(positions / hold)`` new names a day taken in score order, T+1, sold at the close when the holding has lasted
  ``hold`` sessions (a sealed limit-down delays the sale to the next open that is not locked), no stops; costs: commission 2.5 bp
  both sides, stamp duty 5 bp on sells, slippage 5 bp both sides; marked to the close, positions still open on the last day are
  closed at its close with reason ``end``;
* gate: the book's own ungated NAV at the previous close below the mean of the 20 NAVs before it (needs 20 days) keeps only
  ``round(w x per_day)`` of the day's new names (``w`` = 0.25 main, 0.5 shadow); positions already held are never cut.
Invariants: a run is a pure function of (scores, grids); the book is simulated from the first score day without a break, so the
gate's 20 days are real; rerunning on the same inputs gives the same rows.
Not covered: loading data, logs and registry rows (``application.bf_fusion_service``), OS20 / X20 / monitor (``forward``).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np

LOT = 100
INITIAL_CASH = 1_000_000.0
COMMISSION_BP, STAMP_BP, SLIPPAGE_BP = 2.5, 5.0, 5.0
BUY_FEE = (1 + SLIPPAGE_BP / 1e4) * (1 + COMMISSION_BP / 1e4)
SELL_FEE = (1 - SLIPPAGE_BP / 1e4) * (1 - (COMMISSION_BP + STAMP_BP) / 1e4)
FORWARD_START = date(2024, 8, 20)       # first day with a fusion score
FROZEN = date(2026, 11, 1)              # the main book was frozen on this day
SLOTS, OFFER, LOOK = 20, 40, 20


@dataclass(frozen=True)
class BookRule:
    """One pre-registered book variant."""

    key: str
    hold: int
    gate_low: float          # share of the day's new names kept on a gated day (before rounding)
    gate_label: str          # text in the log's ``rule`` field
    slots: int = SLOTS
    offer: int = OFFER
    look: int = LOOK

    @property
    def per_day(self) -> int:
        return max(1, round(self.slots / self.hold))


MAIN = BookRule("main", 5, 0.25, f"hold 5, {SLOTS} slots, top {OFFER}, gate {LOOK}d/0.25")
H10 = BookRule("h10", 10, 0.5, f"SHADOW hold 10, {SLOTS} slots, top {OFFER}, gate {LOOK}d, keep 0.5 of new buys when gated")
RULES = {r.key: r for r in (MAIN, H10)}


@dataclass(frozen=True)
class Trade:
    code: str
    entry_date: date
    exit_date: date
    entry_price: float
    exit_price: float
    return_pct: float
    holding_days: int
    exit_reason: str


@dataclass
class _Pos:
    ci: int
    entry_idx: int
    entry_px: float
    shares: int
    cost: float
    last_px: float
    pending: str | None = None


@dataclass(frozen=True)
class BookRun:
    dates: list[date]
    nav: np.ndarray           # 100 at the start
    exposure: np.ndarray
    trades: list[Trade]

    def held_at_end(self) -> list[str]:
        return sorted(t.code for t in self.trades if t.exit_reason == "end")


def top_signals(scores: Mapping[date, tuple[Sequence[str], np.ndarray]], offer: int) -> dict[date, list[str]]:
    """Per day the ``offer`` best codes by score, best first; equal scores keep code order. Non-finite scores are not signals."""
    out: dict[date, list[str]] = {}
    for d, (codes, s) in scores.items():
        s = np.asarray(s, dtype=np.float64)
        ok = np.flatnonzero(np.isfinite(s))
        order = ok[np.argsort(-s[ok], kind="stable")][:offer]
        out[d] = [codes[i] for i in order]
    return out


def gate(nav: np.ndarray, look: int, low: float) -> np.ndarray:
    """Gate weight per day from the ungated NAV: ``low`` when the previous NAV is below the mean of the ``look`` NAVs before it
    (the NAV of the day itself is not used), else 1."""
    n = len(nav)
    ma = np.full(n, np.nan)
    for t in range(look, n):
        ma[t] = nav[t - look:t].mean()
    prev = np.r_[np.nan, nav[:-1]]
    with np.errstate(invalid="ignore"):
        return np.where(np.isfinite(ma) & (prev < ma), low, 1.0)


def keep_count(w: float, per_day: int) -> int:
    """Names kept on a day with gate weight ``w``."""
    return per_day if w >= 1 else int(round(w * per_day))


def gated_signals(signals: Mapping[date, list[str]], weights: Mapping[date, float], per_day: int) -> dict[date, list[str]]:
    """Cut each gated day's list to its first ``keep`` names (0 = buy nothing that day)."""
    out = {}
    for d, names in signals.items():
        w = weights.get(d, 1.0)
        out[d] = names if w >= 1.0 else names[:keep_count(w, per_day)]
    return out


def simulate(dates: Sequence[date], codes: Sequence[str], grid: Mapping[str, np.ndarray], signals: Mapping[date, list[str]],
             *, hold: int, slots: int, per_day: int, start: date, end: date | None = None) -> BookRun:
    """Run the book over ``dates`` in [start, end]; ``grid`` has float arrays open/close and bool-like is_limit_down,
    open_at_limit, open_at_floor of shape [len(dates), len(codes)] (NaN open = no bar)."""
    idx = [i for i, d in enumerate(dates) if d >= start and (end is None or d <= end)]
    if not idx:
        return BookRun([], np.array([]), np.array([]), [])
    lo, hi = idx[0], idx[-1] + 1
    days = list(dates[lo:hi])
    n_days = len(days)
    ci_of = {c: i for i, c in enumerate(codes)}
    open_ = np.asarray(grid["open"][lo:hi], dtype=np.float64)
    close = np.asarray(grid["close"][lo:hi], dtype=np.float64)
    present = np.isfinite(close) & np.isfinite(open_)
    at_limit = np.nan_to_num(np.asarray(grid["open_at_limit"][lo:hi], dtype=np.float64), nan=0.0) > 0
    floor_lock = (np.nan_to_num(np.asarray(grid["is_limit_down"][lo:hi], dtype=np.float64), nan=0.0) > 0) & (
        np.nan_to_num(np.asarray(grid["open_at_floor"][lo:hi], dtype=np.float64), nan=0.0) > 0)
    day_pos = {d: i for i, d in enumerate(days)}
    by_day: dict[int, list[int]] = {}
    for d, names in signals.items():
        t = day_pos.get(d)
        if t is not None:
            by_day[t] = [ci_of[c] for c in names if c in ci_of]

    cash = INITIAL_CASH
    positions: list[_Pos] = []
    trades: list[Trade] = []
    nav: list[float] = []
    exposure: list[float] = []

    def close_position(pos: _Pos, t: int, fill: float, reason: str) -> None:
        nonlocal cash
        proceeds = pos.shares * fill * SELL_FEE
        cash += proceeds
        trades.append(Trade(codes[pos.ci], days[pos.entry_idx], days[t], round(pos.entry_px, 4), round(fill, 4),
                            round((proceeds / pos.cost - 1) * 100, 4), t - pos.entry_idx, reason))

    def equity_value() -> float:
        return cash + sum(p.shares * p.last_px for p in positions)

    for t in range(n_days):
        # A) a sale delayed by a locked limit-down executes at today's open when it can
        kept: list[_Pos] = []
        for pos in positions:
            if pos.pending is not None and present[t, pos.ci] and not floor_lock[t, pos.ci]:
                close_position(pos, t, float(open_[t, pos.ci]), pos.pending)
            else:
                kept.append(pos)
        positions = kept
        # B) entries at today's open from yesterday's signals
        if t >= 1 and (t - 1) in by_day and len(positions) < slots:
            held = {p.ci for p in positions}
            accepted: list[int] = []
            room = slots - len(positions)
            for ci in by_day[t - 1]:
                if len(accepted) >= min(per_day, room):
                    break
                if ci in held or not present[t, ci] or at_limit[t, ci] or not open_[t, ci] > 0:
                    continue
                accepted.append(ci)
            if accepted:
                base = equity_value() / slots
                for ci in accepted:
                    price = float(open_[t, ci])
                    per_share = price * BUY_FEE
                    shares = int(min(base, cash) / per_share // LOT) * LOT
                    if shares < LOT:
                        continue
                    cost = shares * per_share
                    cash -= cost
                    positions.append(_Pos(ci, t, price, shares, cost, price))
        # C) time exits for positions bought before today (T+1); the entry bar only marks to market
        kept = []
        for pos in positions:
            ci = pos.ci
            if not present[t, ci]:
                kept.append(pos)
                continue
            c = float(close[t, ci])
            if pos.entry_idx >= t or pos.pending is not None:
                pos.last_px = c
                kept.append(pos)
                continue
            if t - pos.entry_idx >= hold:
                if floor_lock[t, ci]:
                    pos.pending = "time"
                    pos.last_px = c
                    kept.append(pos)
                else:
                    close_position(pos, t, c, "time")
                continue
            pos.last_px = c
            kept.append(pos)
        positions = kept
        if t == n_days - 1:
            for pos in positions:
                last = float(close[t, pos.ci]) if present[t, pos.ci] else pos.last_px
                close_position(pos, t, last, "end")
            positions = []
        value = equity_value()
        nav.append(value)
        exposure.append(sum(p.shares * p.last_px for p in positions) / value if value > 0 else 0.0)
    return BookRun(days, np.array(nav) / INITIAL_CASH * 100.0, np.array(exposure), trades)


def run_book(rule: BookRule, dates: Sequence[date], codes: Sequence[str], grid: Mapping[str, np.ndarray],
             scores: Mapping[date, tuple[Sequence[str], np.ndarray]], *, start: date = FORWARD_START,
             end: date | None = None) -> tuple[BookRun, BookRun, np.ndarray, dict[date, list[str]]]:
    """Ungated run, gate weights, gated run. Returns (gated run, ungated run, weights per day, top signals)."""
    sig = top_signals(scores, rule.offer)
    kw = dict(hold=rule.hold, slots=rule.slots, per_day=rule.per_day, start=start, end=end)
    raw = simulate(dates, codes, grid, sig, **kw)
    w = gate(raw.nav, rule.look, rule.gate_low)
    wmap = dict(zip(raw.dates, w.tolist()))
    gated = simulate(dates, codes, grid, gated_signals(sig, wmap, rule.per_day), **kw)
    return gated, raw, w, sig


def decision_row(rule: BookRule, gated: BookRun, raw: BookRun, w: np.ndarray, sig: Mapping[date, list[str]],
                 *, scores_digest: str) -> dict:
    """The log row of the book's last day: the gate weight of that day's signals, the names it will try to buy, holdings, NAVs."""
    last = gated.dates[-1]
    held = gated.held_at_end()
    weight = float(w[raw.dates.index(last)]) if last in raw.dates else 1.0
    keep = keep_count(weight, rule.per_day)
    offer = sig.get(last, [])
    today = offer if weight >= 1.0 else offer[:keep]
    buys = [c for c in today if c not in held][:keep]
    nav_since = [n for d, n in zip(gated.dates, gated.nav) if d >= FROZEN]
    since = round(float(nav_since[-1] / nav_since[0] - 1) * 100, 3) if len(nav_since) > 1 else None
    return {"date": last.isoformat(), "gate_w": weight, "new_buys_allowed": keep, "try_buy": buys, "offer_top10": list(offer[:10]),
            "holdings": held, "nav": round(float(gated.nav[-1]), 4), "nav_since_freeze_pct": since,
            "ungated_nav": round(float(raw.nav[-1]), 4), "scores_sha256_16": scores_digest, "rule": rule.gate_label}
