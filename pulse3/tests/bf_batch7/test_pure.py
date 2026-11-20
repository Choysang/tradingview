"""Batch 7 pure checks: exact limit flags from raw prices (rounding, ex-right day, uncertain reference), fills at the open limit, the D+3 exit with a limit-down day, the judge rule, the frozen prefix."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

import polars as pl
import pytest

from pulse.application import bf_batch7_inputs as IN
from pulse.application.refdata_service import instrument_id_of
from pulse.domain.batch6 import panel as PN
from pulse.domain.batch7 import rules as R7
from bf_batch7.support import DEC, flat, window, weekdays

DAYS = weekdays(date(2026, 11, 2), 10)


def one(code, prices, factors=None):
    """Panel rows of one stock whose bars are (open, close) pairs, volume 1000."""
    w = window({code: [(o, c, 1000) for o, c in prices]}, DAYS[:len(prices)], factors=factors)
    panel, sessions, flags = IN.exact_panel(w, frozenset())
    return panel.sort("date"), flags.sort("trade_date")


def test_limit_up_price_rounds_half_up_to_the_cent_from_the_raw_previous_close():
    # 5.05 x 1.10 = 5.555 -> 5.56 (half up): a close of 5.56 is a limit-up close, 5.55 is not
    p, _ = one("600001", [("5.05", "5.05"), ("5.05", "5.56"), ("5.56", "5.56")])
    assert p["up_c"].to_list()[1] == 556 and p["lu"].to_list() == [False, True, False]
    p2, _ = one("600001", [("5.05", "5.05"), ("5.05", "5.55")])
    assert p2["lu"].to_list() == [False, False] and p2["up_c"][1] == 556


def test_streak_counts_exact_limit_up_closes_and_restarts_on_a_normal_day():
    p, _ = one("600001", [("10.00", "10.00"), ("10.00", "11.00"), ("11.00", "12.10"), ("12.10", "13.31"), ("13.31", "13.00")])
    assert p["streak"].to_list() == [0, 1, 2, 3, 0]            # 11.00, 12.10 and 13.31 are exactly +10 %; the first row has no previous close (not classified)


def test_ex_right_day_uses_the_factor_derived_reference():
    # raw previous close 10.00, dividend 1.00: the exchange reference is 9.00 (factor 1 -> 0.9), so the limit-up price is 9.90 and a close of 9.90 IS a limit-up close
    fac = pl.DataFrame({"instrument_id": [instrument_id_of("600001")] * 2, "effective_from": [DAYS[0], DAYS[2]], "factor": [D("1"), D("0.9")],
                        "available_at": [None, None], "revision": [1, 1]},
                       schema={"instrument_id": pl.String, "effective_from": pl.Date, "factor": pl.Decimal(38, 16), "available_at": pl.Datetime("us", "UTC"), "revision": pl.Int32})
    p, flags = one("600001", [("10.00", "10.00"), ("10.00", "10.00"), ("9.00", "9.90")], factors=fac)
    assert p["lu"].to_list()[2] is True and p["up_c"][2] == 990
    assert flags["ref_method"].to_list()[2] == "factor_derived"
    q, _ = one("600001", [("10.00", "10.00"), ("10.00", "10.00"), ("9.00", "9.90")])        # without a factor file the 9.90 close looks like -1 %: no limit-up
    assert q["lu"].to_list()[2] is False and q["up_c"][2] == 1100


def test_reference_within_a_hundredth_of_a_half_tick_is_not_classified():
    fac = pl.DataFrame({"instrument_id": [instrument_id_of("600001")] * 2, "effective_from": [DAYS[0], DAYS[2]], "factor": [D("1"), D("0.9")],
                        "available_at": [None, None], "revision": [1, 1]},
                       schema={"instrument_id": pl.String, "effective_from": pl.Date, "factor": pl.Decimal(38, 16), "available_at": pl.Datetime("us", "UTC"), "revision": pl.Int32})
    # raw 10.05 x 0.9 = 9.045: exactly half a tick -> the reference may round either way
    p, flags = one("600001", [("10.05", "10.05"), ("10.05", "10.05"), ("9.00", "9.95")], factors=fac)
    assert flags["ref_uncertain"].to_list()[2] is True
    assert p["flag_ok"].to_list()[2] is False and p["lu"].to_list()[2] is False and p["up_c"][2] is None and p["streak"].to_list()[2] == 0


def test_buy_not_filled_at_an_open_on_the_limit_up_price_and_d3_exit_defers_past_a_limit_down_day():
    # S: D = d2. d3 open 10.00 fills. d4 close 10.30, d5 (D+3) closes at the limit-down 9.27 (10.30 x 0.9) -> cannot sell; d6 opens at its limit-down 8.34 -> cannot sell;
    # d6 closes 8.50 > 8.34 -> sold there: return 8.50 / 10.00 - 1.  T: d3 opens at the limit-up price 11.00 (10.00 x 1.1) -> not filled.
    s = [("10.00", "10.00"), ("10.00", "10.00"), ("10.00", "10.00"), ("10.00", "10.20"), ("10.20", "10.30"), ("10.30", "9.27"), ("8.34", "8.50"), ("8.50", "8.60")]
    t = [("10.00", "10.00")] * 3 + [("11.00", "11.00")] * 5
    w = window({"600001": [(o, c, 1000) for o, c in s], "600002": [(o, c, 1000) for o, c in t]}, DAYS[:8])
    panel, sessions, _ = IN.exact_panel(w, frozenset())
    f = PN.forward(panel, 3, len(sessions)).filter(pl.col("date") == DAYS[2]).sort("code")
    rows = {r["code"]: r for r in f.iter_rows(named=True)}
    assert rows["600002"]["status"] == "blocked_buy"
    r = rows["600001"]
    assert r["status"] == "filled" and r["exit_si"] == 6 and r["exit_c"] == 850 and r["entry_c"] == 1000
    assert r["ret"] == pytest.approx(0.85 / 1.0 - 1)


def test_a_row_whose_flags_are_unknown_cannot_be_bought_or_not():
    # ST state unknown cannot happen here (the set of ST codes is known); an unclassifiable entry day is the uncertain-reference case
    fac = pl.DataFrame({"instrument_id": [instrument_id_of("600001")] * 2, "effective_from": [DAYS[0], DAYS[3]], "factor": [D("1"), D("0.9")],
                        "available_at": [None, None], "revision": [1, 1]},
                       schema={"instrument_id": pl.String, "effective_from": pl.Date, "factor": pl.Decimal(38, 16), "available_at": pl.Datetime("us", "UTC"), "revision": pl.Int32})
    prices = [("10.05", "10.05")] * 3 + [("9.00", "9.30")] * 3
    w = window({"600001": [(o, c, 1000) for o, c in prices]}, DAYS[:6], factors=fac)
    panel, sessions, _ = IN.exact_panel(w, frozenset())
    f = PN.forward(panel, 1, len(sessions)).filter(pl.col("date") == DAYS[2])
    assert f["status"][0] == "flag_unknown"                   # D+1 is the uncertain ex-right day: neither buyable nor blocked can be said


# ---- judge --------------------------------------------------------------------------------------------------------------------------------
def j(hyp, n, s, ex, p, today=date(2027, 1, 15)):
    return R7.judge(hyp, n_filled=n, sessions=s, excess=ex, p_value=p, today=today)


def test_judge_waits_for_300_signals_and_60_sessions_and_then_applies_size_and_p():
    assert j("H1p", 299, 80, -0.01, 0.001).state == "progress"
    assert j("H1p", 300, 59, -0.01, 0.001).state == "progress"
    ok = j("H1p", 300, 60, -0.0030, 0.049)
    assert ok.state == "judged" and ok.passed is True and ok.sufficient is True
    assert j("H1p", 300, 60, -0.0025, 0.01).passed is False            # -0.25 % is not below -0.252 %
    assert j("H1p", 300, 60, -0.0030, 0.05).passed is False            # p must be below 0.05
    assert j("H1p", 500, 90, 0.004, 0.001).passed is False             # wrong sign
    assert j("H4p", 149, 90, -0.01, 0.001).state == "progress" and j("H4p", 150, 60, -0.01, 0.001).passed is True


def test_judge_at_the_deadline_uses_the_sample_it_has_and_says_so():
    d = j("H1p", 120, 55, -0.004, 0.01, today=date(2027, 6, 30))
    assert d.state == "judged" and d.passed is True and d.sufficient is False and "insufficient" in d.reason
    assert j("H1p", 120, 55, None, None, today=date(2027, 7, 1)).passed is False
    assert j("H1p", 120, 55, -0.004, 0.01, today=date(2027, 6, 29)).state == "progress"


def test_the_frozen_prefix_of_the_preregistration_matches_its_hash():
    path = Path(__file__).resolve().parents[3] / R7.SPEC_PATH
    if not path.exists():
        pytest.skip("pre-registration not present in this checkout")
    raw = path.read_bytes()
    assert R7.frozen_hash(raw) == R7.SPEC_SHA256
    assert len(R7.frozen_prefix(raw)) == 5447
    # appending results after the section 5 heading must not change the frozen part
    assert R7.frozen_hash(raw + "\n追加的判定。\n".encode("utf-8")) == R7.SPEC_SHA256
    assert R7.frozen_hash(raw.replace("匹配超额 < −0.252%".encode("utf-8"), "匹配超额 < −0.300%".encode("utf-8"))) != R7.SPEC_SHA256
