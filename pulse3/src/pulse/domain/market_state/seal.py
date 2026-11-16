"""Seal facts from ~60 s quote snapshots: first/last seal time, broken count, sealed at close, close seal amount (#5/#124).

Why: the market page's seal clock and the stock page's seal row need the time a stock first sealed at its limit-up price. The
only source is the recorded 60-second sampled snapshots, so EVERY figure here is labelled ``estimated_from_60s_snapshots``.

Rules (versioned ``seal-facts-v2``):
* only snapshots in the continuous windows count: 09:25:00-11:30:00 and 13:00:00 up to (not including) 14:57:00 Asia/Shanghai
  (09:15-09:25 shows tentative auction matches, 11:30-13:00 repeats the last quote, and 14:57-15:00 is the closing call auction:
  the vendor shows bid 1 = ask 1 = the tentative match and an empty book at 15:00, which is not a broken seal; v1 counted it
  as one, seen on the server for 2026-11-13: 53 sealed, 0 sealed at close, 153 breaks);
* a snapshot is SEALED when the price equals the limit-up price, bid 1 stands at the limit price with volume, and the best ask
  (ask 1) is known and empty (the best ask empty means no asks at all; a deeper level with volume contradicts it and is not sealed); it is NOT sealed when it is in-window with a price and fails that test; when a needed value is
  missing it is UNKNOWN and changes nothing (missing is never turned into "not sealed");
* ``first_seal_at`` / ``last_seal_at`` are the quote times of the first snapshot of the first / last sealed run; the true time lies
  up to one sampling gap earlier (``first_seal_uncertainty_seconds``); ``broken_count`` counts sealed -> not sealed transitions
  that two consecutive snapshots could see, so it is a LOWER bound (a break and re-seal inside one gap is invisible);
* ``sealed_at_close`` is the state at the start of the closing auction: it needs an in-window snapshot at or after 14:50;
  otherwise it is None (not observed), never False. The closing auction result itself is not observed;
* ``close_seal_order_amount_cny`` = bid1 lots x 100 x limit-up price of the last snapshot, only when sealed at close.
Pure: no IO, no clock. Not covered: queue position, order cancellations, auction seals before 09:25.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

POLICY_VERSION = "seal-facts-v2"
ESTIMATION = "estimated_from_60s_snapshots"
CST = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc
WINDOWS = ((time(9, 25), time(11, 30)), (time(13, 0), time(14, 56, 59, 999999)))   # closing call auction from 14:57
CLOSE_OBSERVED_FROM = time(14, 50)
LOT = Decimal(100)
ZERO = Decimal(0)
CENT = Decimal("0.01")


@dataclass(frozen=True)
class SealSnapshot:
    at: datetime                              # quote time, tz-aware
    price: Decimal | None
    limit_up: Decimal | None
    bid1_px: Decimal | None
    bid1_lots: Decimal | None
    ask_lots: tuple[Decimal | None, ...]      # five levels, None = unknown


@dataclass(frozen=True)
class SealFact:
    code: str
    name: str | None
    session_date: object                      # datetime.date
    limit_up: Decimal | None
    snapshots_in_window: int
    touched_limit: bool
    first_seal_at: datetime | None
    last_seal_at: datetime | None
    broken_count: int
    sealed_at_close: bool | None
    close_seal_order_amount_cny: Decimal | None
    first_seal_uncertainty_seconds: int | None
    max_gap_seconds: int | None
    estimation: str = ESTIMATION
    policy_version: str = POLICY_VERSION


def in_window(at: datetime) -> bool:
    t = at.astimezone(CST).time()
    return any(a <= t <= b for a, b in WINDOWS)


def classify(s: SealSnapshot) -> bool | None:
    """True sealed, False in-window but not sealed, None unknown (needed value missing)."""
    if s.price is None or s.limit_up is None or s.limit_up <= ZERO:
        return None
    if s.price != s.limit_up:
        return False
    if s.bid1_px is None or s.bid1_lots is None or not s.ask_lots or s.ask_lots[0] is None:
        return None
    if s.bid1_px != s.limit_up or s.bid1_lots <= ZERO:
        return False
    if s.ask_lots[0] > ZERO:
        return False
    return not any(a is not None and a > ZERO for a in s.ask_lots[1:])      # best ask empty -> no asks at all; a deeper level > 0 contradicts it


def touched(s: SealSnapshot) -> bool:
    return s.price is not None and s.limit_up is not None and s.limit_up > ZERO and s.price == s.limit_up


def derive(code: str, name: str | None, session_date, snapshots: Iterable[SealSnapshot]) -> SealFact:
    snaps = sorted((s for s in snapshots if in_window(s.at)), key=lambda s: s.at)
    first = last = None
    first_unc = None
    broken = 0
    state: bool | None = None                  # last definite state
    prev_at: datetime | None = None
    max_gap = None
    touch = False
    limit = None
    last_def: SealSnapshot | None = None
    last_def_state: bool | None = None
    for s in snaps:
        ct = s.at.astimezone(CST).time()
        if prev_at is None:
            gap = None
        elif prev_at.astimezone(CST).time() <= WINDOWS[0][1] and ct >= WINDOWS[1][0]:
            gap = None                          # the lunch break is not a sampling gap
        else:
            gap = int((s.at - prev_at).total_seconds())
        if gap is not None:
            max_gap = gap if max_gap is None else max(max_gap, gap)
        touch = touch or touched(s)
        if s.limit_up is not None and s.limit_up > ZERO:
            limit = s.limit_up
        c = classify(s)
        if c is not None:
            last_def, last_def_state = s, c
            if c and state is not True:
                last = s.at
                if first is None:
                    first = s.at
                    first_unc = gap
            if state is True and c is False:
                broken += 1
            state = c
        prev_at = s.at
    sealed_close: bool | None = None
    amount = None
    if last_def is not None and last_def.at.astimezone(CST).time() >= CLOSE_OBSERVED_FROM:
        sealed_close = last_def_state
        if sealed_close and last_def.bid1_lots is not None and last_def.limit_up is not None:
            amount = (last_def.bid1_lots * LOT * last_def.limit_up).quantize(CENT, rounding=ROUND_HALF_EVEN)
    return SealFact(code, name, session_date, limit, len(snaps), touch, first, last, broken, sealed_close, amount, first_unc, max_gap)


# ---- seal clock (aggregation of the day's facts) -----------------------------------------------------------------------------
def bucket_axis(bucket_minutes: int) -> list[time]:
    """Bucket start times of the continuous windows (the 09:25 bucket is the opening auction result)."""
    if bucket_minutes <= 0 or 60 % bucket_minutes:
        raise ValueError("bucket_minutes must divide 60")
    out: list[time] = []
    for a, b in WINDOWS:
        m = a.hour * 60 + a.minute
        if a == time(9, 25):
            out.append(a)
            m = 9 * 60 + 30
        end = b.hour * 60 + b.minute
        while m < end:
            out.append(time(m // 60, m % 60))
            m += bucket_minutes
    return out


def seal_clock(facts: Sequence[SealFact], bucket_minutes: int = 5) -> dict:
    """First-seal histogram and cumulative curve over the day's facts (counts of stocks whose FIRST seal falls in each bucket)."""
    axis = bucket_axis(bucket_minutes)
    firsts = [f.first_seal_at.astimezone(CST).time() for f in facts if f.first_seal_at is not None]
    n_sealed = len(firsts)
    counts = [0] * len(axis)
    for t in firsts:
        idx = max(i for i, st in enumerate(axis) if st <= t)
        counts[idx] += 1
    cum, buckets = 0, []
    for st, c in zip(axis, counts):
        cum += c
        ratio = None if n_sealed == 0 else (Decimal(cum) / Decimal(n_sealed)).quantize(Decimal("0.0001"))
        buckets.append({"clock": st.strftime("%H:%M"), "first_seal_count": c, "cumulative_count": cum, "cumulative_ratio": ratio})
    return {"bucket_minutes": bucket_minutes, "buckets": buckets, "sealed_ever_count": n_sealed,
            "touched_limit_count": sum(1 for f in facts if f.touched_limit),
            "sealed_at_close_count": sum(1 for f in facts if f.sealed_at_close),
            "broken_count_total": sum(f.broken_count for f in facts),
            "instruments_observed": len(facts)}
