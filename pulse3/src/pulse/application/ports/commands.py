"""Idempotent command port (W02, INTEGRATION_GUIDE section 6, ops.command_receipt).

Why: a client retries after a lost response; the same Idempotency-Key with the same request must return
the original answer and run the business effect once. A different request under the same key is a
conflict. Invariants: the receipt commits in the same transaction as the business write and its outbox
event; receipts are scoped by principal + operation; expired receipts are treated as absent.
Not covered: authorization, the retention-window policy value (the caller supplies ttl).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Protocol
from uuid import UUID


@dataclass(frozen=True)
class CommandResult:
    response: Mapping[str, Any]
    replayed: bool                    # True: returned from a stored receipt, handler did not run
    committed_at: datetime


class CommandStore(Protocol):
    def execute(
        self,
        *,
        principal_id: UUID,
        operation_id: str,
        idempotency_key: str,
        request: Mapping[str, Any],
        response_contract_id: str,
        response_schema_version: str,
        handler: Callable[[Any], Mapping[str, Any]],
        ttl: timedelta,
        now: datetime | None = None,
    ) -> CommandResult: ...
