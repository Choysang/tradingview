"""Rate-limited HTTP client with block detection and a circuit breaker (spec FINAL §12, M1-E04).

Why: public quote endpoints ban aggressive clients. The client enforces a
minimum interval between requests (default 1 s), and the FIRST sign of blocking
(HTTP 403/429/501/456 or a body that is a captcha/login page for a data call)
opens the circuit: no further request leaves until ``reset()`` is called after
the cooldown. The raised error carries the outcome for the health state machine.

Invariants: every call returns the raw bytes untouched; the caller persists them
before parsing; no retries inside the client (retry policy belongs to the
scheduler with backoff recorded in health); the transport is injectable
(``httpx.MockTransport``) so tests never touch the network.
Not covered: proxies, cookies/credentials, concurrency (single-threaded use).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import httpx

BLOCK_STATUS = {403, 429, 456, 501}  # 502/503/504 are gateway faults (TransportFailure), not blocks
USER_AGENT = "Mozilla/5.0 (compatible; pulse3-data-plane/0.1)"


class SourceBlocked(RuntimeError):
    """The endpoint refused us (block/rate limit). Circuit is OPEN; stop immediately."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class TransportFailure(RuntimeError):
    """Network error / timeout / 5xx other than block statuses."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Capture:
    """One HTTP response kept as raw evidence (bytes are never re-encoded)."""
    source_id: str
    endpoint_id: str
    request_key: str
    url: str
    method: str
    form: str | None
    status: int
    body: bytes
    mime_type: str
    fetched_at: datetime


class RateLimitedClient:
    def __init__(self, *, min_interval_seconds: float = 1.0, transport: httpx.BaseTransport | None = None,
                 timeout: float = 20.0, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, now: Callable[[], datetime] | None = None):
        if min_interval_seconds < 0:
            raise ValueError("min_interval_seconds must be >= 0")
        self.min_interval = min_interval_seconds
        self._client = httpx.Client(transport=transport, timeout=timeout, follow_redirects=False,
                                    headers={"User-Agent": USER_AGENT})
        self._clock, self._sleep = clock, sleep
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._last: float | None = None
        self.circuit_open = False
        self.block_reason: str | None = None
        self.requests_made = 0

    def reset(self) -> None:
        """Half-open: allow the next probe request (after the cooldown recorded in health)."""
        self.circuit_open, self.block_reason = False, None

    def close(self) -> None:
        self._client.close()

    def request(self, *, source_id: str, endpoint_id: str, request_key: str, url: str, method: str = "GET",
                headers: dict | None = None, form: str | None = None) -> Capture:
        if self.circuit_open:
            raise SourceBlocked(f"circuit_open:{self.block_reason}")
        if self._last is not None:
            wait = self.min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()
        self.requests_made += 1
        try:
            if method == "POST":
                resp = self._client.post(url, headers=headers, content=(form or "").encode("utf-8"))
            else:
                resp = self._client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise TransportFailure(f"{type(exc).__name__}: {exc}") from exc
        fetched_at = self._now()
        if resp.status_code in BLOCK_STATUS:
            self.circuit_open, self.block_reason = True, f"http_{resp.status_code}"
            raise SourceBlocked(f"http_{resp.status_code}", resp.status_code)
        if resp.status_code >= 400:
            raise TransportFailure(f"http_{resp.status_code}", resp.status_code)
        return Capture(source_id, endpoint_id, request_key, url, method, form, resp.status_code, resp.content,
                       resp.headers.get("content-type", "application/octet-stream").split(";")[0].strip(), fetched_at)
