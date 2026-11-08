"""In-process fan-out for the SSE stream. One publisher, many listeners."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager

from contracts.schema import StreamEvent


class Broadcaster:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[StreamEvent]] = set()

    @contextmanager
    def subscription(self, maxsize: int = 8):
        queue: asyncio.Queue[StreamEvent] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    def publish(self, event: StreamEvent) -> None:
        for queue in list(self._subscribers):
            if queue.full():
                # A slow client must never block the ticker; drop the oldest frame.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                continue

    @property
    def listeners(self) -> int:
        return len(self._subscribers)


bus = Broadcaster()
