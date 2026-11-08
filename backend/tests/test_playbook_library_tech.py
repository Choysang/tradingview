"""Playbook library v4 (technical analysis) and the bear-market rule in the daily picks.

Truncation no-look-ahead and SIM coverage for these ids are exercised by the parametrised tests in
test_playbook_library.py (they iterate the whole catalog).
"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from backend.app.playbooks import base, picks
from backend.app.playbooks.library_tech import TECH_IDS, period_lines

START = date(2025, 1, 6)  # a Monday


def bars(closes: list[float], *, code: str = "A", lows: dict[int, float] | None = None,
         opens: dict[int, float] | None = None, volume: dict[int, float] | None = None) -> list[dict]:
    """Weekday bars for one stock; open = previous close unless given, high/low just around the body."""
    rows, prev = [], closes[0]
    day = START
    for i, close in enumerate(closes):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        o = (opens or {}).get(i, prev)
        rows.append({
            "code": code, "date": day, "name": code, "board": "10cm", "open": o, "close": close, "prev_close": prev,
            "high": max(o, close) * 1.001, "low": (lows or {}).get(i, min(o, close) * 0.999),
            "pct_chg": (close / prev - 1) * 100, "amount": 2.0, "days_listed": 300, "volume": (volume or {}).get(i, 100.0),
            "vol_ma5": 100.0, "macd_dif": 0.1, "macd_dea": 0.1, "macd_hist": 0.0, "kdj_j": 50.0, "rsi_6": 50.0,
            "boll_lower": close * 0.9, "ma20": close,
        })
        prev = close
        day += timedelta(days=1)
    return rows


def fired(playbook_id: str, rows: list[dict]) -> list[date]:
    pb = base.resolve(playbook_id)
    return pb.signal(pl.DataFrame(rows).sort("code", "date").lazy(), pb.params()).collect()["date"].to_list()


def falling(n: int = 140, to: float = 10.0) -> list[float]:
    """Flat at 13 for most of the history, then a slide to `to` over the last 10 bars."""
    return [13.0] * (n - 10) + [13.0 - (13.0 - to) * (i + 1) / 10 for i in range(10)]


def test_technical_playbooks_are_registered_with_their_study():
    for playbook_id in TECH_IDS:
        pb = base.resolve(playbook_id)
        assert pb is not None and pb.requires_history == ()
        assert "2026-11-01-technical-bear" in pb.spec.rule and "不设价格止损" in pb.spec.rule


def test_hammer_fires_after_a_fall_near_the_low_and_not_on_a_plain_bar():
    closes = falling() + [10.05]
    hammer = bars(closes, opens={140: 10.0}, lows={140: 9.7})  # body 0.05, lower shadow 0.3
    assert fired("k_hammer", hammer) == [hammer[-1]["date"]]
    plain = bars(closes, opens={140: 10.0})
    assert fired("k_hammer", plain) == []


def test_engulfing_needs_a_red_bar_fully_covered_by_a_green_one():
    closes = falling() + [9.8, 10.3]
    rows = bars(closes, opens={140: 10.1, 141: 9.7})  # day 140 red 10.1 -> 9.8; day 141 opens 9.7, closes 10.3
    assert fired("k_engulfing", rows) == [rows[-1]["date"]]
    short = bars(falling() + [9.8, 10.0], opens={140: 10.1, 141: 9.7})  # closes below the red bar's open
    assert fired("k_engulfing", short) == []


def test_macd_bottom_divergence_wants_a_lower_price_with_a_higher_dif():
    closes = falling(150, 9.0)
    rows = bars(closes)
    for i, r in enumerate(rows):
        r["macd_dif"] = -0.5 if 100 <= i < 140 else -0.2  # DIF bottomed 10-50 bars ago, now higher
        r["macd_hist"] = 0.1 if i == len(rows) - 1 else 0.0
    assert fired("macd_bottom_divergence", rows) == [rows[-1]["date"]]  # the only day the histogram rises
    for r in rows:
        r["macd_dif"] = -0.5 if r is rows[-1] else -0.2  # DIF makes a new low with the price: no divergence
    assert rows[-1]["date"] not in fired("macd_bottom_divergence", rows)


def weekly_ema(values: list[float], span: int) -> list[float]:
    a, out = 2 / (span + 1), []
    for v in values:
        out.append(v if not out else a * v + (1 - a) * out[-1])
    return out


def test_weekly_lines_use_only_completed_weeks_from_the_next_monday():
    closes = [10 + 0.02 * i if i < 150 else 13 - 0.03 * (i - 150) for i in range(190)] + [10.3 + 0.05 * i for i in range(60)]
    rows = bars(closes)
    out = period_lines(pl.DataFrame(rows).lazy(), "1w", "w", 35).collect().sort("date")
    frame = pl.DataFrame(rows).select("date", "close").with_columns(pl.col("date").dt.truncate("1w").alias("p"))
    weekly = frame.group_by("p", maintain_order=True).agg(pl.col("close").last()).sort("p")
    dif = [x - y for x, y in zip(weekly_ema(weekly["close"].to_list(), 12), weekly_ema(weekly["close"].to_list(), 26))]
    dea = weekly_ema(dif, 9)
    starts = frame.group_by("p", maintain_order=True).agg(pl.col("date").min()).sort("p")["date"].to_list()
    # 35 weekly bars are needed: week index 34 is the first one used, from the first day of week 35.
    assert out.filter(pl.col("date") == starts[34])["w_up"][0] is None
    for k in range(35, len(starts)):  # the week k-1 is known from the first day of week k
        day = out.filter(pl.col("date") == starts[k]).row(0, named=True)
        assert day["w_fresh"] is True
        assert day["w_up"] == (dif[k - 1] > dea[k - 1])
        assert day["w_golden"] == (dif[k - 1] > dea[k - 1] and dif[k - 2] <= dea[k - 2])
    # Inside a week nothing changes, and the week in progress is never used.
    by_week = out.with_columns(pl.col("date").dt.truncate("1w").alias("p")).group_by("p").agg(pl.col("w_up").n_unique())
    assert by_week["w_up"].max() == 1
    assert out.filter(pl.col("w_golden").fill_null(False)).height > 0


def test_a_suspension_on_the_first_day_of_a_week_moves_the_fresh_flag_to_the_next_bar():
    closes = [10 + 0.01 * i for i in range(220)]
    rows = bars(closes, code="A") + bars(closes, code="B")
    # Stock A alone gives the calendar: drop B's bar on the first trading day of a late week.
    week = pl.DataFrame(rows[:220]).with_columns(pl.col("date").dt.truncate("1w").alias("p"))
    first = week.group_by("p").agg(pl.col("date").min()).sort("p")["date"].to_list()[-5]
    rows = [r for r in rows if not (r["code"] == "B" and r["date"] == first)]
    out = period_lines(pl.DataFrame(rows).lazy(), "1w", "w", 35).collect().sort("code", "date")
    a = out.filter((pl.col("code") == "A") & pl.col("w_fresh"))["date"].to_list()
    b = out.filter((pl.col("code") == "B") & pl.col("w_fresh"))["date"].to_list()
    nxt = out.filter((pl.col("code") == "B") & (pl.col("date") > first))["date"][0]
    assert first in a and first not in b and nxt in b
    assert len(a) == len(b)  # B gets every week once, only one of them a day late


def test_low_position_long_lower_shadow_on_volume_fires():
    closes = falling() + [10.0]
    rows = bars(closes, opens={140: 10.05}, lows={140: 9.5}, volume={140: 200.0})  # lower 0.5 of a ~0.56 range
    assert fired("k_lower_shadow_volume", rows) == [rows[-1]["date"]]
    quiet = bars(closes, opens={140: 10.05}, lows={140: 9.5})
    assert fired("k_lower_shadow_volume", quiet) == []


# --- bear mode ------------------------------------------------------------------------------


def bench(closes: list[float]) -> SimpleNamespace:
    days = [START + timedelta(days=i) for i in range(len(closes))]
    return SimpleNamespace(benchmark=pl.DataFrame({"date": days, "open": closes, "close": closes}))


def test_market_mode_is_bear_only_below_the_60_day_mean():
    up = bench([100 + i * 0.1 for i in range(80)])
    assert picks.market_mode(up, up.benchmark["date"][-1]) == ("normal", None)
    down = bench([100.0] * 70 + [90.0] * 10)
    mode, detail = picks.market_mode(down, down.benchmark["date"][-1])
    assert mode == "bear" and "90.00" in detail and "60 日均线 98.33" in detail
    # Only closes up to the day count: the same fall seen from before it is not bear.
    assert picks.market_mode(down, down.benchmark["date"][69])[0] == "normal"
    assert picks.market_mode(bench([90.0] * 30), START + timedelta(days=29)) == ("normal", None)  # too short


def test_bear_mode_keeps_only_long_defensive_and_untested_value_voters():
    registry = {pb.spec.id: pb for pb in base.catalog()}
    weights = [
        picks.Weight("low_risk_combo", "long", 0.5), picks.Weight("momentum_12_1", "long", 0.4),
        picks.Weight("limit_pullback", "short", 0.6), picks.Weight("k_hammer", "short", 0.3),
        picks.Weight("w_macd_golden", "swing", 0.3), picks.Weight("value_pe_pb", "long", 0.25, untested=True),
    ]
    assert picks.apply_market_mode(weights, registry, "normal") == weights
    kept = picks.apply_market_mode(weights, registry, "bear")
    assert [w.playbook_id for w in kept] == ["low_risk_combo", "value_pe_pb"]
    notes = picks.bear_notes("中证全指收盘 90.00，低于 60 日均线 98.33", weights, kept,
                             ["短线：当前周期没有战法通过检验……", "长线：别的说明"])
    assert notes[0].startswith("熊市模式：") and "60 日线" in notes[0]
    assert notes[1].startswith("短线：熊市模式") and notes[2].startswith("波段：熊市模式")
    assert "短线：当前周期没有战法通过检验……" not in notes and "长线：别的说明" in notes
    # When long loses every tested voter too, long gets a bear note in place of its idle note.
    only_momentum = [picks.Weight("momentum_12_1", "long", 0.4)]
    notes = picks.bear_notes("x", only_momentum, picks.apply_market_mode(only_momentum, registry, "bear"), ["长线：别的说明"])
    assert any(n.startswith("长线：熊市模式") for n in notes) and "长线：别的说明" not in notes


@pytest.mark.parametrize("playbook_id", ["k_hammer", "macd_bottom_divergence", "w_macd_golden", "m_macd_golden"])
def test_bear_mode_pauses_every_technical_playbook(playbook_id):
    registry = {pb.spec.id: pb for pb in base.catalog()}
    pb = registry[playbook_id]
    assert picks.apply_market_mode([picks.Weight(playbook_id, pb.spec.horizon, 1.0)], registry, "bear") == []
