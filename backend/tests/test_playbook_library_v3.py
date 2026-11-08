"""Playbook library v3 (factor composites): ST detection, exclusions and ranking on hand-built panels.

Truncation no-look-ahead and SIM coverage for these ids are exercised by the parametrised tests in
test_playbook_library.py (they iterate the whole catalog).
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from backend.app.playbooks import base, evaluate, picks, rotation
from backend.app.playbooks.base import Playbook
from backend.app.playbooks.library_factor import FACTORS, ST_LIMIT_WIDENED, composite, not_st, st_like
from contracts.schema import EntryRules

NEW_IDS = ("low_risk_combo", "shrink_oversold")
DAYS = 130
START = date(2026, 1, 5)

SCHEMA = {
    "code": pl.String, "date": pl.Date, "name": pl.String, "board": pl.String,
    **{k: pl.Float64 for k in (
        "open", "high", "low", "close", "prev_close", "pct_chg", "amount", "float_cap", "total_cap", "cap_est",
        "ma60", "rsi_14",
    )},
    "days_listed": pl.Int64,
}


def day(i: int) -> date:
    return START + timedelta(days=i)


def series(code: str, moves: list[float], *, close: float = 10.0, limit_days: tuple[int, ...] = (), name: str = "",
           board: str = "10cm", amount: float = 1.0, float_cap: float = 100.0) -> list[dict]:
    """A price path with the given daily % moves. On `limit_days` the close is the exact limit price
    (prev close x (1 + move%), rounded to the cent) at the day's high (up) or low (down); on other
    days the close sits inside the range."""
    rows, prev = [], close
    for i, move in enumerate(moves):
        now = round(prev * (1 + move / 100), 2)
        at_limit = i in limit_days
        high = now if at_limit and move > 0 else max(prev, now) * 1.004
        low = now if at_limit and move < 0 else min(prev, now) * 0.996
        rows.append({
            "code": code, "date": day(i), "name": name or code, "board": board,
            "open": prev, "high": high, "low": low, "close": now, "prev_close": prev, "pct_chg": (now / prev - 1) * 100,
            "amount": amount, "float_cap": float_cap, "total_cap": float_cap, "cap_est": float_cap,
            "ma60": now, "rsi_14": 50.0, "days_listed": 200,
        })
        prev = now
    return rows


def frame(rows: list[dict]) -> pl.LazyFrame:
    return pl.DataFrame(rows, schema=SCHEMA).sort("code", "date").lazy()


def flags(rows: list[dict], expr: pl.Expr) -> list[bool]:
    return frame(rows).with_columns(expr.alias("flag")).collect()["flag"].to_list()


def calm(n: int = DAYS) -> list[float]:
    return [0.5 if i % 2 else -0.5 for i in range(n)]


def test_new_playbooks_registered_with_rules_that_name_the_st_change():
    for playbook_id in NEW_IDS:
        pb = base.resolve(playbook_id)
        assert pb is not None and pb.requires_history == ()
        assert "2026-07-06" in pb.spec.rule and "不按今天的名称回溯" in pb.spec.rule


# --- ST from prices ----------------------------------------------------------------------


def test_two_closes_on_the_five_percent_limit_mark_a_name_for_the_window():
    moves = calm(10) + [5.0] + calm(5) + [-5.0] + calm(70)
    out = flags(series("S", moves, limit_days=(10, 16)), st_like())
    assert out[:16] == [False] * 16      # one hit is not enough
    assert all(out[16:70])               # second hit at 16; the first one leaves the 60-bar window after 69
    assert not any(out[70:])


@pytest.mark.parametrize("moves,limit_days,board", [
    (calm(10) + [5.0] + calm(70), (10,), "10cm"),                         # a single +5% close at the high
    (calm(5) + [7.0] + calm(4) + [5.0] + calm(3) + [5.0] + calm(10), (10, 14), "10cm"),  # a 7% move: not a 5% limit
    (calm(10) + [5.0] + calm(3) + [5.0] + calm(10), (10, 14), "20cm"),    # ChiNext ST keeps a 20% limit
    (calm(10) + [5.0] + calm(3) + [5.0] + calm(10), (), "10cm"),          # +5% twice but closed below the high
], ids=["one-hit", "wider-move", "other-board", "not-at-high"])
def test_st_like_stays_silent_on_near_misses(moves, limit_days, board):
    assert not any(flags(series("N", moves, limit_days=limit_days, board=board), st_like()))


def test_todays_st_name_is_never_used_for_history():
    rows = series("X", calm((ST_LIMIT_WIDENED - START).days + 3), name="*ST样本")
    assert all(flags(rows, not_st()))


# --- composite ranking -------------------------------------------------------------------


def low_risk_rows() -> list[dict]:
    loud = [3.0 if i % 2 else -3.0 for i in range(DAYS)]
    return (
        series("Q", calm(), float_cap=1000.0)               # low turnover, small moves: best on all three factors
        + series("L", loud, float_cap=100.0)                # ten times the turnover, six times the moves
        + series("C", calm(), close=1.5, float_cap=1000.0)  # as calm as Q but below the price floor
        + series("D", calm(), amount=0.1, float_cap=1000.0)  # as calm as Q but illiquid
    )


def test_low_risk_combo_ranks_the_calm_name_first_and_drops_cheap_and_illiquid_ones():
    pb = base.resolve("low_risk_combo")
    signals = pb.signal(frame(low_risk_rows()), pb.params()).collect()
    last = signals.filter(pl.col("date") == day(DAYS - 1)).sort("score", descending=True)
    assert last["code"].to_list() == ["Q", "L"] and last["score"].to_list() == [1.0, 0.5]
    assert set(signals["code"]) == {"Q", "L"}
    assert signals["date"].min() == day(59)  # the 60-bar volatility window fills on bar 59
    only_best = pb.signal(frame(low_risk_rows()), pb.params({"offer": 1})).collect()
    assert set(only_best["code"]) == {"Q"}


def shrink_rows() -> list[dict]:
    rows = series("A", calm()) + series("B", calm())
    for row in rows:
        i = (row["date"] - START).days
        if row["code"] == "A":  # volume fell to a fifth over the last 5 bars, 10% under MA60, RSI 30
            row.update(amount=1.0 if i < DAYS - 5 else 0.2, ma60=row["close"] * 1.11, rsi_14=30.0)
        else:
            row.update(ma60=row["close"] * 0.95, rsi_14=60.0)
    return rows


def test_shrink_oversold_prefers_dried_up_volume_below_the_60_day_line():
    pb = base.resolve("shrink_oversold")
    signals = pb.signal(frame(shrink_rows()), pb.params()).collect()
    last = signals.filter(pl.col("date") == day(DAYS - 1)).sort("score", descending=True)
    assert last["code"].to_list() == ["A", "B"] and last["score"].to_list() == [1.0, 0.5]


def one_factor_apart(factor: str) -> list[dict]:
    """Two names that differ only in what drives `factor`; "A" is the one the factor should prefer."""
    a, b = series("A", calm(), float_cap=500.0), series("B", calm(), float_cap=500.0)
    for i, (ra, rb) in enumerate(zip(a, b)):
        if factor == "low_turn_5":
            rb.update(float_cap=50.0)
        elif factor in ("max_20", "lowvol_60"):
            rb.update(pct_chg=ra["pct_chg"] * 4)
        elif factor == "turn_shrink_5_60" and i >= DAYS - 5:
            ra.update(amount=0.6)
        elif factor == "bias_60":
            ra.update(ma60=ra["close"] * 1.1)
        elif factor == "rsi_low":
            ra.update(rsi_14=30.0)
    return a + b


@pytest.mark.parametrize("factor", list(FACTORS))
def test_each_factor_points_the_documented_way(factor: str):
    signal = composite((factor,))
    pb = base.resolve("low_risk_combo")
    out = signal(frame(one_factor_apart(factor)), pb.params()).collect()
    last = out.filter(pl.col("date") == day(DAYS - 1)).sort("score", descending=True)
    assert last["code"].to_list() == ["A", "B"] and last["score"].to_list() == [1.0, 0.5], factor


# --- every backtest uses the playbook's own buy rule ---------------------------------------


def spy_on(monkeypatch, module) -> list[EntryRules | None]:
    seen: list[EntryRules | None] = []
    real = module.simulate

    def spy(*args, **kwargs):
        seen.append(kwargs.get("entry"))
        return real(*args, **kwargs)

    monkeypatch.setattr(module, "simulate", spy)
    return seen


def custom_playbook() -> tuple[Playbook, EntryRules]:
    pb = base.resolve("low_risk_combo")
    rule = EntryRules(pullback_pct=2.0)
    return Playbook(spec=pb.spec.model_copy(update={"id": "low_risk_pullback", "entry": rule}), signal=pb.signal), rule


def test_board_rotation_and_replay_all_use_the_playbooks_buy_rule(monkeypatch):
    custom, rule = custom_playbook()
    ctx = evaluate.context()
    seen = spy_on(monkeypatch, evaluate)
    evaluate._compute(custom, ctx, None, custom.spec.exits, custom.spec.sizing, None, None)
    assert len(seen) == 3 and all(entry == rule for entry in seen)

    seen = spy_on(monkeypatch, rotation)
    rotation.trade_book(ctx, [custom])
    assert seen == [rule]

    seen = spy_on(monkeypatch, picks)
    picks.in_sample_weights(ctx, ctx.dates[len(ctx.dates) // 2], registry=[custom])
    assert seen == [rule]
