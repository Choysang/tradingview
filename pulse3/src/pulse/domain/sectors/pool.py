"""Fast-heating sector candidate pool: surge components and the pool state history.

Why: SPEC §6 "快速升温". A sector is scored by seven components - relative-strength
change, turnover acceleration, breadth, leader confirmation, news novelty, community
growth, fund proxy. Each may be missing and is then shown as missing (never 0).
Thresholds are *research parameters* carried in a versioned policy, not truths.
Invariants:
  * a partial composite is never promoted past ``observing`` unless the policy's required
    components are present (no per-row/per-component re-weighting to hide gaps);
  * enter threshold > exit threshold (hysteresis); a minimum dwell prevents flapping;
    a hard invalidation (suspension, membership removed...) overrides dwell;
  * only state *changes* are events; each event has a monotone ``event_sequence`` per
    (pool, instrument) and a deterministic id so redelivery is idempotent;
  * first event is ``observing`` with sequence 1; ``actionable`` needs a validated plan.
Not covered: calibrating thresholds on a training window; the lead-time test of the
leading features at true available_at (a research task, SPEC §6 last paragraph).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Any
from uuid import UUID

from pulse.contracts import to_wire
from pulse.domain.strategy.canonical import dec_ctx, quantize12, stable_uuid

COMPONENTS = (
    "relative_strength_change",
    "turnover_acceleration",
    "breadth",
    "leader_confirmation",
    "news_novelty",
    "community_growth",
    "fund_proxy",
)
STATES = ("observing", "qualified", "actionable", "cooling", "removed")
_ALLOWED = {
    "observing": {"qualified", "removed"},
    "qualified": {"actionable", "cooling", "removed"},
    "actionable": {"cooling", "removed", "qualified"},
    "cooling": {"qualified", "removed"},
    "removed": {"observing"},
}


@dataclass(frozen=True)
class SurgeComponents:
    """Each field is an already-standardised dimensionless value, or None = missing."""

    relative_strength_change: Decimal | None = None
    turnover_acceleration: Decimal | None = None
    breadth: Decimal | None = None
    leader_confirmation: Decimal | None = None
    news_novelty: Decimal | None = None
    community_growth: Decimal | None = None
    fund_proxy: Decimal | None = None

    def as_map(self) -> dict[str, Decimal | None]:
        return {c: getattr(self, c) for c in COMPONENTS}


@dataclass(frozen=True)
class SurgeAssessment:
    score: Decimal | None
    present: tuple[str, ...]
    missing: tuple[str, ...]
    contributions: Mapping[str, Decimal]
    complete: bool


@dataclass(frozen=True)
class PoolPolicy:
    policy_id: str
    policy_version: str
    weights: Mapping[str, Decimal]
    enter_threshold: Decimal
    exit_threshold: Decimal
    min_dwell_evaluations: int
    cooling_max_evaluations: int
    required_components: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.exit_threshold >= self.enter_threshold:
            raise ValueError("hysteresis requires exit_threshold < enter_threshold")
        unknown = (set(self.weights) | set(self.required_components)) - set(COMPONENTS)
        if unknown:
            raise ValueError(f"unknown surge components: {sorted(unknown)}")
        if not self.required_components <= set(self.weights):
            raise ValueError("a required component must carry a weight")


def assess_surge(policy: PoolPolicy, components: SurgeComponents) -> SurgeAssessment:
    """Score = sum(weight * value) over present components; missing ones are listed."""
    vals = components.as_map()
    present = tuple(c for c in COMPONENTS if vals[c] is not None and c in policy.weights)
    missing = tuple(c for c in COMPONENTS if vals[c] is None)
    if not present:
        return SurgeAssessment(None, (), missing, {}, False)
    with localcontext(dec_ctx()):
        contrib = {c: quantize12(policy.weights[c] * vals[c]) for c in present}  # type: ignore[operator]
        score = quantize12(sum((policy.weights[c] * vals[c] for c in present), Decimal(0)))  # type: ignore[operator]
    complete = all(c in present for c in policy.weights)
    return SurgeAssessment(score, present, missing, contrib, complete)


@dataclass(frozen=True)
class PoolState:
    state: str
    event_sequence: int
    dwell_evaluations: int = 0  # evaluations spent in ``state``


@dataclass(frozen=True)
class PoolEvent:
    pool_event_id: UUID
    pool_id: UUID
    instrument_id: UUID
    event_sequence: int
    policy_id: str
    policy_version: str
    taxonomy_id: str
    taxonomy_version: int
    node_id: str
    membership_snapshot_id: UUID
    release_id: UUID
    previous_state: str | None
    current_state: str
    trigger_code: str
    trigger_payload: Mapping[str, Any]
    plan_ref: tuple[UUID, int] | None
    decision_as_of: datetime
    available_at: datetime
    state: PoolState = field(default=PoolState("observing", 1))


def _check_transition(prev: str | None, new: str) -> None:
    if prev is None:
        if new != "observing":
            raise ValueError("the first pool event must be 'observing'")
    elif new not in _ALLOWED[prev]:
        raise ValueError(f"illegal pool transition {prev} -> {new}")


def _decide(
    policy: PoolPolicy,
    prev: PoolState | None,
    assessment: SurgeAssessment,
    *,
    plan_ref: tuple[UUID, int] | None = None,
    plan_validated: bool = False,
    hard_invalidation: str | None = None,
) -> tuple[PoolState, str, dict[str, Any]] | None:
    """Return (new state, trigger_code, payload) or None when nothing changes."""
    required_ok = all(c in assessment.present for c in policy.required_components)
    score = assessment.score
    base_payload: dict[str, Any] = {
        "score": score,
        "present_components": list(assessment.present),
        "missing_components": list(assessment.missing),
        "enter_threshold": policy.enter_threshold,
        "exit_threshold": policy.exit_threshold,
    }
    if prev is None:
        return PoolState("observing", 1, 1), "entered_pool", base_payload
    cur = prev.state
    dwell = prev.dwell_evaluations + 1

    def move(new: str, code: str, extra: dict[str, Any] | None = None):
        _check_transition(cur, new)
        return PoolState(new, prev.event_sequence + 1, 1), code, {**base_payload, **(extra or {})}

    if hard_invalidation is not None:
        if cur == "removed":
            return None
        return move("removed", "hard_invalidation", {"invalidation": hard_invalidation})
    if cur == "removed":
        if required_ok and score is not None and score >= policy.enter_threshold:
            return move("observing", "re_entered_pool")
        return None
    # An unknown score never promotes and never demotes: the state is held.
    if score is None:
        return None
    if cur == "observing":
        if required_ok and score >= policy.enter_threshold and dwell >= policy.min_dwell_evaluations:
            return move("qualified", "score_above_enter_threshold", {"dwell": dwell})
        return None
    if cur == "qualified":
        if score < policy.exit_threshold and dwell >= policy.min_dwell_evaluations:
            return move("cooling", "score_below_exit_threshold", {"dwell": dwell})
        if plan_ref is not None and plan_validated and required_ok and score >= policy.enter_threshold:
            return move("actionable", "validated_plan_attached", {"plan_ref": plan_ref})
        return None
    if cur == "actionable":
        if score < policy.exit_threshold and dwell >= policy.min_dwell_evaluations:
            return move("cooling", "score_below_exit_threshold", {"dwell": dwell})
        if plan_ref is None or not plan_validated:
            return move("qualified", "plan_lost_validation")
        return None
    if cur == "cooling":
        if required_ok and score >= policy.enter_threshold:
            return move("qualified", "score_recovered")
        if dwell >= policy.cooling_max_evaluations:
            return move("removed", "cooling_expired", {"dwell": dwell})
        return None
    raise AssertionError(cur)


def decide_next_state(
    policy: PoolPolicy,
    prev: PoolState | None,
    assessment: SurgeAssessment,
    *,
    plan_ref: tuple[UUID, int] | None = None,
    plan_validated: bool = False,
    hard_invalidation: str | None = None,
) -> tuple[PoolState, tuple[PoolState, str, dict[str, Any]] | None]:
    """Evaluate once. Returns (state after this evaluation, change-or-None).

    When nothing changes the state is returned with its dwell counter advanced so the
    caller can persist the counter; only a change is an event (see ``make_event``).
    """
    change = _decide(
        policy, prev, assessment, plan_ref=plan_ref, plan_validated=plan_validated,
        hard_invalidation=hard_invalidation,
    )
    if change is not None:
        return change[0], change
    assert prev is not None
    return PoolState(prev.state, prev.event_sequence, prev.dwell_evaluations + 1), None


def make_event(
    *,
    pool_id: UUID,
    instrument_id: UUID,
    policy: PoolPolicy,
    prev: PoolState | None,
    new: tuple[PoolState, str, dict[str, Any]],
    taxonomy: tuple[str, int, str],
    membership_snapshot_id: UUID,
    release_id: UUID,
    plan_ref: tuple[UUID, int] | None,
    decision_as_of: datetime,
    available_at: datetime,
) -> PoolEvent:
    state, code, payload = new
    if available_at < decision_as_of:
        raise ValueError("available_at must not precede decision_as_of")
    if state.state == "actionable" and plan_ref is None:
        raise ValueError("actionable requires a validated plan reference")
    _check_transition(None if prev is None else prev.state, state.state)
    return PoolEvent(
        pool_event_id=stable_uuid("pool_event", pool_id, instrument_id, state.event_sequence),
        pool_id=pool_id,
        instrument_id=instrument_id,
        event_sequence=state.event_sequence,
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        taxonomy_id=taxonomy[0],
        taxonomy_version=taxonomy[1],
        node_id=taxonomy[2],
        membership_snapshot_id=membership_snapshot_id,
        release_id=release_id,
        previous_state=None if prev is None else prev.state,
        current_state=state.state,
        trigger_code=code,
        trigger_payload=to_wire(payload),
        plan_ref=plan_ref if state.state == "actionable" else None,
        decision_as_of=decision_as_of,
        available_at=available_at,
        state=state,
    )


def candidate_pool_wire(
    *, pool_id: UUID, release_id: UUID, policy_id: str, policy_version: str, current: list[PoolEvent]
) -> dict[str, Any]:
    """``CandidatePool`` contract payload: the latest event per instrument."""
    latest: dict[UUID, PoolEvent] = {}
    for e in sorted(current, key=lambda e: e.event_sequence):
        latest[e.instrument_id] = e
    items = [
        {
            "pool_event_id": e.pool_event_id,
            "instrument_id": e.instrument_id,
            "event_sequence": e.event_sequence,
            "taxonomy_node": {"taxonomy_id": e.taxonomy_id, "taxonomy_version": e.taxonomy_version, "node_id": e.node_id},
            "membership_snapshot_id": e.membership_snapshot_id,
            "current_state": e.current_state,
            "trigger_code": e.trigger_code,
            "plan_ref": None if e.plan_ref is None else {"plan_id": e.plan_ref[0], "plan_version": e.plan_ref[1]},
            "available_at": e.available_at,
        }
        for e in sorted(latest.values(), key=lambda e: str(e.instrument_id))
    ]
    return to_wire(
        {"pool_id": pool_id, "release_id": release_id, "policy_id": policy_id, "policy_version": policy_version, "items": items}
    )
