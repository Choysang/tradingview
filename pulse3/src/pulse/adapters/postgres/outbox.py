"""Transactional outbox, update polling and durable consumers on PostgreSQL (W02).

Why: database_design.md section 5 / REFERENCE_RECONCILIATION 12 require one event log for pollUpdates,
SSE and background consumers, with replay, idempotency and an explicit "refetch" answer when a cursor
expired. Implements ``pulse.application.ports.delivery``.
Invariants:
  * append joins the caller's open transaction (so it commits with the business change); on an idle
    connection it commits as its own unit; the stream position is assigned by the
    ``outbox_assign_position`` trigger so the sequence is gap-free in commit order (migration 0006);
  * a cursor is opaque (epoch + scope + position) and never trusted across scopes;
  * a consumer checkpoint is the contiguous completed prefix of its own inbox, advanced under a row lock;
  * same event id + different payload hash is a Conflict, never a silent overwrite.
Not covered: pushing to a broker, archiving outbox rows, ordering across different streams.
"""
from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime, timezone
from typing import Any, Callable

import psycopg
from psycopg.types.json import Jsonb

from pulse.adapters.postgres._canon import sha256_of
from pulse.application.ports.delivery import (
    AppendResult,
    Checkpoint,
    ConsumeOutcome,
    ConsumerBatch,
    DeliveredEvent,
    OutboxEvent,
    PollResult,
    StreamHead,
)
from pulse.application.ports.errors import Conflict, CursorInvalid
from pulse.contracts import to_wire, utc_z

INITIAL_EPOCH = "1"

_EVENT_COLUMNS = (
    "o.event_id, o.event_type, o.schema_version, o.aggregate_type, o.aggregate_id, o.aggregate_version,"
    " o.event_ordinal, o.occurred_at, o.recorded_at, o.payload, o.payload_hash,"
    " p.stream_name, p.stream_epoch, p.stream_sequence"
)


def stream_name_for(aggregate_type: str, aggregate_id: str) -> str:
    return f"{aggregate_type}:{aggregate_id}"


def encode_cursor(stream_name: str, epoch: str, position: int) -> str:
    raw = json.dumps({"v": 1, "s": stream_name, "e": epoch, "p": position}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, str, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        doc = json.loads(raw)
        if doc["v"] != 1 or not isinstance(doc["s"], str) or not isinstance(doc["e"], str):
            raise ValueError("shape")
        position = doc["p"]
        if not isinstance(position, int) or isinstance(position, bool) or position < 0:
            raise ValueError("position")
        return doc["s"], doc["e"], position
    except (ValueError, KeyError, TypeError, binascii.Error) as exc:
        raise CursorInvalid(f"malformed cursor: {exc}") from exc


def _delivered(row: dict[str, Any]) -> DeliveredEvent:
    return DeliveredEvent(**row)


def to_update_page(result: PollResult) -> dict[str, Any]:
    """Wire form of ``UpdatePage`` (contract v3). Callers validate with ``contracts.check``."""
    return {
        "stream_epoch": result.stream_epoch,
        "events": [
            {
                "event_id": str(e.event_id),
                "event_type": e.event_type,
                "schema_version": e.schema_version,
                "aggregate_id": e.aggregate_id,
                "aggregate_version": e.aggregate_version,
                "recorded_at": utc_z(e.recorded_at),
                "payload": to_wire(dict(e.payload)),
            }
            for e in result.events
        ],
        "resume_cursor": result.resume_cursor,
        "reset_required": result.reset_required,
    }


class PgOutbox:
    """Outbox writer, stateless poller and durable-consumer helper over one connection."""

    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    # ---- writing -------------------------------------------------------------------------------
    def append(self, event: OutboxEvent) -> AppendResult:
        payload = to_wire(dict(event.payload))
        payload_hash = sha256_of(payload)
        recorded_at = event.recorded_at or datetime.now(timezone.utc)
        try:
            with self.conn.transaction():
                inserted = self.conn.execute(
                    "INSERT INTO ops.outbox(event_id, event_type, schema_version, aggregate_type, aggregate_id,"
                    " aggregate_version, event_ordinal, occurred_at, recorded_at, producer_version, payload, payload_hash)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                    " ON CONFLICT (event_id) DO NOTHING RETURNING event_id",
                    (event.event_id, event.event_type, event.schema_version, event.aggregate_type, event.aggregate_id,
                     event.aggregate_version, event.event_ordinal, event.occurred_at, recorded_at,
                     event.producer_version, Jsonb(payload), payload_hash),
                ).fetchone()
        except psycopg.errors.UniqueViolation as exc:
            # A different event id already owns (aggregate, version, ordinal).
            raise Conflict(f"aggregate slot {event.aggregate_type}:{event.aggregate_id}"
                           f"#{event.aggregate_version}.{event.event_ordinal} is taken by another event") from exc
        if inserted is not None:
            return AppendResult(event.event_id, True)
        existing = self.conn.execute(
            "SELECT event_type, aggregate_type, aggregate_id, aggregate_version, event_ordinal, payload_hash"
            " FROM ops.outbox WHERE event_id=%s", (event.event_id,)
        ).fetchone()
        same = existing is not None and (
            existing["event_type"], existing["aggregate_type"], existing["aggregate_id"],
            existing["aggregate_version"], existing["event_ordinal"], existing["payload_hash"],
        ) == (event.event_type, event.aggregate_type, event.aggregate_id,
              event.aggregate_version, event.event_ordinal, payload_hash)
        if not same:
            raise Conflict(f"event {event.event_id} already exists with different content")
        return AppendResult(event.event_id, False)

    # ---- stream state --------------------------------------------------------------------------
    def head(self, stream_name: str) -> StreamHead | None:
        row = self.conn.execute(
            "SELECT stream_name, stream_epoch, last_sequence, retained_from_sequence"
            " FROM ops.outbox_stream WHERE stream_name=%s", (stream_name,)
        ).fetchone()
        return StreamHead(**row) if row else None

    def _fetch_after(self, stream_name: str, epoch: str, after: int, limit: int) -> list[DeliveredEvent]:
        rows = self.conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM ops.outbox_position p JOIN ops.outbox o ON o.event_id=p.event_id"
            " WHERE p.stream_name=%s AND p.stream_epoch=%s AND p.stream_sequence>%s"
            " ORDER BY p.stream_sequence LIMIT %s",
            (stream_name, epoch, after, limit),
        ).fetchall()
        return [_delivered(r) for r in rows]

    # ---- stateless polling (pollUpdates) -------------------------------------------------------
    def poll(self, stream_name: str, cursor: str | None, *, limit: int = 100) -> PollResult:
        head = self.head(stream_name)
        epoch = head.stream_epoch if head else INITIAL_EPOCH
        last = head.last_sequence if head else 0
        retained = head.retained_from_sequence if head else 1
        if cursor is None:
            return PollResult(stream_name, epoch, (), encode_cursor(stream_name, epoch, last), True)
        scope, cursor_epoch, position = decode_cursor(cursor)
        if scope != stream_name:
            raise CursorInvalid(f"cursor belongs to scope {scope!r}, not {stream_name!r}")
        if cursor_epoch != epoch or position > last or position < retained - 1:
            return PollResult(stream_name, epoch, (), encode_cursor(stream_name, epoch, last), True)
        events = self._fetch_after(stream_name, epoch, position, limit)
        resume = events[-1].stream_sequence if events else position
        return PollResult(stream_name, epoch, tuple(events), encode_cursor(stream_name, epoch, resume), False)

    # ---- retention -----------------------------------------------------------------------------
    def prune(self, stream_name: str, *, keep_last: int) -> int:
        """Drop delivery positions older than the newest ``keep_last``; cursors behind it must refetch.

        Outbox rows themselves stay (release_publication references them and they are the audit fact)."""
        with self.conn.transaction():
            row = self.conn.execute(
                "SELECT stream_epoch, last_sequence, retained_from_sequence FROM ops.outbox_stream"
                " WHERE stream_name=%s FOR UPDATE", (stream_name,)
            ).fetchone()
            if row is None:
                return 0
            new_from = max(row["retained_from_sequence"], row["last_sequence"] - keep_last + 1)
            self.conn.execute(
                "UPDATE ops.outbox_stream SET retained_from_sequence=%s WHERE stream_name=%s", (new_from, stream_name)
            )
            cur = self.conn.execute(
                "DELETE FROM ops.outbox_position WHERE stream_name=%s AND (stream_epoch<>%s OR stream_sequence<%s)",
                (stream_name, row["stream_epoch"], new_from),
            )
            return cur.rowcount

    # ---- durable consumers ---------------------------------------------------------------------
    def checkpoint(self, consumer_id: str, stream_name: str) -> Checkpoint | None:
        head = self.head(stream_name)
        epoch = head.stream_epoch if head else INITIAL_EPOCH
        row = self.conn.execute(
            "SELECT consumer_id, stream_name, stream_epoch, contiguous_stream_sequence, last_effective_at,"
            " oldest_pending_at FROM ops.consumer_checkpoint"
            " WHERE consumer_id=%s AND stream_name=%s AND stream_epoch=%s",
            (consumer_id, stream_name, epoch),
        ).fetchone()
        return Checkpoint(**row) if row else None

    def _advance(self, consumer_id: str, stream_name: str, epoch: str, retained: int,
                 last_effective_at: datetime | None = None) -> int:
        """Move the checkpoint to the end of the contiguous completed prefix; never past a hole."""
        self.conn.execute(
            "INSERT INTO ops.consumer_checkpoint(consumer_id, stream_name, stream_epoch,"
            " contiguous_stream_sequence, updated_at) VALUES (%s,%s,%s,0,clock_timestamp())"
            " ON CONFLICT DO NOTHING", (consumer_id, stream_name, epoch),
        )
        # The lock serializes concurrent workers of one consumer; the next statement then sees their commits.
        cp = self.conn.execute(
            "SELECT contiguous_stream_sequence AS cp FROM ops.consumer_checkpoint"
            " WHERE consumer_id=%s AND stream_name=%s AND stream_epoch=%s FOR UPDATE",
            (consumer_id, stream_name, epoch),
        ).fetchone()["cp"]
        if cp < retained - 1:
            return cp                                   # expired: only resync() may move it
        row = self.conn.execute(
            "WITH nxt AS ("
            "  SELECT p.stream_sequence AS seq, o.recorded_at, (i.event_id IS NOT NULL) AS done"
            "  FROM ops.outbox_position p JOIN ops.outbox o ON o.event_id=p.event_id"
            "  LEFT JOIN ops.consumer_inbox i ON i.consumer_id=%s AND i.event_id=p.event_id"
            "  WHERE p.stream_name=%s AND p.stream_epoch=%s AND p.stream_sequence>%s),"
            " run AS (SELECT seq, recorded_at, done, bool_and(done) OVER (ORDER BY seq) AS prefix FROM nxt)"
            " SELECT max(seq) FILTER (WHERE prefix) AS new_cp,"
            "        min(recorded_at) FILTER (WHERE NOT done) AS oldest_pending FROM run",
            (consumer_id, stream_name, epoch, cp),
        ).fetchone()
        new_cp = max(cp, row["new_cp"] or 0)
        self.conn.execute(
            "UPDATE ops.consumer_checkpoint SET contiguous_stream_sequence=%s, oldest_pending_at=%s,"
            " last_effective_at=GREATEST(last_effective_at, %s), updated_at=clock_timestamp()"
            " WHERE consumer_id=%s AND stream_name=%s AND stream_epoch=%s",
            (new_cp, row["oldest_pending"], last_effective_at, consumer_id, stream_name, epoch),
        )
        return new_cp

    def next_batch(self, consumer_id: str, stream_name: str, *, limit: int = 100) -> ConsumerBatch:
        with self.conn.transaction():
            head = self.head(stream_name)
            if head is None:
                return ConsumerBatch(consumer_id, stream_name, INITIAL_EPOCH, 0, (), False)
            cp = self._advance(consumer_id, stream_name, head.stream_epoch, head.retained_from_sequence)
            if cp < head.retained_from_sequence - 1:
                return ConsumerBatch(consumer_id, stream_name, head.stream_epoch, cp, (), True)
            rows = self.conn.execute(
                f"SELECT {_EVENT_COLUMNS} FROM ops.outbox_position p JOIN ops.outbox o ON o.event_id=p.event_id"
                " WHERE p.stream_name=%s AND p.stream_epoch=%s AND p.stream_sequence>%s"
                "   AND NOT EXISTS (SELECT 1 FROM ops.consumer_inbox i WHERE i.consumer_id=%s AND i.event_id=p.event_id)"
                " ORDER BY p.stream_sequence LIMIT %s",
                (stream_name, head.stream_epoch, cp, consumer_id, limit),
            ).fetchall()
            return ConsumerBatch(consumer_id, stream_name, head.stream_epoch, cp,
                                 tuple(_delivered(r) for r in rows), False)

    def consume(self, consumer_id: str, event: DeliveredEvent,
                handler: Callable[[psycopg.Connection, DeliveredEvent], None]) -> ConsumeOutcome:
        """Run ``handler`` at most once per (consumer, event); inbox row, effect and checkpoint commit together."""
        with self.conn.transaction():
            inserted = self.conn.execute(
                "INSERT INTO ops.consumer_inbox(consumer_id, event_id, payload_hash, applied_at)"
                " VALUES (%s,%s,%s,clock_timestamp()) ON CONFLICT (consumer_id, event_id) DO NOTHING RETURNING event_id",
                (consumer_id, event.event_id, event.payload_hash),
            ).fetchone()
            if inserted is not None:
                handler(self.conn, event)
                status = "applied"
            else:
                seen = self.conn.execute(
                    "SELECT payload_hash FROM ops.consumer_inbox WHERE consumer_id=%s AND event_id=%s",
                    (consumer_id, event.event_id),
                ).fetchone()["payload_hash"]
                if seen != event.payload_hash:
                    raise Conflict(f"event {event.event_id} redelivered to {consumer_id} with a different payload hash")
                status = "duplicate"
            head = self.head(event.stream_name)
            retained = head.retained_from_sequence if head and head.stream_epoch == event.stream_epoch else 1
            cp = self._advance(consumer_id, event.stream_name, event.stream_epoch, retained,
                               event.occurred_at if status == "applied" else None)
        return ConsumeOutcome(status, event.event_id, cp)

    def resync(self, consumer_id: str, stream_name: str) -> Checkpoint:
        """Acknowledge a full refetch: place the checkpoint at the current head of the current epoch."""
        with self.conn.transaction():
            head = self.head(stream_name)
            epoch = head.stream_epoch if head else INITIAL_EPOCH
            last = head.last_sequence if head else 0
            self.conn.execute(
                "INSERT INTO ops.consumer_checkpoint(consumer_id, stream_name, stream_epoch,"
                " contiguous_stream_sequence, oldest_pending_at, updated_at)"
                " VALUES (%s,%s,%s,%s,NULL,clock_timestamp())"
                " ON CONFLICT (consumer_id, stream_name, stream_epoch) DO UPDATE SET"
                " contiguous_stream_sequence=GREATEST(ops.consumer_checkpoint.contiguous_stream_sequence, EXCLUDED.contiguous_stream_sequence),"
                " oldest_pending_at=NULL, updated_at=clock_timestamp()",
                (consumer_id, stream_name, epoch, last),
            )
        cp = self.checkpoint(consumer_id, stream_name)
        assert cp is not None
        return cp

