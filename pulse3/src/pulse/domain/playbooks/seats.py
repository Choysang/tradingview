"""Dragon-Tiger seat features for the two seat playbooks (smart_seat_follow, institution_net_buy), point in time.

Why: LEGACY inventory §5 crowd family. The rule texts (old library file, read as the SPECIFICATION of the semantics; no code or numbers carried over):
* a seat (营业部) earns a track record from the stocks it bought on the Dragon-Tiger list: buy at the NEXT OPEN after the listing day, sell at the CLOSE three bars later;
  an event counts only once that close is known (its ``settle`` date), so the record on day t uses events settled on or before t, never the listing's own day;
* smart seat: not ``机构专用``, at least 5 settled events and the shrunk mean ``sum(fwd %) / (n + 5)`` at least 1 %; one-day-trip seat: at least 5 settled events and at least 60 % of them
  were followed by the SAME seat on the SAME stock's SELL list of the next market session; ``机构专用`` is an anonymous pool: it gets its own column ``inst_net`` (buy rows minus sell rows).
Output per stock bar: ``None`` on a day whose list is unknown (no reports imported: an unknown day is not a day on which nobody was listed), else the tuple
``(lhb_on, smart_buyers, smart_score, trip_buyers, inst_net)`` where ``inst_net`` is 0.0 when the day is known but has no institution row (a known zero) and ``None`` when an institution
row has no amount.
Point in time: the report of session D is published the evening of D, so a signal of bar D (executed at the open of D+1) may read it; the seat records read events settled by D only.
Not covered: seat identity merging (the raw seat name is the key, as in the old rule), ST screening, intraday paths. Prices are the research (forward adjusted) series: ratios only.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

FORWARD_BARS = 3
SHRINK = 5.0
SMART_MIN_EVENTS = 5
SMART_MIN_RETURN = 1.0           # percent per trade, shrunk
TRIP_MIN_EVENTS = 5
TRIP_MIN_SHARE = 0.6
INSTITUTION = "机构专用"


@dataclass(frozen=True)
class SeatRow:
    code: str
    day: str                     # ISO date of the report window (single-day reports only)
    side: str                    # "buy" | "sell"
    seat: str
    amount: float | None         # CNY on that side


Bars = tuple[Sequence[str], Sequence, Sequence]          # (dates, open, close) of one stock


def _events(rows: Sequence[SeatRow], bars: Mapping[str, Bars], sessions: Sequence[str]) -> dict[str, list[tuple[str, float, bool]]]:
    """seat -> events (settle date, forward return %, tripped) of its non-institution buys, only those whose forward window exists."""
    sell_next: set[tuple[str, str, str]] = {(r.code, r.seat, r.day) for r in rows if r.side == "sell"}
    sidx = {d: i for i, d in enumerate(sessions)}
    index: dict[str, dict[str, int]] = {}
    out: dict[str, list[tuple[str, float, bool]]] = defaultdict(list)
    seen: set[tuple[str, str, str]] = set()
    for r in rows:
        if r.side != "buy" or r.seat == INSTITUTION or r.code not in bars or (r.code, r.day, r.seat) in seen:
            continue
        seen.add((r.code, r.day, r.seat))
        dates, opens, closes = bars[r.code]
        ix = index.setdefault(r.code, {d: i for i, d in enumerate(dates)})
        j = ix.get(r.day)
        if j is None or j + FORWARD_BARS >= len(dates):
            continue
        o, c = opens[j + 1], closes[j + FORWARD_BARS]
        if o is None or c is None or o <= 0:
            continue
        k = sidx.get(r.day)
        nxt = sessions[k + 1] if k is not None and k + 1 < len(sessions) else None
        out[r.seat].append((dates[j + FORWARD_BARS], (c / o - 1.0) * 100.0, nxt is not None and (r.code, r.seat, nxt) in sell_next))
    return out


class _Record:
    """One seat's running record by settle date."""
    __slots__ = ("days", "n", "sum_fwd", "trips")

    def __init__(self, events: list[tuple[str, float, bool]]):
        events = sorted(events)
        self.days = [e[0] for e in events]
        self.n, self.sum_fwd, self.trips = [], [], []
        a = b = 0.0
        t = 0
        for i, (_, f, trip) in enumerate(events):
            a += f
            t += int(trip)
            self.n.append(i + 1)
            self.sum_fwd.append(a)
            self.trips.append(t)

    def at(self, day: str) -> tuple[int, float, int]:
        """(settled events, sum of forward %, trips) among events settled on or before ``day``."""
        k = bisect_right(self.days, day)
        return (0, 0.0, 0) if k == 0 else (self.n[k - 1], self.sum_fwd[k - 1], self.trips[k - 1])


def is_smart(n: int, sum_fwd: float) -> bool:
    return n >= SMART_MIN_EVENTS and sum_fwd / (n + SHRINK) >= SMART_MIN_RETURN


def is_trip(n: int, trips: int) -> bool:
    return n >= TRIP_MIN_EVENTS and trips / n >= TRIP_MIN_SHARE


def seat_series(rows: Sequence[SeatRow], listed: set[tuple[str, str]], known_days: set[str], bars: Mapping[str, Bars], sessions: Sequence[str]) -> dict[str, list]:
    """Per stock a list aligned with its bar dates (see the module docstring); stocks of ``bars`` only."""
    records = {seat: _Record(ev) for seat, ev in _events(rows, bars, sessions).items()}
    smart: dict[tuple[str, str], list[float]] = defaultdict(list)       # (code, day) -> scores of smart buyers
    trips: dict[tuple[str, str], int] = defaultdict(int)
    inst: dict[tuple[str, str], list[float | None]] = defaultdict(list)
    seen: set[tuple[str, str, str, str]] = set()
    for r in rows:
        if r.code not in bars or (r.code, r.day, r.side, r.seat, r.amount) in seen:
            continue
        seen.add((r.code, r.day, r.side, r.seat, r.amount))
        if r.seat == INSTITUTION:
            inst[(r.code, r.day)].append(None if r.amount is None else (r.amount if r.side == "buy" else -r.amount))
        elif r.side == "buy":
            rec = records.get(r.seat)
            n, s, t = rec.at(r.day) if rec else (0, 0.0, 0)
            if is_smart(n, s):
                smart[(r.code, r.day)].append(s / (n + SHRINK))
            if is_trip(n, t):
                trips[(r.code, r.day)] += 1
    out: dict[str, list] = {}
    for code, (dates, _, _) in bars.items():
        col: list = []
        for d in dates:
            if d not in known_days:
                col.append(None)
                continue
            key = (code, d)
            vals = inst.get(key)
            net = 0.0 if not vals else (None if any(v is None for v in vals) else float(sum(vals)))
            col.append((key in listed, len(smart.get(key, ())), float(sum(smart.get(key, ()))), trips.get(key, 0), net))
        out[code] = col
    return out
