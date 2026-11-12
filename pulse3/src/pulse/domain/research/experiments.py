"""Experiment registry: every trial is recorded, failures and eliminations included.

Why: SPEC §9 "记录全部试验、淘汰候选和预算" and STRATEGY_LEAGUE_SPEC §6. Repeated tuning and
repeated looks at the holdout consume independent information; the registry keeps the trial
count per trial family (so multiple-testing corrections can use the true number) and the
number of blind accesses per context.
Invariants: a run is registered BEFORE it runs (queued) and is never deleted; a failed run
keeps ``error_code`` and is not an empty success; parameter variants of one family count as
trials of that family (family = overlap policy of the catalog), not as independent evidence;
blind_final runs are limited by the context's ``blind_access_budget``; a completed run is
immutable. Idempotence: ``fingerprint`` identifies a repeated experiment (same instance,
hypothesis, context, purpose, seed, implementation).
Not covered: running the experiment, metrics (research.metric_value rows via the repo).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from uuid import UUID

from pulse.contracts import to_wire
from pulse.domain.strategy.canonical import canonical_hash, stable_uuid

PURPOSES = ("screening", "validation", "shadow", "blind_final")
STATES = ("queued", "running", "completed", "failed", "cancelled")


class ExperimentError(ValueError):
    pass


class BlindBudgetExceeded(ExperimentError):
    pass


@dataclass(frozen=True)
class ExperimentRecord:
    experiment_id: UUID
    instance_id: UUID
    hypothesis_id: UUID
    hypothesis_version: int
    evaluation_context_id: UUID
    run_purpose: str
    run_state: str
    implementation_digest: str
    random_seed: int
    run_manifest: dict[str, Any]
    lease_epoch: int
    family_key: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result_ref: str | None = None
    result_hash: str | None = None
    error_code: str | None = None

    @property
    def fingerprint(self) -> str:
        return canonical_hash(
            [self.instance_id, self.hypothesis_id, self.hypothesis_version, self.evaluation_context_id,
             self.run_purpose, self.random_seed, self.implementation_digest]
        )


def new_run(
    *, instance_id: UUID, hypothesis_id: UUID, hypothesis_version: int, evaluation_context_id: UUID,
    run_purpose: str, implementation_digest: str, random_seed: int, run_manifest: dict[str, Any],
    family_key: str, lease_epoch: int = 1,
) -> ExperimentRecord:
    if run_purpose not in PURPOSES:
        raise ExperimentError(f"unknown run_purpose {run_purpose!r}")
    if random_seed < 0:
        raise ExperimentError("random_seed must be >= 0")
    eid = stable_uuid("experiment", instance_id, hypothesis_id, hypothesis_version, evaluation_context_id,
                      run_purpose, random_seed, implementation_digest, lease_epoch)
    return ExperimentRecord(eid, instance_id, hypothesis_id, hypothesis_version, evaluation_context_id, run_purpose,
                            "queued", implementation_digest, random_seed, run_manifest, lease_epoch, family_key)


def complete(rec: ExperimentRecord, *, started_at: datetime, completed_at: datetime, result_ref: str, result_hash: str) -> ExperimentRecord:
    if rec.run_state not in ("queued", "running"):
        raise ExperimentError(f"cannot complete a {rec.run_state} run")
    return replace(rec, run_state="completed", started_at=started_at, completed_at=completed_at,
                   result_ref=result_ref, result_hash=result_hash)


def fail(rec: ExperimentRecord, *, started_at: datetime | None, completed_at: datetime, error_code: str) -> ExperimentRecord:
    if rec.run_state not in ("queued", "running"):
        raise ExperimentError(f"cannot fail a {rec.run_state} run")
    if not error_code:
        raise ExperimentError("a failed run needs an error_code (failure is not an empty success)")
    return replace(rec, run_state="failed", started_at=started_at, completed_at=completed_at, error_code=error_code)


class TrialLedger:
    """In-memory registry semantics (the PG tables are the durable form)."""

    def __init__(self, *, blind_access_budget: int = 1) -> None:
        self._runs: dict[UUID, ExperimentRecord] = {}
        self._blind_budget = blind_access_budget

    def register(self, rec: ExperimentRecord) -> ExperimentRecord:
        if rec.experiment_id in self._runs:
            return self._runs[rec.experiment_id]  # idempotent re-registration
        if rec.run_purpose == "blind_final":
            used = sum(1 for r in self._runs.values()
                       if r.run_purpose == "blind_final" and r.evaluation_context_id == rec.evaluation_context_id)
            if used >= self._blind_budget:
                raise BlindBudgetExceeded(f"blind access budget {self._blind_budget} exhausted for this context")
        self._runs[rec.experiment_id] = rec
        return rec

    def update(self, rec: ExperimentRecord) -> None:
        old = self._runs[rec.experiment_id]
        if old.run_state in ("completed", "failed", "cancelled"):
            raise ExperimentError("a closed run is immutable")
        self._runs[rec.experiment_id] = rec

    def all(self) -> list[ExperimentRecord]:
        return list(self._runs.values())

    def trial_count(self, family_key: str) -> int:
        """Trials of a family INCLUDING failed and cancelled ones (multiple-testing denominator)."""
        return sum(1 for r in self._runs.values() if r.family_key == family_key)

    def counts_by_state(self) -> Counter[str]:
        return Counter(r.run_state for r in self._runs.values())


def evaluation_wire(rec: ExperimentRecord, context_wire: dict[str, Any], policy_versions: dict[str, str],
                    hypothesis_ref: tuple[UUID, int], metrics: list[dict[str, Any]],
                    result_artifact: dict[str, Any] | None) -> dict[str, Any]:
    """The ``Evaluation`` contract payload."""
    return to_wire(
        {
            "experiment_id": rec.experiment_id, "instance_id": rec.instance_id,
            "evaluation_context_id": rec.evaluation_context_id, "context_hash": context_wire["context_hash"],
            "hypothesis_id": hypothesis_ref[0], "hypothesis_version": hypothesis_ref[1],
            "run_purpose": rec.run_purpose, "run_state": rec.run_state, "policy_versions": policy_versions,
            "started_at": rec.started_at, "completed_at": rec.completed_at,
            "result": result_artifact, "metrics": metrics, "error_code": rec.error_code, "context": context_wire,
        }
    )
