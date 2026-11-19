"""E077 / E078 domain rules with hand-computed cases (no database)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal as D

from pulse.domain.fwdlog import stats as SS
from pulse.domain.fwdlog import tail as TL

DAY, NEXT = date(2026, 11, 12), date(2026, 11, 13)


def q(code, price, *, prev="9.70", name="NAME", limit="11.00", vr="1.5", to="6", cap="100", open_=None):
    return TL.Quote(code, name, D(price), D(prev), D(open_ or prev), D(limit), D(to), D(cap), D(vr))


def pool10(**over):
    """c01..c10: float cap 20..200, volume ratio 1.1..2.0, price 10.00 on previous close 9.70 (+3.09 percent) at both times."""
    codes = [f"c{i:02d}" for i in range(1, 11)]
    a = {c: q(c, "10.00", cap=str(20 * i), vr=f"{1 + i / 10:.1f}", **over) for i, c in enumerate(codes, 1)}
    return codes, a, dict(a)


def exit_quotes(codes, drop=()):
    return {c: q(c, f"{10 + 0.1 * i:.2f}") for i, c in enumerate(codes, 1) if c not in drop}


def test_quintiles_use_average_rank_and_ceil():
    caps = {f"c{i}": D(i * 10) for i in range(1, 11)}
    assert [TL.quintiles(caps)[f"c{i}"] for i in range(1, 11)] == [1, 1, 2, 2, 3, 3, 4, 4, 5, 5]
    tied = {"a": D(5), "b": D(5), "c": D(5), "d": D(5), "e": D(5)}                      # all tied: average rank 3 of 5 -> ceil(3*5/5) = 3
    assert set(TL.quintiles(tied).values()) == {3}
    assert TL.quintiles({"a": None, "b": D(1)}) == {"a": None, "b": 5}                   # a missing cap has no quintile; one stock -> rank 1 of 1 -> 5


def test_e077_row_hand_computed():
    codes, q30, q50 = pool10()
    row = TL.e077_row(DAY, NEXT, q30, q50, exit_quotes(codes), frozenset(codes))
    # signals: top 5 by volume ratio = c10..c06 (ratio 2.0 .. 1.6). ret_i = i percent; cap5 cells: 1.5 3.5 5.5 7.5 9.5
    assert row["signals"] == ["c10", "c09", "c08", "c07", "c06"] and row["pool"] == 10 and row["n"] == 5 and row["missing_exit"] == 0
    assert (row["ret"], row["cell"], row["ex"], row["net"]) == (D("8.000"), D("7.900"), D("0.100"), D("7.748"))
    assert row["date"] == "2026-11-12" and row["exit_day"] == "2026-11-13"


def test_e077_missing_exit_is_missing_not_zero():
    codes, q30, q50 = pool10()
    row = TL.e077_row(DAY, NEXT, q30, q50, exit_quotes(codes, drop=("c10",)), frozenset(codes))
    # c10 has no exit price: n = 4, missing_exit = 1; cell of quintile 5 is c09 alone (9), ret mean 7.5, cell mean 7.375, ex mean 0.125
    assert (row["n"], row["missing_exit"]) == (4, 1)
    assert (row["ret"], row["cell"], row["ex"], row["net"]) == (D("7.500"), D("7.375"), D("0.125"), D("7.248"))
    none = TL.e077_row(DAY, NEXT, q30, q50, {}, frozenset(codes))
    assert none["n"] == 0 and "ret" not in none and none["signals"][0] == "c10"        # no exit price at all: no statistics, never 0


def test_e077_filters_and_ties():
    codes, q30, q50 = pool10()
    q50["c10"] = q("c10", "10.00", name="ST平安")                                       # ST name: out of the pool
    q50["c09"] = q("c09", "11.00")                                                       # at the limit-up price: out of the pool
    q50["c08"] = q("c08", "1.99", prev="1.90")                                           # price below 2
    q50["c07"] = q("c07", "10.00", prev="0")                                             # no previous close
    q30["c06"] = q("c06", "10.50")                                                       # +8.2 percent at 14:30: outside [3, 5]
    q50["c05"] = q("c05", "10.00", vr="0.99", cap="100")                                 # volume ratio < 1
    pool = TL.e077_pool(q30, q50)
    assert set(pool) == {"c01", "c02", "c03", "c04", "c05", "c06"}
    sig = TL.e077_signals(pool, frozenset(codes))
    assert sig == ["c04", "c03", "c02", "c01"]                                           # c06 fails 14:30, c05 fails volume ratio
    # a tie on volume ratio is broken by code (ascending); band edges are inclusive (turnover 5 and 10, pct 3 and 5)
    edge = {"a": q("a", "10.00", prev="9.70", vr="2", to="5"), "b": q("b", "10.00", prev="9.70", vr="2", to="10")}
    assert TL.e077_signals(TL.e077_pool(edge, edge), frozenset(edge)) == ["a", "b"]
    five = {"x": q("x", "10.50", prev="10", vr="2"), "y": q("y", "10.30", prev="10", vr="2")}
    assert TL.e077_signals(TL.e077_pool(five, five), frozenset(five)) == ["x", "y"]      # +5.0 and +3.0 percent are inside; not in history = out
    assert TL.e077_signals(TL.e077_pool(five, five), frozenset({"y"})) == ["y"]


def test_limit_history_window_and_seed_session():
    dates = [date(2026, 10, 1) + __import__("datetime").timedelta(days=i) for i in range(22)]       # 22 sessions before the decision day
    day = dates[-1] + __import__("datetime").timedelta(days=1)

    def flat(jump_at=None, jump=D("1.10")):
        out, c = [], D(10)
        for i, d in enumerate(dates):
            if i == jump_at:
                c = c * jump
            out.append((d, c))
        return out
    closes = {"in_window": flat(10), "edge97": flat(10, D("1.097")), "below97": flat(10, D("1.0969")), "too_old": flat(1), "first_in_window": flat(2), "no_jump": flat()}
    got = TL.limit_history(closes, day)
    # the window is the last 21 sessions (index 1..21); a return needs a previous row INSIDE the window, so the jump at index 1 (against index 0) is outside
    assert got == {"in_window", "edge97", "first_in_window"}
    assert TL.limit_history({"x": [(dates[0], D(10))]}, day) == frozenset()


def test_halfhour_hand_computed():
    p1000 = {"a": q("a", "10.30", prev="10", open_="10.10"), "b": q("b", "9.90", prev="10", open_="10.00"), "z": q("z", "10", prev="0", open_="10")}
    p1430 = {"a": q("a", "10.50"), "b": q("b", "9.80"), "z": q("z", "1")}
    pclose = {"a": q("a", "10.605"), "b": q("b", "9.702"), "z": q("z", "1")}
    r = TL.halfhour(p1000, p1430, pclose)
    # z is out (previous close 0). x = mean(+3.0, -1.0) = +1.0; x2 = mean(10.30/10.10-1, 9.90/10.00-1) = mean(1.9802%, -1.0%) = 0.4901%; y = mean(+1.0%, -1.0%) = 0
    assert r["n"] == 2 and r["x"] == D("1.0000") and r["x2"] == D("0.4901") and r["y"] == D("0.0000")
    assert TL.halfhour({}, {}, {}) is None
    assert TL.halfhour(p1000, {"a": p1430["a"]}, pclose)["n"] == 1                       # only codes present at every time point


def test_e078_row_marks_index_presence():
    ew = {"n": 1, "x": D("1"), "x2": D("1"), "y": D("1")}
    assert "index" not in TL.e078_row(DAY, ew, None, index_recorded=False)
    assert TL.e078_row(DAY, ew, None, index_recorded=True)["index"] is None             # recorded file, no usable index rows


def test_bootstrap_matches_the_old_draw_and_needs_two_days():
    assert SS.bootstrap_p_le_zero([1.0]) is None and SS.bootstrap_p_le_zero([]) is None
    assert SS.bootstrap_p_le_zero([1.0, 2.0, 3.0]) == 0.0                                  # every resample mean is positive
    assert SS.bootstrap_p_le_zero([-1.0, -2.0]) == 1.0
    assert SS.bootstrap_p_le_zero([0.5, -0.3, 1.2, 0.1]) == SS.bootstrap_p_le_zero([0.5, -0.3, 1.2, 0.1])   # seeded: reproducible


def test_e077_summary_is_pending_before_the_decision_day():
    rows = [{"ex": D("1.000"), "net": D("0.748")}, {"ex": D("-0.2"), "net": D("-0.452")}, {"date": "x"}]
    s = SS.e077_summary(rows)
    assert s["decision_days"] == 3 and s["signal_days"] == 2 and s["mean_ex"] == D("0.400") and s["mean_net"] == D("0.148")
    assert s["decided"] is False and s["verdict"] is None and s["h1"] is None and s["pending"].startswith("pending until 40")
    big = [{"ex": D("0.5"), "net": D("0.3")}] * 40
    d = SS.e077_summary(big)
    assert d["decided"] and d["h1"] is True and d["h2"] is True and d["verdict"] == "works"
    few_signal = [{"ex": D("0.5"), "net": D("0.3")}] * 24 + [{"date": "no signal"}] * 16
    assert SS.e077_summary(few_signal)["decided"] is False                                  # 40 days but only 24 with a signal: not decided


def test_newey_west_slope_hand_case():
    # perfect line y = 2x + 1: slope 2, residuals 0 -> se 0 -> t undefined (nan), not a fake number
    s = SS.nw_slope([1, 2, 3, 4, 5], [3, 5, 7, 9, 11])
    assert round(s[0], 9) == 2.0 and s[1] != s[1]
    assert SS.nw_slope([1, 1, 1], [1, 2, 3]) is None and SS.nw_slope([1, 2], [1, 2]) is None
    s2 = SS.nw_slope([0, 1, 2, 3, 4, 5], [0.1, 0.9, 2.2, 2.8, 4.1, 5.0])
    assert abs(s2[0] - 0.99) < 0.02 and s2[1] > 10
    h = SS.e078_summary([{"date": "d", "index": {"x": D(i), "y": D(i)}, "ew": {"x": D(i), "y": D(2 * i)}} for i in range(5)])
    assert h["decided"] is False and h["h1"] is None and h["index_points"] == 5
