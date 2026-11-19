"""Pure part of the native intraday recorder: vendor parsers on recorded bodies, unit normalisation, frame parity with the legacy import,
call-auction rows, limit queues, hot set, schedule. No network, no database."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import polars as pl
import pytest

from pulse.adapters.sources import legacy_recordings as LR
from pulse.adapters.sources import sina, tencent
from pulse.application import bf_live_service as B
from pulse.application import intraday_rows as R

from . import support as T

POLL = datetime(2026, 11, 12, 9, 58, 0)
WHEN = datetime(2026, 11, 12, 9, 58, 1)


# --- vendor parsers on bodies recorded from the live endpoints ---------------------------------------------------------------------------

def test_sina_live_capture_has_five_levels_and_shares():
    records, empty = sina.parse_hq_book((T.FIXTURES / "sina_hq_live_200.txt").read_bytes())
    assert len(records) == 77 and len(empty) == 123           # 200 symbols asked, answered in full (unknown codes are empty lines)
    r = next(x for x in records if x["symbol"] == "sz000001")
    assert r["name"] == "平安银行" and r["bid1_volume"] == "178900" and r["bid1_price"] == "12.200" and r["ask5_price"] == "12.250"
    row = R.sina_row(r, "full", POLL, T.DAY)
    assert row["bid1_lots"] == 1789.0 and row["ask5_lots"] == 6765.0 and row["volume_lots"] == 115469.5      # shares / 100
    assert row["quote_ts"] == datetime(2026, 11, 19, 9, 35, 18) and row["amount_yuan"] == 141097132.2
    assert row["outer_lots"] is None and row["turnover"] is None and row["float_cap"] is None and row["vwap"] is None   # not carried by Sina


def test_sina_after_hours_trailer_is_lots_and_wan_yuan():
    records, _ = sina.parse_hq_book((T.FIXTURES.parent / "data" / "recorded" / "sina_hq.txt").read_bytes())
    row = R.sina_row(records[0], "after", POLL, T.DAY)
    assert row["after_lots"] == 80.0 and row["after_amount_wan"] == 9.368        # 8000 shares x 11.71 yuan = 93680 yuan
    assert row["bid1_px"] == 11.71 and row["bid2_px"] == 0.0 and row["bid2_lots"] == 0.0


def test_tencent_live_capture_has_every_column():
    records, empty = tencent.parse_qt_book((T.FIXTURES / "tencent_qt_live_80.txt").read_bytes())
    assert len(records) == 60 and empty == []                  # the 20 unknown codes answer v_pv_none_match and are skipped
    row = R.tencent_row(next(x for x in records if x["symbol"] == "sz000001"), "full", POLL)
    assert (row["outer_lots"], row["inner_lots"], row["turnover"], row["float_cap"]) == (78761.0, 36760.0, 0.06, 2367.49)
    assert (row["limit_up"], row["limit_down"], row["volume_ratio"], row["vwap"]) == (13.42, 10.98, 2.71, 12.22)
    assert (row["bid1_px"], row["bid1_lots"], row["ask5_px"], row["ask5_lots"]) == (12.2, 1712.0, 12.25, 6950.0)
    assert row["after_lots"] == 0.0 and row["amount_yuan"] == 141197177.0 and row["quote_ts"] == datetime(2026, 11, 19, 9, 35, 21)
    assert all(row[c] is not None for c in LR.QUOTE_REQUIRED)  # nothing missing on a full Tencent line


def test_tencent_recorded_after_hours_fields():
    records, _ = tencent.parse_qt_book((T.FIXTURES.parent / "data" / "recorded" / "tencent_qt.txt").read_bytes())
    row = R.tencent_row(records[0], "after", POLL)
    assert row["after_lots"] == 86.0 and row["after_amount_wan"] == 10.0706       # 86 lots x 100 x 11.71 = 100706 yuan


def test_both_vendors_normalise_to_the_same_row():
    """Units: Sina shares / Tencent lots, yuan on both; the shared columns of one logical state must agree."""
    st = T.plain()
    s = R.sina_row(sina.parse_hq_book(T.sina_body([st], WHEN))[0][0], "full", POLL, T.DAY)
    t = R.tencent_row(tencent.parse_qt_book(T.tencent_body([st], WHEN))[0][0], "full", POLL)
    carried_by_sina = [c for c in LR.QUOTE_REQUIRED if s[c] is not None]
    assert {"outer_lots", "turnover", "float_cap", "vwap"}.isdisjoint(carried_by_sina)
    for c in carried_by_sina:
        assert (s[c] == pytest.approx(t[c])) if isinstance(s[c], float) else s[c] == t[c], c
    assert s["limit_up"] == 11.0 and s["limit_down"] == 9.0      # rule-based (prev 10.00, main board) equals the vendor's here


def test_zero_price_is_absent_and_empty_levels_become_null_in_the_frame():
    st = T.sealed()
    row = R.sina_row(sina.parse_hq_book(T.sina_body([st], WHEN))[0][0], "full", POLL, T.DAY)
    assert row["ask1_px"] == 0.0 and row["ask1_lots"] == 0.0 and row["bid1_lots"] == 5000.0
    frame = R.rows_to_frame([row], stream="quotes", part="quotes/x", day=T.DAY).row(0, named=True)
    assert frame["ask1_px"] is None and frame["ask1_lots"] == 0.0 and frame["bid1_px"] == Decimal("11.0000") == frame["limit_up"]


def test_risk_warning_name_has_no_rule_based_limit():
    """Sina carries no limit prices: a Shenzhen ST name's band is not in the verified rules and stays NULL rather than guessed
    (a Shanghai ST name has a verified rule, 10% since 2026-07-06)."""
    shenzhen = R.sina_row(sina.parse_hq_book(T.sina_body([T.plain("000001", "*ST测试")], WHEN))[0][0], "full", POLL, T.DAY)
    assert shenzhen["limit_up"] is None and shenzhen["limit_down"] is None
    shanghai = R.sina_row(sina.parse_hq_book(T.sina_body([T.plain("600001", "*ST测试")], WHEN))[0][0], "full", POLL, T.DAY)
    assert shanghai["limit_up"] == 11.0


# --- frame parity with the legacy import -------------------------------------------------------------------------------------------------

def test_frame_equals_the_legacy_import_of_the_same_rows(tmp_path):
    records, _ = tencent.parse_qt_book((T.FIXTURES / "tencent_qt_live_80.txt").read_bytes())
    sina_records, _ = sina.parse_hq_book((T.FIXTURES / "sina_hq_live_200.txt").read_bytes())
    rows = [R.tencent_row(r, "full", POLL) for r in records] + [R.sina_row(r, "hot", POLL, T.DAY) for r in sina_records]
    native = R.rows_to_frame(rows, stream="quotes", part="quotes/part-0001.parquet", day=T.DAY)
    d = tmp_path / "intraday" / T.DAY.isoformat() / "quotes"
    d.mkdir(parents=True)
    R.legacy_frame(rows).write_parquet(d / "part-0001.parquet")                 # what the old recorder writes
    rec = LR.LegacyRecordings(tmp_path)
    imported = rec.read_quotes(rec.parts("quotes", T.DAY)[0], T.DAY)
    assert native.schema == imported.schema == pl.Schema(B.LR_SNAPSHOT_SCHEMA)
    assert native.columns == imported.columns
    assert native.equals(imported)
    assert native.height == 60 + 77


def test_legacy_frame_has_exactly_the_old_recorders_columns():
    assert list(R.RAW_SCHEMA) == list(LR.QUOTE_REQUIRED)
    assert R.RAW_SCHEMA["poll_ts"] == pl.Datetime("ms") and R.RAW_SCHEMA["code"] == pl.String and R.RAW_SCHEMA["bid3_lots"] == pl.Float64


# --- call auction ------------------------------------------------------------------------------------------------------------------------

def test_auction_rows_are_kept_as_received():
    """09:15-09:24: bid1 == ask1 is the indicative price, its size the matched quantity, bid2 / ask2 size the unmatched surplus."""
    st = T.Stock("600000", "浦发银行", 10.0, 10.3, bids=[(10.3, 800), (10.2, 1200), (0, 0), (0, 0), (0, 0)],
                 asks=[(10.3, 800), (10.4, 0), (0, 0), (0, 0), (0, 0)], limit_up=11.0, limit_down=9.0)
    when = datetime(2026, 11, 12, 9, 20, 3)
    for row in (R.sina_row(sina.parse_hq_book(T.sina_body([st], when))[0][0], "auction", datetime(2026, 11, 12, 9, 20, 2), T.DAY),
                R.tencent_row(tencent.parse_qt_book(T.tencent_body([st], when))[0][0], "auction", datetime(2026, 11, 12, 9, 20, 2))):
        assert row["kind"] == "auction" and row["bid1_px"] == row["ask1_px"] == 10.3
        assert row["bid1_lots"] == row["ask1_lots"] == 800.0 and row["bid2_lots"] == 1200.0 and row["quote_ts"] == when
    frame = R.rows_to_frame([row], stream="quotes", part="p", day=T.DAY).row(0, named=True)
    assert frame["kind"] == "auction" and frame["quote_at"] == datetime(2026, 11, 12, 1, 20, 3, tzinfo=T.UTC)


def test_auction_schedule():
    c = R.Clock()
    at = lambda h, m, s=0: datetime(2026, 11, 12, h, m, s)            # noqa: E731
    assert R.due(at(9, 14, 59), c) == [] and R.due(at(9, 15, 1), c) == []        # before the minute offset
    assert R.due(at(9, 15, 2), c) == ["full"] and R.due(at(9, 18, 2), c) == ["full"]
    assert set(R.due(at(9, 24, 2), c)) == {"full", "auction_hot"}
    assert set(R.due(at(9, 19, 31), c)) == {"full", "auction_hot"}                 # the dense static-pool grid starts at 09:19:30
    c.last_full, c.last_auction = R.slot(at(9, 19, 31), 60), R.slot(at(9, 19, 31), 5)
    assert R.due(at(9, 19, 33), c) == [] and R.due(at(9, 19, 35), c) == ["auction_hot"]
    assert R.due(at(9, 25, 10), c) == ["auction_hot", "auction_final"]             # the last grid slot and the 09:25 result
    assert R.due(at(9, 25, 12), c) == ["auction_final"] and R.due(at(9, 27, 1), c) == []
    assert R.due(at(9, 28), c) == []                                               # nothing between the auction and the open


def test_session_break_and_after_hours_schedule():
    c = R.Clock()
    at = lambda h, m, s=0: datetime(2026, 11, 12, h, m, s)            # noqa: E731
    assert set(R.due(at(9, 30, 2), c)) == {"full", "hot"} and set(R.due(at(10, 0, 5), c)) == {"full", "hot"}
    assert R.due(at(11, 31, 5), c) == [] and R.due(at(12, 0, 5), c) == [] and R.due(at(12, 59, 59), c) == []     # the 11:30-13:00 break
    assert set(R.due(at(13, 0, 2), c)) == {"full", "hot"}
    assert R.due(at(15, 1, 30), c) == ["close"] and R.due(at(15, 3, 50), c) == ["close"] and R.due(at(15, 4, 1), c) == []
    assert R.due(at(15, 1, 40), R.Clock(done={"close"})) == []
    assert set(R.due(at(15, 5, 6), R.Clock())) == {"after_hot", "after_open"}
    c.done = {"close", "after_open"}
    c.last_after = R.slot(at(15, 5, 6), 60)
    assert R.due(at(15, 5, 30), c) == [] and set(R.due(at(15, 15, 5), c)) == {"after_hot", "after_mid"}
    assert R.due(at(15, 30, 6), R.Clock(done={"close", "after_open", "after_mid"}, last_after=R.slot(at(15, 30, 6), 60))) == ["after_end"]
    assert R.due(at(15, 34), R.Clock()) == []
    assert R.missed(at(10, 0, 0), at(10, 0, 30), 10) == 2 and R.missed(at(11, 30, 0), at(13, 0, 10), 10) == 0     # across the break: none


# --- limit queues and the hot set ------------------------------------------------------------------------------------------------------------

def _row(price, **kw):
    base = {"code": kw.pop("code", "600000"), "price": price, "limit_up": 11.0, "limit_down": 9.0, "high": price, "low": price,
            "bid1_lots": 7000.0, "ask1_lots": 300.0}
    return {**base, **kw}


def test_limit_queue_up_down_and_none():
    assert R.limit_queue(_row(11.0)) == ("up", 7000.0)             # sealed at the limit-up: the bid1 size is the queue
    assert R.limit_queue(_row(9.0, bid1_lots=10.0)) == ("down", 300.0)   # at the limit-down: the ask1 size is the queue
    assert R.limit_queue(_row(10.5)) is None and R.limit_queue(_row(None)) is None
    assert R.limit_queue(_row(11.0, bid1_lots=None)) == ("up", None)     # the vendor gave no size: unknown, not 0
    assert R.limit_queue(_row(11.0, limit_up=None)) is None            # no limit price (unverified band): no claim


def test_native_queue_equals_what_seal_facts_read():
    row = R.sina_row(sina.parse_hq_book(T.sina_body([T.sealed()], WHEN))[0][0], "full", POLL, T.DAY)
    assert R.limit_queue(row) == ("up", 5000.0) and row["price"] == row["limit_up"] == row["bid1_px"]


def test_hot_set_grows_sticky_and_capped():
    near = _row(10.9, code="a")                                    # within 2% of the limit-up
    touched = _row(10.0, code="b", high=11.0)                      # touched the limit earlier
    far = _row(10.0, code="c")
    down = _row(9.1, code="d")
    hot, capped = R.grow_hot({"s"}, [near, touched, far, down])
    assert hot == {"s", "a", "b", "d"} and not capped
    hot, _ = R.grow_hot(hot, [far])                                # names stay in: a hot name that cools is still polled
    assert hot == {"s", "a", "b", "d"}
    hot, capped = R.grow_hot({"s"}, [near, touched, down], cap=3)     # room for two: the nearest limits win
    assert capped and hot == {"s", "a", "d"}                         # a and d are nearest a limit; b only touched it earlier


def test_static_codes_from_a_session_frame():
    rows = [_row(11.0, code="up", high=11.0), _row(9.0, code="dn", low=9.0), _row(10.0, code="no", high=10.5, low=9.5)]
    frame = R.rows_to_frame([{**r, "name": "x", "kind": "close", "poll_ts": POLL, "quote_ts": POLL} for r in rows], stream="quotes", part="p", day=T.DAY)
    assert R.static_codes(frame) == {"up", "dn"} and R.static_codes(frame.clear()) == set()


def test_holiday_quotes_are_not_stamped_today():
    rows = [{"quote_ts": datetime(2026, 11, 11, 15, 0)}] * 9 + [{"quote_ts": None}]
    assert not R.stamped_today(rows, T.DAY) and R.stamped_today([{"quote_ts": datetime(2026, 11, 12, 9, 30)}], T.DAY)
    assert not R.stamped_today([], T.DAY)
