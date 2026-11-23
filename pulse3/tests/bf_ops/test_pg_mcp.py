"""MCP endpoint (#65) on a real PostgreSQL: tools are the manifest's read-only GET operations, calls are the REST routes."""
from __future__ import annotations

import json
import uuid

import pytest

from pulse.api import routes_bf_mcp as R
from pulse.application import agent_access

pytestmark = pytest.mark.pg


class Mcp:
    def __init__(self, api, cookies: dict | None = None):
        self.api, self.cookies, self._id = api, api.cookies() if cookies is None else cookies, 0

    def raw(self, body, headers: dict | None = None):
        kw = {"content": body} if isinstance(body, (bytes, str)) else {"json": body}
        return self.api.c.post("/mcp", cookies=self.cookies, headers=headers or {}, **kw)

    def rpc(self, method: str, params: dict | None = None):
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        r = self.raw(msg)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == self._id and body["jsonrpc"] == "2.0"
        return body

    def call(self, name: str, **arguments):
        return self.rpc("tools/call", {"name": name, "arguments": arguments})


@pytest.fixture()
def mcp(fresh, api_on):
    conn, dsn = fresh
    return Mcp(api_on(dsn, uuid.uuid4()))


def manifest_of(mcp: Mcp) -> dict:
    return mcp.api.c.get("/v1/x/bf/ops/manifest").json()["data"]


def strip_meta(body: dict) -> dict:
    out = json.loads(json.dumps(body))
    for k in ("request_id", "generated_at", "trace_id"):
        out["meta"].pop(k, None)
    return out


# ---- protocol -------------------------------------------------------------------------------------------------------------------
def test_initialize_ping_notification_and_no_server_stream(mcp):
    init = mcp.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})["result"]
    assert init["protocolVersion"] == "2025-03-26" and init["capabilities"] == {"tools": {"listChanged": False}}
    assert mcp.rpc("initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"] == "2025-06-18"
    assert mcp.rpc("ping")["result"] == {}
    r = mcp.raw({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202 and r.content == b""
    assert mcp.api.c.get("/mcp").status_code == 405 and mcp.api.c.delete("/mcp").status_code == 405
    assert mcp.rpc("resources/list")["error"]["code"] == -32601


def test_malformed_requests_are_refused_without_a_traceback(mcp):
    assert mcp.raw("{not json").status_code == 400
    assert mcp.raw("{not json").json()["error"]["code"] == -32700
    assert mcp.raw([{"jsonrpc": "2.0", "id": 1, "method": "ping"}]).status_code == 400          # batches are not supported
    assert mcp.raw({"jsonrpc": "1.0", "id": 1, "method": "ping"}).status_code == 400
    assert mcp.raw({"jsonrpc": "2.0", "id": True, "method": "ping"}).status_code == 400
    assert mcp.raw(b"x" * (R.MAX_BODY + 1)).status_code == 413
    r = mcp.raw({"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    ok = mcp.raw({"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Origin": "http://localhost:8200"})
    assert ok.status_code == 200 and ok.json()["result"] == {}


# ---- tools/list == manifest -------------------------------------------------------------------------------------------------------
def test_tools_list_is_the_manifests_read_only_operations_and_llms_txt_marks_the_same_set(mcp):
    manifest = manifest_of(mcp)
    ops = {o["operation_id"]: o for o in manifest["operations"]}
    tools = mcp.rpc("tools/list")["result"]["tools"]
    names = [t["name"] for t in tools]
    assert len(names) == len(set(names)) and len(names) >= 50
    assert set(names) == {oid for oid in agent_access.MCP_TOOL_IDS if oid in ops}
    assert [oid for oid in agent_access.MCP_TOOL_IDS if oid not in ops] == [], "policy ids that the manifest does not list"
    for t in tools:
        o, meta = ops[t["name"]], t["_meta"]["pulse"]
        assert (o["method"], meta["method"]) == ("GET", "GET"), "only read operations may be tools"
        assert (meta["path"], meta["auth"], meta["source"]) == (o["path"], o["auth"], o["source"])
        assert t["annotations"]["readOnlyHint"] is True and t["annotations"]["destructiveHint"] is False
        schema = t["inputSchema"]
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert set(agent_access.PATH_PARAM.findall(o["path"])) <= set(schema["required"])
        assert "$ref" not in json.dumps(schema), "schemas are self-contained"
    # nothing user-private or state-changing is offered
    assert not {n for n in names if n.startswith(("save", "create", "delete", "schedule", "run", "import", "propose", "register", "send"))}
    assert not {"getAccountProjection", "listWatchlists", "listAnnotations", "getLayout", "queryAccountOrders",
                 "listSavedScreens", "queryScreenRun", "listFillrCustomRuns", "getFillrCustomRun"} & set(names)
    text = mcp.api.c.get("/llms.txt").text
    marked = {ln.split(" - ")[1].split(":")[0].split(" ")[0] for ln in text.splitlines() if ln.startswith("- GET /") and ln.endswith("[mcp]")}
    assert marked == set(names)
    assert "POST /mcp" in text


def test_tool_schemas_come_from_the_operations_own_request_definitions(mcp):
    tools = {t["name"]: t for t in mcp.rpc("tools/list")["result"]["tools"]}
    release = tools["getRelease"]["inputSchema"]
    assert release["required"] == ["release_id"] and release["properties"]["release_id"]["format"] == "uuid"
    ext = tools["queryMarketToday"]["inputSchema"]
    assert ext["required"] == ["release_id"] and set(ext["properties"]) == {"release_id", "account_id"}
    bars = tools["getStockBars"]["inputSchema"]
    assert {"instrument_id", "timeframe", "start", "end"} <= set(bars["required"])


# ---- tools/call -------------------------------------------------------------------------------------------------------------------
def test_call_returns_the_rest_payload_for_a_public_read(mcp):
    rest = mcp.api.c.get("/v1/data-status")
    assert rest.status_code == 200, rest.text
    res = mcp.call("getDataStatus")["result"]
    assert res["isError"] is False
    assert strip_meta(res["structuredContent"]) == strip_meta(rest.json())
    assert json.loads(res["content"][0]["text"]) == res["structuredContent"]


def test_call_with_query_parameters_and_a_missing_resource_is_a_tool_error_with_the_rest_error_body(mcp):
    rest = mcp.api.c.get("/v1/releases/current", params={"slot_key": "nope"})
    assert rest.status_code == 404
    res = mcp.call("getCurrentRelease", slot_key="nope")["result"]
    assert res["isError"] is True
    assert res["structuredContent"]["error"]["code"] == rest.json()["error"]["code"] == "NOT_FOUND"
    rid = str(uuid.uuid4())
    assert mcp.call("getRelease", release_id=rid)["result"]["structuredContent"]["error"]["code"] == "NOT_FOUND"   # path parameter
    page = mcp.call("listStrategies", role="x")["result"]
    rest_page = mcp.api.c.get("/v1/strategies", params={"role": "x"})
    assert page["isError"] == (rest_page.status_code >= 400)
    assert strip_meta(page["structuredContent"]) == strip_meta(rest_page.json())


def test_extension_and_backfill_tools_validate_through_their_own_routes(mcp):
    rid = str(uuid.uuid4())
    res = mcp.call("queryMarketToday", release_id=rid)["result"]                       # extension request travels as query text
    rest = mcp.api.c.get("/v1/x/query-market-today", params={"release_id": rid}, cookies=mcp.cookies)
    assert res["isError"] is True and res["structuredContent"]["error"]["code"] == rest.json()["error"]["code"]
    found = mcp.call("instrument_search_v1_x_bf_board_instrument_search_get", q="600", limit=3)["result"]   # backfill route, OpenAPI params
    rest_found = mcp.api.c.get("/v1/x/bf/board/instrument-search", params={"q": "600", "limit": "3"}, cookies=mcp.cookies)
    assert found["isError"] == (rest_found.status_code >= 400)
    assert strip_meta(found["structuredContent"]) == strip_meta(rest_found.json())
    assert R._query_value(["a", "b"], json_params=False) == "a,b" and R._query_value(["a", "b"], json_params=True) == '["a","b"]'
    assert R._query_value(True, False) == "true" and R._query_value({"k": "é"}, True) == '{"k":"é"}'
    status = mcp.call("queryDatasetStatus", dataset_id="nope", page={"limit": 5})["result"]            # object argument -> JSON text
    assert status["structuredContent"]["meta"]["contract_version"]


def test_unknown_tool_and_bad_arguments_are_protocol_errors(mcp):
    err = mcp.call("saveAlertRule")["error"]
    assert err["code"] == -32602 and "unknown tool" in err["message"]
    assert mcp.call("getAccountProjection", account_id=str(uuid.uuid4()))["error"]["code"] == -32602   # a read, but not offered
    assert mcp.rpc("tools/call", {"arguments": {}})["error"]["code"] == -32602
    assert mcp.rpc("tools/call", {"name": "getDataStatus", "arguments": []})["error"]["code"] == -32602
    unknown = mcp.call("getDataStatus", surprise=1)["error"]
    assert unknown["code"] == -32602 and "surprise" in unknown["message"]
    assert mcp.call("getRelease")["error"]["code"] == -32602                                           # missing path argument
    for evil in ("../v1/accounts", "a/b", "..", ""):
        assert mcp.call("getRelease", release_id=evil)["error"]["code"] == -32602


# ---- auth -------------------------------------------------------------------------------------------------------------------------
def test_session_tools_follow_the_rest_principal_rules(fresh, api_on, monkeypatch):
    conn, dsn = fresh
    me = uuid.uuid4()
    api = api_on(dsn, me)
    with_cookie = Mcp(api)
    rest_ok = api.c.get("/v1/x/bf/fusion/status", cookies=api.cookies())
    mine = with_cookie.call("getFusionStatus")["result"]
    assert mine["isError"] == (rest_ok.status_code >= 400) and mine["structuredContent"]["meta"]["contract_version"]
    assert mine["structuredContent"].get("error", {}).get("code") != "AUTH_REQUIRED"
    for cookies in ({}, {"pulse_session": "tok-unknown"}):                                  # no session / a session nobody issued
        anon = Mcp(api, cookies)
        assert anon.rpc("tools/list")["result"]["tools"], "listing is public like the manifest"
        assert anon.call("getDataStatus")["result"]["isError"] is False                       # a public read needs no session
        denied = anon.call("getFusionStatus")["result"]
        assert denied["isError"] is True and denied["structuredContent"]["error"]["code"] == "AUTH_REQUIRED"
    assert api.c.get("/v1/x/bf/fusion/status").status_code == 403                             # the same refusal over REST


def test_dev_principal_behaviour_is_unchanged(fresh, api_on, monkeypatch):
    conn, dsn = fresh
    api = api_on(dsn, uuid.uuid4())
    anon = Mcp(api, {})
    assert anon.call("getFusionStatus")["result"]["structuredContent"]["error"]["code"] == "AUTH_REQUIRED"
    monkeypatch.setenv("PULSE_ENV", "dev")
    monkeypatch.setenv("PULSE_DEV_PRINCIPAL", str(uuid.UUID(int=7)))
    dev = anon.call("getFusionStatus")["result"]
    assert dev["structuredContent"].get("error", {}).get("code") != "AUTH_REQUIRED"
    monkeypatch.setenv("PULSE_ENV", "prod")                                                 # a stray dev principal outside dev is refused
    broken = anon.call("getFusionStatus")["result"]                                          # the route fails: a tool error, no detail leaked
    assert broken["isError"] is True and broken["structuredContent"]["error"]["code"] == "UPSTREAM_ERROR"
    assert "RuntimeError" not in broken["content"][0]["text"] and "PULSE_DEV_PRINCIPAL" not in broken["content"][0]["text"]
