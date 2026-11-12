"""Strategy lifecycle state machine.

Why: SPEC §9 fixes discovered -> specified -> data_ready -> screened -> validated -> shadow
-> active; paused and retired can be triggered by rules and keep a re-evaluation path.
Invariants: forward moves are exactly one step (no skipping); only a decision with
``outcome = approved`` advances; ``active`` is reachable only from ``shadow``; paused/retired
need a reason; re-evaluation: paused -> screened (re-screen) or retired; retired ->
specified (new data/formula/mechanism, with a revival reference). Implementation status,
reproduction status and health are separate fields and never imply lifecycle.
Not covered: gate evidence (promotion.py), persistence (strategy_repo.apply_transition).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

LIFECYCLE = ("discovered", "specified", "data_ready", "screened", "validated", "shadow", "active")
ALL_STATES = LIFECYCLE + ("paused", "retired")
PAUSABLE = {"screened", "validated", "shadow", "active"}


class LifecycleError(ValueError):
    pass


def allowed_targets(state: str) -> set[str]:
    if state not in ALL_STATES:
        raise LifecycleError(f"unknown lifecycle {state!r}")
    out: set[str] = set()
    if state in LIFECYCLE and state != "active":
        out.add(LIFECYCLE[LIFECYCLE.index(state) + 1])
    if state in PAUSABLE:
        out.add("paused")
    if state != "retired":
        out.add("retired")
    if state == "paused":
        out |= {"screened"}
    if state == "retired":
        out |= {"specified"}
    return out


@dataclass(frozen=True)
class LifecycleTransition:
    instance_id: str
    from_state: str
    to_state: str
    reason: str
    decided_at: datetime
    policy_version: str
    revival_ref: str | None = None

    def validate(self) -> None:
        if self.to_state not in allowed_targets(self.from_state):
            raise LifecycleError(f"illegal lifecycle transition {self.from_state} -> {self.to_state}")
        if not self.reason.strip():
            raise LifecycleError("a transition needs a reason")
        if self.from_state == "retired" and not self.revival_ref:
            raise LifecycleError("reviving a retired strategy needs a revival reference")


def is_forward(from_state: str, to_state: str) -> bool:
    return from_state in LIFECYCLE and to_state in LIFECYCLE and LIFECYCLE.index(to_state) > LIFECYCLE.index(from_state)
