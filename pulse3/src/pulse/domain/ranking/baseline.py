"""Explainable baseline scorer: group normalisation, de-duplication, fixed weights.

Why: FINAL_SYSTEM_SPEC §6 / RECONCILIATION §6. One explicit forecast target; features
are normalised inside the same universe + target (cross-sectional z-score, winsorised);
same-source and highly correlated features are collapsed so a duplicated signal does not
get duplicate weight; the combination is a fixed-weight sum.
Invariants:
  * a row missing ANY retained feature is excluded with an explicit reason - weights are
    never re-normalised per row (RECONCILIATION §6) and missing is never read as 0;
  * weights are normalised once per run over the retained features, not per row;
  * ties are broken by ``str(instrument_id)`` so alpha_rank is a unique 1..N order;
  * a coverage below policy blocks the run (RankingBlocked) instead of publishing thin.
Not covered: learning rankers, probability calibration, regime routing (they plug in via
the trial-budget fusion in ``run.py``); IO of feature values (caller passes plain data).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, localcontext
from uuid import UUID

from pulse.domain.ranking.universe import Exclusion, UniverseResult
from pulse.domain.strategy.canonical import dec_ctx, quantize12, sqrt_dec

ZERO = Decimal("0")
ONE = Decimal("1")
SCORE_SCALE_ID = "weighted_zscore_v1"

FeatureMatrix = Mapping[UUID, Mapping[str, "Decimal | None"]]


class RankingBlocked(RuntimeError):
    """A new complete READY ranking must not be produced (baseline invalid / thin)."""


@dataclass(frozen=True)
class RankingTarget:
    """The single explicit target all scores in one ranking refer to."""

    target_id: str  # e.g. "rel_opportunity_5td_after_declared_costs"
    horizon_id: str
    cost_profile_version: str
    benchmark_id: str


@dataclass(frozen=True)
class FeatureSpec:
    feature_id: str
    definition_version: str
    direction: int  # +1 higher is better, -1 lower is better
    weight: Decimal
    source_group: str  # features of one source group are duplicates of each other

    def __post_init__(self) -> None:
        if self.direction not in (1, -1):
            raise ValueError("direction must be +1 or -1")
        if not isinstance(self.weight, Decimal) or not self.weight.is_finite() or self.weight <= 0:
            raise ValueError("weight must be a positive finite Decimal")


@dataclass(frozen=True)
class BaselinePolicy:
    policy_version: str
    target: RankingTarget
    features: tuple[FeatureSpec, ...]
    correlation_threshold: Decimal = Decimal("0.95")
    winsor_z: Decimal = Decimal("3")
    min_coverage_ratio: Decimal = Decimal("0.5")
    score_scale_id: str = SCORE_SCALE_ID

    def __post_init__(self) -> None:
        ids = [f.feature_id for f in self.features]
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("features must be a nonempty list of distinct feature_id")


@dataclass(frozen=True)
class DroppedFeature:
    feature_id: str
    kept_instead: str
    reason: str  # same_source_group | correlated
    correlation: Decimal | None = None


@dataclass(frozen=True)
class BaselineScores:
    policy_version: str
    score_scale_id: str
    scores: Mapping[UUID, Decimal]
    #: instrument -> feature_id -> contribution to the score
    contributions: Mapping[UUID, Mapping[str, Decimal]]
    normalized: Mapping[UUID, Mapping[str, Decimal]]
    retained_weights: Mapping[str, Decimal]
    dropped_features: tuple[DroppedFeature, ...]
    degenerate_features: tuple[str, ...]
    exclusions: tuple[Exclusion, ...]
    universe_count: int
    scored_count: int

    @property
    def coverage_ratio(self) -> Decimal:
        with localcontext(dec_ctx()):
            return ONE if self.universe_count == 0 else Decimal(self.scored_count) / Decimal(self.universe_count)


def _mean_std(values: list[Decimal]) -> tuple[Decimal, Decimal]:
    with localcontext(dec_ctx()):
        n = Decimal(len(values))
        mean = sum(values, ZERO) / n
        var = sum(((v - mean) ** 2 for v in values), ZERO) / n
        return mean, sqrt_dec(var)


def pearson(xs: list[Decimal], ys: list[Decimal]) -> Decimal | None:
    """Pearson correlation; ``None`` when undefined (n < 3 or zero variance)."""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    with localcontext(dec_ctx()):
        mx, sx = _mean_std(xs)
        my, sy = _mean_std(ys)
        if sx == 0 or sy == 0:
            return None
        n = Decimal(len(xs))
        cov = sum(((x - mx) * (y - my) for x, y in zip(xs, ys)), ZERO) / n
        return cov / (sx * sy)


def deduplicate_features(
    policy: BaselinePolicy, universe_ids: list[UUID], matrix: FeatureMatrix
) -> tuple[tuple[FeatureSpec, ...], tuple[DroppedFeature, ...]]:
    """Greedy, declaration-ordered collapse of same-source / highly correlated features."""
    kept: list[FeatureSpec] = []
    dropped: list[DroppedFeature] = []
    for spec in policy.features:
        verdict: DroppedFeature | None = None
        for k in kept:
            if k.source_group == spec.source_group:
                verdict = DroppedFeature(spec.feature_id, k.feature_id, "same_source_group")
                break
            pairs = [
                (matrix[i][spec.feature_id], matrix[i][k.feature_id])
                for i in universe_ids
                if i in matrix and matrix[i].get(spec.feature_id) is not None and matrix[i].get(k.feature_id) is not None
            ]
            corr = pearson([p[0] for p in pairs], [p[1] for p in pairs])
            if corr is not None and abs(corr) >= policy.correlation_threshold:
                verdict = DroppedFeature(spec.feature_id, k.feature_id, "correlated", quantize12(corr))
                break
        if verdict is None:
            kept.append(spec)
        else:
            dropped.append(verdict)
    return tuple(kept), tuple(dropped)


def score_baseline(
    policy: BaselinePolicy,
    universe: UniverseResult,
    matrix: FeatureMatrix,
    *,
    baseline_valid: bool = True,
) -> BaselineScores:
    """Score the included universe. Raises ``RankingBlocked`` when it cannot be trusted."""
    if not baseline_valid:
        raise RankingBlocked("baseline inputs/implementation invalid: no new READY ranking")
    ids = [f.instrument_id for f in universe.included]
    if not ids:
        raise RankingBlocked("empty universe: nothing to rank")
    kept, dropped = deduplicate_features(policy, ids, matrix)

    exclusions = list(universe.exclusions)
    scoring_ids: list[UUID] = []
    for iid in ids:
        row = matrix.get(iid, {})
        missing = [f.feature_id for f in kept if row.get(f.feature_id) is None]
        if missing:
            exclusions.append(Exclusion(iid, tuple(f"missing_feature:{m}" for m in missing)))
        else:
            scoring_ids.append(iid)
    scored_count = len(scoring_ids)
    result_cov = Decimal(scored_count) / Decimal(len(ids))
    if scored_count == 0 or result_cov < policy.min_coverage_ratio:
        raise RankingBlocked(
            f"feature coverage {result_cov:.4f} below policy minimum {policy.min_coverage_ratio}"
        )

    with localcontext(dec_ctx()):
        total_w = sum((f.weight for f in kept), ZERO)
        weights = {f.feature_id: f.weight / total_w for f in kept}
        z: dict[UUID, dict[str, Decimal]] = {i: {} for i in scoring_ids}
        degenerate: list[str] = []
        for spec in kept:
            vals = [matrix[i][spec.feature_id] for i in scoring_ids]
            mean, std = _mean_std(vals)
            if std == 0:
                degenerate.append(spec.feature_id)
            for i in scoring_ids:
                zi = ZERO if std == 0 else (matrix[i][spec.feature_id] - mean) / std
                zi = max(-policy.winsor_z, min(policy.winsor_z, zi))
                z[i][spec.feature_id] = zi * spec.direction
        scores: dict[UUID, Decimal] = {}
        contrib: dict[UUID, dict[str, Decimal]] = {}
        for i in scoring_ids:
            parts = {fid: quantize12(weights[fid] * zv) for fid, zv in z[i].items()}
            contrib[i] = parts
            scores[i] = quantize12(sum((weights[fid] * zv for fid, zv in z[i].items()), ZERO))
        norm_q = {i: {fid: quantize12(v) for fid, v in row.items()} for i, row in z.items()}
        retained = {fid: quantize12(w) for fid, w in weights.items()}
    return BaselineScores(
        policy_version=policy.policy_version,
        score_scale_id=policy.score_scale_id,
        scores=scores,
        contributions=contrib,
        normalized=norm_q,
        retained_weights=retained,
        dropped_features=dropped,
        degenerate_features=tuple(degenerate),
        exclusions=tuple(exclusions),
        universe_count=len(ids),
        scored_count=scored_count,
    )
