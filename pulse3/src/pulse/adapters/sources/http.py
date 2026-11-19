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
Cross-process pacing (optional): ``RateLimitedClient`` paces per process, but several recorders of one server (quotes, extended intraday,
news, the daily intel pull) can hit the SAME host. When the environment variable ``PULSE_HOST_PACING_DIR`` names a directory, every request
first takes an exclusive OS file lock on ``<dir>/<host>.lock``, waits until ``min_interval`` has passed since the last request of ANY process to
that host (the time is kept in the file), writes its own time and releases the lock, so the processes share one request budget per host.
Unset (the default) nothing changes. Not covered: proxies, cookies/credentials, concurrency inside one process (single-threaded use).
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import httpx

PACING_ENV = "PULSE_HOST_PACING_DIR"
_STAMP_AT = 8             # the lock byte is 0; the timestamp lives after it (a Windows byte-range lock also blocks reads of its bytes)
BLOCK_STATUS = {403, 429, 456, 501}  # 502/503/504 are gateway faults (TransportFailure), not blocks
USER_AGENT = "Mozilla/5.0 (compatible; pulse3-data-plane/0.1)"


def _lock(f) -> None:
    if os.name == "nt":
        import msvcrt

        f.seek(0)
        while True:
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)       # gives up with OSError after ~10 s: keep waiting for the holder
                return
            except OSError:
                continue
    import fcntl

    fcntl.flock(f, fcntl.LOCK_EX)


def _unlock(f) -> None:
    if os.name == "nt":
        import msvcrt

        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(f, fcntl.LOCK_UN)


def pace_host_across_processes(url: str, min_interval: float, sleep: Callable[[float], None] = time.sleep,
                               wall: Callable[[], float] = time.time) -> None:
    """Block until ``min_interval`` seconds passed since the last request to the host of ``url`` by any process sharing PULSE_HOST_PACING_DIR,
    then record this request's time. No-op when the variable is unset."""
    directory = os.environ.get(PACING_ENV)
    host = urlsplit(url).hostname
    if not directory or not host:
        return
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    with open(path / (re.sub(r"[^A-Za-z0-9.-]", "_", host) + ".lock"), "a+b") as f:
        _lock(f)
        try:
            f.seek(_STAMP_AT)
            try:
                last = float(f.read(32).decode("ascii").strip() or 0.0)
            except ValueError:
                last = 0.0
            wait = min_interval - (wall() - last)
            if wait > 0:
                sleep(min(wait, min_interval))              # a stamp from the future (clock step) never waits longer than one interval
            f.seek(_STAMP_AT)
            f.truncate(_STAMP_AT)
            f.write(f"{wall():.6f}".encode("ascii"))
            f.flush()
        finally:
            _unlock(f)


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
        pace_host_across_processes(url, self.min_interval, self._sleep)
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
