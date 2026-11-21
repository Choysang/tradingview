"""Seat playbooks (smart_seat_follow, institution_net_buy): the pure seat features, the signals, and NO LOOK-AHEAD.
SYNTHETIC lists: proves mechanics and point-in-time behaviour, not the value of the strategies."""
from __future__ import annotations

import pytest

from pulse.domain.playbooks import library as L
from pulse.domain.playbooks import seats as SE
from pulse.domain.playbooks.builder import prepare, run_signal

from .synth import panel, sessions

DAYS = sessions(30)
A = "stockA"
BARS = {A: (tuple(DAYS), tuple([100.0] * 30), tuple([110.0] * 30))}          # every forward return is +10 %


def rows_for(seat: str, days: list[int], side: str = "buy", amount: float = 1e7, code: str = A) -> list[SE.SeatRow]:
    return [SE.SeatRow(code, DAYS[d], side, seat, amount) for d in days]


def series(rows, known=None, bars=BARS):
    listed = {(r.code, r.day) for r in rows}
    known = {r.day for r in rows} if known is None else known
    return SE.seat_series(rows, listed, known, bars, DAYS)


def test_a_seat_becomes_smart_only_once_its_fifth_forward_window_has_closed():
    rows = rows_for("S", [0, 1, 2, 3, 4]) + rows_for("S", [10])
    col = series(rows, known=set(DAYS))[A]
    # event on listing day j settles at the close of bar j + 3; the fifth (j = 4) settles on bar 7
    rows_early = rows_for("S", [0, 1, 2, 3, 4]) + rows_for("S", [6])
    assert series(rows_early, known=set(DAYS))[A][6][1] == 0, "bar 6: only the events of days 0..3 have closed"
    rows_ok = rows_for("S", [0, 1, 2, 3, 4]) + rows_for("S", [7])
    cell = series(rows_ok, known=set(DAYS))[A][7]
    assert cell[0] is True and cell[1] == 1 and cell[2] == pytest.approx(50.0 / 10.0) and cell[3] == 0
    assert col[10][1] == 1


def test_institution_pool_is_never_smart_and_gets_its_own_net_with_exact_duplicates_counted_once():
    rows = (rows_for(SE.INSTITUTION, [0, 1, 2, 3, 4], amount=1e7)
            + [SE.SeatRow(A, DAYS[9], "buy", SE.INSTITUTION, 5e8), SE.SeatRow(A, DAYS[9], "buy", SE.INSTITUTION, 5e8),      # same row repeated for another listing reason
               SE.SeatRow(A, DAYS[9], "sell", SE.INSTITUTION, 1e8)])
    col = series(rows, known=set(DAYS))[A]
    assert all(c[1] == 0 for c in col if c is not None)
    assert col[9][4] == pytest.approx(4e8)
    assert col[12][4] == 0.0 and col[12][0] is False, "known day, nobody listed: a known zero"


def test_an_institution_row_without_an_amount_makes_the_net_unknown_not_zero():
    rows = [SE.SeatRow(A, DAYS[3], "buy", SE.INSTITUTION, None), SE.SeatRow(A, DAYS[3], "buy", SE.INSTITUTION, 1e8)]
    assert series(rows)[A][3][4] is None


def test_a_day_without_an_imported_list_is_unknown_not_empty():
    rows = rows_for("S", [3])
    col = series(rows, known={DAYS[3], DAYS[5]})[A]
    assert col[4] is None and col[3] is not None and col[5] is not None and col[5][0] is False


def test_one_day_trip_seat_is_counted_and_is_not_smart():
    buy = rows_for("T", [0, 1, 2, 3, 4])
    sell = [SE.SeatRow(A, DAYS[d + 1], "sell", "T", 1e7) for d in range(5)]          # sold on the next listed session each time
    rows = buy + sell + rows_for("T", [10])
    cell = series(rows, known=set(DAYS))[A][10]
    assert cell[3] == 1, "five settled buys, all sold next session: a one-day trip seat"
    assert cell[1] == 1 and cell[2] > 0, "the same seat is also profitable here; the playbook rule excludes the day because trip buyers > 0"


def test_published_the_evening_of_d_features_of_a_bar_never_use_later_lists():
    base = rows_for("S", [0, 1, 2, 3, 4, 5, 6]) + rows_for(SE.INSTITUTION, [4], amount=3e7)
    later = base + rows_for("S", [20, 21, 22]) + rows_for(SE.INSTITUTION, [15], amount=9e8)
    full = series(later, known=set(DAYS))[A]
    cut = 12
    short_bars = {A: (tuple(DAYS[:cut]), BARS[A][1][:cut], BARS[A][2][:cut])}
    short_rows = [r for r in later if r.day <= DAYS[cut - 1]]
    short = SE.seat_series(short_rows, {(r.code, r.day) for r in short_rows}, set(DAYS[:cut]), short_bars, DAYS[:cut])[A]
    # events whose forward window would extend past the cut do not exist yet in the short panel; they close by bar j+3 <= cut-1, so bars <= cut-1 agree
    assert full[:cut] == short


def test_signals_use_the_cells_and_return_none_without_a_list():
    days, raws, views = panel(3, 300)
    prepare(views, days)
    v = views[0]
    i = next(k for k in range(150, 290) if L.liquid(v, k, 0.5e8) and v.close[k] is not None and all(abs(v.pct()[k - d] or 0.0) < 8 for d in (0, 1, 2)))
    smart, inst = L.REGISTRY["smart_seat_follow"], L.REGISTRY["institution_net_buy"]
    assert all(x is None for x in run_signal(smart, v)) and all(x is None for x in run_signal(inst, v)), "no seats attached: nothing fires"
    amt = v.turnover_cny()
    cells: list = [None] * v.n
    cells[i] = (True, 2, 3.5, 0, 0.0)
    cells[i + 1] = (True, 1, 3.5, 0, 0.0)             # only one smart buyer
    cells[i + 2] = (True, 3, 3.5, 1, 0.0)             # a one-day trip seat among the buyers
    cells[i + 3] = (False, 3, 3.5, 0, 0.0)            # not listed that day
    cells[i - 1] = (True, 0, 0.0, 0, 0.04 * amt[i - 1])      # institution net above 3 % of the turnover proxy
    cells[i - 2] = (True, 0, 0.0, 0, 0.02 * amt[i - 2])
    v.seats = cells
    out = run_signal(smart, v)
    assert out[i] == pytest.approx(3.5) and out[i + 1] is None and out[i + 2] is None and out[i + 3] is None
    net = run_signal(inst, v)
    assert net[i - 2] is None, "2 % of the turnover is below the 3 % floor"
    assert net[i - 1] == pytest.approx(0.04)
    cells[i - 1] = (True, 0, 0.0, 0, -5e7)
    v.seats = cells
    assert run_signal(inst, v)[i - 1] is None, "a net sale never fires"


@pytest.mark.parametrize("cut", [200, 260])
def test_seat_signals_truncation_equals_prefix(cut):
    days, raws, views = panel(3, 300)
    prepare(views, days)
    v = views[0]
    v.seats = [None if k % 5 == 0 else (k % 3 == 0, k % 4, 1.0 + (k % 7), k % 2, float(k % 9 - 3) * 1e7) for k in range(v.n)]
    prepare([v], days)
    full = {pid: run_signal(L.REGISTRY[pid], v) for pid in ("smart_seat_follow", "institution_net_buy")}
    short = v.truncate(cut)
    prepare([short], days[:cut])
    for pid, sig in full.items():
        assert run_signal(L.REGISTRY[pid], short) == sig[:cut], pid


def test_remaining_unavailable_playbooks_state_their_reason_and_the_seat_pair_is_live():
    for pid in ("smart_seat_follow", "institution_net_buy"):
        pb = L.REGISTRY[pid]
        assert pb.requires == ("lhb_seats",) and pb.unavailable_reason is None
    for pid in ("industry_follower", "industry_first_board", "name_cluster_follower"):
        assert L.REGISTRY[pid].unavailable_reason
