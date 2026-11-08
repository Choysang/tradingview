"""FastAPI entry point. Owns CORS, the tick loop and the single API prefix."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import logging
import threading

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from contracts.schema import StreamEvent
from .api import admin, crowd, data, events, lab, market, notify, ops, picks, playbooks, regime, research, screener, strategies, stream, watchlist
from .agent.llms import llms_txt
from .agent.mcp_server import MCP_PATH, McpEndpoint
from .config import ROOT, settings
from .core.bus import bus
from .core.engine import engine
from . import scheduler

logger = logging.getLogger("pulse")
TICK_ERRORS_BEFORE_WARN = 3
PREWARM_PATTERN_LIMIT = 40  # what the frontend requests; the scan cache is keyed by limit


async def _ticker() -> None:
    failures = 0
    while True:
        await asyncio.sleep(settings.refresh_seconds)
        try:
            produced = await asyncio.to_thread(engine().refresh)
            provenance = await asyncio.to_thread(engine().provenance)
            failures = 0
        except Exception:
            failures += 1
            if failures >= TICK_ERRORS_BEFORE_WARN:
                logger.exception("market refresh failed %s times", failures)
            continue
        bus.publish(
            StreamEvent(
                sequence=provenance.sequence,
                as_of=provenance.as_of,
                mode=provenance.mode,
                events=produced,
            )
        )


def _prewarm() -> None:
    """Build the pattern-scan panel once so the first user request hits the cache."""
    try:
        lab.scan_patterns(PREWARM_PATTERN_LIMIT)
    except Exception:
        logger.exception("pattern scan prewarm failed")


mcp_endpoint = McpEndpoint()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.to_thread(engine)
    # A daemon thread: startup must not wait for a 2 s cold panel build.
    threading.Thread(target=_prewarm, name="pattern-prewarm", daemon=True).start()
    task = asyncio.create_task(_ticker())
    scheduler.start()  # no-op when PULSE_SCHEDULER=0
    try:
        async with mcp_endpoint.run():
            yield
    finally:
        scheduler.stop()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(
    title="A-Share Pulse API",
    version=settings.version,
    description="A 股盘中市场结构、可解释多因子筛选与确定性回测。契约见 /openapi.json。",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "OPTIONS"],
    allow_headers=["Content-Type"],
)

api = APIRouter(prefix="/api/v1")
for module in (
    market, screener, events, strategies, watchlist, research, lab, data, regime, playbooks, picks, crowd, notify,
    stream, admin, ops,
):
    api.include_router(module.router)
app.include_router(api)
app.add_route(MCP_PATH, mcp_endpoint, methods=["GET", "POST", "DELETE"], include_in_schema=False)


@app.get("/llms.txt", response_class=PlainTextResponse, include_in_schema=False)
def llms() -> str:
    return llms_txt(settings.version, MCP_PATH)


# A built frontend (`pnpm build`) is served from the same origin, so one process
# runs the whole site on a server; in development Vite serves it and proxies /api.
FRONTEND_DIST = ROOT / "frontend" / "dist"
if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
