"""Hand-computed cases: auction features, selections, active buy ratio and the field-check counters."""
from __future__ import annotations

import pytest

from pulse.domain.batch6 import auction as A


def feat(**kw):
    base = dict(prev_close=10.0, price_919=None, price_925=None, matched_volume_925=None, amount_925=None, avg_amount_20=None, bid_lots_5_925=None,
                matched_915=None, unmatched_ok=True)
    base.update(kw)
    return A.features(**base)


def test_gap_fade_and_amount_ratio():
    f = feat(price_919=10.6, price_925=10.3, matched_volume_925=500.0, amount_925=5_150_000.0, avg_amount_20=50_000_000.0, bid_lots_5_925=120.0, matched_915=7.0)
    assert f.gap_919 == pytest.approx(0.06)
    assert f.gap_925 == pytest.approx(0.03)
    assert f.fade == pytest.approx(0.03)
    assert f.amt_925_ratio == pytest.approx(0.103)
    assert f.unmatched_buy == 120.0 and f.matched_915 == 7.0


def test_no_match_means_no_gap_925_ratio_or_fade():
    f = feat(price_919=10.6, price_925=10.0, matched_volume_925=0.0, amount_925=0.0, avg_amount_20=1e7)
    assert f.gap_925 is None and f.fade is None and f.amt_925_ratio is None and f.gap_919 == pytest.approx(0.06)
    assert feat(price_919=None).gap_919 is None
    assert feat(price_925=10.3, matched_volume_925=10.0, amount_925=1e5, avg_amount_20=0.0).amt_925_ratio is None
    assert feat(price_925=10.3, matched_volume_925=10.0, amount_925=1e5, avg_amount_20=1e7, bid_lots_5_925=5.0, unmatched_ok=False).unmatched_buy is None


def F(g925=None, ratio=None, g919=None, fade=None):
    return A.AuctionFeatures(g919, g925, fade, ratio, None, None)


def test_h5_top_fifth_of_amount_ratio_and_gap_band():
    feats = {f"s{i}": F(g925=0.03, ratio=float(i)) for i in range(10)}          # ratios 0..9: top 20 % = s9, s8
    feats["s8"] = F(g925=0.08, ratio=8.0)                                        # outside +2..+7 %
    feats["s7"] = F(g925=0.03, ratio=None)
    assert A.h5_signals(feats) == ["s9"]
    assert A.h5_signals({"a": F(g925=0.02, ratio=1.0)}) == ["a"]               # band edges inclusive; the single name is the top 20 %
    assert A.h5_signals({"a": F(g925=0.0701, ratio=1.0)}) == []


def test_h6_needs_gap_919_5pct_and_fade_3_points():
    feats = {"a": F(g919=0.05, g925=0.02, fade=0.03), "b": F(g919=0.049, g925=0.0, fade=0.049), "c": F(g919=0.08, g925=0.06, fade=0.02), "d": F(g919=0.06)}
    assert A.h6_signals(feats) == ["a"]


def test_abr_and_h7():
    assert A.abr(100, 50, 160, 90) == pytest.approx(60 / 100)
    assert A.abr(100, 50, 100, 50) is None                                       # no trades
    assert A.abr(100, 50, 90, 60) is None                                        # counters went backwards
    assert A.abr(None, 1, 2, 3) is None
    abr = {f"s{i}": i / 10 for i in range(10)}                                   # top 10 % = s9
    chg = {f"s{i}": 0.01 for i in range(10)}
    ask = {f"s{i}": 5.0 for i in range(10)}
    assert A.h7_signals(chg, abr, ask) == ["s9"]
    assert A.h7_signals({**chg, "s9": 0.051}, abr, ask) == []                    # over +5 %
    assert A.h7_signals({**chg, "s9": -0.001}, abr, ask) == []                   # below 0
    assert A.h7_signals(chg, abr, {**ask, "s9": 0.0}) == []                      # no ask1 volume: cannot buy


def test_field_check_counts_and_conclusion():
    c = A.FieldCounts()
    for _ in range(100):
        A.count_auction_row(c, price=10.0, bid1_px=10.0, ask1_px=10.0, bid1_lots=50.0, ask1_lots=50.0, bid2_lots=3.0, ask2_lots=0.0)
    A.count_auction_row(c, price=0.0, bid1_px=0.0, ask1_px=0.0, bid1_lots=0.0, ask1_lots=0.0, bid2_lots=0.0, ask2_lots=0.0)     # no price: ignored
    for _ in range(100):
        A.count_final_row(c, price=10.0, volume_lots=60.0, amount=60.0 * 100 * 10.0, bid1_px=9.99, ask1_px=10.01, last_auction_price=10.0, last_auction_matched=50.0)
    r = A.conclude(c)
    assert c.auction_rows == 100 and r["share_bid1_lots_eq_ask1_lots"] == 1.0 and r["share_bid2_and_ask2_both_positive"] == 0.0
    assert r["median_final_over_last_indicative_matched_volume_same_price"] == pytest.approx(1.2)
    assert r["unmatched_buy_computable"] and r["bid1_ask1_lots_0915_0924"].startswith("indicative matched")
    bad = A.FieldCounts()
    for _ in range(10):
        A.count_final_row(bad, price=10.0, volume_lots=60.0, amount=1.0, bid1_px=10.01, ask1_px=10.0, last_auction_price=None, last_auction_matched=None)
    rb = A.conclude(bad)
    assert not rb["unmatched_buy_computable"] and rb["unmatched_buy_definition"] == "not computable" and rb["bid1_ask1_lots_0915_0924"] == "undetermined"


def test_rows_without_an_indicative_book_are_not_evidence():
    c = A.FieldCounts()
    A.count_auction_row(c, price=10.0, bid1_px=None, ask1_px=None, bid1_lots=0.0, ask1_lots=0.0, bid2_lots=0.0, ask2_lots=0.0)
    A.count_auction_row(c, price=10.0, bid1_px=10.0, ask1_px=10.0, bid1_lots=7.0, ask1_lots=7.0, bid2_lots=0.0, ask2_lots=1.0)
    assert (c.auction_rows, c.no_indication) == (1, 1)
    r = A.conclude(c)
    assert r["share_bid1_ask1_px_eq_price"] == 1.0 and r["rows_without_indicative_book"] == 1 and r["rule_version"] == A.FIELD_RULE_VERSION


def test_final_volume_can_only_grow_after_the_last_indicative_poll():
    c = A.FieldCounts()
    A.count_final_row(c, price=10.0, volume_lots=60.0, amount=60 * 1000.0, bid1_px=9.99, ask1_px=10.01, last_auction_price=10.0, last_auction_matched=50.0)
    A.count_final_row(c, price=10.0, volume_lots=40.0, amount=40 * 1000.0, bid1_px=9.99, ask1_px=10.01, last_auction_price=10.0, last_auction_matched=50.0)
    A.count_final_row(c, price=10.0, volume_lots=40.0, amount=40 * 1000.0, bid1_px=9.99, ask1_px=10.01, last_auction_price=9.9, last_auction_matched=50.0)       # price moved: not compared
    assert (c.same_price_rows, c.same_price_ge) == (2, 1)
    assert A.conclude(c)["share_final_volume_at_least_last_indicative_same_price"] == 0.5
