"""Exact limit flags from RAW prices (``refdata_core.limit_flags_raw``): tick rounding, ex-right reference price, ST rules (offline)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import polars as pl
import pytest

from pulse.adapters.sources import ref_market as M
from pulse.application import refdata_core as C

from .support import fixture

D = Decimal
UTC = timezone.utc


def bars(code: str, rows: list[tuple], *, st: bool | None = False, **extra) -> pl.DataFrame:
    """rows: (date, open, high, low, close) as strings."""
    return pl.DataFrame({"instrument_id": [f"i{code}"] * len(rows), "code": [code] * len(rows), "trade_date": [r[0] for r in rows],
                         "open": [D(r[1]) for r in rows], "high": [D(r[2]) for r in rows], "low": [D(r[3]) for r in rows],
                         "close": [D(r[4]) for r in rows], "is_st": [st] * len(rows), **extra})


def factors(code: str, rows: list[tuple[date, str]], revision: int = 1) -> pl.DataFrame:
    return pl.DataFrame({"instrument_id": [f"i{code}"] * len(rows), "effective_from": [r[0] for r in rows],
                         "factor": [D(r[1]) for r in rows], "available_at": [datetime(2026, 11, 19, tzinfo=UTC)] * len(rows),
                         "revision": [revision] * len(rows)}, schema=C.FACTOR_SCHEMA)


def one(frame: pl.DataFrame, day: date) -> dict:
    return frame.filter(pl.col("trade_date") == day).row(0, named=True)


# ---- tick rounding ----------------------------------------------------------------------------------------------------------------
def test_a_9_9837_percent_day_is_a_true_limit_up_when_the_exchange_rounds_the_band():
    # 000029-style: previous close 24.54 -> 10% is 26.994, the exchange rounds to the 0.01 tick: the limit is 26.99, which is +9.9837%
    prev, limit = D("24.54"), D("26.99")
    assert float(limit / prev - 1) == pytest.approx(0.099837, abs=1e-6) and limit / prev < D("1.10")        # a naive >= 10% test would call this "not limit-up"
    out = C.limit_flags_raw(bars("000029", [(date(2026, 6, 9), "24.40", "24.60", "24.30", "24.54"),
                                            (date(2026, 6, 10), "24.60", "26.99", "24.50", "26.99"),
                                            (date(2026, 6, 11), "26.99", "27.50", "26.50", "26.98")]))
    r = one(out, date(2026, 6, 10))
    assert (r["limit_up_price"], r["limit_down_price"], r["ref_method"], r["rule_status"], r["limit_ratio"]) == (D("26.99"), D("22.09"), "prev_close_raw_no_factor_file", "verified", D("0.10"))
    assert r["is_limit_up_close"] is True and r["touched_limit_up"] is True and r["open_at_limit_up"] is False and r["is_limit_down"] is False
    nxt = one(out, date(2026, 6, 11))                                                                  # 26.99 * 1.1 = 29.689 -> 29.69; 26.98 is no limit
    assert nxt["limit_up_price"] == D("29.69") and nxt["is_limit_up_close"] is False and nxt["limit_up_streak"] == 0


def test_the_band_is_rounded_half_up_not_half_even_and_a_near_miss_is_not_a_limit():
    out = C.limit_flags_raw(pl.concat([
        bars("000001", [(date(2026, 6, 9), "10.15", "10.15", "10.15", "10.15"), (date(2026, 6, 10), "11.16", "11.17", "11.0", "11.16")]),
        bars("000002", [(date(2026, 6, 9), "10.05", "10.05", "10.05", "10.05"), (date(2026, 6, 10), "9.05", "9.50", "9.04", "9.05")])]))
    a, b = one(out.filter(pl.col("code") == "000001"), date(2026, 6, 10)), one(out.filter(pl.col("code") == "000002"), date(2026, 6, 10))
    assert a["limit_up_price"] == D("11.17") and a["is_limit_up_close"] is False and a["touched_limit_up"] is True      # 11.165 -> 11.17 (half-even would say 11.16)
    assert b["limit_down_price"] == D("9.05") and b["is_limit_down"] is True                                            # 9.045 -> 9.05 (half-even 9.04)


def test_one_character_wide_board_open_at_limit_up_and_streak_counts():
    days = [date(2026, 6, d) for d in (1, 2, 3, 4, 5, 8)]
    out = C.limit_flags_raw(bars("600001", [(days[0], "10.00", "10.00", "10.00", "10.00"),
                                            (days[1], "11.00", "11.00", "11.00", "11.00"),           # one-character board: open == high == low == close == limit
                                            (days[2], "12.10", "12.10", "11.50", "12.10"),
                                            (days[3], "13.31", "13.31", "13.00", "13.31"),
                                            (days[4], "13.40", "13.60", "13.20", "13.50"),           # broken: the streak ends
                                            (days[5], "14.85", "14.85", "14.00", "14.85")]))         # 13.50 * 1.1 = 14.85 -> limit again, streak restarts at 1
    assert out["is_limit_up_close"].to_list() == [None, True, True, True, False, True]
    assert out["limit_up_streak"].to_list() == [None, 1, 2, 3, 0, 1]
    assert one(out, days[1])["open_at_limit_up"] is True and one(out, days[4])["open_at_limit_up"] is False
    assert one(out, days[0])["rule_status"] == "no_previous_close"                                       # the first row has no reference: NULL, not a guess


# ---- ex-right day ---------------------------------------------------------------------------------------------------------------------
def test_on_an_ex_right_day_the_reference_comes_from_the_factor_not_from_the_raw_previous_close():
    d0, d1 = date(2026, 9, 23), date(2026, 9, 24)
    rows = [(d0, "10.00", "10.10", "9.90", "10.00"), (d1, "9.60", "10.47", "9.55", "10.47")]
    fac = factors("000001", [(date(1990, 1, 1), "1.0500000000000000"), (d1, "1.0000000000000000")])
    out = C.limit_flags_raw(bars("000001", rows), fac)
    r = one(out, d1)
    # ref = 10.00 * 1.0 / 1.05 = 9.5238 -> 9.52; the limit is 9.52 * 1.1 = 10.472 -> 10.47: a limit-up although the raw close is +4.7% on the raw previous close
    assert (r["ref_method"], r["ref_price"], r["prev_close_raw"], r["limit_up_price"]) == ("factor_derived", D("9.52"), D("10.00"), D("10.47"))
    assert r["is_limit_up_close"] is True and r["ref_uncertain"] is False
    assert one(out, d0)["ref_method"] is None and one(out, d0)["rule_status"] == "no_previous_close"
    # without the factor file the same row would be judged against 10.00 and missed; the method says so
    blind = one(C.limit_flags_raw(bars("000001", rows)), d1)
    assert blind["ref_method"] == "prev_close_raw_no_factor_file" and blind["limit_up_price"] == D("11.00") and blind["is_limit_up_close"] is False
    # a code with a factor file but no change on the day uses the raw previous close
    calm = one(C.limit_flags_raw(bars("000001", rows), factors("000001", [(date(1990, 1, 1), "1.0")])), d1)
    assert calm["ref_method"] == "prev_close_raw"
    # the exchange's own reference price wins when a source gives it
    given = one(C.limit_flags_raw(bars("000001", rows).with_columns(pl.Series("exchange_ref_price", [None, D("9.53")], dtype=pl.Decimal(38, 12))), fac), d1)
    assert given["ref_method"] == "exchange_reference" and given["limit_up_price"] == D("10.48") and given["is_limit_up_close"] is False


def test_the_derived_reference_on_real_ex_right_days_of_000001_has_the_exchange_precision():
    rows = M.parse_kline(fixture("sina_adj_kline_sz000001.json"))
    fac = M.parse_factor_rows(fixture("sina_adj_factor_sz000001.js"))
    frame = bars("000001", [(r["date"], r["text"]["open"], r["text"]["high"], r["text"]["low"], r["text"]["close"]) for r in rows])
    out = C.limit_flags_raw(frame, factors("000001", [(d, f) for d, f in fac]))
    ex = out.filter(pl.col("ref_method") == "factor_derived")
    assert ex["trade_date"].to_list() == [date(2025, 6, 12), date(2025, 10, 15), date(2026, 6, 12), date(2026, 9, 24)]
    assert ex["ref_price"].to_list() == [D("11.49"), D("11.33"), D("10.94"), D("11.35")]            # dividend days: BELOW the raw previous close
    assert all(r < p for r, p in zip(ex["ref_price"].to_list(), ex["prev_close_raw"].to_list()))
    assert not any(ex["ref_uncertain"].to_list())
    assert set(out["rule_status"].to_list()) == {"verified", "no_previous_close"} and out["ref_method"].null_count() == 1


def test_a_derived_reference_next_to_a_half_tick_is_marked_uncertain():
    d0, d1 = date(2026, 9, 23), date(2026, 9, 24)
    # 10.00 * 0.9995 / 1.0 = 9.995: exactly half a tick, so the exchange's own rounding of its (unseen) unrounded reference decides
    fac = factors("000001", [(date(1990, 1, 1), "1.0"), (d1, "0.9995")])
    r = one(C.limit_flags_raw(bars("000001", [(d0, "10.00", "10.00", "10.00", "10.00"), (d1, "9.90", "9.90", "9.90", "9.90")]), fac), d1)
    assert (r["ref_method"], r["ref_price"], r["ref_uncertain"]) == ("factor_derived", D("10.00"), True)


# ---- ST and other boards ---------------------------------------------------------------------------------------------------------------
def test_st_bands_come_from_the_registry_and_unverified_or_unknown_gives_null():
    pre, post = date(2026, 7, 3), date(2026, 7, 6)
    sse_before = C.limit_flags_raw(bars("600001", [(date(2026, 7, 2), "10.00", "10.00", "10.00", "10.00"), (pre, "10.50", "10.50", "10.50", "10.50")], st=True))
    r = one(sse_before, pre)
    assert (r["limit_ratio"], r["limit_up_price"], r["is_limit_up_close"], r["rule_status"]) == (D("0.05"), D("10.50"), True, "verified")      # SSE ST: 5% before 2026-07-06
    sse_after = C.limit_flags_raw(bars("600001", [(date(2026, 7, 3), "10.00", "10.00", "10.00", "10.00"), (post, "10.50", "10.50", "10.50", "10.50")], st=True))
    r = one(sse_after, post)
    assert (r["limit_ratio"], r["limit_up_price"], r["is_limit_up_close"]) == (D("0.10"), D("11.00"), False)                                   # 10% from 2026-07-06
    szse = C.limit_flags_raw(bars("000001", [(date(2026, 7, 2), "10.00", "10.00", "10.00", "10.00"), (pre, "10.50", "10.50", "10.50", "10.50")], st=True))
    r = one(szse, pre)
    assert r["is_limit_up_close"] is None and r["limit_up_price"] is None and r["rule_status"].startswith("unverified:")      # SZSE main-board ST is not in the rule source
    unknown = C.limit_flags_raw(bars("600001", [(date(2026, 7, 2), "10.00", "10.00", "10.00", "10.00"), (pre, "10.50", "10.50", "10.50", "10.50")], st=None))
    assert one(unknown, pre)["rule_status"] == "st_unknown" and one(unknown, pre)["is_limit_up_close"] is None


def test_other_boards_and_the_ipo_window_and_missing_inputs():
    d0, d1 = date(2026, 6, 9), date(2026, 6, 10)
    chinext = C.limit_flags_raw(bars("300001", [(d0, "10.00", "10.00", "10.00", "10.00"), (d1, "12.00", "12.00", "11.00", "12.00")]))
    assert one(chinext, d1)["limit_ratio"] == D("0.20") and one(chinext, d1)["is_limit_up_close"] is True
    ipo = C.limit_flags_raw(bars("600001", [(d0, "10.00", "10.00", "10.00", "10.00"), (d1, "11.00", "11.00", "11.00", "11.00")],
                                 in_ipo_window=[True, True]))
    assert one(ipo, d1)["rule_status"] == "ipo_no_limit_window_not_modelled" and one(ipo, d1)["is_limit_up_close"] is None
    with pytest.raises(ValueError, match="is_st"):
        C.limit_flags_raw(bars("600001", [(d0, "1", "1", "1", "1")]).drop("is_st"))
    with pytest.raises(ValueError, match="exchange"):
        C.limit_flags_raw(bars("600001", [(d0, "1", "1", "1", "1")]).drop("code"))
    explicit = C.limit_flags_raw(bars("600001", [(d0, "10", "10", "10", "10"), (d1, "11", "11", "11", "11")]).drop("code")
                                 .with_columns(pl.lit("XSHG").alias("exchange"), pl.lit("main").alias("board")))
    assert one(explicit, d1)["is_limit_up_close"] is True


def test_the_latest_revision_of_a_raw_frame_is_the_one_judged_and_instruments_do_not_share_a_previous_close():
    d0, d1 = date(2026, 6, 9), date(2026, 6, 10)
    a = bars("600001", [(d0, "10.00", "10.00", "10.00", "10.00"), (d1, "10.80", "10.80", "10.80", "10.80"), (d1, "11.00", "11.00", "11.00", "11.00")],
             revision=[1, 1, 2])
    b = bars("600002", [(d1, "50.00", "50.00", "50.00", "50.00")], revision=[1])
    out = C.limit_flags_raw(pl.concat([b, a]))
    assert out.height == 3
    assert one(out.filter(pl.col("code") == "600001"), d1)["is_limit_up_close"] is True                  # revision 2 (11.00) is judged
    assert one(out.filter(pl.col("code") == "600002"), d1)["rule_status"] == "no_previous_close"          # the other instrument's close is not its reference
