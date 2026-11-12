"""Source health state machine per endpoint partition (spec FINAL §12, M1-E04).

Why: "enabled", "configured", "transport ok", "new content" and "fresh" are five
different facts. HTTP 200 with old content is not recovery, a login page is not
data, a blocked client must stop (circuit OPEN) and a quiet news interval or a
closed market is not a failure. The output maps 1:1 onto ``ingest.source_health``
(runtime_state, freshness_state and the timestamp columns).

Invariants: only a data-bearing attempt (success_data/success_empty/not_modified)
advances ``last_transport_ok_at``; only new records advance ``last_new_record_at``;
freshness is computed from the *content* clock (``newest_content_at``), never from
the contact time; a future content timestamp is rejected; a blocked/OPEN circuit
keeps ``next_attempt_at`` in the future and does not recover by itself on HTTP 200.
Not covered: the polling daemon and persistence (application layer).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

OUTCOMES = ("success_data", "success_empty", "not_modified", "rate_limited", "auth_required", "transport_error",
            "parse_error", "schema_changed", "partial_page", "blocked")
DATA_BEARING = ("success_data", "success_empty", "not_modified")


@dataclass(frozen=True)
class HealthState:
    enabled: bool = True
    configured: bool = True
    runtime_state: str = "unconfigured"
    freshness_state: str = "unknown"
    last_attempt_at: datetime | None = None
    last_transport_ok_at: datetime | None = None
    last_new_record_at: datetime | None = None
    last_canonical_ready_at: datetime | None = None
    consecutive_failures: int = 0
    next_attempt_at: datetime | None = None
    last_error_code: str | None = None
    circuit: str = "closed"  # closed | open | half_open (process view; persisted via runtime_state+error code)


@dataclass(frozen=True)
class HealthPolicy:
    degraded_after_failures: int = 1
    unavailable_after_failures: int = 3
    backoff_base_seconds: int = 30
    backoff_max_seconds: int = 1800
    blocked_cooldown_seconds: int = 3600
    stale_after_seconds: int = 300


def _backoff(failures: int, p: HealthPolicy) -> timedelta:
    return timedelta(seconds=min(p.backoff_max_seconds, p.backoff_base_seconds * (2 ** max(0, failures - 1))))


def evaluate_freshness(newest_content_at: datetime | None, now: datetime, *, stale_after_seconds: int,
                       content_expected_now: bool = True, required_after: datetime | None = None) -> str:
    """current | stale | unknown | not_expected from the CONTENT clock."""
    if newest_content_at is not None and newest_content_at > now:
        raise ValueError("content_clock_in_future_requires_review")
    if not content_expected_now:
        return "not_expected"
    if newest_content_at is None:
        return "unknown"
    if required_after is not None:
        return "current" if newest_content_at >= required_after else "stale"
    return "current" if (now - newest_content_at).total_seconds() <= stale_after_seconds else "stale"


def apply_attempt(state: HealthState, outcome: str, now: datetime, policy: HealthPolicy = HealthPolicy(), *,
                  new_records: int = 0, newest_content_at: datetime | None = None, content_expected_now: bool = True,
                  required_after: datetime | None = None, canonical_ready: bool = False,
                  error_code: str | None = None) -> HealthState:
    """Return the successor state after one fetch attempt."""
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}")
    if not state.enabled:
        return replace(state, runtime_state="paused", freshness_state="unknown", last_attempt_at=now)
    if not state.configured:
        return replace(state, runtime_state="unconfigured", last_attempt_at=now)
    if newest_content_at is not None and newest_content_at > now:
        raise ValueError("content_clock_in_future_requires_review")
    if state.circuit == "open" and state.next_attempt_at and now < state.next_attempt_at:
        raise ValueError("attempt_while_circuit_open")  # callers must wait for next_attempt_at
    s = replace(state, last_attempt_at=now)
    if outcome in DATA_BEARING:
        newest = newest_content_at
        fresh = evaluate_freshness(newest, now, stale_after_seconds=policy.stale_after_seconds,
                                   content_expected_now=content_expected_now, required_after=required_after)
        # HTTP 200 carrying stale content is contact, not recovery: a previously impaired source stays degraded.
        impaired = s.runtime_state in ("unavailable", "degraded", "auth_required")
        runtime = "degraded" if (fresh == "stale" and impaired) else "healthy"
        return replace(
            s, runtime_state=runtime, circuit="closed", consecutive_failures=0, next_attempt_at=None,
            last_error_code=None, last_transport_ok_at=now,
            last_new_record_at=now if new_records > 0 else s.last_new_record_at,
            last_canonical_ready_at=now if canonical_ready else s.last_canonical_ready_at,
            freshness_state=fresh)
    failures = s.consecutive_failures + 1
    code = error_code or outcome
    if outcome == "blocked":
        return replace(s, runtime_state="unavailable", circuit="open", consecutive_failures=failures,
                       next_attempt_at=now + timedelta(seconds=policy.blocked_cooldown_seconds),
                       last_error_code=f"blocked:{code}" if not code.startswith("blocked") else code)
    if outcome == "auth_required":
        return replace(s, runtime_state="auth_required", consecutive_failures=failures,
                       next_attempt_at=now + _backoff(failures, policy), last_error_code=code)
    if outcome in ("schema_changed", "parse_error"):
        return replace(s, runtime_state="degraded", consecutive_failures=failures,
                       next_attempt_at=now + _backoff(failures, policy), last_error_code=code)
    runtime = ("unavailable" if failures >= policy.unavailable_after_failures
               else "degraded" if failures >= policy.degraded_after_failures else s.runtime_state)
    return replace(s, runtime_state=runtime, consecutive_failures=failures,
                   next_attempt_at=now + _backoff(failures, policy), last_error_code=code)


def half_open(state: HealthState, now: datetime) -> HealthState:
    """After the cooldown a single probe may be sent; success closes the circuit, failure re-opens."""
    if state.circuit == "open" and state.next_attempt_at and now >= state.next_attempt_at:
        return replace(state, circuit="half_open")
    return state
