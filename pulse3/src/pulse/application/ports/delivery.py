"""Outbox and update-delivery ports (W02, database_design.md section 5, REFERENCE_RECONCILIATION 12).

Why: pollUpdates, SSE and background consumers must share one event identity, cursor and replay
semantic. Invariants: every event is appended in the transaction of the business change; a stream's
sequence is gap-free in commit order; a consumer's checkpoint is the *contiguous* completed prefix,
never the highest processed number; the same event id with a different payload hash is a conflict.
Not covered: broker transports (JetStream), outbox row archival.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Protocol
from uuid import UUID


@dataclass(frozen=True)
class OutboxEvent:
    event_id: UUID
    event_type: str
    schema_version: str
    aggregate_type: str
    aggregate_id: str
    aggregate_version: int
    occurred_at: datetime
    producer_version: str
    payload: Mapping[str, Any]
    event_ordinal: int = 0
    recorded_at: datetime | None = None


@dataclass(frozen=True)
class DeliveredEvent:
    """An outbox event together with its position in its delivery stream."""

    event_id: UUID
    event_type: str
    schema_version: str
    aggregate_type: str
    aggregate_id: str
    aggregate_version: int
    event_ordinal: int
    occurred_at: datetime
    recorded_at: datetime
    payload: Mapping[str, Any]
    payload_hash: str
    stream_name: str
    stream_epoch: str
    stream_sequence: int

    @property
    def scope(self) -> str:
        return self.stream_name

    @property
    def event_time(self) -> datetime:
        return self.occurred_at

    @property
    def release_id(self) -> str | None:
        value = self.payload.get("release_id")
        return str(value) if value is not None else None


@dataclass(frozen=True)
class AppendResult:
    event_id: UUID
    created: bool                     # False: identical event already present (idempotent retry)


@dataclass(frozen=True)
class StreamHead:
    stream_name: str
    stream_epoch: str
    last_sequence: int
    retained_from_sequence: int


@dataclass(frozen=True)
class PollResult:
    """Result of a stateless poll. ``reset_required`` means: refetch the current snapshot, then resume
    from ``resume_cursor`` (taken at the stream head *before* that refetch, so nothing is missed)."""

    stream_name: str
    stream_epoch: str
    events: tuple[DeliveredEvent, ...]
    resume_cursor: str
    reset_required: bool


@dataclass(frozen=True)
class ConsumerBatch:
    consumer_id: str
    stream_name: str
    stream_epoch: str
    checkpoint: int
    events: tuple[DeliveredEvent, ...]
    reset_required: bool


@dataclass(frozen=True)
class ConsumeOutcome:
    status: str                       # "applied" | "duplicate"
    event_id: UUID
    checkpoint: int                   # contiguous completed watermark after this call


@dataclass(frozen=True)
class Checkpoint:
    consumer_id: str
    stream_name: str
    stream_epoch: str
    contiguous_stream_sequence: int
    last_effective_at: datetime | None
    oldest_pending_at: datetime | None


class Outbox(Protocol):
    def append(self, event: OutboxEvent) -> AppendResult: ...

    def head(self, stream_name: str) -> StreamHead | None: ...

    def poll(self, stream_name: str, cursor: str | None, *, limit: int = 100) -> PollResult: ...

    def prune(self, stream_name: str, *, keep_last: int) -> int: ...


class DeliveryConsumer(Protocol):
    def next_batch(self, consumer_id: str, stream_name: str, *, limit: int = 100) -> ConsumerBatch: ...

    def consume(
        self,
        consumer_id: str,
        event: DeliveredEvent,
        handler: Callable[[Any, DeliveredEvent], None],
    ) -> ConsumeOutcome: ...

    def resync(self, consumer_id: str, stream_name: str) -> Checkpoint: ...

    def checkpoint(self, consumer_id: str, stream_name: str) -> Checkpoint | None: ...


__all__ = [
    "OutboxEvent", "DeliveredEvent", "AppendResult", "StreamHead", "PollResult", "ConsumerBatch",
    "ConsumeOutcome", "Checkpoint", "Outbox", "DeliveryConsumer",
]
