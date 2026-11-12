"""PlanCompiler: strategy role outputs -> formal TradePlan (contract wire dict).

Why: SPEC §7/§7.1 and STRATEGY_LEAGUE_SPEC §2: selection, allocation, entry, holding,
execution and exit strategies each emit a typed output; the compiler composes them into one
plan with trigger conditions, raw price zone, sizing, horizon, exit and invalidation.
Invariants: compile output is state ``watching`` with validation ``pending`` - draft/compiled
are not formal states and ``actionable`` is reached only through ``attach_validation`` with a
passed validation AND every referenced strategy instance admitted (lifecycle in the allowed
set ``ADMITTED_DEFAULT`` = only ``active``; not overridable by callers); an incomplete role set raises ``CompileError``; all condition
trees pass static validation (unit-consistent); no price is invented (zone may be None).
Not covered: running the validation itself (research/execution), quantity from the live
account (done at actionability time with the account snapshot).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pulse.contracts import to_wire, utc_z
from pulse.domain.plans.conditions import validate_condition
from pulse.domain.plans.versions import PlanVersionError, check_append, latest, next_version

REQUIRED_ROLES = ("entry", "exit", "allocation", "holding")
ADMITTED_DEFAULT = frozenset({"active"})


class CompileError(ValueError):
    pass


@dataclass(frozen=True)
class StrategyOutput:
    """One strategy instance's typed output for one role."""

    role: str
    instance_ref: dict[str, Any]  # {instance_id, strategy_id, strategy_version}
    lifecycle: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class CompileResult:
    plan: dict[str, Any]
    not_admitted: tuple[str, ...]
    warnings: tuple[str, ...] = field(default=())


def _single(outputs: Sequence[StrategyOutput], role: str) -> StrategyOutput:
    found = [o for o in outputs if o.role == role]
    if not found:
        raise CompileError(f"missing role output: {role}")
    if len(found) > 1:
        raise CompileError(f"role {role} has {len(found)} outputs; compose them in a new StrategyVersion first")
    return found[0]


def _check_zone(zone: dict[str, Any] | None) -> None:
    if zone is None:
        return
    if Decimal(str(zone["lower_price_raw"])) > Decimal(str(zone["upper_price_raw"])):
        raise CompileError("price zone lower bound above upper bound")
    if zone.get("adjustment_mode") != "raw" or zone.get("currency") != "CNY":
        raise CompileError("buy/sell zones are raw CNY prices")


def compile_plan(
    *,
    plan_id: UUID,
    instrument_id: UUID,
    release_id: UUID,
    outputs: Sequence[StrategyOutput],
    valid_from: datetime,
    expires_at: datetime,
    available_at: datetime,
    validation_policy_version: str,
    account_id: UUID | None = None,
    expected_account_version: int | None = None,
    model_run_id: UUID | None = None,
    evidence_revision_ids: Sequence[UUID] = (),
    basis_ranking_state: str = "ready",
) -> CompileResult:
    if basis_ranking_state != "ready":
        # RECONCILIATION §6: an old/stale ranking is read-only; it never seeds a new plan.
        raise CompileError(f"plan cannot be derived from a {basis_ranking_state} ranking")
    if (account_id is None) != (expected_account_version is None):
        raise CompileError("account_id and expected_account_version go together")
    if expires_at <= valid_from:
        raise CompileError("expires_at must be after valid_from")
    entry, exit_, alloc, hold = (_single(outputs, r) for r in REQUIRED_ROLES)
    execution = next((o for o in outputs if o.role == "execution"), None)
    for tag, cond in (("entry", entry.payload["entry_conditions"]), ("exit", exit_.payload["exit_conditions"]),
                      ("invalidation", exit_.payload["invalidation"])):
        try:
            validate_condition(cond)
        except ValueError as exc:
            raise CompileError(f"{tag} condition invalid: {exc}") from exc
    _check_zone(entry.payload.get("entry_price_zone"))
    refs_unique: list[dict[str, Any]] = []
    for o in outputs:
        if o.instance_ref["instance_id"] not in {r["instance_id"] for r in refs_unique}:
            refs_unique.append(o.instance_ref)
    not_admitted = tuple(sorted({str(o.instance_ref["instance_id"]) for o in outputs if o.lifecycle not in ADMITTED_DEFAULT}))
    plan = to_wire(
        {
            "plan_id": plan_id,
            "plan_version": 1,
            "instrument_id": instrument_id,
            "based_on_release_id": release_id,
            "account_id": account_id,
            "expected_account_version": expected_account_version,
            "state": "watching",
            "strategy_refs": refs_unique,
            "model_run_id": model_run_id,
            "evidence_revision_ids": list(evidence_revision_ids),
            "position_side": "long",
            "entry_conditions": entry.payload["entry_conditions"],
            "entry_price_zone": entry.payload.get("entry_price_zone"),
            "sizing": alloc.payload["sizing"],
            "exit_conditions": exit_.payload["exit_conditions"],
            "invalidation": exit_.payload["invalidation"],
            "holding_horizon": hold.payload["holding_horizon"],
            "trigger_policy": entry.payload["trigger_policy"],
            "valid_from": valid_from,
            "expires_at": expires_at,
            "replace_policy": (execution.payload["replace_policy"] if execution else "cancel_remaining_and_reconcile"),
            "validation": {
                "status": "pending", "policy_version": validation_policy_version,
                "evaluation_id": None, "result_ref": None, "validated_at": None,
            },
            "available_at": available_at,
            "supersedes": None,
        }
    )
    warnings = tuple(f"strategy_not_admitted:{i}" for i in not_admitted)
    return CompileResult(plan, not_admitted, warnings)


def attach_validation(
    history: Sequence[dict[str, Any]],
    *,
    status: str,
    result_ref: str,
    validated_at: datetime,
    available_at: datetime,
    evaluation_id: UUID | None,
    strategy_lifecycles: dict[str, str],
) -> dict[str, Any]:
    """Append the next version carrying a validation result (actionable only when passed)."""
    last = latest(history)
    if last is None:
        raise PlanVersionError("no plan to validate")
    validation = {
        "status": status, "policy_version": last["validation"]["policy_version"],
        "evaluation_id": None if evaluation_id is None else str(evaluation_id),
        "result_ref": result_ref, "validated_at": utc_z(validated_at),
    }
    target = "watching"
    if status == "passed":
        bad = [r["instance_id"] for r in last["strategy_refs"]
               if strategy_lifecycles.get(r["instance_id"]) not in ADMITTED_DEFAULT]
        if bad:
            raise PlanVersionError(f"strategy not admitted for actionable plans: {bad}")
        target = "actionable"
    new = next_version(last, state=target, available_at=available_at, validation=validation)
    check_append(history, new)
    return new
