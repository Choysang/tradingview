"""Server-sent events. The stream carries a tick heartbeat, not a second data model."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from contracts.schema import StreamEvent
from ..core.bus import bus
from ..core.engine import engine

router = APIRouter(tags=["stream"])
KEEPALIVE_SECONDS = 15.0


def _encode(event: StreamEvent) -> str:
    return f"event: snapshot\ndata: {event.model_dump_json()}\n\n"


@router.get("/stream/sse")
async def stream(request: Request) -> StreamingResponse:
    async def generator():
        yield ": pulse stream open\n\n"
        provenance = await asyncio.to_thread(engine().provenance)
        # The first frame is a state sync, not a diff, so it carries no events.
        yield _encode(
            StreamEvent(
                sequence=provenance.sequence,
                as_of=provenance.as_of,
                mode=provenance.mode,
                events=0,
            )
        )
        with bus.subscription() as queue:
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield _encode(event)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
