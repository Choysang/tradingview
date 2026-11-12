"""Deterministic health/checkpoint logic. This module runs no polling daemon."""
from dataclasses import dataclass


@dataclass
class SourceHealth:
    enabled: bool = True
    transport_timeout_seconds: int = 90
    unavailable_after_seconds: int = 300
    last_success: int | None = None
    last_content_at: int | None = None
    last_push_heartbeat: int | None = None
    last_error_at: int | None = None

    def transport_success(self, now: int, newest_content_at: int | None = None):
        if newest_content_at is not None:
            if newest_content_at > now:
                raise ValueError("content_clock_in_future_requires_review")
        self.last_success = max(now, self.last_success if self.last_success is not None else now)
        if newest_content_at is not None:
            self.last_content_at = max(newest_content_at, self.last_content_at if self.last_content_at is not None else newest_content_at)

    def state(self, now: int, content_required_after: int | None = None, push_expected=True,
              content_expected_now=True):
        if not self.enabled:
            return {"transport": "disabled", "freshness": "unknown", "poll_due": False}
        if self.last_push_heartbeat is not None and self.last_push_heartbeat > now:
            raise ValueError("push_heartbeat_clock_in_future")
        if self.last_success is None:
            transport = "unknown"
        else:
            age = now - self.last_success
            if age < 0:
                raise ValueError("clock_moved_backwards")
            transport = ("unavailable" if age > self.unavailable_after_seconds else
                         "degraded" if age > self.transport_timeout_seconds else "healthy")
        # A quiet news interval has no mandatory content deadline. Transport can
        # be healthy while a scheduled quote/release dataset is still stale.
        freshness = ("not_expected" if not content_expected_now else
                     "unknown" if content_required_after is None else
                     "current" if self.last_content_at is not None and self.last_content_at >= content_required_after
                     else "stale")
        push_stale = self.last_push_heartbeat is None or now - self.last_push_heartbeat > self.transport_timeout_seconds
        return {"transport": transport, "freshness": freshness,
                "poll_due": transport != "healthy" or (push_expected and push_stale)}


@dataclass
class DurableCursor:
    """Illustrates contiguous integer cursors; real adapters may use opaque tokens."""
    committed_sequence: int = 0

    def commit(self, start: int, end: int, raw_persisted: bool):
        if start < 1 or end < start:
            raise ValueError("invalid_cursor_interval")
        if not raw_persisted:
            raise ValueError("checkpoint_requires_durable_raw_commit")
        if end <= self.committed_sequence:
            return False
        if start > self.committed_sequence + 1:
            raise ValueError("unresolved_source_sequence_gap")
        self.committed_sequence = end
        return True
