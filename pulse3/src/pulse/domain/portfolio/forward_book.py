"""Forward books: the user's frozen main book and its 10-day shadow as tracked paper accounts (old ``forward_book*.py`` rules).

Why: since 2026-11-01 the user tracks a frozen rule - hold 5 trading days, 20 slots, 4 new names a day from the top 40 scores,
50,000 CNY per name - and a 10-day-hold shadow (2 new names a day). pulse3 keeps them as ``LEGACY_FORWARD`` paper accounts on the
ONE ledger, computed day by day by the execution kernel from the CURRENT release's ranking.
Invariants:
* these books are labelled 检验中 and have ``ranking_weight = none``: they never feed a ranking weight or a promotion;
* a decision is made once per signal session from the ranking of the release published for it; a day is never decided twice and
  never back-filled from a later release (no point-in-time ranking exists for earlier days, so the book starts at the first day
  it sees);
* decided on the close of session S, executed at the raw OPEN of the next trading day with a bar; a buy at a limit-up open or with
  no bar/volume is unfilled (reason recorded), a sell at a limit-down open waits; there is no queue model;
* entry day D, sellable D+1, exit at the open of the ``hold_days``-th trading day after D, decided on the close before it;
* the old equity-curve gate (keep a quarter of new buys when the book is under its 20-day mean) is NOT modelled: the
  pre-registered comparison is ungated vs ungated.
Not covered: intraday data, partial fills by volume, the gate, fusion scores of the old pipeline (this book uses the release's
market ranking).
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable

LOT = 100
MAIN_H5 = "main_h5"
SHADOW_H10 = "shadow_h10"
FROZEN_ON = date(2026, 11, 1)
RULES: dict[str, dict] = {
    MAIN_H5: {"hold_days": 5, "slots": 20, "new_per_day": 4, "offer_top": 40, "per_name_cny": "50000", "horizon_id": "5td",
              "gate": "not_modelled"},
    SHADOW_H10: {"hold_days": 10, "slots": 20, "new_per_day": 2, "offer_top": 40, "per_name_cny": "50000", "horizon_id": "5td",
                 "gate": "not_modelled"},
}
LABELS = {MAIN_H5: "前向主书（持有5日）", SHADOW_H10: "前向影子（持有10日）"}
OPENING_CASH = "1000000"
TRACKING_KIND = "LEGACY_FORWARD"


class HybridCalendar:
    """Observed trading days are exact; after the last observed day only Monday-Friday is known (exchange holidays are not).

    ``assumed_after`` exposes the first day that rests on the weekday assumption so a result can say so.
    """

    def __init__(self, observed: Iterable[date]):
        self._days = sorted(set(observed))
        if not self._days:
            raise ValueError("calendar needs at least one observed trading day")

    @property
    def assumed_after(self) -> date:
        return self._days[-1]

    def is_trading_day(self, day: date) -> bool:
        if day <= self._days[-1]:
            i = bisect.bisect_left(self._days, day)
            return i < len(self._days) and self._days[i] == day
        return day.weekday() < 5

    def next_trading_day(self, day: date) -> date:
        d = day + timedelta(days=1)
        while not self.is_trading_day(d):
            d += timedelta(days=1)
        return d

    def trading_days_between(self, start: date, end: date) -> int:
        """Number of trading days in (start, end]."""
        n, d = 0, start
        while d < end:
            d = self.next_trading_day(d)
            if d <= end:
                n += 1
        return n


def lot_quantity(per_name_cny: Decimal, price: Decimal) -> int:
    if price <= 0:
        return 0
    return int(per_name_cny / price) // LOT * LOT


@dataclass(frozen=True)
class Holding:
    instrument_id: str
    quantity: int
    entry_day: date


@dataclass(frozen=True)
class Plan:
    sells: tuple[str, ...]
    buys: tuple[tuple[str, int], ...]          # (instrument_id, alpha_rank)
    reasons: dict


def plan_day(rule: dict, signal_day: date, ranking: list[tuple[str, int]], holdings: list[Holding], pending_buys: set[str],
             cal: HybridCalendar) -> Plan:
    """Decisions on the close of ``signal_day``. ``ranking`` = [(instrument_id, alpha_rank)] ascending rank."""
    hold = rule["hold_days"]
    sells = tuple(sorted(h.instrument_id for h in holdings if cal.trading_days_between(h.entry_day, signal_day) >= hold - 1))
    held_after = {h.instrument_id for h in holdings} - set(sells)
    free = rule["slots"] - len(held_after) - len(pending_buys)
    offer = [(i, r) for i, r in sorted(ranking, key=lambda x: x[1])[:rule["offer_top"]]]
    buys: list[tuple[str, int]] = []
    skipped = {"already_held": 0, "pending": 0, "no_slot": 0}
    for iid, rank in offer:
        if len(buys) >= rule["new_per_day"]:
            break
        if iid in held_after or iid in {h.instrument_id for h in holdings}:
            skipped["already_held"] += 1
            continue
        if iid in pending_buys:
            skipped["pending"] += 1
            continue
        if free - len(buys) <= 0:
            skipped["no_slot"] += 1
            break
        buys.append((iid, rank))
    return Plan(sells, tuple(buys), {"offer_top": rule["offer_top"], "free_slots": max(free, 0), "skipped": skipped})
