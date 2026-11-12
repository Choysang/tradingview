"""Ranking use cases: build a frozen ranking and store it against a BUILDING release.

Why: M3 (W05). Orchestrates domain (universe -> baseline -> trial fusion -> ranks) and the
decision repository, and hands the release writer the component reference to register.
Invariants: nothing is stored when the domain raises ``RankingBlocked`` (baseline invalid /
thin coverage) - the caller must not publish a new READY release from it; the explanation
bodies are returned for the artifact store (their content hash is already the ref).
Ports (small Protocols, bound by the integrator; nothing is imported from sibling packages):
``FeatureMatrixPort`` (features package), ``ReleaseComponentWriter`` (release package).
Not covered: publishing the release, artifact storage, account overlays.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

import psycopg

from pulse.adapters.postgres import decision_repo, research_repo
from pulse.domain.ranking.baseline import BaselinePolicy, FeatureMatrix
from pulse.domain.ranking.run import ActionAssessment, RankingRun, TrialCandidate, build_ranking
from pulse.domain.ranking.universe import InstrumentFacts, UniversePolicy, apply_universe
from pulse.domain.strategy.canonical import canonical_hash


class FeatureMatrixPort(Protocol):
    def matrix(
        self, instrument_ids: Sequence[UUID], feature_ids: Sequence[str], decision_as_of: datetime
    ) -> FeatureMatrix: ...


class ReleaseComponentWriter(Protocol):
    def add_component(self, release_id: UUID, component_key: str, artifact_ref: str, artifact_hash: str) -> None: ...


@dataclass(frozen=True)
class BuiltRanking:
    run: RankingRun
    artifact_hash: str
    explanations: Mapping[str, Mapping[str, Any]]


def build_and_store_ranking(
    conn: psycopg.Connection,
    *,
    ranking_id: UUID,
    release_id: UUID,
    ranking_scope: str,
    scope_key: str,
    universe_policy: UniversePolicy,
    facts: Sequence[InstrumentFacts],
    policy: BaselinePolicy,
    features: FeatureMatrixPort,
    decision_as_of: datetime,
    created_at: datetime,
    writer: ReleaseComponentWriter | None = None,
    component_key: str = "ranking",
    baseline_valid: bool = True,
    trial_candidates: Sequence[TrialCandidate] = (),
    actions: Mapping[UUID, ActionAssessment] | None = None,
    taxonomy_node: Mapping[str, Any] | None = None,
    membership_snapshot_id: UUID | None = None,
) -> BuiltRanking:
    if trial_candidates:                                   # a budget needs an applied promotion that admits trial
        research_repo.verify_trial_candidates(conn, trial_candidates)
    for a in (actions or {}).values():                    # the plan state an action cites must be the stored one
        if a.plan_ref is not None and a.status == "actionable" and decision_repo.plan_state(conn, *a.plan_ref) != a.plan_state:
            raise ValueError(f"plan {a.plan_ref} is not stored in state {a.plan_state!r}")
    universe = apply_universe(universe_policy, facts)
    matrix = features.matrix(
        [f.instrument_id for f in universe.included], [f.feature_id for f in policy.features], decision_as_of
    )
    run = build_ranking(
        ranking_id=ranking_id, release_id=release_id, ranking_scope=ranking_scope, scope_key=scope_key, policy=policy,
        universe=universe, matrix=matrix, baseline_valid=baseline_valid, trial_candidates=trial_candidates,
        actions=actions, taxonomy_node=taxonomy_node, membership_snapshot_id=membership_snapshot_id,
    )
    decision_repo.insert_ranking_run(conn, run, created_at=created_at)
    digest = canonical_hash(run.to_wire())
    if writer is not None:
        writer.add_component(release_id, component_key, f"ranking:{ranking_id}", digest)
    return BuiltRanking(run, digest, run.explanations)
