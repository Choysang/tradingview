"""HTTP contract tests against the real FastAPI app."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.core.storage import storage


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_health_reports_provenance(client: TestClient) -> None:
    body = client.get("/api/v1/health").json()
    assert body["status"] == "ok"
    assert body["provenance"]["mode"] in {"LIVE", "SIM"}
    assert body["provenance"]["coverage"] > 0


def test_overview_shape(client: TestClient) -> None:
    body = client.get("/api/v1/market/overview").json()
    assert body["total_stocks"] == body["provenance"]["coverage"]
    assert isinstance(body["limit_ladder_tiers"], list)
    assert isinstance(body["market_curve"], list)


def test_screener_rejects_inverted_range(client: TestClient) -> None:
    response = client.post("/api/v1/screener/query", json={"price_min": 100, "price_max": 1})
    assert response.status_code == 422


def test_screener_rejects_unknown_field(client: TestClient) -> None:
    response = client.post("/api/v1/screener/query", json={"not_a_field": 1})
    assert response.status_code == 422


def test_screener_returns_records(client: TestClient) -> None:
    response = client.post("/api/v1/screener/query", json={"limit": 5})
    assert response.status_code == 200
    body = response.json()
    assert len(body["records"]) <= 5
    record = body["records"][0]
    assert record["code"].isdigit() and len(record["code"]) == 6
    assert record["board"] in {"10cm", "20cm", "30cm"}
    assert len(record["factors"]) == 4


def test_stock_detail_and_errors(client: TestClient) -> None:
    code = client.post("/api/v1/screener/query", json={"limit": 1}).json()["records"][0]["code"]
    body = client.get(f"/api/v1/stocks/{code}").json()
    assert body["stock"]["code"] == code
    assert body["bars"]
    assert body["history_provenance"]["mode"] in {"LIVE", "SIM"}
    assert client.get("/api/v1/stocks/000000").status_code == 404
    assert client.get("/api/v1/stocks/abc").status_code == 422


def test_strategy_catalog(client: TestClient) -> None:
    strategies = client.get("/api/v1/strategies").json()["strategies"]
    assert len(strategies) >= 3
    for strategy in strategies:
        assert strategy["methodology"]
        assert strategy["source_url"].startswith("https://")
        assert set(strategy["factor_weights"]) == {"momentum", "liquidity", "quality", "trend"}


def test_watchlist_roundtrip(client: TestClient) -> None:
    original = client.get("/api/v1/watchlist").json()["codes"]
    try:
        saved = client.put("/api/v1/watchlist", json={"codes": ["600519", "000001", "600519"]}).json()
        assert saved["codes"] == ["600519", "000001"]
        assert client.get("/api/v1/watchlist").json()["codes"] == ["600519", "000001"]
    finally:
        storage.save_watchlist(original)


def test_backtest_flow(client: TestClient) -> None:
    code = client.post("/api/v1/screener/query", json={"limit": 1}).json()["records"][0]["code"]
    created = client.post(
        "/api/v1/research/backtests",
        json={"strategy_id": "momentum_ignition", "code": code, "lookback": 15, "holding_days": 4},
    )
    assert created.status_code == 200
    body = created.json()
    assert body["equity"] and body["assumptions"]
    run_id = body["run_id"]
    assert client.get(f"/api/v1/research/backtests/{run_id}").json()["run_id"] == run_id
    assert any(item["run_id"] == run_id for item in client.get("/api/v1/research/backtests").json()["runs"])
    assert client.get("/api/v1/research/backtests/nope").status_code == 404


def test_backtest_unknown_strategy(client: TestClient) -> None:
    response = client.post("/api/v1/research/backtests", json={"strategy_id": "unknown_strategy"})
    assert response.status_code == 404


def test_batch_backtest_compares_codes(client: TestClient) -> None:
    codes = [row["code"] for row in client.post("/api/v1/screener/query", json={"limit": 3}).json()["records"]]
    response = client.post(
        "/api/v1/research/backtests/batch",
        json={"strategy_id": "momentum_ignition", "codes": codes, "lookback": 15, "holding_days": 4},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["requested"] == len(codes)
    assert body["completed"] == len(codes)
    assert len(body["rows"]) == len(codes)
    assert body["median_return"] is not None
    for row in body["rows"]:
        assert row["code"] in codes
        assert row["error"] is None
        assert row["excess_return"] is not None


def test_batch_backtest_validates_input(client: TestClient) -> None:
    base = {"strategy_id": "momentum_ignition"}
    assert client.post("/api/v1/research/backtests/batch", json={**base, "codes": []}).status_code == 422
    assert client.post("/api/v1/research/backtests/batch", json={**base, "codes": ["abc"]}).status_code == 422
    assert client.post(
        "/api/v1/research/backtests/batch", json={"strategy_id": "unknown_strategy", "codes": ["002085"]}
    ).status_code == 404


def test_providers_declare_every_boundary(client: TestClient) -> None:
    body = client.get("/api/v1/providers").json()
    assert {provider["key"] for provider in body["providers"]} == {
        "scenario",
        "akshare",
        "duckdb",
        "sqlite",
    }
    active = {provider["key"] for provider in body["providers"] if provider["active"]}
    assert {"scenario", "sqlite"} <= active
    for provider in body["providers"]:
        assert provider["detail"], provider["key"]


def test_quality_reports_real_coverage(client: TestClient) -> None:
    body = client.get("/api/v1/quality").json()
    rows = body["rows"]
    assert rows > 0
    fields = {field["field"]: field for field in body["fields"]}
    assert fields["code"]["coverage"] == 100
    seal = fields["seal_to_amount_ratio"]
    # Only sealed boards carry a seal ratio; everything else stays null.
    assert seal["coverage"] < 100
    assert seal["missing"] > 0
    assert abs(seal["coverage"] - (rows - seal["missing"]) / rows * 100) < 0.2


def test_mode_switch_is_explicit(client: TestClient) -> None:
    body = client.post("/api/v1/admin/mode", json={"mode": "SIM"}).json()
    assert body["mode"] == "SIM"
    assert client.post("/api/v1/admin/mode", json={"mode": "LIVE"}).json()["mode"] in {"LIVE", "SIM"}
    client.post("/api/v1/admin/mode", json={"mode": "SIM"})


def test_agent_manifest_lists_real_routes(client: TestClient) -> None:
    manifest = client.get("/api/v1/agent/manifest").json()
    paths = set(client.get("/openapi.json").json()["paths"])
    assert manifest["tools"]
    for tool in manifest["tools"]:
        assert tool["path"] in paths


def test_records_carry_plugin_columns(client: TestClient) -> None:
    body = client.post("/api/v1/screener/query", json={"limit": 5, "pool_type": "limit_up"}).json()
    sealed = [row for row in body["records"] if row["is_limit_up"]]
    assert sealed, "fixture must contain limit-up names"
    for row in sealed:
        assert row["seal_span_minutes"] is not None
        assert isinstance(row["early_seal"], bool)


def test_records_carry_plugin_columns(client: TestClient) -> None:
    body = client.post("/api/v1/screener/query", json={"limit": 5, "pool_type": "limit_up"}).json()
    sealed = [row for row in body["records"] if row["is_limit_up"]]
    assert sealed, "fixture must contain limit-up names"
    for row in sealed:
        assert row["seal_span_minutes"] is not None
        assert isinstance(row["early_seal"], bool)
