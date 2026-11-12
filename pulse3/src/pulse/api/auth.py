"""Principal resolution for sessionAuth (cookie ``pulse_session``).

Why: openapi declares cookie session auth for user-owned resources (layouts,
watchlists, annotations, accounts). The platform is single-tenant behind an SSH
tunnel, so a session token maps to a principal through PULSE_SESSIONS
("token=uuid,token2=uuid") or the file named by PULSE_SESSIONS_FILE (same format,
one per line). PULSE_DEV_PRINCIPAL lets local development skip the cookie, but only when PULSE_ENV=dev AND the API
host (config api_host) is a loopback address; any other combination refuses to start (``validate_dev_principal``).
A cookie that is presented but unknown is always 403: it never falls back to the dev principal.
Not covered: login UI, token issuance, multi-tenant authorization.
"""
from __future__ import annotations

import ipaddress
import os
from functools import lru_cache
from pathlib import Path
from uuid import UUID

from fastapi import Request

from pulse import config
from pulse.api.envelope import ApiError


@lru_cache(maxsize=1)
def _sessions() -> dict[str, UUID]:
    raw = os.environ.get("PULSE_SESSIONS", "")
    path = os.environ.get("PULSE_SESSIONS_FILE")
    if path and Path(path).exists():
        raw += "," + Path(path).read_text(encoding="utf-8").replace("\n", ",")
    out: dict[str, UUID] = {}
    for item in raw.split(","):
        if "=" in item:
            token, pid = item.strip().split("=", 1)
            out[token.strip()] = UUID(pid.strip())
    return out


def _is_loopback(host: str) -> bool:
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip().strip("[]")).is_loopback
    except ValueError:
        return False


def validate_dev_principal() -> UUID | None:
    """The dev principal, or None when unset. Raises RuntimeError unless PULSE_ENV=dev and the API host is loopback."""
    dev = os.environ.get("PULSE_DEV_PRINCIPAL")
    if not dev:
        return None
    if os.environ.get("PULSE_ENV") != "dev":
        raise RuntimeError("PULSE_DEV_PRINCIPAL is set but PULSE_ENV is not 'dev': refusing to start with an unauthenticated principal")
    host = config.load().api_host
    if not _is_loopback(host):
        raise RuntimeError(f"PULSE_DEV_PRINCIPAL requires a loopback API host (PULSE_API_HOST), got {host!r}: refusing to start")
    return UUID(dev)


def principal(request: Request) -> UUID:
    token = request.cookies.get("pulse_session")
    if token:                                     # a presented credential is authoritative: no fallback
        pid = _sessions().get(token)
        if pid is not None:
            return pid
        raise ApiError(403, "AUTH_REQUIRED", "需要有效会话")
    dev = validate_dev_principal()
    if dev is not None:
        return dev
    raise ApiError(403, "AUTH_REQUIRED", "需要有效会话")
