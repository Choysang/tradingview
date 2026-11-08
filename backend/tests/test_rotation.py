"""Walk-forward rotation: point-in-time elections, cash when nothing works, honest comparison."""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.playbooks import rotation


def days(n: int, start: date = date(2025, 1, 1)) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def trades(rows: list[tuple[date, date, float]], code: str = "600000") -> pl.DataFrame:
    return pl.DataFrame(
        {
            "code": [code] * len(rows), "entry": [r[0] for r in rows], "exit": [r[1] for r in rows],
            "ret": [r[2] for r in rows], "trend": ["range"] * len(rows), "sentiment": ["repair"] * len(rows),
        },
        schema={"code": pl.Utf8, "entry": pl.Date, "exit": pl.Date, "ret": pl.Float64, "trend": pl.Utf8, "sentiment": pl.Utf8},
    )


def test_scores_use_only_trades_closed_by_the_decision_day():
    d = days(30)
    book = {
        "a": trades([(d[i], d[i + 2], 2.0) for i in range(6)]),  # all closed by d[7]
        "b": trades([(d[i], d[20], 9.0) for i in range(6)]),  # still open at d[10]
    }
    table = rotation.scores(book, d[0], d[10])
    assert set(table) == {"a"}
    score, n, raw = table["a"]
    assert n == 6 and raw == pytest.approx(2.0)
    assert score == pytest.approx(12.0 / (6 + rotation.SHRINK))  # shrunk toward zero


def test_few_trades_do_not_qualify_and_losers_are_never_elected():
    d = days(30)
    book = {
        "lucky": trades([(d[0], d[1], 30.0)] * (rotation.MIN_TRADES - 1)),
        "loser": trades([(d[i], d[i + 1], -1.0) for i in range(8)]),
        "steady": trades([(d[i], d[i + 1], 0.5) for i in range(8)]),
    }
    table = rotation.scores(book, d[0], d[20])
    assert "lucky" not in table
    assert rotation.elect(table, 3) == ("steady",)


def test_walk_goes_to_cash_when_no_playbook_has_a_positive_record():
    d = days(80)
    loser = trades([(d[i], d[i + 1], -1.0) for i in range(0, 78)])
    periods = rotation.walk({"loser": loser}, d, {}, "trailing", 20, 3)
    assert periods and all(p.chosen == () and p.selected == 0.0 for p in periods)
    assert all(p.baseline == pytest.approx(-1.0) for p in periods)
    stats = rotation.period_stats(periods)
    assert stats.cash_periods == len(periods) and stats.excess_avg == pytest.approx(1.0)
    assert stats.hit_rate == 100.0 and stats.invested_periods == 0


def test_walk_follows_the_recent_winner_into_the_next_period():
    d = days(100)
    # "early" wins in the first half and then loses; "late" does the reverse.
    early = trades([(d[i], d[i + 1], 2.0 if i < 50 else -2.0) for i in range(98)])
    late = trades([(d[i], d[i + 1], -2.0 if i < 50 else 2.0) for i in range(98)])
    periods = rotation.walk({"early": early, "late": late}, d, {}, "trailing", 20, 1)
    first, last = periods[0], periods[-1]
    assert first.chosen == ("early",) and first.selected == pytest.approx(2.0)
    assert last.chosen == ("late",) and last.selected == pytest.approx(2.0)
    assert all(p.baseline == pytest.approx(0.0) for p in periods)


def test_validation_needs_positive_significant_and_frequent_excess():
    good = rotation.RotationStats(periods=30, excess_avg=0.5, hit_rate=60.0, t_stat=2.0)
    assert rotation.validated(good)
    assert not rotation.validated(good.model_copy(update={"t_stat": 1.0}))
    assert not rotation.validated(good.model_copy(update={"hit_rate": 50.0}))
    assert not rotation.validated(good.model_copy(update={"excess_avg": -0.1}))
    assert not rotation.validated(good.model_copy(update={"periods": rotation.MIN_OOS_PERIODS - 1}))


def test_newey_west_t_shrinks_when_neighbouring_periods_move_together():
    alternating = [1.0, -0.5] * 10
    trending = [1.0] * 5 + [-0.5] * 5 + [1.0] * 5 + [-0.5] * 5
    assert rotation.newey_west_t(alternating, lag=0) == pytest.approx(rotation.newey_west_t(trending, lag=0))
    assert rotation.newey_west_t(trending) < rotation.newey_west_t(alternating)
    assert rotation.newey_west_t([1.0, 2.0]) is None


def test_configuration_is_chosen_on_early_periods_and_tested_on_later_ones():
    d = days(200)
    split = d[139]
    # "early" wins before the split and loses after it: the in-sample record does not
    # carry over, and once its trailing record turns negative the walk goes to cash.
    early = trades([(d[i], d[i + 1], 2.0 if i < 140 else -2.0) for i in range(198)])
    flat = trades([(d[i], d[i + 1], 0.0) for i in range(198)])
    periods = rotation.walk({"early": early, "flat": flat}, d, {}, "trailing", 20, 1)
    config = rotation.summarise(periods, "trailing", 20, 1, split)
    assert config.in_sample.periods and config.out_of_sample.periods
    assert all(p.end <= split or p.start > split or (p.start <= split < p.end) for p in periods)
    assert config.in_sample.excess_avg > 0 and config.in_sample.cash_periods == 0
    assert config.out_of_sample.invested_excess_avg <= 0 < config.out_of_sample.cash_periods
    assert config.in_sample.periods + config.out_of_sample.periods <= len(periods)


def test_choose_takes_the_best_in_sample_t_and_ignores_out_of_sample():
    def cfg(key, t_in, t_out):
        return rotation.RotationConfig(
            key=key, label=key, method="trailing", window=20, top_k=3,
            in_sample=rotation.RotationStats(periods=10, t_stat=t_in), out_of_sample=rotation.RotationStats(periods=5, t_stat=t_out),
        )
    configs = [cfg("trailing-20-3", 1.0, 5.0), cfg("trailing-60-3", 2.0, -1.0)]
    assert rotation.choose(configs).key == "trailing-60-3"
    blank = [cfg("trailing-20-3", None, 3.0), cfg("trailing-60-3", None, 4.0)]
    assert rotation.choose(blank).key == "trailing-20-3"  # the fixed fallback


def test_live_election_sits_on_the_walk_anchors():
    assert rotation.current_anchor(19, "trailing", 20) is None
    assert rotation.current_anchor(20, "trailing", 20) == 20
    assert rotation.current_anchor(39, "trailing", 20) == 20
    assert rotation.current_anchor(40, "trailing", 20) == 40


def test_rotation_endpoint_reports_every_configuration():
    with TestClient(app) as client:
        response = client.get("/api/v1/playbooks/rotation")
    assert response.status_code == 200
    body = response.json()
    expected = len(rotation.WINDOWS) * len(rotation.TOP_KS) + len(rotation.TOP_KS)
    assert len(body["configs"]) == expected
    assert body["default_key"] in {config["key"] for config in body["configs"]}
    assert body["split_date"] and (body["decided_on"] is None or body["decided_on"] <= body["provenance"]["trade_date"])
    assert body["provenance"]["mode"] == "SIM" and body["verdict"]
    assert sum(row["selected"] for row in body["current"]) <= 3
