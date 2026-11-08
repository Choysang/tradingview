"""Box5 state machine (E059): box, breakout, redraw, converged filter, SL/TP, entry bar, flat at 1500, afternoon restart."""
from __future__ import annotations

import pytest

from backend.app.domain.box5 import (DAY_BARS, Bar, BoxWatcher, first_signal, pos_weight, run_day)

TICK = 0.01


def bar(i: int, c: float, o: float | None = None, h: float | None = None, l: float | None = None, n: int = 1,
        blocked: bool = False) -> Bar:
    o = c if o is None else o
    return Bar(i, o, max(o, c) if h is None else h, min(o, c) if l is None else l, c, n, blocked)


def box(start: int = 0) -> list[Bar]:
    """Five bars with H_box = 10.1 and L_box = 9.9."""
    return [bar(start, 10.0), bar(start + 1, 10.0), bar(start + 2, 10.0, h=10.1), bar(start + 3, 10.0, l=9.9), bar(start + 4, 10.0)]


def quiet(start: int, n: int, c: float = 10.0) -> list[Bar]:
    return [bar(start + k, c) for k in range(n)]


def feed(w: BoxWatcher, bars: list[Bar]):
    return [w.step(b) for b in bars]


def test_box_from_first_five_bars():
    w = BoxWatcher()
    feed(w, box(0)[:4])
    assert w.mode == "box"
    w.step(box(0)[4])
    assert (w.mode, w.box_high, w.box_low, w.redraw_count) == ("watch", 10.1, 9.9, 0)


def test_box_waits_for_five_minute_boundary():
    w = BoxWatcher()
    for b in quiet(1, 4):  # indices 1..4: not a boundary
        assert w.step(b) is None
    assert w.mode == "idle"
    w.step(bar(5, 10.0))
    assert w.mode == "box"


def test_long_breakout_within_five_bars():
    w = BoxWatcher()
    feed(w, box())
    sigs = feed(w, [bar(5, 10.0), bar(6, 10.05), bar(7, 10.3, o=10.05)])
    assert sigs[:2] == [None, None]
    s = sigs[2]
    assert (s.side, s.idx, s.redraw_count) == (1, 7, 0)


def test_short_breakout_within_five_bars():
    w = BoxWatcher()
    feed(w, box())
    s = feed(w, [bar(5, 10.0), bar(6, 9.7, o=10.0)])[1]
    assert (s.side, s.idx) == (-1, 6)


def test_touch_without_close_is_not_a_breakout():
    w = BoxWatcher()
    feed(w, box())
    assert w.step(bar(5, 10.05, h=10.5)) is None  # high above H_box, close inside


def test_redraw_after_five_quiet_bars_counts():
    w = BoxWatcher()
    feed(w, box())
    watch = [bar(5, 10.0), bar(6, 10.0, h=10.08), bar(7, 10.0, l=9.95), bar(8, 10.0), bar(9, 10.0)]
    assert feed(w, watch) == [None] * 5
    assert (w.redraw_count, w.count, w.box_high, w.box_low) == (1, 0, 10.08, 9.95)  # new box = the five watch bars
    feed(w, quiet(10, 5))
    assert w.redraw_count == 2
    s = w.step(bar(15, 10.3, o=10.0))
    assert s is not None and s.redraw_count == 2


def test_breakout_uses_redrawn_box():
    w = BoxWatcher()
    feed(w, box())
    feed(w, [bar(5, 10.0), bar(6, 10.0), bar(7, 10.0), bar(8, 10.0), bar(9, 10.0)])  # new box 10.0 / 10.0
    s = w.step(bar(10, 10.05, o=10.0))  # inside the OLD box, above the new one
    assert s is not None and s.side == 1 and s.redraw_count == 1


def test_converged_filter_needs_redraw_and_r():
    # a redraw-0 breakout is taken by version 1 and skipped by version 2
    day = box() + [bar(5, 10.3, o=10.0)] + quiet(6, 4, 10.3)
    assert len(run_day(day, TICK)) == 1
    assert run_day(day, TICK, min_redraw=1, min_r_frac=0.004) == []
    # the same breakout after one redraw: R = 10.3 - 9.99 = 0.31 (3.0%) passes
    redrawn = box() + quiet(5, 5) + [bar(10, 10.3, o=10.0)] + quiet(11, 4, 10.3)
    t = run_day(redrawn, TICK, min_redraw=1, min_r_frac=0.004)
    assert len(t) == 1 and t[0].redraw_count == 1
    # a one-tick breakout after a redraw: R = 0.02 = 0.2% < 0.4% fails the R floor, passes without it
    tiny = [bar(i, 10.0) for i in range(10)] + [bar(10, 10.01, o=10.0), bar(11, 10.01)]
    assert run_day(tiny, TICK, min_redraw=1, min_r_frac=0.004) == []
    assert len(run_day(tiny, TICK, min_redraw=1)) == 1


def test_rejected_breakout_keeps_watching_and_redraws():
    # a redraw-0 breakout is skipped by version 2; the watch window goes on, redraws, and the next breakout is taken
    day = box() + [bar(5, 10.3, o=10.0), bar(6, 10.3), bar(7, 10.3), bar(8, 10.3), bar(9, 10.3)]
    day += [bar(10, 10.9, o=10.3), bar(11, 10.9), bar(12, 10.9)]
    trades = run_day(day, TICK, min_redraw=1, min_r_frac=0.004)
    assert len(trades) == 1 and trades[0].redraw_count == 1 and trades[0].signal_idx == 10


def test_sl_tp_use_prev_close_extension_and_entry_at_next_open():
    # breakout bar gaps up: open 10.6 > prev close 10.0, so the candle low is the PREVIOUS CLOSE, not the bar low
    day = box() + [bar(5, 10.8, o=10.6), bar(6, 10.9, o=10.85)]  # signal at 5 (close 10.8), entry at bar 6 open 10.85
    day += quiet(7, 3, 11.0)
    t = run_day(day, TICK)[0]
    assert t.signal_idx == 5 and t.entry_idx == 6
    assert t.entry == 10.85 != 10.8  # next open, not the breakout close
    assert t.sl == pytest.approx(9.99)  # min(low 10.6, prev close 10.0) - tick
    assert t.r == pytest.approx(10.85 - 9.99)
    assert t.tp == pytest.approx(10.85 + 1.5 * (10.85 - 9.99))


def test_short_candle_high_uses_prev_close_and_tp_below():
    day = box() + [bar(5, 9.5, o=9.7), bar(6, 9.45, o=9.6)] + quiet(7, 3, 8.9)
    t = run_day(day, TICK)[0]
    assert t.side == -1 and t.sl == pytest.approx(10.01)  # max(high 9.7, prev close 10.0) + tick
    assert t.entry == 9.6
    assert t.tp == pytest.approx(9.6 - 1.5 * (10.01 - 9.6))
    assert t.reason == "tp"  # closes at 8.9 < TP 8.985


def test_stop_is_filled_at_the_close_not_the_level():
    day = box() + [bar(5, 10.3, o=10.0), bar(6, 10.3), bar(7, 9.5, o=10.3)]  # close 9.5 << SL 9.99
    t = run_day(day, TICK)[0]
    assert (t.reason, t.exit_price, t.intrabar_fill) == ("sl", 9.5, False)
    assert t.gross_pct == pytest.approx((9.5 - 10.3) / 10.3 * 100)


def test_intrabar_touch_fills_at_level_only_with_three_snapshots():
    base = box() + [bar(5, 10.3, o=10.0), bar(6, 10.3)]  # entry 10.3, SL 9.99, TP 10.3 + 0.465 = 10.765
    seen = run_day(base + [bar(7, 10.4, l=9.9, n=3)], TICK)[0]
    assert (seen.reason, seen.exit_price, seen.intrabar_fill) == ("sl", 9.99, True)
    one = run_day(base + [bar(7, 10.4, l=9.9, n=1)] + quiet(8, 3, 10.4), TICK)[0]
    assert one.reason == "eod"  # a lone snapshot's low is not observed


def test_sl_checked_before_tp():
    base = box() + [bar(5, 10.3, o=10.0), bar(6, 10.3)]
    t = run_day(base + [bar(7, 10.4, h=11.0, l=9.0, n=4)], TICK)[0]
    assert t.reason == "sl"


def test_flat_at_1500_bar_close():
    day = box(230) + [bar(235, 10.3, o=10.0), bar(236, 10.3), bar(237, 10.35), bar(238, 10.4), bar(239, 10.45)]
    t = run_day(day, TICK)[0]
    assert (t.reason, t.exit_idx, t.exit_price) == ("eod", DAY_BARS - 1, 10.45)


def test_signal_on_last_bar_is_dropped():
    assert run_day(box(230) + quiet(235, 4) + [bar(239, 10.3, o=10.0)], TICK) == []


def test_afternoon_restarts_box_with_zero_redraws():
    morning = box(0) + quiet(5, 115)  # idx 0..119: many redraws
    w = BoxWatcher()
    feed(w, morning)
    assert w.redraw_count >= 1
    afternoon = box(120)
    feed(w, afternoon)
    assert (w.mode, w.redraw_count, w.box_high, w.box_low) == ("watch", 0, 10.1, 9.9)
    s = w.step(bar(125, 10.3, o=10.0))
    assert s is not None and s.redraw_count == 0


def test_open_trade_carries_across_lunch():
    # box 110..114, signal on 118, entry at 119 open; the trade is still open at 120/121 and exits on the TP at 122
    day = box(110) + quiet(115, 3) + [bar(118, 10.3, o=10.0), bar(119, 10.3), bar(120, 10.35), bar(121, 10.35), bar(122, 10.8)]
    t = run_day(day, TICK)
    assert len(t) == 1 and (t[0].entry_idx, t[0].exit_idx, t[0].reason) == (119, 122, "tp")


def test_entry_across_lunch_uses_first_afternoon_open():
    # box 110..114, fifth watch bar 119 breaks out; the signal is known at 11:30 and fills at the 13:01 open
    day = box(110) + quiet(115, 4) + [bar(119, 10.3, o=10.0), bar(120, 10.4, o=10.32)] + quiet(121, 3, 10.4)
    t = run_day(day, TICK)[0]
    assert (t.signal_idx, t.entry_idx, t.entry) == (119, 120, 10.32)


def test_blocked_bar_gives_no_signal_or_entry():
    assert run_day(box() + [bar(5, 10.3, o=10.0, blocked=True), bar(6, 10.0), bar(7, 10.0)], TICK) == []
    # the entry bar (6) is sealed: the first signal is not taken; bar 7 breaks again and is entered at bar 8
    day = box() + [bar(5, 10.3, o=10.0), bar(6, 10.3, blocked=True), bar(7, 10.3), bar(8, 10.3)]
    ts = run_day(day, TICK)
    assert [t.signal_idx for t in ts] == [7] and ts[0].entry_idx == 8


def test_gap_through_stop_is_not_a_trade():
    day = box() + [bar(5, 10.3, o=10.0), bar(6, 9.5, o=9.5)]  # next open 9.5 below SL 9.99
    assert run_day(day, TICK) == []


def test_long_only_ignores_short_breakouts():
    day = box() + [bar(5, 9.7, o=10.0), bar(6, 9.6, o=9.7)]
    assert run_day(day, TICK, allow_short=False) == []
    assert len(run_day(day, TICK)) == 1


def test_new_trade_after_exit_waits_for_next_boundary():
    day = box() + [bar(5, 10.3, o=10.0), bar(6, 10.3), bar(7, 9.5, o=10.3)]  # stopped out at idx 7
    day += quiet(8, 2, 9.5)  # 8, 9: idle, no box may start off the boundary
    day += [bar(10 + k, 9.5) for k in range(5)] + [bar(15, 9.9, o=9.5), bar(16, 9.9)]  # box 10..14, break up on 15
    ts = run_day(day, TICK)
    assert [t.signal_idx for t in ts] == [5, 15] and ts[1].redraw_count == 0


def test_pos_weight_cap_and_risk_sizing():
    assert pos_weight(10.0, 0.05, 0.2) == pytest.approx(0.2)  # 0.5% stop -> 2.0 capped at 0.2
    assert pos_weight(10.0, 0.05, 0.1) == pytest.approx(0.1)
    assert pos_weight(10.0, 0.5, 0.2) == pytest.approx(0.2)  # 5% stop -> 0.2 exactly at the cap
    assert pos_weight(10.0, 1.0, 0.2) == pytest.approx(0.1)  # 10% stop -> 0.1 below the cap


def test_first_signal_sells_at_next_open():
    day = box() + [bar(5, 10.0), bar(6, 9.7, o=10.0), bar(7, 9.6, o=9.65)]
    sig, idx, px = first_signal(day, -1)
    assert (sig.idx, idx, px) == (6, 7, 9.65)
    assert first_signal(box() + quiet(5, 4), -1) is None
