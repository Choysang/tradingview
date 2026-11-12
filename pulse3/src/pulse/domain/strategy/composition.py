"""Combination strategies, lineage and correlated-family identification.

Why: SPEC §7.1 / STRATEGY_LEAGUE_SPEC §7. A combination is a NEW StrategyVersion that stores
its parents (exact versions), weights, routing policy and interaction constraints; its worth
is the increment of a unified-account RERUN with vs without it, never a sum of independent
net-value curves. Near-duplicate strategies (parameter variants / high correlation) form
families that share trial budget and opponent slots.
Invariants: parent set non-empty, versions explicit (no 'latest'), weights finite and >= 0,
a fallback baseline is declared; the child id/version differ from every parent; the marginal
increment requires references to two account reruns and refuses a 'summed curves' source;
families are the connected components of the |corr| >= threshold graph (deterministic).
Not covered: running the account rerun (ledger/execution), choosing weights.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from pulse.contracts import to_wire
from pulse.domain.strategy.canonical import canonical_hash, stable_uuid
from pulse.domain.strategy.seed import DefinitionSeed


class CompositionError(ValueError):
    pass


@dataclass(frozen=True)
class ParentRef:
    strategy_id: UUID
    strategy_version: int
    contribution_role: str  # selection/allocation/entry/holding/execution/exit/blend
    weight: Decimal
    evidence_ref: str


@dataclass(frozen=True)
class CompositionSpec:
    name: str
    role: str
    forecast_target: str
    horizon_id: str
    parents: tuple[ParentRef, ...]
    routing_policy_ref: str
    interaction_constraints: Mapping[str, Any]
    fallback_baseline_ref: str
    composition_policy_version: str


def build_composite_definition(spec: CompositionSpec, *, strategy_version: int = 1) -> tuple[DefinitionSeed, tuple[ParentRef, ...]]:
    if not spec.parents:
        raise CompositionError("a combination needs at least one parent")
    seen: set[tuple[UUID, int, str]] = set()
    for p in spec.parents:
        if p.strategy_version < 1:
            raise CompositionError("parents must name an exact version")
        if not p.weight.is_finite() or p.weight < 0:
            raise CompositionError("weights must be finite and nonnegative")
        k = (p.strategy_id, p.strategy_version, p.contribution_role)
        if k in seen:
            raise CompositionError("duplicate parent/role")
        seen.add(k)
    if not spec.fallback_baseline_ref:
        raise CompositionError("a combination must declare its fallback baseline")
    if not spec.routing_policy_ref:
        raise CompositionError("a combination must declare its routing policy version")
    body = to_wire({
        "kind": "composite", "composition_policy_version": spec.composition_policy_version,
        "parents": [{"strategy_id": p.strategy_id, "strategy_version": p.strategy_version, "role": p.contribution_role,
                     "weight": p.weight, "evidence_ref": p.evidence_ref} for p in spec.parents],
        "routing_policy_ref": spec.routing_policy_ref, "interaction_constraints": dict(spec.interaction_constraints),
        "fallback_baseline_ref": spec.fallback_baseline_ref,
        "capacity_note": "combined result must be re-run on the unified account; parent curves are not additive",
    })
    h = canonical_hash(body)
    sid = stable_uuid("composite_strategy", spec.name, h)
    if any(p.strategy_id == sid for p in spec.parents):
        raise CompositionError("a combination cannot be its own parent")
    d = DefinitionSeed(catalog_id=f"composite:{spec.name}", strategy_id=sid, strategy_version=strategy_version,
                       display_name=spec.name, role=spec.role, method_family="composite", forecast_target=spec.forecast_target,
                       horizon_id=spec.horizon_id, specification=body, definition_hash=h)
    return d, spec.parents


@dataclass(frozen=True)
class MarginalIncrement:
    with_run_ref: str
    without_run_ref: str
    increment: Decimal  # unified-account TWR difference (with - without), same context/cost/horizon


def marginal_contribution(
    *, with_run_ref: str | None, without_run_ref: str | None, with_twr: Decimal, without_twr: Decimal,
    source: str = "unified_account_rerun",
) -> MarginalIncrement:
    """Increment of a candidate inside the combined account vs the same account without it."""
    if source != "unified_account_rerun":
        raise CompositionError("only a unified-account rerun may measure a marginal contribution (curves are not additive)")
    if not with_run_ref or not without_run_ref or with_run_ref == without_run_ref:
        raise CompositionError("need two distinct rerun references (with / without)")
    return MarginalIncrement(with_run_ref, without_run_ref, with_twr - without_twr)


def identify_families(strategy_ids: Sequence[str], correlations: Mapping[tuple[str, str], Decimal], threshold: Decimal) -> list[list[str]]:
    """Connected components of the graph with edges |corr| >= threshold (union-find, sorted)."""
    parent = {s: s for s in strategy_ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for (a, b), c in correlations.items():
        if a in parent and b in parent and abs(c) >= threshold:
            parent[find(a)] = find(b)
    groups: dict[str, list[str]] = {}
    for s in strategy_ids:
        groups.setdefault(find(s), []).append(s)
    return sorted((sorted(g) for g in groups.values()), key=lambda g: g[0])
