"""Single-flight shared computation cache keyed by the full FeatureKey (port of reference_features).

Why: charts, ranking and plans asking for the same FeatureKey must share one
computation. Only the first compute callable for an in-flight key runs; failed
or cancelled producers are removed so a retry recomputes; waiter cancellation
does not abort the shared calculation; results are LRU bounded and copied at
the boundary. Successful ``None`` is a cacheable result.
Invariants: one asyncio loop per store; new data/revisions need a new key (no
TTL expiry is used to repair missing version dimensions).
Not covered: distributed locks, durable jobs (fencing via feature_result.lease_epoch
in the repository), execution capacity limits.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Generic, TypeVar

from .key import FeatureKey

T = TypeVar("T")


@dataclass(frozen=True)
class CacheStats:
    completed_entries: int
    inflight_entries: int
    started: int
    joined: int
    hits: int


class SingleFlightFeatureStore(Generic[T]):
    """Share one async calculation per key and shield it from waiter cancellation.

    Successful results are kept in a bounded LRU cache. Failed/cancelled producers
    are removed and can be retried. Small result objects are copied at ingestion
    and on return, preventing producer/consumer mutations from poisoning cache.
    Production large features should use immutable data or artifact references.
    """
    def __init__(self, max_entries: int = 128) -> None:
        if type(max_entries) is not int or max_entries <= 0:
            raise ValueError("max_entries must be a positive integer")
        self._max_entries = max_entries
        self._values: OrderedDict[FeatureKey, T] = OrderedDict()
        self._inflight: dict[FeatureKey, asyncio.Task[T]] = {}
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._started = self._joined = self._hits = 0

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("store is limited to one asyncio event loop")

    async def get(self, key: FeatureKey, compute: Callable[[], Awaitable[T]]) -> T:
        """Get a result; only the first compute callable for an in-flight key runs."""
        self._bind_loop()
        if not isinstance(key, FeatureKey):
            raise TypeError("key must be a FeatureKey")
        async with self._lock:
            if self._closed:
                raise RuntimeError("store is closed")
            if key in self._values:
                self._hits += 1
                self._values.move_to_end(key)
                return deepcopy(self._values[key])
            task = self._inflight.get(key)
            if task is None:
                self._started += 1
                task = asyncio.create_task(self._run(key, compute))
                # Observe errors even if all original waiters have cancelled.
                task.add_done_callback(self._observe_completion)
                self._inflight[key] = task
            else:
                self._joined += 1
        return deepcopy(await asyncio.shield(task))

    @staticmethod
    def _observe_completion(task: asyncio.Task[Any]) -> None:
        if not task.cancelled():
            task.exception()

    async def _run(self, key: FeatureKey, compute: Callable[[], Awaitable[T]]) -> T:
        try:
            # Detach the canonical cache value from a mutable producer-owned
            # object before exposing separate copies to waiting consumers.
            value = deepcopy(await compute())
            async with self._lock:
                if not self._closed:
                    self._values[key] = value
                    self._values.move_to_end(key)
                    while len(self._values) > self._max_entries:
                        self._values.popitem(last=False)
            return value
        finally:
            # Removal happens before the producer task becomes done, so a later
            # call after an exception can immediately start a fresh calculation.
            async with self._lock:
                if self._inflight.get(key) is asyncio.current_task():
                    del self._inflight[key]

    async def stats(self) -> CacheStats:
        self._bind_loop()
        async with self._lock:
            return CacheStats(len(self._values), len(self._inflight), self._started,
                              self._joined, self._hits)

    async def aclose(self) -> None:
        """Share one shutdown; cancellation of its waiter does not abort cleanup."""
        self._bind_loop()
        async with self._lock:
            if self._close_task is None:
                self._closed = True
                pending = tuple(self._inflight.values())
                self._close_task = asyncio.create_task(self._shutdown(pending))
                self._close_task.add_done_callback(self._observe_completion)
            close_task = self._close_task
        await asyncio.shield(close_task)

    async def _shutdown(self, pending: tuple[asyncio.Task[T], ...]) -> None:
        # Repeated/concurrent aclose calls must not cancel a producer twice:
        # its first cancellation may already be running async finally cleanup.
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        async with self._lock:
            self._values.clear()
            # A task cancelled before its first instruction never enters _run's
            # finally block. Shutdown must clear those entries explicitly too.
            self._inflight.clear()

