"""MCP server whose tools are generated from the capability registry.

Tool name, description and typed input all come from `CAPABILITIES`; a call runs
`Capability.invoke`, i.e. the very route function HTTP clients hit.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import inspect
from typing import Annotated, Any

from fastapi import HTTPException
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field, ValidationError
from starlette.responses import PlainTextResponse
from starlette.types import Receive, Scope, Send

from ..config import settings
from .capabilities import CAPABILITIES, Capability

MCP_PATH = "/mcp"
INSTRUCTIONS = (
    "A-Share Pulse：A 股盘中市场结构与研究工作台。SIM 模式的数据来自场景模拟器，不是真实行情；"
    "字段为 null 表示未知，不是 0。金额与市值单位为亿元，比率单位为百分点。"
)
# Local research tool: accept loopback names with or without a port. Tests talk
# to it as http://localhost, so no test-only host is allowed here.
_LOCAL_HOSTS = ["127.0.0.1", "localhost", "[::1]"]
SECURITY = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=_LOCAL_HOSTS + [f"{host}:*" for host in _LOCAL_HOSTS],
    allowed_origins=[f"http://{host}" for host in _LOCAL_HOSTS] + [f"http://{host}:*" for host in _LOCAL_HOSTS],
)


def _tool_function(capability: Capability):
    """A function whose *signature* is the input model's fields, so the tool's
    JSON schema is flat and typed. The body validates once more through the
    model, which also applies model-level validators the signature cannot."""
    model = capability.input_model
    parameters = [
        inspect.Parameter(
            name,
            inspect.Parameter.KEYWORD_ONLY,
            default=inspect.Parameter.empty if field.is_required() else field.get_default(call_default_factory=True),
            annotation=Annotated[(field.annotation, *field.metadata, Field(description=field.description))],
        )
        for name, field in (model.model_fields.items() if model else ())
    ]

    async def call(**arguments: Any) -> BaseModel:
        try:
            return await asyncio.to_thread(capability.invoke, arguments)
        except ValidationError as error:
            raise ToolError(f"参数无效：{error.errors(include_url=False)}") from error
        except HTTPException as error:
            raise ToolError(str(error.detail)) from error

    call.__name__ = capability.name
    call.__signature__ = inspect.Signature(parameters, return_annotation=capability.output_model)  # type: ignore[attr-defined]
    return call


def build_mcp_server() -> MCPServer:
    server = MCPServer("pulse", description="A-Share Pulse", instructions=INSTRUCTIONS, version=settings.version)
    for capability in CAPABILITIES:
        if capability.handler is None:
            continue
        server.add_tool(
            _tool_function(capability),
            name=capability.name,
            description=capability.description,
            annotations=ToolAnnotations(
                readOnlyHint=capability.read_only,
                destructiveHint=capability.destructive,
                idempotentHint=capability.idempotent,
                openWorldHint=False,
            ),
        )
    return server


class McpEndpoint:
    """ASGI endpoint for `/mcp`.

    The SDK's session manager can run only once per instance, while a FastAPI app
    (and the test suite) enters its lifespan repeatedly. So each lifespan builds a
    fresh server and this thin gateway forwards requests to the current one.
    """

    def __init__(self) -> None:
        self._app = None

    @asynccontextmanager
    async def run(self):
        server = build_mcp_server()
        self._app = server.streamable_http_app(streamable_http_path=MCP_PATH, transport_security=SECURITY)
        try:
            async with server.session_manager.run():
                yield
        finally:
            self._app = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._app is None:
            await PlainTextResponse("MCP endpoint is not running", status_code=503)(scope, receive, send)
            return
        await self._app(scope, receive, send)
