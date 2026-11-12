"""Plan use cases: append TradePlan versions, late model results, actionability overlay.

Why: M3 (W06). Keeps the order compile -> (validate elsewhere) -> append, and the rule that
a slow result cannot overwrite a newer plan, in one place the API/workers call.
Invariants: every append is checked with ``check_append`` against the stored history inside
the caller's transaction (the (plan_id, plan_version) key makes a lost race a unique
violation, never a silent overwrite); actionability is stored as an immutable snapshot.
Ports: the account is read through ``AccountSnapshotPort`` (ledger package) defined in
``domain.portfolio.coordinator`` and consumed by the caller when it builds the input.
Not covered: running the validation (research/execution), order placement.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from pulse.adapters.postgres import decision_repo
from pulse.domain.plans.versions import Acceptance, LateResult, check_append, judge_late_result
from pulse.domain.portfolio.actionability import ActionabilityInput, evaluate_actionability


def append_plan(conn: psycopg.Connection, plan: dict[str, Any]) -> None:
    history = decision_repo.load_plan_history(conn, UUID(plan["plan_id"]))
    check_append(history, plan)
    decision_repo.append_plan_version(conn, plan)


def accept_late_model_result(
    conn: psycopg.Connection, plan_id: UUID, result: LateResult, candidate: dict[str, Any], *, now: datetime
) -> Acceptance:
    """Append ``candidate`` only when the late result may still replace the formal plan."""
    history = decision_repo.load_plan_history(conn, plan_id)
    verdict = judge_late_result(history, result, now=now)
    if verdict.may_replace_formal_plan:
        append_plan(conn, candidate)
    return verdict


def store_actionability(conn: psycopg.Connection, inp: ActionabilityInput) -> dict[str, Any]:
    payload = evaluate_actionability(inp)
    decision_repo.insert_actionability(conn, payload)
    return payload
