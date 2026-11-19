"""E059 five-candle box breakout as a SHADOW sell rule ``S_Box5m`` for held limit-up names (the old ledger asks for a paper shadow row first).

Why: E059 tested a 5-minute box / breakout machine on 1-minute bars; as a sell rule for held limit-ups it was inconclusive (5 recorded days, one week of
trading, bars resting on one snapshot). The old ledger's reactivation says to run it as a paper shadow row; this module is that row: the first
close-confirmed DOWNWARD box break of the day sells at the next bar's open (a break on the last bar sells at that bar's close); no break = the close.
Bars: 1-minute bars built from the recorded snapshots (kinds full / close / hot) exactly as the old recorder's synthesis does: a bar labelled HHMM ends at
HH:MM:00 and holds the snapshots quoted in (HH:MM - 1 min, HH:MM] plus 5 s; open / high / low are of the snapshot prices inside the minute, so a bar
resting on one snapshot has no observed intrabar range (n_snaps says how much it rests on). Day index 0..239 = labels 0931..1130 and 1301..1500; bars after
the first real bar are forward filled flat at the previous close with n_snaps 0.
Machine (no indicator of any kind): BOX = five bars from a multiple-of-5 index -> high / low of the box; WATCH = the next five bars: a close beyond the
box is a breakout, five bars without one redraw the box from those five bars; a new session (index 120) restarts the machine.
Invariants: SHADOW only (no order, no ledger booking); prices Decimal; a signal is known only when its bar closes.
Not covered: the long scenarios A / B of E059 (convertible bonds, intraday round trips: inconclusive or negative, retired from the live set), position sizing.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import NamedTuple, Sequence

from .common import CST, ZERO

BOX_BARS = 5
WATCH_BARS = 5
SESSION_BARS = 120
DAY_BARS = 240
LAG = timedelta(seconds=5)
IDLE, BOX, WATCH = "idle", "box", "watch"
KINDS = ("full", "close", "hot")


class Bar(NamedTuple):
    idx: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    n_snaps: int = 1


@dataclass(frozen=True)
class Signal:
    side: int          # +1 up breakout, -1 down breakout
    idx: int           # breakout bar
    box_high: Decimal
    box_low: Decimal
    redraw_count: int


def label_index(label: str) -> int:
    """HHMM -> day index: 0931..1130 -> 0..119, 1301..1500 -> 120..239, anything else (0930, 1300) -> -1."""
    mins = int(label[:2]) * 60 + int(label[2:])
    if 571 <= mins <= 690:
        return mins - 571
    if 781 <= mins <= 900:
        return mins - 781 + 120
    return -1


def idx_label(i: int) -> str:
    m = 571 + i if i < 120 else 781 + (i - 120)
    return f"{m // 60:02d}{m % 60:02d}"


def bar_start(day: date, idx: int) -> datetime:
    """When the bar with day index ``idx`` starts (its label is the minute it ends)."""
    label = idx_label(idx)
    return datetime.combine(day, time(int(label[:2]), int(label[2:])), CST) - timedelta(minutes=1)


def minute_label(quote_at: datetime) -> str | None:
    """The label of the bar a snapshot belongs to: the minute end after subtracting the 5 s lag; None outside the continuous sessions."""
    t = quote_at.astimezone(CST)
    hms = t.strftime("%H:%M:%S")
    if not (("09:30:00" <= hms <= "11:30:59") or ("13:00:00" <= hms <= "15:00:59")):
        return None
    end = (t - LAG).replace(second=0, microsecond=0) + timedelta(minutes=1)
    label = end.strftime("%H%M")
    return label if ("0930" <= label <= "1130" or "1300" <= label <= "1500") else None


def day_bars(snaps: Sequence[tuple[datetime, Decimal | None, Decimal | None]]) -> list[Bar]:
    """Day bars of one code from its snapshots ``(quote_at, price, cumulative volume lots)`` (a row without price or volume is not used).

    One snapshot per quote time (the first wins); bars before the first real bar are absent; later gaps are forward filled flat with n_snaps 0."""
    seen: set[datetime] = set()
    by_idx: dict[int, list[Decimal]] = {}
    for at, price, vol in sorted(snaps, key=lambda s: s[0]):
        if price is None or vol is None or at in seen:
            continue
        seen.add(at)
        label = minute_label(at)
        idx = -1 if label is None else label_index(label)
        if idx >= 0:
            by_idx.setdefault(idx, []).append(price)
    if not by_idx:
        return []
    first = min(by_idx)
    out: list[Bar] = []
    prev_close: Decimal | None = None
    for i in range(first, DAY_BARS):
        ps = by_idx.get(i)
        if ps:
            bar = Bar(i, ps[0], max(ps), min(ps), ps[-1], len(ps))
        else:
            bar = Bar(i, prev_close, prev_close, prev_close, prev_close, 0)
        out.append(bar)
        prev_close = bar.close
    return out


class BoxWatcher:
    """Box / watch / redraw machine. ``step(bar)`` returns a Signal on a close-confirmed breakout of an allowed side."""

    def __init__(self, allow_long: bool = True, allow_short: bool = True) -> None:
        self.allow_long, self.allow_short = allow_long, allow_short
        self._session = -1
        self.reset()

    def reset(self) -> None:
        self.mode = IDLE
        self.redraw_count = 0
        self.count = 0
        self.box_high = self.box_low = ZERO
        self._box: list[Bar] = []
        self._watch: list[Bar] = []

    def _set_box(self, bars: Sequence[Bar]) -> None:
        self.box_high = max(b.high for b in bars)
        self.box_low = min(b.low for b in bars)

    def _redraw(self) -> None:
        self._set_box(self._watch[-WATCH_BARS:])
        self.redraw_count += 1
        self.count = 0
        self._watch = []

    def step(self, bar: Bar) -> Signal | None:
        session = bar.idx // SESSION_BARS
        if session != self._session:
            self.reset()
            self._session = session
        if self.mode == IDLE:
            if bar.idx % BOX_BARS == 0:
                self._box, self.mode = [bar], BOX
            return None
        if self.mode == BOX:
            self._box.append(bar)
            if len(self._box) == BOX_BARS:
                self._set_box(self._box)
                self.mode, self.count, self._watch = WATCH, 0, []
            return None
        self.count += 1
        self._watch.append(bar)
        side = 1 if bar.close > self.box_high else -1 if bar.close < self.box_low else 0
        if side and ((side > 0 and self.allow_long) or (side < 0 and self.allow_short)):
            return Signal(side, bar.idx, self.box_high, self.box_low, self.redraw_count)
        if self.count >= WATCH_BARS:
            self._redraw()
        return None


def first_signal(bars: Sequence[Bar], side: int = -1) -> tuple[Signal, int, Decimal] | None:
    """First close-confirmed breakout of ``side`` of the day (a sell rule: -1) -> (signal, fill bar index, fill price).
    Fill = the next bar's open; a signal on the last bar fills at that bar's close."""
    watcher = BoxWatcher(allow_long=side > 0, allow_short=side < 0)
    for k, bar in enumerate(bars):
        sig = watcher.step(bar)
        if sig is not None:
            if k + 1 < len(bars):
                return sig, bars[k + 1].idx, bars[k + 1].open
            return sig, bar.idx, bar.close
    return None
