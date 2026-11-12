"""PromotionPolicy and promotion decisions - independent of league standing.

Why: SPEC §9 / STRATEGY_LEAGUE_SPEC §6. Formal promotion requires executable definition,
data/rules satisfied, kernel benchmark passed, registered training/validation, a repeatable
holdout increment over a reasonable baseline, explainable cost/execution sensitivity,
forward shadow within policy and a marginal portfolio contribution. Thresholds are written
in a *versioned policy* (never invented here, never lowered ad hoc for a paper).
Invariants:
  * ``PromotionEvidence`` has NO league field: league rank/score/Elo can never grant a state
    or a weight (tested by signature);
  * a gate with unknown evidence is ``unknown`` and blocks approval - never defaulted to pass;
  * approved => exactly one forward step (``lifecycle.allowed_targets``); rejected keeps the
    old state and still records every gate;
  * pausing/retiring from a drift signal requires that data outage and cost-profile change
    were ruled out first (SPEC §6 of the league spec).
Not covered: computing the evidence numbers (research jobs), persistence.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from decimal import Decimal
from typing import Any

from pulse.domain.strategy.lifecycle import LifecycleError, LifecycleTransition

GATES_BY_TARGET: dict[str, tuple[str, ...]] = {
    "specified": ("definition_typed",),
    "data_ready": ("data_requirements_met",),
    "screened": ("screening_completed", "trials_registered"),
    "validated": (
        "kernel_benchmark_passed", "training_validation_registered", "trials_registered",
        "holdout_increment_positive", "holdout_accessed_once", "cost_sensitivity_explained",
        "independent_samples_sufficient",
    ),
    "shadow": ("shadow_plan_registered",),
    "active": (
        "kernel_benchmark_passed", "forward_shadow_within_policy", "marginal_portfolio_contribution",
        "independent_forward_samples_sufficient",
    ),
}


@dataclass(frozen=True)
class PromotionPolicy:
    policy_version: str
    min_independent_samples: int
    min_forward_samples: int
    min_holdout_increment_lower: Decimal
    max_forward_shadow_gap: Decimal
    min_marginal_contribution: Decimal


@dataclass(frozen=True)
class PromotionEvidence:
    """Evidence only. Deliberately no league rank/score field."""

    definition_typed: bool | None = None
    data_requirements_met: bool | None = None
    screening_completed: bool | None = None
    trials_registered: bool | None = None
    kernel_benchmark_passed: bool | None = None
    training_validation_registered: bool | None = None
    holdout_increment_lower: Decimal | None = None
    holdout_accessed_once: bool | None = None
    cost_sensitivity_explained: bool | None = None
    independent_samples: int | None = None
    shadow_plan_registered: bool | None = None
    forward_shadow_gap: Decimal | None = None
    marginal_contribution: Decimal | None = None
    independent_forward_samples: int | None = None


@dataclass(frozen=True)
class GateResult:
    gate: str
    status: str  # pass | fail | unknown
    detail: str


@dataclass(frozen=True)
class PromotionDecisionRecord:
    instance_id: str
    evaluation_experiment_id: str
    policy_version: str
    from_state: str
    to_state: str  # equals from_state when rejected
    outcome: str  # approved | rejected
    gate_results: tuple[GateResult, ...]
    reason: str
    decided_at: datetime

    def gate_results_json(self) -> list[dict[str, Any]]:
        return [{"gate": g.gate, "status": g.status, "detail": g.detail} for g in self.gate_results]


def _bool_gate(name: str, v: bool | None) -> GateResult:
    if v is None:
        return GateResult(name, "unknown", "no evidence supplied")
    return GateResult(name, "pass" if v else "fail", str(v))


def evaluate_gates(policy: PromotionPolicy, ev: PromotionEvidence, target: str) -> tuple[GateResult, ...]:
    if target not in GATES_BY_TARGET:
        raise LifecycleError(f"no promotion gates defined for target {target!r}")
    out: list[GateResult] = []
    for g in GATES_BY_TARGET[target]:
        if g == "holdout_increment_positive":
            v = ev.holdout_increment_lower
            out.append(GateResult(g, "unknown", "no evidence supplied") if v is None else
                       GateResult(g, "pass" if v > policy.min_holdout_increment_lower else "fail",
                                  f"lower_bound={v} threshold={policy.min_holdout_increment_lower}"))
        elif g == "independent_samples_sufficient":
            v = ev.independent_samples
            out.append(GateResult(g, "unknown", "no evidence supplied") if v is None else
                       GateResult(g, "pass" if v >= policy.min_independent_samples else "fail",
                                  f"n={v} min={policy.min_independent_samples}"))
        elif g == "independent_forward_samples_sufficient":
            v = ev.independent_forward_samples
            out.append(GateResult(g, "unknown", "no evidence supplied") if v is None else
                       GateResult(g, "pass" if v >= policy.min_forward_samples else "fail",
                                  f"n={v} min={policy.min_forward_samples}"))
        elif g == "forward_shadow_within_policy":
            v = ev.forward_shadow_gap
            out.append(GateResult(g, "unknown", "no evidence supplied") if v is None else
                       GateResult(g, "pass" if abs(v) <= policy.max_forward_shadow_gap else "fail",
                                  f"gap={v} max={policy.max_forward_shadow_gap}"))
        elif g == "marginal_portfolio_contribution":
            v = ev.marginal_contribution
            out.append(GateResult(g, "unknown", "no evidence supplied") if v is None else
                       GateResult(g, "pass" if v > policy.min_marginal_contribution else "fail",
                                  f"marginal={v} min={policy.min_marginal_contribution}"))
        else:
            out.append(_bool_gate(g, getattr(ev, g)))
    return tuple(out)


def decide_promotion(
    policy: PromotionPolicy,
    *,
    instance_id: str,
    evaluation_experiment_id: str,
    from_state: str,
    target_state: str,
    evidence: PromotionEvidence,
    decided_at: datetime,
) -> PromotionDecisionRecord:
    """Gate a one-step forward move. Approval needs every gate to pass."""
    LifecycleTransition(instance_id, from_state, target_state, "gate check", decided_at, policy.policy_version).validate()
    if target_state not in GATES_BY_TARGET:
        raise LifecycleError(f"{target_state!r} is not a promotion target (use decide_demotion)")
    gates = evaluate_gates(policy, evidence, target_state)
    bad = [g for g in gates if g.status != "pass"]
    if bad:
        reason = "rejected: " + "; ".join(f"{g.gate}={g.status}" for g in bad)
        return PromotionDecisionRecord(instance_id, evaluation_experiment_id, policy.policy_version,
                                       from_state, from_state, "rejected", gates, reason, decided_at)
    return PromotionDecisionRecord(instance_id, evaluation_experiment_id, policy.policy_version,
                                   from_state, target_state, "approved", gates,
                                   f"all {len(gates)} gates passed for {target_state}", decided_at)


def decide_demotion(
    policy: PromotionPolicy,
    *,
    instance_id: str,
    evaluation_experiment_id: str,
    from_state: str,
    target_state: str,
    reason: str,
    data_outage_ruled_out: bool,
    cost_profile_change_ruled_out: bool,
    decided_at: datetime,
) -> PromotionDecisionRecord:
    """Rule-triggered pause/retire. Drift is only blamed on the strategy after the rest is excluded."""
    if target_state not in ("paused", "retired"):
        raise LifecycleError("demotion target must be paused or retired")
    LifecycleTransition(instance_id, from_state, target_state, reason or "-", decided_at, policy.policy_version,
                        revival_ref="n/a").validate()
    gates = (
        GateResult("data_outage_ruled_out", "pass" if data_outage_ruled_out else "fail", str(data_outage_ruled_out)),
        GateResult("cost_profile_change_ruled_out", "pass" if cost_profile_change_ruled_out else "fail",
                   str(cost_profile_change_ruled_out)),
    )
    if not all(g.status == "pass" for g in gates):
        return PromotionDecisionRecord(instance_id, evaluation_experiment_id, policy.policy_version, from_state,
                                       from_state, "rejected", gates,
                                       "rejected: rule out data outage and cost change before blaming the strategy", decided_at)
    return PromotionDecisionRecord(instance_id, evaluation_experiment_id, policy.policy_version, from_state,
                                   target_state, "approved", gates, reason, decided_at)


def evidence_field_names() -> tuple[str, ...]:
    return tuple(f.name for f in fields(PromotionEvidence))

