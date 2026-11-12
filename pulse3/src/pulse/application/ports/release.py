"""AnalysisRelease port (W02, FINAL_SYSTEM_SPEC release protocol, IMPLEMENTATION_GUIDE section 4).

Why: one place defines how an analysis bundle is built, completed, published and read, so pages never
choose "latest" on their own. The PostgreSQL adapter implements it with the parent-lock/CAS protocol.
Invariants: a READY release has every required component READY; the slot pointer moves only through a
stage that is still the latest issued one on an unchanged base; publication and its outbox event
commit together; a published release is immutable.
Not covered: rollback (the package defines only forward publication), component computation itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Protocol, Sequence
from uuid import UUID

DEGRADATION_POLICIES = ("ALLOW_MISSING", "ALLOW_ANY")
COMPONENT_STATES = ("READY", "MISSING", "FAILED", "STALE")


@dataclass(frozen=True)
class ComponentValue:
    component_key: str
    component_state: str
    artifact_ref: str
    artifact_hash: str | None
    snapshot_id: UUID | None
    input_version_hash: str
    computed_at: datetime | None
    valid_until: datetime | None


@dataclass(frozen=True)
class ReleaseView:
    release_id: UUID
    release_state: str
    decision_as_of: datetime
    availability_policy_version: str
    universe_policy_version: str
    market_rules_version: str
    required_components: tuple[str, ...]
    optional_components: Mapping[str, str]          # component_key -> degradation policy
    release_hash: str | None
    created_at: datetime
    published_at: datetime | None
    components: tuple[ComponentValue, ...] = ()


@dataclass(frozen=True)
class StageRef:
    stage_id: UUID
    slot_key: str
    stage_generation: int
    base_pointer_generation: int
    base_release_id: UUID | None
    release_id: UUID
    decision_as_of: datetime
    input_context_hash: str


@dataclass(frozen=True)
class PublishResult:
    stage_id: UUID
    slot_key: str
    release_id: UUID
    pointer_generation: int
    previous_release_id: UUID | None
    event_id: UUID
    release_hash: str
    published_at: datetime
    degraded_components: tuple[str, ...] = ()
    replayed: bool = False                          # True when the stage had already been published


@dataclass(frozen=True)
class CurrentRelease:
    slot_key: str
    release_id: UUID
    pointer_generation: int
    release_hash: str
    decision_as_of: datetime
    published_at: datetime
    degraded_components: tuple[str, ...] = field(default=())


class AnalysisRelease(Protocol):
    def ensure_slot(self, slot_key: str) -> None: ...

    def create_release(
        self,
        *,
        release_id: UUID,
        decision_as_of: datetime,
        availability_policy_version: str,
        universe_policy_version: str,
        market_rules_version: str,
        required_components: Sequence[str],
        optional_components: Mapping[str, str] | None = None,
        created_at: datetime | None = None,
    ) -> ReleaseView: ...

    def put_component(
        self,
        release_id: UUID,
        component_key: str,
        *,
        component_state: str,
        artifact_ref: str,
        input_version_hash: str,
        artifact_hash: str | None = None,
        snapshot_id: UUID | None = None,
        computed_at: datetime | None = None,
        valid_until: datetime | None = None,
    ) -> ComponentValue: ...

    def mark_ready(
        self,
        release_id: UUID,
        component_key: str,
        *,
        artifact_ref: str,
        artifact_hash: str,
        input_version_hash: str,
        snapshot_id: UUID | None = None,
        computed_at: datetime | None = None,
        valid_until: datetime | None = None,
    ) -> ComponentValue: ...

    def fail_release(self, release_id: UUID) -> None: ...

    def begin_stage(self, *, stage_id: UUID, slot_key: str, release_id: UUID, input_context_hash: str) -> StageRef: ...

    def publish(self, stage_id: UUID, *, producer_version: str, event_id: UUID | None = None) -> PublishResult: ...

    def current(self, slot_key: str) -> CurrentRelease | None: ...

    def get_release(self, release_id: UUID) -> ReleaseView: ...
