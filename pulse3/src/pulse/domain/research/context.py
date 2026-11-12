"""EvaluationContext: the fixed, hashed comparison environment for experiments.

Why: STRATEGY_LEAGUE_SPEC §3 / SPEC §9. Candidates are comparable only inside one context
fixing: data snapshots and their closure, decision_as_of + availability policy, universe and
target horizon, market rules / execution / cost / ledger / performance / numeric policies,
benchmarks, resource budget and train / validation / blind intervals (+ embargo).
Invariants: ``context_hash`` is a pure function of the whole specification (any change ->
new context); train < validation <= blind, intervals positive, embargo >= 0; a reconstructed
availability policy must declare its delay assumption; a context needs at least one input
snapshot, a benchmark list and an explicit resource budget; the id is derived from the hash.
Not covered: sealing in PG (research_repo), running experiments.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from pulse.contracts import to_wire
from pulse.domain.strategy.canonical import canonical_hash, stable_uuid


class ContextError(ValueError):
    pass


@dataclass(frozen=True)
class InputSnapshot:
    snapshot_id: UUID
    dataset_id: str
    manifest_version: int
    content_hash: str
    input_role: str  # bars / evidence / sector_membership / calendar ...


@dataclass(frozen=True)
class EvaluationContextSpec:
    forecast_target: str
    horizon_id: str
    decision_as_of: datetime
    availability_policy_version: str
    availability_mode: str  # observed | reconstructed
    availability_rule_ref: dict[str, Any]  # ArtifactRef
    assumed_ingestion_delay_ms: int | None
    assumption_note: str | None
    universe_policy_version: str
    inputs: tuple[InputSnapshot, ...]
    market_rules_version: str
    execution_policy_version: str
    cost_policy_version: str
    ledger_policy_version: str
    performance_policy_version: str
    numeric_policy_version: str
    train_start: datetime
    train_end: datetime
    validation_start: datetime
    validation_end: datetime
    blind_start: datetime | None
    blind_end: datetime | None
    embargo_seconds: int
    benchmark_specification: dict[str, Any]
    resource_budget: dict[str, Any]  # e.g. max_trials, compute units, blind_access_budget
    extra: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.inputs:
            raise ContextError("a context needs at least one input snapshot")
        if self.availability_mode not in ("observed", "reconstructed"):
            raise ContextError("availability_mode must be observed or reconstructed")
        if self.availability_mode == "reconstructed" and (self.assumed_ingestion_delay_ms is None or not self.assumption_note):
            raise ContextError("a reconstructed availability policy must declare its delay assumption")
        if not (self.train_end > self.train_start and self.validation_end > self.validation_start
                and self.validation_start >= self.train_end):
            raise ContextError("need train < validation, each with positive length")
        if (self.blind_start is None) != (self.blind_end is None):
            raise ContextError("blind_start and blind_end go together")
        if self.blind_start is not None and not (self.blind_start >= self.validation_end and self.blind_end > self.blind_start):  # type: ignore[operator]
            raise ContextError("blind interval must follow validation and be positive")
        if self.embargo_seconds < 0:
            raise ContextError("embargo_seconds must be >= 0")
        if not self.benchmark_specification.get("benchmarks"):
            raise ContextError("benchmark_specification.benchmarks must list the comparison baselines")
        if not self.resource_budget or "max_trials" not in self.resource_budget:
            raise ContextError("resource_budget must declare max_trials (and other compute limits)")

    def hash_body(self) -> dict[str, Any]:
        body = {k: getattr(self, k) for k in self.__dataclass_fields__}
        body["inputs"] = sorted(
            (to_wire(i.__dict__) for i in self.inputs), key=lambda d: (d["input_role"], d["snapshot_id"])
        )
        return body

    def context_hash(self) -> str:
        self.validate()
        return canonical_hash(self.hash_body())

    def context_id(self) -> UUID:
        return stable_uuid("evaluation_context", self.context_hash())

    def access_policy(self) -> dict[str, Any]:
        return {"resource_budget": self.resource_budget, "blind_access_budget": self.resource_budget.get("blind_access_budget", 1)}

    def to_wire(self, *, sealed_at: datetime) -> dict[str, Any]:
        """The ``EvaluationContext`` contract payload."""
        self.validate()
        return to_wire(
            {
                "evaluation_context_id": self.context_id(),
                "context_hash": self.context_hash(),
                "forecast_target": self.forecast_target,
                "horizon_id": self.horizon_id,
                "decision_as_of": self.decision_as_of,
                "availability_policy": {
                    "availability_policy_version": self.availability_policy_version,
                    "mode": self.availability_mode,
                    "available_at_semantics": "actual_processing_completion",
                    "rule_ref": self.availability_rule_ref,
                    "assumed_ingestion_delay_ms": self.assumed_ingestion_delay_ms,
                    "assumption_note": self.assumption_note,
                },
                "universe_policy_version": self.universe_policy_version,
                "input_snapshot_refs": [
                    {"snapshot_id": i.snapshot_id, "dataset_id": i.dataset_id,
                     "manifest_version": i.manifest_version, "content_hash": i.content_hash}
                    for i in self.inputs
                ],
                "train_start": self.train_start, "train_end": self.train_end,
                "validation_start": self.validation_start, "validation_end": self.validation_end,
                "blind_start": self.blind_start, "blind_end": self.blind_end,
                "embargo_seconds": self.embargo_seconds,
                "sealed_at": sealed_at,
            }
        )
