"""Pure rules for modules M2-M4 (user, 2026-11-02). Every threshold is a candidate parameter: frozen here,
tested forward on recorded intraday data, changed only as a new version with its own ledger entry.

* `clusters` (M2): 3+ first boards sharing a relation key in one morning form a cluster.
* `funnel_ok` (M2): the next day's 1进2 entry filter for a cluster member.
* `break_step` (M3): the held limit-up break state machine (benign divergence vs exit), in two versions run
  side by side: M3-Tape (three tape checks; an unknown catalyst does not veto) and M3-Strict (all four checks).
  P1 has no structured event calendar, so the catalyst is mostly unknown and Strict alone would sell every break.
* `expectation_gap` (M4): last night's author consensus against this morning's auction.
* `catalyst_pending` (M3 input): PENDING / REALIZED / UNKNOWN from linked calendar events.
* `legs`: a trade's return split into entry day, overnight gap and next-day legs.
An unknown input (None) never passes a buy check. The one exception is the catalyst in M3-Tape, where only a
catalyst known to have landed (False) blocks the benign reading.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Iterable, Literal

from zoneinfo import ZoneInfo

from contracts.calendar import CalendarEvent, EventCategory
from contracts.news import AuthorExpectation, AuthorView, BreakState, BrokenBoardCheck, GapType, RelationType

CLUSTER_MIN = 3
CLUSTER_BY = time(11, 30)  # "早盘": the third first board must seal by the end of the morning
FUNNEL = {"free_cap_min": 15.0, "free_cap_max": 60.0, "gap_min": 3.0, "gap_max": 7.0}  # 亿, %
VWAP_GRACE_S = 90.0
LATE_BREAK = time(14, 20)
PEERS_MIN = 2
AUTHORS_MIN = 3  # independent authors before a consensus or a 股性 label counts
SURPRISE_GAP, WEAK_GAP = 2.0, 1.0  # % auction gap that contradicts a bearish / bullish consensus


@dataclass(frozen=True)
class FirstBoard:
    instrument_id: str
    sealed_at: datetime
    keys: tuple[tuple[RelationType, str], ...]  # dated relation keys known before the seal


def clusters(boards: Iterable[FirstBoard]) -> dict[tuple[RelationType, str], list[FirstBoard]]:
    """Relation keys with CLUSTER_MIN+ first boards sealed by CLUSTER_BY, members in sealing order."""
    out: dict[tuple[RelationType, str], list[FirstBoard]] = {}
    for b in sorted(boards, key=lambda x: x.sealed_at):
        if b.sealed_at.time() > CLUSTER_BY:
            continue
        for key in dict.fromkeys(b.keys):
            out.setdefault(key, []).append(b)
    return {k: v for k, v in out.items() if len({b.instrument_id for b in v}) >= CLUSTER_MIN}


def funnel_ok(state_owned_or_ma: bool | None, free_cap: float | None, auction_gap_pct: float | None,
              auction_firm: bool | None) -> bool:
    """1进2 filter: local state / M&A attribute, free-float cap band, auction gap band, a firm auction.

    `auction_firm` is the recorded 09:19:50 -> 09:20:05 -> 09:25 matched price and volume holding up: a level-1
    proxy. Cancellations after 09:20 are not allowed and before it they are not observable, so this is not a
    cancel rate. `free_cap` is the free float when known (Tushare free_share), else unknown."""
    f = FUNNEL
    return bool(state_owned_or_ma and auction_firm and free_cap is not None and auction_gap_pct is not None
                and f["free_cap_min"] <= free_cap <= f["free_cap_max"] and f["gap_min"] <= auction_gap_pct <= f["gap_max"])


def break_step(prev: BreakState, at: datetime, sealed: bool, above_vwap: bool | None, below_vwap_s: float,
               peers_sealed: int | None, catalyst_pending: bool | None, sector_dump: bool | None,
               instrument_id: str, variant: Literal["tape", "strict"] = "strict") -> BrokenBoardCheck:
    """One 10 s step for a held limit-up. Exit wins over benign: 90 s under VWAP or a sector dump exits.

    catalyst_pending: True = the event has not landed, False = it landed (REALIZED), None = unknown."""
    normal = at.time() < LATE_BREAK
    common = dict(instrument_id=instrument_id, variant=variant, at=at, above_vwap=above_vwap, peers_sealed=peers_sealed, normal_hours=normal,
                  catalyst_pending=catalyst_pending, below_vwap_s=below_vwap_s, sector_dump=sector_dump)
    if prev is BreakState.EXIT:
        return BrokenBoardCheck(state=BreakState.EXIT, reason="exit already signalled", **common)
    if sealed:
        state = BreakState.RESEALED if prev in (BreakState.BROKEN_WATCH, BreakState.BENIGN) else BreakState.SEALED
        return BrokenBoardCheck(state=state, **common)
    if sector_dump or below_vwap_s >= VWAP_GRACE_S:
        why = "sector dump" if sector_dump else f"below VWAP {below_vwap_s:.0f} s"
        return BrokenBoardCheck(state=BreakState.EXIT, reason=why, **common)
    tape = above_vwap is True and (peers_sealed or 0) >= PEERS_MIN and normal
    benign = tape and (catalyst_pending is not False if variant == "tape" else catalyst_pending is True)
    state = BreakState.BENIGN if benign else BreakState.BROKEN_WATCH
    return BrokenBoardCheck(state=state, reason="" if benign else "checks not all passed", **common)


def consensus(views: Iterable[AuthorExpectation]) -> tuple[int, AuthorView | None]:
    """Independent authors and their majority view; CONDITIONAL views do not vote. None if too few or split."""
    last: dict[str, AuthorView] = {}
    for v in sorted(views, key=lambda x: x.available_at):  # an author's latest view counts once
        if v.view is not AuthorView.CONDITIONAL:
            last[v.author_id] = v.view
    n = len(last)
    if n < AUTHORS_MIN:
        return n, None
    bull = sum(x is AuthorView.STRONG for x in last.values())
    bear = sum(x in (AuthorView.WEAK, AuthorView.NUKE) for x in last.values())
    if bull * 3 >= n * 2:
        return n, AuthorView.STRONG
    if bear * 3 >= n * 2:
        return n, AuthorView.WEAK
    return n, None


def expectation_gap(consensus_view: AuthorView | None, auction_gap_pct: float | None,
                    auction_amount_ratio: float | None) -> GapType:
    """B1 candidate: bearish consensus, auction up SURPRISE_GAP+ on more volume than yesterday's auction.
    Trap: bullish consensus, auction below WEAK_GAP. Anything unknown is NO_CONSENSUS / IN_LINE, never a signal."""
    if consensus_view is None or auction_gap_pct is None:
        return GapType.NO_CONSENSUS
    if consensus_view is AuthorView.WEAK and auction_gap_pct >= SURPRISE_GAP and (auction_amount_ratio or 0) > 1:
        return GapType.POSITIVE_SURPRISE
    if consensus_view is AuthorView.STRONG and auction_gap_pct < WEAK_GAP:
        return GapType.DISAPPOINTMENT
    return GapType.IN_LINE


# --- M1 shadow entries ------------------------------------------------------------------------------

@dataclass(frozen=True)
class ShadowEntry:
    """One virtual order time for one triggering message (M1, user 2026-11-02). All three are recorded for every
    message and each goes through the optimistic / base / pessimistic queue model."""

    tag: Literal["T+15s", "T+60s", "T+300s"]
    decide_at: datetime
    needs: tuple[str, ...]  # validations that must have passed by decide_at


SHADOW_OFFSETS = (("T+15s", 15, ()), ("T+60s", 60, ("tape",)), ("T+300s", 300, ("tape", "comments")))


def shadow_entries(received_at: datetime) -> list[ShadowEntry]:
    """Decision times from our first receipt (never the source's own time): instant tags, plus the 60 s tape
    pulse, plus the 5 min comment check."""
    return [ShadowEntry(tag, received_at + timedelta(seconds=s), needs) for tag, s, needs in SHADOW_OFFSETS]


def shadow_fires(entry: ShadowEntry, tape_ok: bool | None, comments_ok: bool | None) -> bool:
    """A shadow entry fires only when every validation it needs passed (unknown does not pass)."""
    ok = {"tape": tape_ok is True, "comments": comments_ok is True}
    return all(ok[n] for n in entry.needs)


# --- M3 catalyst status from the event calendar ----------------------------------------------------

def _bounds(ev: CalendarEvent) -> tuple[datetime, datetime]:
    """When the event starts and when its window is over, as aware datetimes."""
    tz = ZoneInfo(ev.event_timezone or "Asia/Shanghai")
    start = ev.event_at or datetime.combine(ev.event_date, time(0, 0), tz)
    last = ev.event_end_date or ev.event_date
    end = max(start, datetime.combine(last + timedelta(days=1), time(0, 0), tz))
    return start, end


# Routine economic releases never count as a stock's catalyst, even when linked: hundreds a quarter (752 of the
# 796 synced on 2026-11-02) would keep names PENDING all the time. Conferences, policy dates, company, index
# and overseas market / policy events count once they are linked to the stock.
NOT_CATALYST = frozenset({EventCategory.MACRO_RELEASE})


def catalyst_pending(events: Iterable[CalendarEvent], instrument_id: str, now: datetime) -> bool | None:
    """M3's fourth input from the calendar: True = a linked event is still ahead (PENDING), False = linked events
    have landed (REALIZED: result known, or the event window is over), None = no linked event we knew of by `now`.
    An event under way (started, window open, no result yet) is neither: None. Only events first seen by `now`
    count. One pending linked event outweighs landed ones: something is still ahead. Synced calendar rows carry no
    instruments until a theme mapping links them, so an unlinked event never counts; macro releases never do."""
    seen = [e for e in events if instrument_id in e.instruments and e.category not in NOT_CATALYST
            and e.usable_at(now) and e.status.value != "cancelled"]
    if not seen:
        return None
    pending = False
    for e in seen:
        start, end = _bounds(e)
        landed = (e.result_known_at is not None and e.result_known_at <= now) or now >= end
        if not landed and now < start:
            pending = True
    if pending:
        return True
    return False if all((e.result_known_at is not None and e.result_known_at <= now) or now >= _bounds(e)[1] for e in seen) else None


# --- three-leg returns (lesson a-share-split-intraday-overnight-legs) ------------------------------

def legs(entry: float, d0_close: float, d1_open: float, exit_price: float) -> dict[str, float]:
    """Percent returns of entry -> D0 close, D0 close -> D1 open (overnight), D1 open -> exit; they compound to
    the trade's total. A trade held longer reports its later days inside the third leg."""
    r = lambda a, b: (b / a - 1) * 100  # noqa: E731
    return {"entry_to_close": r(entry, d0_close), "overnight": r(d0_close, d1_open), "next_open_to_exit": r(d1_open, exit_price),
            "total": r(entry, exit_price)}
