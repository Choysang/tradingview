"""Agent surface: one registry drives the manifest, llms.txt and the MCP tools."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from backend.app.agent.capabilities import BY_NAME, CAPABILITIES
from backend.app.main import app

PROTOCOL = "2025-06-18"
EXPECTED = {
    "market_overview", "market_events", "screen_stocks", "parse_question", "stock_detail",
    "strategy_catalog", "run_backtest", "compare_backtests", "pattern_scan", "factor_lab",
    "read_watchlist", "write_watchlist", "list_providers", "data_quality",
}
CALLABLE = {c.name for c in CAPABILITIES if c.handler is not None}


@pytest.fixture(scope="module")
def client():
    # MCP's DNS-rebinding guard only admits loopback host names.
    with TestClient(app, base_url="http://localhost") as test_client:
        yield test_client


class Mcp:
    """Minimal Streamable HTTP client: just enough to drive the mounted endpoint."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        self._id = 0
        reply = self.request(
            "initialize",
            {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}},
        )
        self.initialize_result = reply["result"]
        self.headers["mcp-session-id"] = self.session_id
        self.headers["mcp-protocol-version"] = PROTOCOL
        self.client.post("/mcp", headers=self.headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        response = self.client.post(
            "/mcp", headers=self.headers, json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        )
        assert response.status_code == 200, response.text
        self.session_id = response.headers.get("mcp-session-id") or self.headers.get("mcp-session-id")
        data = next(line[6:] for line in response.text.splitlines() if line.startswith("data: "))
        return json.loads(data)

    def call(self, name: str, arguments: dict) -> dict:
        return self.request("tools/call", {"name": name, "arguments": arguments})["result"]


@pytest.fixture(scope="module")
def mcp(client: TestClient) -> Mcp:
    return Mcp(client)


def test_registry_covers_the_agreed_capabilities() -> None:
    names = [c.name for c in CAPABILITIES]
    assert len(names) == len(set(names))
    assert EXPECTED <= set(names)


def test_every_capability_is_a_real_route(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    for capability in CAPABILITIES:
        assert capability.path in paths, capability.name
        assert capability.method.lower() in paths[capability.path], capability.name


def test_manifest_is_generated_from_the_registry(client: TestClient) -> None:
    manifest = client.get("/api/v1/agent/manifest").json()
    assert [tool["name"] for tool in manifest["tools"]] == [c.name for c in CAPABILITIES]
    assert manifest["tools"] == [json.loads(c.definition().model_dump_json()) for c in CAPABILITIES]
    by_name = {tool["name"]: tool for tool in manifest["tools"]}
    assert by_name["screen_stocks"]["input_schema"] == "ScreenerFilterRequest"
    assert by_name["stock_detail"]["input_schema"] is None  # path parameter, not a body


def test_llms_txt_lists_every_capability(client: TestClient) -> None:
    response = client.get("/llms.txt")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    for capability in CAPABILITIES:
        assert f"`{capability.name}`" in response.text
        assert capability.path in response.text
    assert "/mcp" in response.text
    assert "null" in response.text and "SIM" in response.text


def test_mcp_initialize_and_tools_list(mcp: Mcp) -> None:
    assert mcp.initialize_result["serverInfo"]["name"] == "pulse"
    tools = mcp.request("tools/list")["result"]["tools"]
    assert {tool["name"] for tool in tools} == CALLABLE
    by_name = {tool["name"]: tool for tool in tools}
    for tool in tools:
        assert tool["description"] == BY_NAME[tool["name"]].description
        assert tool["inputSchema"]["type"] == "object"
        assert tool["annotations"]["readOnlyHint"] is BY_NAME[tool["name"]].read_only
    # Typed, flat input derived from the Pydantic model.
    assert "pool_type" in by_name["screen_stocks"]["inputSchema"]["properties"]
    assert by_name["stock_detail"]["inputSchema"]["required"] == ["code"]
    assert by_name["compare_backtests"]["inputSchema"]["required"] == ["strategy_id", "codes"]
    assert by_name["market_events"]["inputSchema"]["properties"]["limit"]["maximum"] == 300


def test_mcp_annotations_reflect_side_effects(mcp: Mcp) -> None:
    tools = {t["name"]: t["annotations"] for t in mcp.request("tools/list")["result"]["tools"]}
    for pure in ("screen_stocks", "parse_question", "factor_lab", "compare_backtests", "market_overview"):
        assert tools[pure]["readOnlyHint"] is True, pure
    assert tools["run_backtest"]["readOnlyHint"] is False
    assert tools["run_backtest"]["idempotentHint"] is False
    assert tools["write_watchlist"]["readOnlyHint"] is False
    assert tools["write_watchlist"]["idempotentHint"] is True
    assert [name for name, hint in tools.items() if hint["destructiveHint"]] == ["write_watchlist"]


def test_testserver_host_is_not_allowed_in_production_config() -> None:
    from backend.app.agent.mcp_server import SECURITY

    assert not any("testserver" in host for host in SECURITY.allowed_hosts)


def test_mcp_tool_call_returns_valid_json(mcp: Mcp) -> None:
    result = mcp.call("parse_question", {"text": "首板 换手低于15%"})
    assert not result.get("isError")
    payload = json.loads(result["content"][0]["text"])
    assert payload["filters"]["limit_streak_exact"] == [1]
    assert payload["filters"]["turnover_today_max"] == 15
    assert result["structuredContent"]["summary"] == payload["summary"]


def test_mcp_call_matches_the_http_route(mcp: Mcp, client: TestClient) -> None:
    result = mcp.call("screen_stocks", {"limit": 3, "pool_type": "limit_up"})
    assert not result.get("isError")
    records = result["structuredContent"]["records"]
    assert 0 < len(records) <= 3
    assert all(record["is_limit_up"] for record in records)
    overview = mcp.call("market_overview", {})
    assert overview["structuredContent"]["total_stocks"] > 0
    assert json.loads(overview["content"][0]["text"])["provenance"]["mode"] in {"LIVE", "SIM"}


def test_mcp_reports_bad_input_as_tool_errors(mcp: Mcp) -> None:
    unknown = mcp.call("stock_detail", {"code": "999999"})
    assert unknown["isError"] is True
    assert "999999" in unknown["content"][0]["text"]
    invalid = mcp.call("market_events", {"limit": 0})
    assert invalid["isError"] is True


def test_invoke_validates_and_dispatches() -> None:
    detail = BY_NAME["market_events"].invoke({"limit": 5})
    assert len(detail.events) <= 5
    with pytest.raises(Exception):
        BY_NAME["market_events"].invoke({"limit": 999})
    with pytest.raises(NotImplementedError):
        BY_NAME["stream_ticks"].invoke()
