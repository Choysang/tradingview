"""MCP endpoint ``/mcp`` (legacy triage #65): read-only tools for outside agents, JSON-RPC 2.0 over streamable HTTP.

Transport: ``POST /mcp`` with one JSON-RPC message; a request gets one ``application/json`` response, a notification gets 202.
There are no server-initiated messages, so ``GET /mcp`` is 405 (the spec allows it) and there is no session id (stateless).
Methods: ``initialize``, ``notifications/*``, ``ping``, ``tools/list``, ``tools/call``; anything else is -32601.
Tools: ``pulse.application.agent_access.build_catalogue`` over the *same manifest* ``GET /v1/x/bf/ops/manifest`` and ``/llms.txt``
serve, so the three cannot drift. Only GET operations are tools; there are no write tools.
A call is dispatched in-process to the REST route itself (ASGI, same app): same query validation, same principal rules
(``pulse_session`` cookie of the MCP request is forwarded, nothing else; no cookie means ``PULSE_DEV_PRINCIPAL`` or a 403, exactly
as for REST) and the same contract-validated ``{meta, data}`` / ``{meta, error}`` body, returned as ``structuredContent``.
A route that answers 4xx/5xx gives ``isError: true`` with that error body, never a JSON-RPC fault; faults (-32602 etc.) are for
unknown tools and arguments that cannot be turned into a request.
Guards: a request that carries a non-loopback ``Origin`` is refused (DNS-rebinding defence; the server binds loopback only);
bodies over ``MAX_BODY`` are refused; path arguments cannot contain ``/``, ``\\`` or ``..``.
Not covered: write tools (a later decision), SSE streaming, sessions, resources/prompts.
"""
from __future__ import annotations

import ipaddress
import json
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

from pulse import contracts
from pulse.api import deps
from pulse.application import agent_access, bf_ops_service

MCP_PATH = "/mcp"
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 256 * 1024
MAX_PATH_ARG = 200
CALL_TIMEOUT_SECONDS = 60.0
INSTRUCTIONS = ("Pulse3 research workbench for China A-share main-board analysis; read-only tools, not investment advice. "
                "Every result is the REST envelope {meta, data} (or {meta, error}); check meta.quality_status and meta.freshness_state. "
                "Money, prices and ratios are decimal strings; null means missing, never 0. Stocks are addressed by instrument_id "
                "(UUID); resolve a code or name with instrument_search_v1_x_bf_board_instrument_search_get first. Pages continue with "
                "meta.next_cursor.")

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602

router = APIRouter()


# ---- JSON-RPC helpers ---------------------------------------------------------------------------------------------------------------
def _fault(rpc_id: Any, code: int, message: str, status: int = 200, data: Any = None) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "error": error}, status_code=status)


def _ok(rpc_id: Any, result: Any) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": result})


def _is_loopback_origin(origin: str) -> bool:
    host = (urlsplit(origin).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# ---- catalogue (cached per app: manifest rows and OpenAPI only change with a migration or a restart) -----------------------------
def _load_catalogue(app) -> agent_access.Catalogue:
    cached = getattr(app.state, "mcp_catalogue", None)
    if cached is not None:
        return cached
    with deps.pool().connection() as conn:                    # released before any tool call: a call needs its own connection
        manifest = bf_ops_service.build_manifest(conn, app.openapi()["paths"])
    catalogue = agent_access.build_catalogue(manifest, app.openapi()["paths"])
    app.state.mcp_catalogue = catalogue
    return catalogue


# ---- tools/call ---------------------------------------------------------------------------------------------------------------------
class _BadArguments(ValueError):
    pass


def _query_value(value: Any, json_params: bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        if not json_params and isinstance(value, list) and all(isinstance(x, (str, int, float)) and not isinstance(x, bool) for x in value):
            return ",".join(str(x) for x in value)              # backfill routes take id lists as one comma-separated string
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _request_parts(tool: agent_access.Tool, arguments: dict[str, Any]) -> tuple[str, dict[str, str]]:
    allowed = set(tool.input_schema.get("properties", {}))
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise _BadArguments("unknown arguments: " + ", ".join(unknown))
    path = tool.path
    for name in tool.path_params:
        value = arguments.get(name)
        if value is None or not isinstance(value, (str, int)) or isinstance(value, bool):
            raise _BadArguments(f"path argument {name} is required (string)")
        text = str(value)
        if not text or len(text) > MAX_PATH_ARG or "/" in text or "\\" in text or ".." in text:
            raise _BadArguments(f"path argument {name} is not a valid identifier")
        path = path.replace("{" + name + "}", quote(text, safe=""))
    query = {k: _query_value(v, tool.json_params) for k, v in arguments.items() if k in tool.query_params and v is not None}
    return path, query


async def _dispatch(request: Request, tool: agent_access.Tool, path: str, query: dict[str, str]) -> dict[str, Any]:
    headers = {}
    if request.headers.get("cookie"):
        headers["cookie"] = request.headers["cookie"]            # the caller's own session decides what the route returns
    transport = httpx.ASGITransport(app=request.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://pulse.internal", timeout=CALL_TIMEOUT_SECONDS) as client:
        response = await client.get(path, params=query, headers=headers)
    try:
        body = response.json()
    except ValueError:
        body = None
    is_error = response.status_code >= 400
    if not isinstance(body, dict):
        is_error = True
        body = {"error": {"code": "NON_JSON_RESPONSE" if response.status_code < 400 else "UPSTREAM_ERROR",
                          "message": f"route answered HTTP {response.status_code} without a JSON body", "retryable": False}}
    result: dict[str, Any] = {"content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False, separators=(",", ":"))}],
                              "structuredContent": body, "isError": is_error}
    return result


async def _tools_call(request: Request, rpc_id: Any, params: Any) -> JSONResponse:
    if not isinstance(params, dict) or not isinstance(params.get("name"), str):
        return _fault(rpc_id, INVALID_PARAMS, "tools/call needs params.name")
    arguments = params.get("arguments", {})
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _fault(rpc_id, INVALID_PARAMS, "params.arguments must be an object")
    catalogue = await run_in_threadpool(_load_catalogue, request.app)
    tool = catalogue.by_name().get(params["name"])
    if tool is None:
        return _fault(rpc_id, INVALID_PARAMS, f"unknown tool: {params['name'][:80]}")
    try:
        path, query = _request_parts(tool, arguments)
    except _BadArguments as e:
        return _fault(rpc_id, INVALID_PARAMS, str(e))
    return _ok(rpc_id, await _dispatch(request, tool, path, query))


# ---- endpoint -----------------------------------------------------------------------------------------------------------------------
@router.get(MCP_PATH, include_in_schema=False)
@router.delete(MCP_PATH, include_in_schema=False)
def mcp_no_stream() -> Response:
    return Response(status_code=405, headers={"Allow": "POST"})


@router.post(MCP_PATH, include_in_schema=False)
async def mcp(request: Request) -> Response:
    origin = request.headers.get("origin")
    if origin is not None and not _is_loopback_origin(origin):
        return _fault(None, INVALID_REQUEST, "Origin not allowed", status=403)
    raw = await request.body()
    if len(raw) > MAX_BODY:
        return _fault(None, INVALID_REQUEST, "request body too large", status=413)
    try:
        message = json.loads(raw)
    except ValueError:
        return _fault(None, PARSE_ERROR, "body is not valid JSON", status=400)
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return _fault(None, INVALID_REQUEST, "expected one JSON-RPC 2.0 request or notification object", status=400)
    method, params = message["method"], message.get("params")
    if "id" not in message:                                       # a notification (e.g. notifications/initialized): accepted, no body
        return Response(status_code=202)
    rpc_id = message["id"]
    if isinstance(rpc_id, bool) or not isinstance(rpc_id, (str, int)):
        return _fault(None, INVALID_REQUEST, "id must be a string or an integer", status=400)

    if method == "initialize":
        asked = params.get("protocolVersion") if isinstance(params, dict) else None
        version = asked if asked in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        return _ok(rpc_id, {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                            "serverInfo": {"name": "pulse3", "version": contracts.CONTRACT_VERSION}, "instructions": INSTRUCTIONS})
    if method == "ping":
        return _ok(rpc_id, {})
    if method == "tools/list":
        catalogue = await run_in_threadpool(_load_catalogue, request.app)
        return _ok(rpc_id, {"tools": [t.wire() for t in catalogue.tools]})
    if method == "tools/call":
        return await _tools_call(request, rpc_id, params)
    return _fault(rpc_id, METHOD_NOT_FOUND, f"method not found: {method[:80]}")
