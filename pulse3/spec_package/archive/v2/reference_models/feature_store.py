"""Single-process, single-event-loop feature computation reference.

No external data is read here. A compute function must be pure relative to the
complete FeatureKey: every input revision and dependency must be represented.
This is an in-memory example, not distributed locking or durable computation.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Generic, TypeVar

T = TypeVar("T")


def parameter_digest(parameters: dict[str, Any]) -> str:
    """Hash canonical JSON parameters; reject ambiguous keys and non-finite values."""
    def check(value: Any) -> None:
        if isinstance(value, dict):
            if any(not isinstance(k, str) for k in value):
                raise TypeError("parameter object keys must be strings")
            for item in value.values():
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)
        elif value is not None and type(value) not in (str, int, float, bool):
            raise TypeError("parameters must contain JSON values only")

    if not isinstance(parameters, dict):
        raise TypeError("parameters must be a JSON object")
    check(parameters)
    encoded = json.dumps(parameters, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class FeatureKey:
    """Versioned input identity, intentionally without implicit defaults.

    snapshot_id identifies the data snapshot and input_revision its corrections.
    decision_as_of is a required aware decision instant, normalized to UTC.
    Availability/session policy versions identify temporal visibility rules.
    A key cannot enforce those rules: the data query must filter visible inputs.
    dependency_digest covers upstream feature/code versions.
    Instruments preserve order because order can affect a feature's output.
    An unused dimension still needs an explicit value such as 'not-applicable'.
    """
    feature_id: str
    definition_version: str
    implementation_digest: str
    parameter_digest: str
    instruments: tuple[str, ...]
    interval: str
    price_mode: str
    snapshot_id: str
    decision_as_of: datetime
    availability_policy_version: str
    session_policy_version: str
    input_revision: str
    input_schema_version: str
    dependency_digest: str
    universe_version: str
    calendar_version: str
    adjustment_version: str
    market_rules_version: str
    numeric_policy_version: str

    def __post_init__(self) -> None:
        for field in fields(self):
            name, value = field.name, getattr(self, field.name)
            if name == "decision_as_of":
                if not isinstance(value, datetime) or value.utcoffset() is None:
                    raise ValueError("decision_as_of must be a timezone-aware datetime")
                object.__setattr__(self, name, value.astimezone(timezone.utc))
            elif name == "instruments":
                if not isinstance(value, tuple) or not value:
                    raise ValueError("instruments must be a nonempty tuple")
                if any(not isinstance(x, str) or not x.strip() for x in value):
                    raise ValueError("instrument identifiers must be nonempty strings")
                if len(set(value)) != len(value):
                    raise ValueError("duplicate instruments are not allowed")
            elif not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be an explicit nonempty string")


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

