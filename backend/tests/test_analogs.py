"""Analog forecast on a synthetic warehouse built in a temp dir (no network)."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.research import analogs
from backend.app.warehouse.store import Warehouse

DAYS = 420


def _weekdays(count: int) -> list[date]:
    days, cursor = [], date(2025, 1, 1)
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def make_warehouse(root, codes: list[str], days: int = DAYS, dataset: str = "sim", short: dict[str, int] | None = None) -> Warehouse:
    rng = np.random.default_rng(7)
    calendar = _weekdays(days)
    rows: dict[str, list] = {key: [] for key in ("code", "date", "open", "high", "low", "close", "volume")}
    for code in codes:
        length = (short or {}).get(code, days)
        price = 20.0
        for index in range(length):
            previous = price
            price = max(1.0, price * (1 + rng.normal(0.0004, 0.02)))
            rows["code"].append(code)
            rows["date"].append(calendar[days - length + index])
            rows["open"].append(previous)
            rows["high"].append(max(previous, price) * 1.01)
            rows["low"].append(min(previous, price) * 0.99)
            rows["close"].append(price)
            rows["volume"].append(float(rng.uniform(1e6, 5e6)))
    wh = Warehouse(root, dataset)
    wh.write("bars", pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date)))
    return wh


@pytest.fixture
def wh(tmp_path) -> Warehouse:
    codes = [f"60{index:04d}" for index in range(25)]
    return make_warehouse(tmp_path / "sim", codes, short={"600024": 40})


def test_quantiles_are_monotone_for_every_day(wh) -> None:
    result = analogs.forecast("600000", 20, wh)
    assert result.horizon == 20 and len(result.path) == 20
    assert [point.day for point in result.path] == list(range(1, 21))
    for point in result.path:
        assert point.p10 <= point.p25 <= point.p50 <= point.p75 <= point.p90
    # The dispersion grows with the horizon: day 20 is wider than day 1.
    assert result.path[-1].p90 - result.path[-1].p10 > result.path[0].p90 - result.path[0].p10


def test_summary_statistics_and_examples(wh) -> None:
    result = analogs.forecast("600000", 10, wh)
    assert 0 < result.neighbors <= analogs.NEIGHBORS
    assert result.prob_up is not None and 0 <= result.prob_up <= 1
    assert result.mean_return is not None
    assert len(result.examples) == analogs.EXAMPLES
    distances = [item.distance for item in result.examples]
    assert distances == sorted(distances)
    assert set(result.features) == set(analogs.FEATURES)
    assert all(value is not None for value in result.features.values())
    assert any("不是预测" in note for note in result.notes)
    assert any("Kronos" in note for note in result.notes)


def test_forecast_is_deterministic(wh) -> None:
    first = analogs.forecast("600003", 20, wh).model_dump()
    second = analogs.forecast("600003", 20, wh).model_dump()
    for payload in (first, second):
        payload["provenance"].pop("as_of")
    assert first == second


def test_sampling_is_deterministic_and_disclosed(wh, monkeypatch) -> None:
    monkeypatch.setattr(analogs, "MAX_CANDIDATES", 2_000)
    first = analogs.forecast("600001", 20, wh)
    second = analogs.forecast("600001", 20, wh)
    assert first.path == second.path and first.examples == second.examples
    assert any("抽样" in note for note in first.notes)


def test_target_stocks_recent_rows_are_excluded(tmp_path) -> None:
    """With one stock only, every neighbour must be older than 120 bars before today."""
    single = make_warehouse(tmp_path / "single", ["600000"])
    result = analogs.forecast("600000", 20, single)
    calendar = _weekdays(DAYS)
    cutoff = calendar[DAYS - 1 - analogs.SAME_CODE_MIN_AGE].isoformat()
    assert result.examples
    assert all(item.date <= cutoff for item in result.examples)
    assert all(item.code == "600000" for item in result.examples)


def test_neighbours_of_one_stock_do_not_overlap_in_time(tmp_path) -> None:
    single = make_warehouse(tmp_path / "single2", ["600000"])
    result = analogs.forecast("600000", 20, single)
    calendar = {day.isoformat(): index for index, day in enumerate(_weekdays(DAYS))}
    positions = sorted(calendar[item.date] for item in result.examples)
    assert all(b - a >= 20 for a, b in zip(positions, positions[1:]))


def test_unknown_code_raises_lookup(wh) -> None:
    with pytest.raises(LookupError):
        analogs.forecast("688888", 20, wh)


def test_short_history_returns_empty_forecast_not_an_error(wh) -> None:
    result = analogs.forecast("600024", 20, wh)
    assert result.neighbors == 0 and result.path == [] and result.prob_up is None
    assert any("60" in note for note in result.notes)


@pytest.mark.parametrize("horizon", [4, 61])
def test_horizon_bounds(wh, horizon: int) -> None:
    with pytest.raises(ValueError):
        analogs.forecast("600000", horizon, wh)


def test_provenance_follows_the_dataset(tmp_path) -> None:
    live = make_warehouse(tmp_path / "live", [f"60{index:04d}" for index in range(8)], dataset="live")
    result = analogs.forecast("600000", 20, live)
    assert result.provenance.mode == "LIVE" and result.provenance.status == "ready"
    assert not any("SIM" in note for note in result.notes)
    sim = make_warehouse(tmp_path / "sim2", [f"60{index:04d}" for index in range(8)], dataset="sim")
    simulated = analogs.forecast("600000", 20, sim)
    assert simulated.provenance.mode == "SIM"
    assert any("SIM" in note for note in simulated.notes)


def test_cache_follows_warehouse_changes(tmp_path) -> None:
    store = make_warehouse(tmp_path / "grow", [f"60{index:04d}" for index in range(8)], days=300)
    before = analogs.forecast("600000", 20, store)
    extra = make_warehouse(tmp_path / "other", ["600100"], days=300)
    store.write("bars", extra.read("bars").drop("ingested_at"))
    after = analogs.forecast("600100", 20, store)
    assert after.code == "600100" and before.code == "600000"


def test_analogs_endpoint(wh, monkeypatch) -> None:
    monkeypatch.setattr(analogs, "active", lambda: wh)
    with TestClient(app) as client:
        ok = client.get("/api/v1/stocks/600005/analogs", params={"horizon": 15})
        assert ok.status_code == 200
        body = ok.json()
        assert body["code"] == "600005" and body["horizon"] == 15 and len(body["path"]) == 15
        assert client.get("/api/v1/stocks/688888/analogs").status_code == 404
        assert client.get("/api/v1/stocks/600005/analogs", params={"horizon": 2}).status_code == 422
        assert client.get("/api/v1/stocks/60abc/analogs").status_code == 422


def test_target_under_1500ms_on_the_sim_dataset() -> None:
    """The real SIM warehouse (266 stocks x 780 days); warm and cold timings both stay in budget."""
    import time

    from backend.app.warehouse.store import active

    store = active()
    code = store.scan("bars").select("code").head(1).collect()["code"][0]
    started = time.perf_counter()
    analogs.forecast(code, 20, store)
    cold = time.perf_counter() - started
    started = time.perf_counter()
    analogs.forecast(code, 20, store)
    warm = time.perf_counter() - started
    assert warm < 1.5 and cold < 5.0
