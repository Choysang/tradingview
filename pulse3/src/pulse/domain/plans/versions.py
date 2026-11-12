"""TradePlan version history: append-only versions, supersedes, late-result guard.

Why: SPEC §7 and RECONCILIATION §9: "每个计划修订追加版本并声明supersedes"; a slow model
result must not overwrite a newer formal plan. Formal states are exactly watching,
actionable, holding, superseded, expired (draft/compiled are not states).
Invariants: versions are consecutive integers starting at 1 (numbers burned by unpublished
release attempts, ``dead_versions``, are skipped and never referenced); each new version after the
first declares ``supersedes`` = the previous (plan_id, version); state changes follow
``ALLOWED``; ``available_at`` never moves backwards; ``actionable`` requires a passed
validation (also enforced by the contract); superseded/expired are terminal.
Not covered: storage (decision_repo.append_plan_version), compile (compiler.py).
"""
from __future__ import annotations

import copy
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pulse.contracts import to_wire, utc_z

ALLOWED: dict[str, set[str]] = {
    "watching": {"watching", "actionable", "superseded", "expired"},
    "actionable": {"watching", "holding", "superseded", "expired"},
    "holding": {"holding", "superseded", "expired"},
    "superseded": set(),
    "expired": set(),
}


class PlanVersionError(ValueError):
    pass


def ref(plan: dict[str, Any]) -> dict[str, Any]:
    return {"plan_id": plan["plan_id"], "plan_version": plan["plan_version"]}


def latest(history: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    return max(history, key=lambda p: p["plan_version"]) if history else None


def check_append(history: Sequence[dict[str, Any]], new: dict[str, Any], *, dead_versions: Collection[int] = frozenset()) -> None:
    """Raise unless ``new`` is a legal next version of ``history``.

    ``history`` holds the visible versions only. ``dead_versions`` are version numbers stored by release attempts that
    never published: they stay occupied (rows are immutable) but are not part of the chain, so the next version
    skips past them and supersedes the last visible version.
    """
    last = latest(history)
    floor = max(dead_versions, default=0)
    if last is None:
        if new["plan_version"] != floor + 1 or new["supersedes"] is not None:
            raise PlanVersionError(f"first version must be {floor + 1} and supersede nothing")
        if new["state"] not in ("watching", "actionable", "holding"):
            raise PlanVersionError(f"a plan cannot be born {new['state']}")
        return
    if new["plan_id"] != last["plan_id"]:
        raise PlanVersionError("plan_id mismatch")
    expected = max(last["plan_version"], floor) + 1
    if new["plan_version"] != expected:
        raise PlanVersionError(f"expected version {expected}, got {new['plan_version']}")
    if new["supersedes"] != ref(last):
        raise PlanVersionError("a new version must supersede the previous one")
    if new["state"] not in ALLOWED[last["state"]]:
        raise PlanVersionError(f"illegal state transition {last['state']} -> {new['state']}")
    if new["available_at"] < last["available_at"]:
        raise PlanVersionError("available_at must not move backwards")
    if new["state"] == "actionable" and new["validation"]["status"] != "passed":
        raise PlanVersionError("actionable requires passed validation")


def next_version(
    last: dict[str, Any], *, state: str | None = None, available_at: datetime, **changes: Any
) -> dict[str, Any]:
    """A new wire dict = previous version + changes, with version/supersedes/available_at set."""
    new = copy.deepcopy(last)
    new.update(to_wire(changes))
    new["plan_version"] = last["plan_version"] + 1
    new["supersedes"] = ref(last)
    new["available_at"] = utc_z(available_at)
    if state is not None:
        new["state"] = state
    return new


@dataclass(frozen=True)
class LateResult:
    """A model/analysis result computed against an earlier plan version."""

    based_on_plan_version: int
    analysis_as_of: datetime
    input_revoked: bool
    critical_data_valid_until: datetime | None
    model_run_id: str | None = None


@dataclass(frozen=True)
class Acceptance:
    may_replace_formal_plan: bool
    keep_as_history: bool
    reasons: tuple[str, ...]


def judge_late_result(history: Sequence[dict[str, Any]], result: LateResult, *, now: datetime) -> Acceptance:
    """A slow result may be kept as a historical explanation but never overwrites a newer plan."""
    last = latest(history)
    reasons: list[str] = []
    if last is None:
        reasons.append("no_plan_to_replace")
    else:
        if last["plan_version"] > result.based_on_plan_version:
            reasons.append("superseded_by_newer_plan_version")
        if last["state"] in ("superseded", "expired"):
            reasons.append(f"plan_is_{last['state']}")
        if datetime.fromisoformat(last["available_at"].replace("Z", "+00:00")) > result.analysis_as_of:
            reasons.append("newer_plan_available_after_analysis_as_of")
        if datetime.fromisoformat(last["expires_at"].replace("Z", "+00:00")) <= now:
            reasons.append("plan_expired")
    if result.input_revoked:
        reasons.append("input_revoked")
    if result.critical_data_valid_until is not None and result.critical_data_valid_until <= now:
        reasons.append("critical_data_stale")
    return Acceptance(may_replace_formal_plan=not reasons, keep_as_history=True, reasons=tuple(reasons))
