"""Market regime detection on the SIM dataset: rules, labels, no look-ahead, API."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import polars as pl
import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.playbooks import regime
from backend.app.warehouse.store import Warehouse, active
from backend.app.warehouse.synthetic import generate
from contracts.schema import RegimeHistoryResponse

BASE_COLUMNS = [
    "date", "trend", "liquidity", "sentiment", "style", "index_close", "breadth_ma20",
    "limit_up_count", "broken_rate", "max_height", "amount_ratio", "premium",
]
MIN_AGREEMENT = 0.55  # measured ~0.6: the stated rules lag turning points by design


@pytest.fixture(scope="module")
def wh() -> Warehouse:
    return active()


@pytest.fixture(scope="module")
def frame(wh: Warehouse) -> pl.DataFrame:
    return regime.regime_frame(wh)


def test_frame_has_the_documented_columns_and_valid_labels(frame: pl.DataFrame):
    assert frame.columns[: len(BASE_COLUMNS)] == BASE_COLUMNS
    assert frame["date"].is_unique().all() and frame["date"].is_sorted()
    assert set(frame["trend"]) <= {"bull", "bear", "range"}
    assert set(frame["liquidity"]) <= {"expanding", "steady", "contracting"}
    assert set(frame["sentiment"]) <= {"freezing", "repair", "climax", "ebb"}
    assert set(frame["style"]) <= {"large", "small", "balanced"}
    assert frame["breadth_ma20"].drop_nulls().is_between(0, 100).all()
    assert frame["broken_rate"].drop_nulls().is_between(0, 100).all()
    # Every trend and liquidity label occurs over 780 days of alternating segments.
    assert frame["trend"].n_unique() == 3 and frame["liquidity"].n_unique() == 3
    assert frame["sentiment"].n_unique() >= 3


def test_trend_agrees_with_the_generator_segments(frame: pl.DataFrame, capsys):
    segments = generate(end=frame["date"].max())["regimes"]
    merged = frame.join(segments, on="date")
    assert merged.height == frame.height
    agreement = float((merged["trend"] == merged["segment"]).mean())
    warm = merged.slice(60)  # MA60 needs 60 bars before it can say anything but "range"
    warm_agreement = float((warm["trend"] == warm["segment"]).mean())
    with capsys.disabled():
        print(f"\n[regime] trend agreement with generator: all={agreement:.1%} after-warm-up={warm_agreement:.1%}")
    assert agreement >= MIN_AGREEMENT and warm_agreement >= MIN_AGREEMENT
    table = merged.group_by("segment", "trend").len()

    def count(segment: str, trend: str) -> int:
        row = table.filter((pl.col("segment") == segment) & (pl.col("trend") == trend))
        return int(row["len"][0]) if row.height else 0

    # Bull segments are never mostly labelled bear, and bear segments never mostly bull.
    assert count("bull", "bear") < count("bull", "bull")
    assert count("bear", "bull") < count("bear", "bear")


def test_liquidity_follows_the_amount_ratio(frame: pl.DataFrame):
    known = frame.filter(pl.col("amount_ratio").is_not_null())
    assert known.filter((pl.col("amount_ratio") > 1.15) != (pl.col("liquidity") == "expanding")).is_empty()
    assert known.filter((pl.col("amount_ratio") < 0.85) != (pl.col("liquidity") == "contracting")).is_empty()
    assert frame.filter(pl.col("amount_ratio").is_null())["liquidity"].unique().to_list() == ["steady"]


def test_mode_smooth_removes_flicker_and_prefers_recent_on_ties():
    raw = ["range", "bull", "bull", "range", "bull", "bull", "bear", "bear", "bear"]
    smooth = regime.mode_smooth(raw, 5)
    assert smooth[0] == "range" and smooth[2] == "bull"
    assert smooth[-1] == "bear"
    assert regime.mode_smooth(["bull", "bear"], 5)[-1] == "bear"  # tie -> most recent
    assert regime.mode_smooth([], 5) == []


def test_sentiment_rules_on_hand_built_series():
    limit_up = np.concatenate([np.full(260, 40.0), np.full(40, 5.0)])
    premium = np.full(300, 1.0)
    premium[-1] = -1.0
    phases, percentiles = regime.classify_sentiment(limit_up, np.full(300, 3.0), premium)
    assert percentiles[10] is None  # fewer than 30 observations
    assert phases[-1] == "freezing"  # p < 20 and premium <= 0

    hot = np.concatenate([np.full(250, 20.0), [80.0]])
    hot_height = np.concatenate([np.full(250, 3.0), [9.0]])
    phases, percentiles = regime.classify_sentiment(hot, hot_height, np.full(251, 2.0))
    assert percentiles[-1] > 80 and phases[-1] == "climax"

    ramp = np.concatenate([np.full(100, 30.0), np.linspace(30, 60, 30)])
    falling_height = np.linspace(5, 1, 130)  # keeps the day below its 80th height percentile: not climax
    phases, _ = regime.classify_sentiment(ramp, falling_height, np.full(130, 1.0))
    assert "repair" in phases[100:]
    phases, _ = regime.classify_sentiment(ramp[::-1].copy(), falling_height, np.full(130, 1.0))
    assert phases[-1] == "ebb"


def test_current_regime_label_and_explanation(wh: Warehouse, frame: pl.DataFrame):
    state = regime.current_regime(wh)
    parts = state.label.split(" · ")
    assert len(parts) == 4
    assert parts[0] in {"牛市", "熊市", "震荡"} and parts[1] in {"增量", "存量", "缩量"}
    assert parts[2] in {"冰点", "修复", "高潮", "退潮"} and parts[3] in {"小盘占优", "大盘占优", "风格均衡"}
    assert 3 <= len(state.explanation) <= 5
    assert any(char.isdigit() for char in "".join(state.explanation))
    assert state.date == frame["date"].max().isoformat()
    assert state.trend == frame["trend"][-1]


def test_history_response_contract(wh: Warehouse):
    response = regime.regime_history(wh, 120)
    RegimeHistoryResponse.model_validate(response.model_dump())
    assert len(response.points) == 120
    assert response.points[-1].date == response.current.date
    assert response.provenance.mode == "SIM" and response.provenance.coverage > 100
    assert response.index_name == "中证全指"
    assert len(response.rules) >= 5 and any("MA60" in rule for rule in response.rules)


def test_regime_has_no_lookahead(wh: Warehouse, frame: pl.DataFrame, tmp_path: Path):
    cutoff = frame["date"][600]
    copy = Warehouse(tmp_path, wh.dataset)
    for table in ("bars", "index_bars"):
        copy.write(table, wh.read(table).filter(pl.col("date") <= cutoff))
    copy.write("securities", wh.read("securities"))
    short = regime.regime_frame(copy)
    head = frame.filter(pl.col("date") <= cutoff)
    assert short.height == head.height
    for column in ("trend", "liquidity", "sentiment", "style", "limit_up_count", "max_height"):
        assert short[column].to_list() == head[column].to_list(), column
    for column in ("index_close", "breadth_ma20", "amount_ratio", "broken_rate"):
        diff = (short[column].fill_null(0) - head[column].fill_null(0)).abs().max()
        assert diff < 1e-9, column


def test_regime_without_index_bars_falls_back_to_equal_weight(wh: Warehouse, tmp_path: Path):
    copy = Warehouse(tmp_path, wh.dataset)
    copy.write("bars", wh.read("bars"))
    copy.write("securities", wh.read("securities"))
    result = regime.regime_history(copy, 100)
    assert "等权" in result.index_name
    assert result.current.style == "balanced"
    assert any("缺少" in line for line in result.current.explanation)


def test_api_smoke():
    with TestClient(app) as client:
        reply = client.get("/api/v1/regime", params={"days": 90})
        assert reply.status_code == 200, reply.text
        body = reply.json()
        assert len(body["points"]) == 90
        assert body["current"]["label"].count("·") == 3
        assert body["provenance"]["mode"] in {"SIM", "LIVE"}
        assert client.get("/api/v1/regime", params={"days": 10}).status_code == 422
        assert client.get("/api/v1/regime", params={"days": 5000}).status_code == 422
        assert len(client.get("/api/v1/regime").json()["points"]) == 500
