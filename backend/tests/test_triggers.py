from datetime import datetime, timedelta

import polars as pl

from backend.app.domain import triggers as T
from backend.app.warehouse import intraday_view as V

T0 = datetime(2026, 11, 2, 10, 0, 0)


def row(sec, price, bid1_lots=5000.0, ask1_px=0.0, vwap=10.5, up=11.0):
    return {"known_time": T0 + timedelta(seconds=sec), "price": price, "limit_up": up, "bid1_px": price if price >= up else price - 0.01,
            "bid1_lots": bid1_lots, "ask1_px": ask1_px if price >= up else price + 0.01, "vwap": vwap}


def test_b1_gap_and_auction_amount_from_lots():
    final = {"prev_close": 10.0, "open": 10.3, "amount_yuan": None, "volume_lots": 1000.0, "price": 10.3}
    assert T.auction_amount(final) == 1000 * 100 * 10.3
    assert T.b1(final, prev_amount_yuan=30_000_000.0)  # gap 3%, auction 1.03 million >= 3% of 30 million
    assert not T.b1({**final, "open": 10.8}, 30_000_000.0)  # gap 8%: above the band
    assert not T.b1(final, 50_000_000.0) and not T.b1(final, None)


def test_b3_needs_a_long_enough_break_before_the_reseal():
    b = T.B3()
    assert not any(b.feed(r) for r in (row(0, 11.0), row(10, 10.9), row(20, 11.0)))  # 10 s break: too short
    b = T.B3()
    assert [b.feed(r) for r in (row(0, 11.0), row(10, 10.9), row(40, 10.95), row(50, 11.0), row(60, 10.9), row(100, 11.0))] == \
        [False, False, False, True, False, False]  # fires once


def test_s1_fires_when_the_bid_at_the_limit_decays():
    s = T.S1()
    assert [s.feed(r) for r in (row(0, 11.0, 1000), row(10, 11.0, 200))] == [False, False]  # peak below the minimum
    s = T.S1()
    assert [s.feed(r) for r in (row(0, 11.0, 10000), row(10, 11.0, 4000), row(20, 11.0, 2500))] == [False, False, True]


def test_s3_counts_time_under_vwap_in_a_row():
    s = T.S3()
    assert [s.feed(r) for r in (row(0, 10.4), row(60, 10.4), row(70, 10.6), row(80, 10.4), row(170, 10.4))] == \
        [False, False, False, False, True]


def test_view_reads_only_rows_known_by_the_clock(tmp_path):
    poll = datetime(2026, 11, 2, 10, 0, 0)
    base = {"name": "x", "quote_ts": poll, "price": 1.0}
    pl.DataFrame([{**base, "code": "600001", "kind": "full", "poll_ts": poll},
                  {**base, "code": "600001", "kind": "hot", "poll_ts": poll + timedelta(seconds=10), "price": 2.0}]).write_parquet(tmp_path / "quotes.parquet")
    f = V.load_day(tmp_path)
    assert V.asof(f, poll + timedelta(seconds=4)).height == 0  # a whole-board poll is known 5 s after it starts
    assert V.asof(f, poll + timedelta(seconds=5))["price"][0] == 1.0
    assert V.asof(f, poll + timedelta(seconds=12))["price"][0] == 2.0  # a hot poll 2 s after
