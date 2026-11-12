"""Frozen ranking run: trial fusion, alpha_rank vs action_rank, explanations, wire form.

Why: SPEC §6 + RECONCILIATION §6. ``alpha_rank`` is the research order and never changes
after publication; ``action_rank`` orders only entries whose plan/conditions make them
actionable at publication. A ranking is pinned to ``ranking_id`` + ``release_id``; the
account-specific order lives in the ActionabilitySnapshot, not here.
Invariants: fusion uses ``allocate_trial_ranking_budget`` (candidate shares absolute,
residual -> baseline); a trial candidate names the applied promotion decision that admits it
(``promotion_id``; checked against the registry by ``research_repo.verify_trial_candidates``);
an entry is ``actionable`` only when the plan version it cites is itself ``actionable``; a trial candidate lacking a score for any scored instrument, or on
a different score scale, is ineligible (its share returns to baseline) instead of being
averaged over fewer rows; explanation refs are content hashes of the explanation body.
Not covered: persistence (adapters/postgres/decision_repo.py), release publishing.
"""
from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from typing import Any
from uuid import UUID

from pulse.contracts import to_wire
from pulse.domain.ranking.baseline import (
    BaselinePolicy,
    BaselineScores,
    FeatureMatrix,
    RankingBlocked,
    score_baseline,
)
from pulse.domain.ranking.continuity import (
    BaselineUnavailable,
    TrialRankingBudget,
    allocate_trial_ranking_budget,
)
from pulse.domain.ranking.universe import Exclusion, UniverseResult
from pulse.domain.strategy.canonical import canonical_hash, dec_ctx, quantize12

ACTION_STATUSES = ("watching", "actionable", "blocked", "missing_data")
#: Lifecycles whose promotion decision admits a share of the ranking (forward shadow and live). Not overridable by callers.
TRIAL_ADMITTED_LIFECYCLES = frozenset({"shadow", "active"})


@dataclass(frozen=True)
class TrialCandidate:
    """A candidate ranker with an approved absolute budget and its per-instrument scores.

    ``approved_budget`` is only meaningful together with ``promotion_id``: the applied, approved promotion decision of
    ``instance_id`` whose target lifecycle is in ``TRIAL_ADMITTED_LIFECYCLES``. The storage-backed check is
    ``research_repo.verify_trial_candidates``; ``build_and_store_ranking`` runs it before any budget is used.
    """

    candidate_id: str
    approved_budget: Decimal
    scores: Mapping[UUID, Decimal]
    score_scale_id: str
    instance_id: UUID
    promotion_id: UUID


@dataclass(frozen=True)
class ActionAssessment:
    instrument_id: UUID
    status: str  # one of ACTION_STATUSES
    reason_codes: tuple[str, ...] = ()
    plan_ref: tuple[UUID, int] | None = None  # (plan_id, plan_version)
    plan_state: str | None = None  # state of that plan version (decision.trade_plan.state), verified against storage

    def __post_init__(self) -> None:
        if self.status not in ACTION_STATUSES:
            raise ValueError(f"unknown action status {self.status!r}")
        if self.status == "actionable" and self.plan_ref is None:
            raise ValueError("an actionable entry must reference its validated plan version")
        if self.status == "actionable" and self.plan_state != "actionable":
            raise ValueError(f"an actionable entry needs an actionable plan version (plan state is {self.plan_state!r})")


@dataclass(frozen=True)
class RankedEntry:
    instrument_id: UUID
    alpha_rank: int
    action_rank: int | None
    relative_rank_score: Decimal
    calibrated_probability: Decimal | None
    action_status: str
    explanation_ref: str
    plan_ref: tuple[UUID, int] | None


@dataclass(frozen=True)
class RankingRun:
    ranking_id: UUID
    release_id: UUID
    ranking_scope: str  # market | sector | watchlist | account
    scope_key: str
    horizon_id: str
    forecast_target: str
    ranking_policy_version: str
    taxonomy_node: Mapping[str, Any] | None
    membership_snapshot_id: UUID | None
    entries: tuple[RankedEntry, ...]
    exclusions: tuple[Exclusion, ...]
    explanations: Mapping[str, Mapping[str, Any]]  # explanation_ref -> body
    budget: TrialRankingBudget
    ineligible_candidates: Mapping[str, str] = field(default_factory=dict)
    universe_policy_version: str = ""
    coverage_ratio: Decimal = Decimal("1")

    def to_wire(self) -> dict[str, Any]:
        """The ``Ranking`` contract payload (exclusions are served separately)."""
        return to_wire(
            {
                "ranking_id": self.ranking_id,
                "release_id": self.release_id,
                "ranking_scope": self.ranking_scope,
                "scope_key": self.scope_key,
                "horizon_id": self.horizon_id,
                "forecast_target": self.forecast_target,
                "ranking_policy_version": self.ranking_policy_version,
                "taxonomy_node": self.taxonomy_node,
                "membership_snapshot_id": self.membership_snapshot_id,
                "entries": [entry_wire(e) for e in self.entries],
            }
        )


def entry_wire(e: RankedEntry) -> dict[str, Any]:
    return {
        "instrument_id": e.instrument_id,
        "alpha_rank": e.alpha_rank,
        "action_rank": e.action_rank,
        "relative_rank_score": e.relative_rank_score,
        "calibrated_probability": e.calibrated_probability,
        "action_status": e.action_status,
        "explanation_ref": e.explanation_ref,
        "plan_ref": None if e.plan_ref is None else {"plan_id": e.plan_ref[0], "plan_version": e.plan_ref[1]},
    }


def select_eligible_candidates(
    candidates: Sequence[TrialCandidate], scored_ids: Sequence[UUID], scale_id: str
) -> tuple[set[str], dict[str, str]]:
    """Candidates must cover every scored instrument on the baseline scale."""
    eligible: set[str] = set()
    rejected: dict[str, str] = {}
    for c in candidates:
        if c.score_scale_id != scale_id:
            rejected[c.candidate_id] = f"score_scale_mismatch:{c.score_scale_id}"
        elif any(c.scores.get(i) is None for i in scored_ids):
            rejected[c.candidate_id] = "incomplete_input_coverage"
        else:
            eligible.add(c.candidate_id)
    return eligible, rejected


def fuse_scores(
    baseline: BaselineScores, candidates: Sequence[TrialCandidate], budget: TrialRankingBudget
) -> tuple[dict[UUID, Decimal], dict[UUID, dict[str, Decimal]]]:
    """Final score = baseline_weight * baseline + sum(candidate weight * candidate)."""
    weights = dict(budget.candidate_weights)
    by_id = {c.candidate_id: c for c in candidates}
    fused: dict[UUID, Decimal] = {}
    parts: dict[UUID, dict[str, Decimal]] = {}
    with localcontext(dec_ctx()):
        for iid, base in baseline.scores.items():
            row = {budget.baseline_id: quantize12(budget.baseline_weight * base)}
            total = budget.baseline_weight * base
            for cid, w in weights.items():
                if w == 0:
                    continue
                contribution = w * by_id[cid].scores[iid]
                row[cid] = quantize12(contribution)
                total += contribution
            fused[iid] = quantize12(total)
            parts[iid] = row
    return fused, parts


def build_ranking(
    *,
    ranking_id: UUID,
    release_id: UUID,
    ranking_scope: str,
    scope_key: str,
    policy: BaselinePolicy,
    universe: UniverseResult,
    matrix: FeatureMatrix,
    baseline_id: str = "baseline",
    baseline_valid: bool = True,
    trial_candidates: Sequence[TrialCandidate] = (),
    actions: Mapping[UUID, ActionAssessment] | None = None,
    default_action_status: str = "watching",
    calibrated_probabilities: Mapping[UUID, Decimal] | None = None,
    taxonomy_node: Mapping[str, Any] | None = None,
    membership_snapshot_id: UUID | None = None,
    trial_total_cap: Decimal = Decimal("0.1"),
) -> RankingRun:
    """Build one frozen ranking. Raises RankingBlocked if baseline invalid or too thin."""
    if ranking_scope == "sector" and (taxonomy_node is None or membership_snapshot_id is None):
        raise ValueError("a sector ranking requires taxonomy_node and membership_snapshot_id")
    baseline = score_baseline(policy, universe, matrix, baseline_valid=baseline_valid)
    scored_ids = sorted(baseline.scores, key=str)
    eligible, rejected = select_eligible_candidates(trial_candidates, scored_ids, baseline.score_scale_id)
    try:
        budget = allocate_trial_ranking_budget(
            baseline_id=baseline_id,
            approved_budgets={c.candidate_id: c.approved_budget for c in trial_candidates},
            eligible_candidate_ids=eligible,
            baseline_available=baseline_valid,
            trial_total_cap=trial_total_cap,
        )
    except BaselineUnavailable as exc:  # surfaced uniformly as "block READY"
        raise RankingBlocked(str(exc)) from exc
    fused, parts = fuse_scores(baseline, trial_candidates, budget)

    order = sorted(fused, key=lambda i: (-fused[i], str(i)))
    actions = actions or {}
    explanations: dict[str, dict[str, Any]] = {}
    entries: list[RankedEntry] = []
    next_action_rank = 1
    for rank, iid in enumerate(order, start=1):
        act = actions.get(iid) or ActionAssessment(iid, default_action_status)
        prob = (calibrated_probabilities or {}).get(iid)
        if prob is not None and not (Decimal(0) <= prob <= Decimal(1)):
            raise ValueError("calibrated probability must lie in [0,1]")
        body = {
            "policy_version": policy.policy_version,
            "score_scale_id": baseline.score_scale_id,
            "target": policy.target.target_id,
            "alpha_rank": rank,
            "final_score": fused[iid],
            "baseline_weight": budget.baseline_weight,
            "component_contributions": parts[iid],
            "feature_contributions": baseline.contributions[iid],
            "normalized_features": baseline.normalized[iid],
            "retained_weights": baseline.retained_weights,
            "dropped_features": [
                {"feature_id": d.feature_id, "kept_instead": d.kept_instead, "reason": d.reason}
                for d in baseline.dropped_features
            ],
            "action_status": act.status,
            "action_reason_codes": list(act.reason_codes),
            "universe_policy_version": universe.policy_version,
        }
        ref = "explain:" + canonical_hash(body)
        explanations[ref] = to_wire(body)
        action_rank = None
        if act.status == "actionable":
            action_rank, next_action_rank = next_action_rank, next_action_rank + 1
        entries.append(
            RankedEntry(
                instrument_id=iid,
                alpha_rank=rank,
                action_rank=action_rank,
                relative_rank_score=fused[iid],
                calibrated_probability=None if prob is None else quantize12(prob),
                action_status=act.status,
                explanation_ref=ref,
                plan_ref=act.plan_ref,
            )
        )
    return RankingRun(
        ranking_id=ranking_id,
        release_id=release_id,
        ranking_scope=ranking_scope,
        scope_key=scope_key,
        horizon_id=policy.target.horizon_id,
        forecast_target=policy.target.target_id,
        ranking_policy_version=policy.policy_version,
        taxonomy_node=taxonomy_node,
        membership_snapshot_id=membership_snapshot_id,
        entries=tuple(entries),
        exclusions=baseline.exclusions,
        explanations=explanations,
        budget=budget,
        ineligible_candidates=rejected,
        universe_policy_version=universe.policy_version,
        coverage_ratio=baseline.coverage_ratio,
    )


# ---------------------------------------------------------------- stable pagination


class CursorMismatch(ValueError):
    """The cursor was issued for a different pinned object (ranking/release/actionability)."""


def encode_cursor(*, pinned: Mapping[str, str], after_rank: int) -> str:
    body = {"v": 1, "pin": dict(sorted(pinned.items())), "after": after_rank}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    sig = hashlib.sha256(b"pulse-cursor-v1|" + raw).hexdigest()[:16]
    return base64.urlsafe_b64encode(raw).decode().rstrip("=") + "." + sig


def decode_cursor(cursor: str, *, pinned: Mapping[str, str]) -> int:
    """Return the ``after_rank`` of a cursor, enforcing the pinned ids match exactly."""
    try:
        b64, sig = cursor.split(".")
        raw = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
        if hashlib.sha256(b"pulse-cursor-v1|" + raw).hexdigest()[:16] != sig:
            raise ValueError("bad signature")
        body = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - any malformed cursor is one error class
        raise CursorMismatch("malformed cursor") from exc
    if body.get("pin") != dict(sorted(pinned.items())):
        raise CursorMismatch("cursor is pinned to a different ranking/release/actionability")
    return int(body["after"])


@dataclass(frozen=True)
class Page:
    items: tuple[RankedEntry, ...]
    next_cursor: str | None


def paginate(run: RankingRun, *, limit: int, cursor: str | None = None) -> Page:
    """Keyset pagination over alpha_rank, pinned to ranking_id + release_id."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    pin = {"ranking_id": str(run.ranking_id), "release_id": str(run.release_id)}
    after = decode_cursor(cursor, pinned=pin) if cursor else 0
    rest = [e for e in run.entries if e.alpha_rank > after]
    items = tuple(rest[:limit])
    nxt = encode_cursor(pinned=pin, after_rank=items[-1].alpha_rank) if len(rest) > limit else None
    return Page(items, nxt)
