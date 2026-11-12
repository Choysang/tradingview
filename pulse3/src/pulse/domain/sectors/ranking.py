"""Sector opportunity ranking, in-sector stock ranking, membership overlap.

Why: SPEC §6 "板块榜": three rankings - between sectors (SectorOverview), inside one
sector (Ranking scope=sector) and the fast-heating pool (see ``pool.py``). Each carries
its own taxonomy_id/version and the membership snapshot it used.
Invariants: providers' taxonomies are never merged; overlapping members stay in every
sector they belong to (so one stock can appear in several sector rankings) and the
multiplicity is reported for the portfolio layer to price concentration; a sector with
zero observed members is excluded (unknown, not 0); coverage < policy ratio is
``partial`` quality, never silently ``valid``.
Not covered: computing member scores (they come from the baseline ranking), taxonomy IO.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Any
from uuid import UUID

from pulse.contracts import to_wire
from pulse.domain.ranking.universe import Exclusion, UniverseResult
from pulse.domain.strategy.canonical import canonical_hash, dec_ctx, quantize12

RANK_KINDS = ("sector_opportunity_rank", "sector_attention_rank", "sector_rising_rank")


@dataclass(frozen=True)
class SectorNode:
    taxonomy_id: str
    taxonomy_version: int
    node_id: str

    def wire(self) -> dict[str, Any]:
        return {"taxonomy_id": self.taxonomy_id, "taxonomy_version": self.taxonomy_version, "node_id": self.node_id}


@dataclass(frozen=True)
class SectorSnapshot:
    """Membership of one node as known at a snapshot (never today's members for history)."""

    node: SectorNode
    membership_snapshot_id: UUID
    member_ids: tuple[UUID, ...]
    #: when the platform actually knew this membership (None = unknown)
    available_at: datetime | None = None


@dataclass(frozen=True)
class SectorPolicy:
    policy_version: str
    top_k: int = 5
    min_observed_ratio_valid: Decimal = Decimal("0.8")


@dataclass(frozen=True)
class SectorScore:
    snapshot: SectorSnapshot
    rank_kind: str
    score: Decimal
    member_count: int
    observed_member_count: int
    quality_status: str
    explanation: Mapping[str, Any]


def score_sectors_by_opportunity(
    policy: SectorPolicy,
    snapshots: Sequence[SectorSnapshot],
    member_scores: Mapping[UUID, Decimal],
    decision_as_of: datetime | None = None,
) -> tuple[list[SectorScore], list[tuple[SectorNode, str]]]:
    """Mean of the top-k observed member scores. Returns (scored, excluded-with-reason).

    With ``decision_as_of`` a membership known only after that time (or of unknown availability)
    is excluded - today's members never back-fill a historical ranking.
    """
    scored: list[SectorScore] = []
    excluded: list[tuple[SectorNode, str]] = []
    for s in snapshots:
        if decision_as_of is not None:
            if s.available_at is None:
                excluded.append((s.node, "membership_availability_unknown"))
                continue
            if s.available_at > decision_as_of:
                excluded.append((s.node, "membership_not_yet_available"))
                continue
        members = list(dict.fromkeys(s.member_ids))
        observed = sorted((member_scores[m] for m in members if m in member_scores), reverse=True)
        if not observed:
            excluded.append((s.node, "no_observed_members"))
            continue
        top = observed[: policy.top_k]
        with localcontext(dec_ctx()):
            mean = sum(top, Decimal(0)) / Decimal(len(top))
            ratio = Decimal(len(observed)) / Decimal(len(members))
        quality = "valid" if ratio >= policy.min_observed_ratio_valid else "partial"
        scored.append(
            SectorScore(
                snapshot=s,
                rank_kind="sector_opportunity_rank",
                score=quantize12(mean),
                member_count=len(members),
                observed_member_count=len(observed),
                quality_status=quality,
                explanation={
                    "method": f"mean_of_top_{policy.top_k}_observed_member_scores",
                    "observed_ratio": quantize12(ratio),
                    "policy_version": policy.policy_version,
                    "taxonomy": s.node.wire(),
                },
            )
        )
    return scored, excluded


def assemble_sector_overview(
    *,
    ranking_id: UUID,
    release_id: UUID,
    taxonomy_id: str,
    taxonomy_version: int,
    horizon_id: str,
    forecast_target: str,
    ranking_policy_version: str,
    scores: Sequence[SectorScore],
) -> tuple[dict[str, Any], dict[str, Mapping[str, Any]]]:
    """Build the ``SectorOverview`` wire payload (one rank_kind per call) + explanations."""
    kinds = {s.rank_kind for s in scores}
    if len(kinds) > 1:
        raise ValueError("one overview ranking holds one rank_kind")
    if any(s.snapshot.node.taxonomy_id != taxonomy_id or s.snapshot.node.taxonomy_version != taxonomy_version for s in scores):
        raise ValueError("providers/taxonomy versions are never merged into one overview")
    if len({s.snapshot.membership_snapshot_id for s in scores}) > 1:
        raise ValueError("one overview ranking is pinned to one membership snapshot (taxonomy-level)")
    ordered = sorted(scores, key=lambda s: (-s.score, s.snapshot.node.node_id))
    explanations: dict[str, Mapping[str, Any]] = {}
    entries = []
    for rank, s in enumerate(ordered, start=1):
        ref = "explain:" + canonical_hash(s.explanation)
        explanations[ref] = to_wire(s.explanation)
        entries.append(
            {
                "taxonomy_node": s.snapshot.node.wire(),
                "rank_kind": s.rank_kind,
                "rank": rank,
                "score": s.score,
                "member_count": s.member_count,
                "observed_member_count": s.observed_member_count,
                "membership_snapshot_id": s.snapshot.membership_snapshot_id,
                "explanation_ref": ref,
                "quality_status": s.quality_status,
            }
        )
    payload = to_wire(
        {
            "ranking_id": ranking_id,
            "release_id": release_id,
            "ranking_scope": "sector_overview",
            "taxonomy_id": taxonomy_id,
            "taxonomy_version": taxonomy_version,
            "horizon_id": horizon_id,
            "forecast_target": forecast_target,
            "ranking_policy_version": ranking_policy_version,
            "entries": entries,
        }
    )
    return payload, explanations


def restrict_universe(universe: UniverseResult, snapshot: SectorSnapshot) -> UniverseResult:
    """Universe for an in-sector ranking: members that pass the same universe policy.

    Members dropped by the policy keep their universe exclusion; members unknown to the
    universe at all are reported as ``not_in_universe`` rather than vanishing.
    """
    members = set(snapshot.member_ids)
    included = tuple(f for f in universe.included if f.instrument_id in members)
    known = {f.instrument_id for f in universe.included} | {e.instrument_id for e in universe.exclusions}
    kept_excl = tuple(e for e in universe.exclusions if e.instrument_id in members)
    unknown = tuple(Exclusion(m, ("not_in_universe",)) for m in sorted(members - known, key=str))
    return UniverseResult(universe.policy_version, included, kept_excl + unknown)


@dataclass(frozen=True)
class OverlapReport:
    multiplicity: Mapping[UUID, int]
    multi_member_instruments: tuple[UUID, ...]
    jaccard: Mapping[tuple[str, str], Decimal]


def overlap_report(snapshots: Sequence[SectorSnapshot]) -> OverlapReport:
    """Overlapping members are kept; this exposes how often so concentration can be priced."""
    counts: Counter[UUID] = Counter()
    sets: dict[str, set[UUID]] = {}
    for s in snapshots:
        uniq = set(s.member_ids)
        sets[f"{s.node.taxonomy_id}/{s.node.node_id}"] = uniq
        counts.update(uniq)
    ids = sorted(sets)
    jac: dict[tuple[str, str], Decimal] = {}
    with localcontext(dec_ctx()):
        for i, a in enumerate(ids):
            for b in ids[i + 1 :]:
                inter, union = len(sets[a] & sets[b]), len(sets[a] | sets[b])
                if inter:
                    jac[(a, b)] = quantize12(Decimal(inter) / Decimal(union))
    multi = tuple(sorted((i for i, c in counts.items() if c > 1), key=str))
    return OverlapReport(dict(counts), multi, jac)
