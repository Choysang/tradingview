"""Hand-computed cases for the batch 6 fill rules and the E093 roles / events."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from pulse.domain.batch6 import roles as RL
from pulse.domain.batch6 import rules as R


def test_spec_hash_matches_the_frozen_document():
    import hashlib
    path = Path(__file__).resolve().parents[3] / R.SPEC_PATH
    if not path.exists():
        pytest.skip("pre-registration not present in this checkout")
    raw = path.read_bytes().replace(b"\r\n", b"\n")
    marker = "（空，结果出来后填写。）\n".encode("utf-8")        # sections 1-6 and this placeholder are the frozen part; the section 7 results are appended after it
    head = raw[: raw.index(marker) + len(marker)]
    assert hashlib.sha256(head).hexdigest() == R.SPEC_SHA256


def test_limit_prices_half_up_in_integer_cents():
    assert R.limit_cents(1000) == (1100, 900)          # 10.00 -> 11.00 / 9.00
    assert R.limit_cents(505) == (556, 455)            # 5.05 x 1.1 = 5.555 -> 5.56 (half up); x 0.9 = 4.545 -> 4.55
    assert R.limit_cents(333) == (366, 300)            # 3.663 -> 3.66; 2.997 -> 3.00
    assert R.limit_cents(1250, 500) == (1313, 1188)    # ratio 5 %: 13.125 -> 13.13 ; 11.875 -> 11.88


def test_buy_not_filled_at_or_above_limit_up_open():
    assert R.buy_filled(1099, 1100)
    assert not R.buy_filled(1100, 1100)                # sealed open: not filled
    assert not R.buy_filled(None, 1100)
    assert not R.buy_filled(0, 1100)


def test_sell_blocked_at_limit_down_and_deferred_to_next_point():
    assert R.sell_possible(901, 900)
    assert not R.sell_possible(900, 900)
    # planned close is at limit-down, next open too, next close is free: the third point sells
    assert R.exit_with_deferral([(900, 900), (810, 810), (820, 810)]) == (820, 2)
    assert R.exit_with_deferral([(900, 900), (None, None)]) is None


def test_segments_and_scope():
    assert R.segment_of(date(2026, 6, 30)) == "PRE" and R.segment_of(date(2026, 7, 1)) == "DEV"
    assert R.main_board("600000") and R.main_board("002001") and not R.main_board("300001") and not R.main_board("688001")
    assert R.is_st("*ST abc", False) and R.is_st("ST x", False) and R.is_st("abc", True) and not R.is_st("Stone", False)


def test_listed_ok_from_counts_the_listing_day():
    sess = [date(2026, 1, d) for d in (5, 6, 7, 8, 9)] + [date(2026, 1, d) for d in (12, 13, 14, 15, 16)]
    assert R.listed_ok_from(date(2026, 1, 6), sess, need=3) == date(2026, 1, 8)    # 6, 7, 8 are three sessions
    assert R.listed_ok_from(date(2026, 1, 6), sess, need=20) is None
    assert R.listed_ok_from(None, sess) is None
    assert R.listed_ok_from(date(2020, 1, 1), sess) == date.min


M = RL.Member


def test_leader_needs_streak_three_and_breaks_ties_by_turnover():
    ms = [M("a", 2, True, 9.0, 1.0), M("b", 3, True, 5.0, 1.0), M("c", 3, True, 7.0, 1.0)]
    assert RL.leader_of(ms).code == "c"
    assert RL.leader_of([M("a", 2, True, 9.0, 1.0)]) is None


def test_core_uses_cap_median_and_excludes_leader():
    ms = [M("L", 4, True, 100.0, 50.0), M("x", 0, False, 30.0, 10.0), M("y", 0, False, 20.0, 30.0), M("z", 1, True, 10.0, 40.0), M("w", 0, False, 90.0, 5.0)]
    # caps 50,10,30,40,5 -> median 30 ; eligible (>= 30, not leader): y (30), z (40); largest turnover: y
    assert RL.cap_median(ms) == 30.0
    assert RL.core_of(ms, frozenset({"L"})).code == "y"
    assert RL.core_of([M("a", 0, False, 1.0, None)], frozenset()) is None
    assert RL.cap_median([M("a", 0, False, 1.0, 10.0), M("b", 0, False, 1.0, 20.0)]) == 15.0     # even count: mean of the middle two


def test_followers_are_first_board_only():
    ms = [M("L", 4, True, 1.0, 1.0), M("a", 1, True, 1.0, 1.0), M("b", 2, True, 1.0, 1.0), M("c", 0, False, 1.0, 1.0), M("d", 1, True, 1.0, 1.0)]
    assert [m.code for m in RL.followers_of(ms, frozenset({"L"}))] == ["a", "d"]


def test_events_break_and_promotion_and_targets():
    prev = M("L", 3, True, 100.0, 50.0)
    today_break = [M("L", 0, False, 80.0, 50.0), M("f1", 1, True, 10.0, 10.0), M("f2", 1, True, 12.0, 20.0), M("big", 0, False, 70.0, 60.0)]
    ev = RL.event_of(prev, today_break)
    assert ev.kind == "break" and ev.leader == "L"
    # caps 50,10,20,60 -> median 35 ; eligible: L (excluded), big (60) -> core big ; followers f1, f2
    assert RL.targets(ev, today_break) == ["big", "f1", "f2"]
    today_promo = [M("L", 4, True, 120.0, 50.0), M("big", 0, False, 70.0, 60.0), M("f1", 1, True, 10.0, 10.0)]
    ev2 = RL.event_of(prev, today_promo)
    assert ev2.kind == "promotion"
    assert RL.targets(ev2, today_promo) == ["big"]                      # promotion buys the core only
    assert RL.event_of(prev, [M("other", 1, True, 1.0, 1.0)]) is None   # leader suspended: no event
    assert RL.event_of(M("L", 2, True, 1.0, 1.0), today_break) is None  # previous leader needs streak >= 3
    assert RL.event_of(None, today_break) is None


def test_targets_exclude_a_different_leader_of_the_day():
    prev = M("L", 3, True, 100.0, 50.0)
    today = [M("L", 4, True, 100.0, 40.0), M("H", 5, True, 200.0, 60.0), M("m", 0, False, 50.0, 70.0)]
    ev = RL.event_of(prev, today)
    # D's leader is H (streak 5): excluded together with L; caps 40,60,70 -> median 60 ; eligible m (70), H (excluded) -> core m
    assert RL.targets(ev, today) == ["m"]
