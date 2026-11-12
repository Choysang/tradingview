"""Event tape: what changed between two consecutive quote snapshots (old-site 盘口异动, PEX16 transport).

Why: LEGACY_FEATURE_INVENTORY §1 keeps seal / reseal / break / surge / plunge / new-high events. Events are
derived by diffing two snapshots of the *same source*; nothing is inferred between snapshots, and an
instrument present in only one snapshot produces nothing (no half evidence).
Rules (version ``TAPE_VERSION``):
  * sealed = last >= limit-up price and no sell queue (ask is None). Limit-up price = previous close x
    (1 + ratio) rounded half-up (``daily.limit_prices``); without previous close or verified ratio the
    instrument yields no limit events (surge / plunge still need the previous close).
  * seal: not sealed -> sealed and never sealed earlier today; reseal: the same but sealed earlier today;
    break: sealed -> not sealed; surge: pct crosses up through +5 while not sealed; plunge: pct crosses down
    through -5; new_high: a first seal whose streak (yesterday's streak + 1) exceeds yesterday's market height.
  * surge / plunge repeat only after a cooldown (``COOLDOWN_SECONDS``) per instrument and kind.
Coverage is the quoted set (the platform quotes a focus set, not the whole market); the tape never claims
market-wide completeness. Pure: no IO; the caller supplies the prior state read from the event log.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Mapping

from .daily import limit_prices

TAPE_VERSION = "tape-v1"
D = Decimal
SURGE_PCT, PLUNGE_PCT = D(5), D(-5)
COOLDOWN_SECONDS = 900
KINDS = ("seal", "reseal", "break", "surge", "plunge", "new_high")


@dataclass(frozen=True)
class Quote:
    iid: str
    last: Decimal | None
    ask: Decimal | None
    event_time: datetime | None


@dataclass(frozen=True)
class TapeRef:
    """Reference facts of an instrument for today's tape."""
    iid: str
    code: str | None
    name: str
    prev_close: Decimal | None
    ratio: Decimal | None
    prev_streak: int                      # limit-up streak of yesterday's close (0 = not limit-up)
    themes: tuple[str, ...] = ()


@dataclass(frozen=True)
class TapeEvent:
    kind: str
    iid: str
    code: str | None
    name: str
    price: Decimal
    pct_chg: Decimal
    limit_streak: int | None
    detail: str
    themes: tuple[str, ...]
    event_time: datetime | None


def pct_of(q: Quote, ref: TapeRef) -> Decimal | None:
    if q.last is None or ref.prev_close is None or ref.prev_close <= 0 or q.last <= 0:
        return None
    return (q.last / ref.prev_close - 1) * 100


def is_sealed(q: Quote, ref: TapeRef) -> bool | None:
    if q.last is None or ref.prev_close is None or ref.prev_close <= 0 or ref.ratio is None:
        return None
    up, _ = limit_prices(ref.prev_close, ref.ratio)
    return q.last >= up and q.ask is None


def diff_snapshots(prev: Mapping[str, Quote], curr: Mapping[str, Quote], refs: Mapping[str, TapeRef], *, yesterday_max_height: int,
                   ever_sealed: frozenset[str], last_fired: Mapping[tuple[str, str], datetime]) -> list[TapeEvent]:
    """Events going from ``prev`` to ``curr``, ordered by instrument id then kind."""
    out: list[TapeEvent] = []
    for iid in sorted(set(prev) & set(curr)):
        ref = refs.get(iid)
        if ref is None:
            continue
        a, b = prev[iid], curr[iid]
        pa, pb = pct_of(a, ref), pct_of(b, ref)
        if pb is None or b.last is None:
            continue
        sa, sb = is_sealed(a, ref), is_sealed(b, ref)
        now = b.event_time

        def make(kind: str, detail: str, streak: int | None = None) -> TapeEvent:
            return TapeEvent(kind, iid, ref.code, ref.name, b.last, pb.quantize(D("0.01")), streak, detail, ref.themes[:3], now)  # type: ignore[arg-type]

        def cooled(kind: str) -> bool:
            t = last_fired.get((iid, kind))
            return t is None or now is None or (now - t).total_seconds() >= COOLDOWN_SECONDS

        if sa is False and sb:
            streak = ref.prev_streak + 1
            if iid in ever_sealed:
                out.append(make("reseal", "回封涨停", streak))
            else:
                out.append(make("seal", "封涨停 · " + ("首板" if streak == 1 else f"{streak} 连板"), streak))
                if yesterday_max_height >= 1 and streak > yesterday_max_height:
                    out.append(make("new_high", f"晋级 {streak} 板 · 市场新高度", streak))
        elif sa and sb is False:
            out.append(make("break", "炸板", ref.prev_streak + 1))
        if pa is not None:
            if sb is not True and pa < SURGE_PCT <= pb and cooled("surge"):
                out.append(make("surge", "涨幅突破 5%"))
            if pa > PLUNGE_PCT >= pb and cooled("plunge"):
                out.append(make("plunge", f"跌幅扩大至 {pb:.1f}%"))
    return out
