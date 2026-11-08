"""Box5: a 5-minute box / breakout state machine on 1-minute bars (E059). Pure functions and a small state class, no I/O.

Bars carry a day index 0..239: 0..119 = labels 0931..1130 (morning), 120..239 = labels 1301..1500 (afternoon). A box
starts only at an index that is a multiple of 5 (09:30, 09:35, ...; 13:00, 13:05, ...), so box and watch windows never
straddle lunch (both sessions are 120 bars = 24 blocks of 5).

Cycle, per instrument and day (no indicator of any kind):
* BOX   : five consecutive bars from a boundary -> H_box = max high, L_box = min low, redraw_count = 0.
* WATCH : the next five bars (count 1..5). A close above H_box is a long breakout, below L_box a short breakout. Five bars
          without a breakout: the box is redrawn from those five bars, redraw_count += 1, the count restarts.
* The signal is known only when the breakout bar closes, so the trade enters at the NEXT bar's open (also across lunch;
  a signal on the last bar of the day is dropped). The breakout candle spans from the previous bar's close to its own
  extreme: candle_low = min(low, prev_close), candle_high = max(high, prev_close). SL = one tick outside that extreme,
  R = |entry - SL|, TP = entry +/- 1.5 R, nothing else. Exit is evaluated on the entry bar and every later bar: a bar
  close beyond the level fills at that close (conservative); on a bar with n_snaps >= 3 the intrabar high/low is observed,
  so a touch fills at the level itself. SL is checked before TP. Flat at the last bar's close (index 239 = 1500).
* A new session (index 120) restarts the box machine with redraw_count 0; an open trade carries across lunch. After an exit
  the machine idles until the next boundary and draws a fresh box (redraw_count 0).
* A breakout that is not taken (side not allowed, bar blocked, invalid R, converged filter) leaves the watch state alone: the
  count goes on, and a pending redraw (fifth bar) is done.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple, Sequence

BOX_BARS = 5
WATCH_BARS = 5
SESSION_BARS = 120
DAY_BARS = 240
RR = 1.5
RISK_PER_TRADE = 0.01
INTRABAR_SNAPS = 3  # a bar resting on >= 3 snapshots has an observed high/low

IDLE, BOX, WATCH = "idle", "box", "watch"


class Bar(NamedTuple):
    idx: int
    open: float
    high: float
    low: float
    close: float
    n_snaps: int = 1
    blocked: bool = False  # sealed at a price limit: no signal on it, no entry at its open


@dataclass(frozen=True)
class Signal:
    side: int  # +1 long breakout, -1 short breakout
    idx: int  # breakout bar
    box_high: float
    box_low: float
    redraw_count: int
    candle_low: float
    candle_high: float


@dataclass(frozen=True)
class Trade:
    side: int
    redraw_count: int
    box_high: float
    box_low: float
    signal_idx: int
    entry_idx: int
    entry: float
    sl: float
    tp: float
    r: float
    exit_idx: int
    exit_price: float
    reason: str  # sl | tp | eod
    intrabar_fill: bool

    @property
    def r_frac(self) -> float:
        return self.r / self.entry

    @property
    def gross_pct(self) -> float:
        return self.side * (self.exit_price - self.entry) / self.entry * 100.0

    @property
    def hold_bars(self) -> int:
        return self.exit_idx - self.entry_idx + 1


class BoxWatcher:
    """Box / watch / redraw machine. `step(bar)` returns a Signal on a taken-side close-confirmed breakout."""

    def __init__(self, allow_long: bool = True, allow_short: bool = True) -> None:
        self.allow_long, self.allow_short = allow_long, allow_short
        self._session = -1
        self._last_close: float | None = None
        self.reset()

    def reset(self) -> None:
        self.mode = IDLE
        self.redraw_count = 0
        self.count = 0
        self.box_high = self.box_low = float("nan")
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

    def reject(self) -> None:
        """The last signal was not taken: a fifth-bar signal still owes its redraw."""
        if self.mode == WATCH and self.count >= WATCH_BARS:
            self._redraw()

    def step(self, bar: Bar) -> Signal | None:
        session = bar.idx // SESSION_BARS
        if session != self._session:
            self.reset()
            self._session = session
        prev_close, self._last_close = self._last_close, bar.close
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
        allowed = (side > 0 and self.allow_long) or (side < 0 and self.allow_short)
        if side and allowed and not bar.blocked:
            pc = bar.open if prev_close is None else prev_close
            return Signal(side, bar.idx, self.box_high, self.box_low, self.redraw_count, min(bar.low, pc), max(bar.high, pc))
        if self.count >= WATCH_BARS:
            self._redraw()
        return None


def pos_weight(entry: float, r: float, max_weight: float = 0.2, risk: float = RISK_PER_TRADE) -> float:
    """Risk 1% of the account on the stop, capped at max_weight of the account."""
    return min(max_weight, risk / (r / entry))


def _plan(sig: Signal, bar: Bar, tick: float, min_redraw: int, min_r_frac: float) -> tuple[float, float, float] | None:
    """(entry, sl, tp) for a signal whose next bar is `bar`, or None when the trade is not taken."""
    if bar.blocked:
        return None
    entry = bar.open
    sl = round(sig.candle_low - tick, 6) if sig.side > 0 else round(sig.candle_high + tick, 6)
    if (entry - sl) * sig.side <= 0:  # gapped through the stop: no valid R
        return None
    r = abs(entry - sl)
    if sig.redraw_count < min_redraw or r / entry < min_r_frac:
        return None
    return entry, sl, entry + sig.side * RR * r


def _exit(side: int, sl: float, tp: float, bar: Bar) -> tuple[float, str, bool] | None:
    """(fill, reason, intrabar) when the bar hits the stop or the target; SL first. Close beyond a level fills at the close;
    a touch on a bar with an observed high/low fills at the level."""
    seen = bar.n_snaps >= INTRABAR_SNAPS
    adverse_close = bar.close <= sl if side > 0 else bar.close >= sl
    adverse_touch = seen and (bar.low <= sl if side > 0 else bar.high >= sl)
    if adverse_close:
        return bar.close, "sl", False
    if adverse_touch:
        return sl, "sl", True
    fav_close = bar.close >= tp if side > 0 else bar.close <= tp
    fav_touch = seen and (bar.high >= tp if side > 0 else bar.low <= tp)
    if fav_close:
        return bar.close, "tp", False
    if fav_touch:
        return tp, "tp", True
    return None


def run_day(bars: Sequence[Bar], tick: float, allow_long: bool = True, allow_short: bool = True,
            min_redraw: int = 0, min_r_frac: float = 0.0) -> list[Trade]:
    """All trades of one instrument-day. Version 1 = defaults; version 2 (converged) = min_redraw=1, min_r_frac=0.004."""
    watcher = BoxWatcher(allow_long, allow_short)
    trades: list[Trade] = []
    pending: Signal | None = None
    live: tuple[Signal, int, float, float, float] | None = None  # signal, entry idx, entry, sl, tp
    for k, bar in enumerate(bars):
        if pending is not None:
            plan = _plan(pending, bar, tick, min_redraw, min_r_frac)
            if plan is None:
                watcher.reject()
            else:
                live = (pending, bar.idx, *plan)
                watcher.reset()
            pending = None
        if live is not None:
            sig, e_idx, entry, sl, tp = live
            hit = _exit(sig.side, sl, tp, bar)
            last = bar.idx == DAY_BARS - 1 or k == len(bars) - 1
            if hit is None and last:
                hit = (bar.close, "eod", False)
            if hit is not None:
                trades.append(Trade(sig.side, sig.redraw_count, sig.box_high, sig.box_low, sig.idx, e_idx, entry, sl, tp,
                                    abs(entry - sl), bar.idx, hit[0], hit[1], hit[2]))
                live = None
            continue
        pending = watcher.step(bar)
    return trades


def first_signal(bars: Sequence[Bar], side: int = -1) -> tuple[Signal, int, float] | None:
    """First close-confirmed breakout of `side` of the day (a sell rule: side=-1) -> (signal, fill bar idx, fill price).
    Fill = the next bar's open; a signal on the last bar fills at that bar's close."""
    watcher = BoxWatcher(allow_long=side > 0, allow_short=side < 0)
    for k, bar in enumerate(bars):
        sig = watcher.step(bar)
        if sig is not None:
            if k + 1 < len(bars):
                return sig, bars[k + 1].idx, bars[k + 1].open
            return sig, bar.idx, bar.close
    return None
