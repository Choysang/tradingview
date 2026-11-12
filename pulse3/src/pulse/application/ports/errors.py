"""Typed failures shared by the database-backed ports (W02).

Why: application code must tell "retry later", "you lost the race" and "your request contradicts a
recorded fact" apart without parsing driver messages. Adapters translate database errors into these.
Invariant: every class is a plain Exception subclass with no driver dependency.
Not covered: HTTP status mapping (the API layer owns it; VERSION_CONFLICT etc. are chosen there).
"""
from __future__ import annotations


class PortError(Exception):
    """Base class for all W02 port failures."""


class NotFound(PortError):
    """The addressed aggregate does not exist."""


class Conflict(PortError):
    """Same identity, different content (idempotency key, event id, stage retry, hash)."""


class FencedOut(PortError):
    """The caller's lease epoch is no longer the owner; its result must be discarded."""


class StaleStage(PortError):
    """The stage is not the latest issued one, or its fixed base no longer matches the slot pointer."""


class ReleaseStateError(PortError):
    """The release is not in a state that allows the requested operation."""


class IncompleteRelease(PortError):
    """Publication refused: a required component is missing/not READY, expired, or its snapshot unpublished."""

    def __init__(self, message: str, *, missing: tuple[str, ...] = ()):
        super().__init__(message)
        self.missing = missing


class CursorInvalid(PortError):
    """The cursor is malformed or belongs to a different scope."""
