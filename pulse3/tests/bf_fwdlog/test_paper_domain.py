"""Paper replay rules (triggers v2.1): sealed, B1, B3, S1, S3, M3, catalyst, queue fill, market heat, sell policy, box5, and one hand-computed replay day."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal as D
from fractions import Fraction

import pytest

from pulse.domain.fwdlog import board as BD
from pulse.domain.fwdlog import box5 as BX
from pulse.domain.fwdlog import heat as HT
from pulse.domain.fwdlog import paper as PP
from pulse.domain.fwdlog import queue as QF
from pulse.domain.fwdlog import triggers as TR
from pulse.domain.fwdlog.common import CST, UTC

DAY = date(2026, 11, 12)


def t(hh, mm, ss=0, day=DAY):
    return datetime.combine(day, time(hh, mm, ss), CST).astimezone(UTC)


def snap(code="600001", at=(9, 40), *, price="11.00", bid="11.00", lots="5000", ask=None, vwap="10.9", limit="11.00", kind="full", vol="100", high=None, prev="10.00",
         open_=None, amount=None, known=None):
    k = t(*at) if known is None else known
    return TR.Snap(code, kind, k, k - timedelta(seconds=3), D(price) if price else None, D(high or price) if (high or price) else None, D(prev), D(open_) if open_ else None,
                   D(limit) if limit else None, D(bid) if bid else None, D(lots) if lots else None, D(ask) if ask else None, D(vwap) if vwap else None, D(vol), D(amount) if amount else None)


# ---- triggers ----------------------------------------------------------------------------------------------------------------------
def test_sealed_needs_price_bid_at_limit_and_empty_ask():
    assert TR.sealed(snap())
    assert not TR.sealed(snap(price="10.99"))
    assert not TR.sealed(snap(bid="10.99"))
    assert not TR.sealed(snap(bid=None))
    assert not TR.sealed(snap(ask="11.01"))                      # something offered
    assert not TR.sealed(snap(limit=None))                       # no limit price: unknown, never sealed


def test_b1_gap_band_and_auction_amount():
    def final(open_, amount="3500000"):
        return snap(kind="auction_final", at=(9, 25, 5), price=open_, bid=None, lots=None, vwap=None, limit="11.00", prev="10.00", open_=open_, amount=amount)
    prev_amount = D("100000000")                                 # 1e8: 3 percent = 3 000 000
    assert TR.b1(final("10.20"), prev_amount)                    # +2.0 percent is inside (inclusive)
    assert not TR.b1(final("10.19"), prev_amount)
    assert TR.b1(final("10.69"), prev_amount)
    assert not TR.b1(final("10.70"), prev_amount)               # +7.0 percent is outside (exclusive)
    assert TR.b1(final("10.30", "3000000"), prev_amount) and not TR.b1(final("10.30", "2999999"), prev_amount)
    assert not TR.b1(final("10.30"), None)                       # the previous amount unknown: never a signal
    lots_only = snap(kind="auction_final", price="10.30", bid=None, lots=None, vwap=None, open_="10.30", amount=None, vol="300")        # amount missing: lots x 100 x price
    assert TR.auction_amount(lots_only) == D("309000") and not TR.b1(lots_only, prev_amount)


def test_b3_needs_30s_open_and_before_1430():
    def feed(points):
        b = TR.B3()
        return [b.feed(snap(at=a, price=p, bid=bd, ask=ak, lots="100")) for a, p, bd, ak in points]
    seal, brk = ("11.00", "11.00", None), ("10.90", "10.89", "10.91")
    assert feed([((9, 40), *seal), ((9, 45), *brk), ((9, 45, 29), *seal)]) == [False, False, False]      # 29 s is not enough
    assert feed([((9, 40), *seal), ((9, 45), *brk), ((9, 45, 30), *seal)]) == [False, False, True]       # 30 s is
    assert feed([((9, 40), *seal), ((14, 29), *brk), ((14, 30), *seal)]) == [False, False, False]        # re-seal at 14:30 is too late
    assert feed([((9, 40), *seal), ((9, 40, 30), *seal)]) == [False, False]                               # never broke
    b = TR.B3()
    assert [b.feed(snap(at=(9, 40))), b.feed(snap(at=(9, 41), price="10.9", bid="10.89", ask="10.91")), b.feed(snap(at=(9, 43))), b.feed(snap(at=(9, 44)))] == [False, False, True, False]   # fires once


def test_s1_peak_decay():
    s = TR.S1()
    seq = [("5000", False), ("5000", False), ("1500", False), ("1499", True), ("100", False)]            # 0.3 x 5000 = 1500: strictly below fires; fires once
    assert [s.feed(snap(lots=l)) for l, _ in seq] == [f for _, f in seq]
    thin = TR.S1()
    assert [thin.feed(snap(lots=l)) for l in ("1999", "10")] == [False, False]                           # a peak below 2000 lots never arms it
    reset = TR.S1()
    reset.feed(snap(lots="5000"))
    assert reset.feed(snap(price="10.9", bid="10.89", ask="10.91")) is False and reset.peak == 0           # an open board resets the peak


def test_s3_seconds_under_vwap():
    s3 = TR.S3()
    below = [s3.below_s(snap(at=(9, 40, x), price="10.80", vwap="10.90", known=t(9, 40) + timedelta(seconds=x))) for x in (0, 30, 90)]
    assert below == [0.0, 30.0, 90.0]
    assert s3.below_s(snap(at=(9, 42), price="10.90", vwap="10.90")) == 0.0 and s3.below_since is None  # back at VWAP resets
    assert s3.below_s(snap(vwap=None)) == 0.0


def test_sell_policies():
    open_, close_ = (t(9, 30), D("11")), (t(15, 0), D("10.5"))
    intraday = (t(10, 0), D("11"), "S1")
    assert TR.sell_policy(True, "hot", intraday, open_, close_)[:2] == open_
    assert TR.sell_policy(True, "mid", None, open_, close_)[2].startswith("open")
    assert TR.sell_policy(True, "cold", intraday, open_, close_) == intraday
    assert TR.sell_policy(True, "cold", None, open_, close_)[:2] == close_
    assert TR.sell_policy_v3(False, intraday, open_, close_)[:2] == open_
    assert TR.sell_policy_v3(True, intraday, open_, close_) == intraday and TR.sell_policy_v3(True, None, open_, close_)[:2] == close_
    assert TR.ACTIVE == {"B1": False, "B3": False} and TR.TRIGGERS_VERSION == "triggers-v2.1"


# ---- M3 and the catalyst ------------------------------------------------------------------------------------------------------------
def test_break_step_tape_vs_strict_and_exit_wins():
    S = BD.BreakState
    args = dict(sealed=False, above_vwap=True, below_vwap_s=0.0, peers_sealed=2, sector_dump=False)
    assert BD.break_step(S.SEALED, t(10, 0), catalyst_pending=None, variant="tape", **args).state is S.BENIGN          # unknown catalyst does not veto the tape version
    assert BD.break_step(S.SEALED, t(10, 0), catalyst_pending=None, variant="strict", **args).state is S.BROKEN_WATCH  # strict needs a catalyst known to be pending
    assert BD.break_step(S.SEALED, t(10, 0), catalyst_pending=True, variant="strict", **args).state is S.BENIGN
    assert BD.break_step(S.SEALED, t(10, 0), catalyst_pending=False, variant="tape", **args).state is S.BROKEN_WATCH   # a landed catalyst blocks benign
    assert BD.break_step(S.SEALED, t(14, 20), catalyst_pending=True, variant="strict", **args).state is S.BROKEN_WATCH  # late in the day
    assert BD.break_step(S.SEALED, t(10, 0), catalyst_pending=True, variant="strict", **{**args, "peers_sealed": 1}).state is S.BROKEN_WATCH
    assert BD.break_step(S.SEALED, t(10, 0), catalyst_pending=True, variant="strict", **{**args, "peers_sealed": None}).state is S.BROKEN_WATCH
    assert BD.break_step(S.SEALED, t(10, 0), catalyst_pending=True, variant="strict", **{**args, "below_vwap_s": 90.0}).state is S.EXIT   # exit wins
    assert BD.break_step(S.BENIGN, t(10, 0), catalyst_pending=True, **{**args, "sector_dump": True}).reason == "sector dump"
    assert BD.break_step(S.EXIT, t(10, 0), catalyst_pending=True, **args).state is S.EXIT
    assert BD.break_step(S.BENIGN, t(10, 0), **{**args, "sealed": True}, catalyst_pending=None).state is S.RESEALED


def ev(name="互联网大会", category="conference", start=date(2026, 11, 20), end=None, seen=datetime(2026, 11, 1, tzinfo=UTC), codes=("600001",), result=None, status="official_scheduled"):
    return BD.CalEvent("e", name, category, status, start, seen, end, None, "Asia/Shanghai", result, frozenset(codes))


def test_catalyst_pending_landed_unknown():
    now = t(10, 0)
    assert BD.catalyst_pending([ev()], "600001", now) is True                                    # starts later: pending
    assert BD.catalyst_pending([ev(start=date(2026, 11, 10))], "600001", now) is False          # its window is over: landed
    assert BD.catalyst_pending([ev(start=DAY)], "600001", now) is None                          # under way today: neither
    assert BD.catalyst_pending([ev(start=DAY, end=DAY + timedelta(days=2))], "600001", now) is None
    assert BD.catalyst_pending([ev(seen=datetime(2026, 11, 13, tzinfo=UTC))], "600001", now) is None   # first seen later: not usable (point in time)
    assert BD.catalyst_pending([ev(category="macro_release")], "600001", now) is None            # routine releases never count
    assert BD.catalyst_pending([ev(status="cancelled")], "600001", now) is None
    assert BD.catalyst_pending([ev()], "600002", now) is None                                    # not linked to this stock
    assert BD.catalyst_pending([ev(start=date(2026, 11, 10)), ev()], "600001", now) is True     # one pending event outweighs a landed one
    assert BD.catalyst_pending([ev(result=datetime(2026, 11, 11, tzinfo=UTC))], "600001", now) is False   # result known: landed even before the date


def test_event_linking_by_industry_members_and_skip_categories():
    rules = [BD.LinkRule("互联网大会", ("软件开发", "IT服务Ⅱ"), "互联网"), BD.LinkRule("半导体", ("半导体",), "半导体")]
    members = {"软件开发": frozenset({"600001", "600002"}), "IT服务Ⅱ": frozenset({"600003"}), "半导体": frozenset({"600009"})}
    out = BD.link_events([ev(codes=()), ev(name="其他", codes=()), ev(name="互联网大会", category="market_rule", codes=())], members, rules, ("macro_release", "market_rule"))
    assert out[0].instruments == frozenset({"600001", "600002", "600003"}) and out[1].instruments == frozenset() and out[2].instruments == frozenset()   # market_rule is skipped


# ---- queue fill -------------------------------------------------------------------------------------------------------------------
def qrow(m, bid, vol, price="11.00", bidpx="11.00"):
    return QF.QueueRow(t(9, m), D(price) if price else None, D("11.00"), D(bidpx) if bidpx else None, D(bid), D(vol))


def test_queue_three_scenarios_hand_computed():
    rows = [qrow(40, "1000", "100"), qrow(41, "1200", "150"), qrow(42, "800", "600"), qrow(43, "800", "1305")]
    out = QF.simulate(rows, t(9, 40, 30), D("10"), t(15, 0))
    # arrival 09:40:30 -> first row at or after = 09:41 (k = 1). Queue ahead: OPT 1000 (bid before), BASE and PESS 1200.
    # volume at the limit per interval: 50, 450, 705 -> consumed 1205 (OPT counts every interval, here all end at the limit)
    assert out[QF.Scenario.OPTIMISTIC].queue_ahead == D("1000") and out[QF.Scenario.BASE].queue_ahead == D("1200")
    assert out[QF.Scenario.OPTIMISTIC].filled_lots == D("10") and out[QF.Scenario.OPTIMISTIC].fill_time == t(9, 43)
    assert out[QF.Scenario.BASE].filled_lots == D("5") and out[QF.Scenario.BASE].fill_time is None            # 1205 - 1200 = 5 lots of 10
    assert out[QF.Scenario.PESSIMISTIC].filled_lots == D("5")


def test_queue_broken_board_fills_everyone_and_no_row_after_arrival_fills_nothing():
    rows = [qrow(40, "1000", "100"), qrow(41, "1200", "150"), qrow(42, "300", "160", price="10.90", bidpx="10.89")]
    out = QF.simulate(rows, t(9, 40, 30), D("10"), t(15, 0))
    assert all(f.filled_lots == D("10") and f.broken and f.fill_time == t(9, 42) for f in out.values())
    none = QF.simulate(rows, t(10, 0), D("10"), t(15, 0))
    assert all(f.filled_lots == D("0") and f.fill_time is None for f in none.values())
    assert QF.simulate([qrow(40, "1000", "100"), qrow(41, "1000", "100")], t(9, 40, 30), D("10"), t(15, 0))[QF.Scenario.BASE].filled_lots == D("0")   # no volume: nothing filled


# ---- market heat -------------------------------------------------------------------------------------------------------------------
def test_limit_up_price_rounds_half_up_in_cents():
    assert HT.limit_up_cents(1005) == 1106                       # 10.05 x 1.1 = 11.055 -> 11.06
    assert HT.limit_up_cents(1000) == 1100 and HT.limit_up_cents(333) == 366      # 3.33 x 1.1 = 3.663 -> 3.66
    assert PP.limit_price(D("10.05")) == D("11.06")


def test_daily_counts_hand_computed():
    d = [date(2026, 11, 9) + timedelta(days=i) for i in range(3)]
    bars = {"X": [(d[0], D("10"), D("10")), (d[1], D("11"), D("11")), (d[2], D("11.6"), D("11.5"))],        # day 2: close at the limit (11.0 on 10.0); day 3 not
            "Y": [(d[0], D("10"), D("10")), (d[1], D("11"), D("10.8")), (d[2], D("10.8"), D("10.8"))]}     # day 2: touched the limit, closed below: a failed board
    assert HT.daily_counts(bars) == [(d[1], 1, 1), (d[2], 0, 0)]


def test_state_at_boundaries_use_exact_fractions():
    d0 = date(2026, 1, 1)
    counts = [(d0 + timedelta(days=i), i + 1, 0) for i in range(60)]                           # earlier days: 1..60 limit-ups, no failed boards
    def at(last: int):
        return HT.state_at(counts + [(d0 + timedelta(days=60), last, 0)], d0 + timedelta(days=60))
    assert at(20).heat == "cold" and at(21).heat == "mid"        # 19/60 < 1/3 but 20/60 = 1/3 exactly is not below it
    assert at(40).heat == "mid" and at(41).heat == "hot"         # 39/60 < 2/3 but 40/60 = 2/3 exactly is the upper third
    short = HT.state_at(counts[:59] + [(d0 + timedelta(days=59), 30, 0)], d0 + timedelta(days=59))
    assert short.heat == "unknown" and short.failed == "unknown"                                # fewer than 60 earlier sessions: never guessed
    rates = [(d0 + timedelta(days=i), 10, (i % 3)) for i in range(60)]                          # earlier failed rates: 0, 1/11, 2/12 repeating
    hi = HT.state_at(rates + [(d0 + timedelta(days=60), 10, 5)], d0 + timedelta(days=60))
    assert hi.failed == "hi_fail" and hi.failed_rate == Fraction(1, 3)
    assert HT.state_at([], DAY).heat == "unknown"


# ---- box5 ---------------------------------------------------------------------------------------------------------------------------
def test_minute_labels_and_forward_fill():
    assert BX.minute_label(t(9, 31, 2)) == "0931" and BX.minute_label(t(9, 30, 3)) == "0930" and BX.minute_label(t(9, 29, 59)) is None
    assert BX.minute_label(t(11, 30, 30)) is None and BX.minute_label(t(13, 0, 2)) == "1300" and BX.minute_label(t(15, 0, 4)) == "1500"
    assert BX.label_index("0931") == 0 and BX.label_index("1130") == 119 and BX.label_index("1301") == 120 and BX.label_index("1500") == 239 and BX.label_index("0930") == -1
    bars = BX.day_bars([(t(9, 30, 40), D("10"), D("1")), (t(9, 31, 2), D("10.2"), D("2")), (t(9, 34, 2), D("10.1"), D("3")), (t(9, 35, 2), None, D("3"))])
    assert [b.idx for b in bars][:4] == [0, 1, 2, 3] and len(bars) == 240
    b0, b1, b3 = bars[0], bars[1], bars[3]
    assert (b0.open, b0.high, b0.low, b0.close, b0.n_snaps) == (D("10"), D("10.2"), D("10"), D("10.2"), 2)
    assert (b1.open, b1.close, b1.n_snaps) == (D("10.2"), D("10.2"), 0) and b3.close == D("10.1")        # a minute without a snapshot is flat at the previous close


def test_box_down_break_sells_at_the_next_open():
    flat = [BX.Bar(i, D("10"), D("10.2"), D("9.8"), D("10"), 1) for i in range(5)]                      # box idx 0..4: high 10.2, low 9.8
    watch = [BX.Bar(5, D("10"), D("10.1"), D("9.9"), D("10"), 1), BX.Bar(6, D("10"), D("10"), D("9.6"), D("9.7"), 1), BX.Bar(7, D("9.69"), D("9.7"), D("9.6"), D("9.65"), 1)]
    sig, idx, px = BX.first_signal(flat + watch, -1)
    assert (sig.idx, sig.side, sig.box_low, sig.redraw_count) == (6, -1, D("9.8"), 0) and idx == 7 and px == D("9.69")      # close 9.7 < 9.8: sell at bar 7's open
    assert BX.first_signal(flat + watch[:1], -1) is None
    last = BX.first_signal(flat + watch[:1] + [BX.Bar(6, D("10"), D("10"), D("9.6"), D("9.7"), 1)], -1)
    assert last[1] == 6 and last[2] == D("9.7")                                                           # a signal on the last bar fills at its close
    five_quiet = flat + [BX.Bar(5 + i, D("10"), D("10.05"), D("9.95"), D("10"), 1) for i in range(5)] + [BX.Bar(10, D("10"), D("10"), D("9.96"), D("9.99"), 1)]
    assert BX.first_signal(five_quiet, -1) is None                                                       # five bars without a break redraw the box (9.95-10.05): 9.99 is inside
    assert BX.first_signal(flat + watch, +1) is None                                                      # up-breakouts are not a sell rule


# ---- one replay day -----------------------------------------------------------------------------------------------------------------
def replay_inputs(heat="hot", events=None):
    code, other = "600001", "600002"
    book_rows = [snap(code, (9, 30), known=t(9, 30, 7), lots="5000", price="11.00"), snap(code, (9, 40), lots="5000"), snap(code, (9, 50), lots="1400"),
                 snap(code, (10, 30), price="10.80", bid="10.79", ask="10.81", lots="300", vwap="10.9"), snap(code, (15, 0), price="10.70", bid="10.69", ask="10.71", vwap="10.9", kind="close")]
    b3_rows = [snap(other, (9, 40)), snap(other, (9, 45), price="10.90", bid="10.89", ask="10.91"), snap(other, (9, 46)),
               snap(other, (9, 50), price="10.90", bid="10.89", ask="10.91", vol="900"), snap(other, (15, 0), price="10.80", bid="10.79", ask="10.81", kind="close")]
    ms = HT.MarketState(date(2026, 11, 11), 50, Fraction(1, 4), heat, "lo_fail")
    return PP.PaperInputs(day=DAY, names={code: "甲", other: "乙"}, book=[(code, D("10.00"))], session={code: book_rows, other: b3_rows}, finals={},
                          close_px={code: D("10.70"), other: D("10.80")}, prev_amount={}, market=ms, sector=PP.SectorContext(), sw2={}, events=events, minute_bars={},
                          t_open=t(9, 30), t_close=t(15, 0), t_final=t(9, 25, 15), instrument_id=lambda c: f"id-{c}", next_session=date(2026, 11, 13), book_day=date(2026, 11, 11))


def test_replay_day_hand_computed_sells():
    res = PP.replay_day(replay_inputs())
    rows = {r["rule"]: r for r in res["sells"]}
    assert rows["S0_open"]["price"] == D("11.00") and rows["S0_open"]["return_pct"] == D("10") and rows["S0_open"]["time"] == t(9, 30, 7)
    # S1: peak 5000 lots, 1400 < 0.3 x 5000 at 09:50 (+7 s known lag in the fixture: known = poll time) -> sells at the limit price
    assert rows["S1_decay"]["price"] == D("11.00") and rows["S1_decay"]["time"] == t(9, 50)
    assert rows["Sbreak_open"]["price"] == D("10.79") and rows["Sbreak_open"]["return_pct"] == D("7.9") and rows["HOLD_close"]["price"] == D("10.70")
    # the market state is hot: policy v2 = open (price 11.00); v3 shadow: sealed at the open, S1 at 09:50 is the first intraday exit
    assert rows["POLICY_v2"]["price"] == D("11.00") and rows["POLICY_v2"]["why"].startswith("open: previous-session limit-up, market hot")
    assert rows["POLICY_v3_shadow"]["time"] == t(9, 50)
    assert rows["Sbreak_tape"]["why"].startswith("break, M3") and rows["Sbreak_strict"]["time"] == t(10, 30)
    assert rows["HOLD_close"]["vs_close_pct"] == D("0") and rows["S0_open"]["vs_close_pct"] == (D("11.00") / D("10.70") - 1) * 100
    assert all(r["t1_status"] == "SELLABLE_TODAY" and r["qty"] == 1000 for r in res["sells"])         # 10000 / 10 = 1000 shares, bought at the previous close
    cold = {r["rule"]: r for r in PP.replay_day(replay_inputs("cold"))["sells"]}
    assert cold["POLICY_v2"]["time"] == t(9, 50) and cold["POLICY_v2"]["why"] == rows["S1_decay"]["why"]  # cold state: the first S1 / M3 exit sells instead of the open


def test_replay_day_shadow_buy_is_never_booked_and_t1_holds():
    res = PP.replay_day(replay_inputs())
    b3 = [b for b in res["buys"] if b["rule"] == "B3"]
    assert {b["scenario"] for b in b3} == {"OPTIMISTIC", "BASE", "PESSIMISTIC"} and all(b["booked"] is False for b in b3)
    # re-seal at 09:46 (broke at 09:45 for 60 s) -> the board breaks again at 09:50: the queue was eaten, every scenario fills 9 lots (10000 / 11.00 / 100) at the limit
    assert all(b["status"] == "FILLED" and b["filled_quantity"] == 900 and b["avg_fill_price"] == D("11.00") for b in b3)
    assert b3[0]["d0_return_pct"] == (D("10.80") / D("11.00") - 1) * 100 and b3[0]["code"] == "600002"
    # today's fill is acquired today: eligible to sell on the next trading day, so NOT sellable today
    assert [x["t1_status"] for x in res["t1"]] == ["NOT_SELLABLE_TODAY"] and res["t1"][0]["eligible_on"] == date(2026, 11, 13)
    assert res["summary"]["t1"] == {"booked_buys": 1, "not_sellable_today": 1} and res["summary"]["booked"] == {"B1": False, "B3": False}


def test_replay_day_suspended_holding_and_summary_tables():
    inp = replay_inputs()
    inp = PP.PaperInputs(**{**inp.__dict__, "book": [("600001", D("10.00")), ("600009", D("20.00"))]})
    res = PP.replay_day(inp)
    susp = [r for r in res["sells"] if r["code"] == "600009"]
    assert susp == [{"code": "600009", "name": None, "rule": "ALL", "status": "SUSPENDED", "qty": 500, "cost": D("20.00")}]
    s = res["summary"]
    assert s["book_size"] == 2 and s["market"]["heat"] == "hot" and s["market"]["failed_rate"] == D("0.25")
    tab = {r["rule"]: r for r in s["sells"]}
    assert tab["HOLD_close"]["triggered"] == 1 and tab["HOLD_close"]["better_than_close_share"] == D("0") and tab["S0_open"]["better_than_close_share"] == D("1")
    assert [b["scenario"] for b in s["buys"]] == ["OPTIMISTIC", "BASE", "PESSIMISTIC"]


def test_previous_limit_ups_filters_st_main_board_and_unknown():
    rows = {"600001": (D("11.00"), D("10.00")), "600002": (D("10.99"), D("10.00")), "300001": (D("12"), D("10")), "600003": (D("5.25"), D("5.00")), "600004": (None, D("10")),
            "600005": (D("11"), None), "600006": (D("11.06"), D("10.05"))}
    names = {"600003": "ST某某", "600001": "甲"}
    assert PP.previous_limit_ups(rows, names) == [("600001", D("11.00")), ("600006", D("11.06"))]       # ST (5 percent) and non-main-board names are out; half-up limit 11.06
    assert PP.limit_price(D("10.00")) == D("11.00")


@pytest.mark.parametrize("n", [0])
def test_nothing_to_do_without_a_book(n):
    inp = replay_inputs()
    res = PP.replay_day(PP.PaperInputs(**{**inp.__dict__, "book": [], "session": {}}))
    assert res["sells"] == [] and res["buys"] == [] and res["summary"]["book_size"] == 0
