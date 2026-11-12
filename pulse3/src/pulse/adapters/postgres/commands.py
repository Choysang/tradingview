"""Idempotent commands on ops.command_receipt (W02, INTEGRATION_GUIDE section 6).

Why: a client that lost a response retries with the same Idempotency-Key; it must get the original answer
and the business effect must not run twice. Implements ``pulse.application.ports.commands.CommandStore``.
Invariants:
  * scope = (principal, operation, key); same key + same request hash returns the stored response;
    same key + different hash is a Conflict (VERSION/IDEMPOTENCY conflict is mapped by the API layer);
  * the handler, its outbox events and the receipt share ONE transaction: a failing handler leaves no receipt;
  * concurrent identical requests are serialized by a transaction advisory lock, so the handler runs once;
  * a receipt past ``expires_at`` is treated as absent (published retention policy) and removed.
Not covered: authorization, choosing the retention window (caller passes ttl).
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from pulse.adapters.postgres._canon import sha256_of
from pulse.application.ports.commands import CommandResult
from pulse.application.ports.errors import Conflict
from pulse.contracts import to_wire


class PgCommandStore:
    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    def execute(self, *, principal_id: UUID, operation_id: str, idempotency_key: str, request: Mapping[str, Any],
                response_contract_id: str, response_schema_version: str,
                handler: Callable[[psycopg.Connection], Mapping[str, Any]], ttl: timedelta,
                now: datetime | None = None) -> CommandResult:
        if ttl <= timedelta(0):
            raise ValueError("ttl must be positive")
        request_hash = sha256_of(request)
        with self.conn.transaction():
            self.conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                              (f"{principal_id}|{operation_id}|{idempotency_key}",))
            row = self.conn.execute(
                "SELECT request_hash, response_document, committed_at, (expires_at <= clock_timestamp()) AS expired"
                " FROM ops.command_receipt WHERE principal_id=%s AND operation_id=%s AND idempotency_key=%s",
                (principal_id, operation_id, idempotency_key)).fetchone()
            if row is not None and not row["expired"]:
                if row["request_hash"] != request_hash:
                    raise Conflict(f"idempotency key {idempotency_key!r} was used with a different request")
                return CommandResult(row["response_document"], True, row["committed_at"])
            if row is not None:
                self.conn.execute(
                    "DELETE FROM ops.command_receipt WHERE principal_id=%s AND operation_id=%s AND idempotency_key=%s",
                    (principal_id, operation_id, idempotency_key))
            response = to_wire(dict(handler(self.conn)))
            stamp = now or self.conn.execute("SELECT clock_timestamp() AS t").fetchone()["t"]
            self.conn.execute(
                "INSERT INTO ops.command_receipt(principal_id, operation_id, idempotency_key, request_hash,"
                " response_contract_id, response_schema_version, response_document, committed_at, expires_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (principal_id, operation_id, idempotency_key, request_hash, response_contract_id,
                 response_schema_version, Jsonb(response), stamp, stamp + ttl))
        return CommandResult(response, False, stamp)
