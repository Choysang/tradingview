"""Swiss-style research league: scheduling and comparison records.

Why: SPEC §9 / STRATEGY_LEAGUE_SPEC §5. The league is a *research scheduling rule*: with
limited rounds, candidates that are close and not yet compared are paired on the same
window and budget. Points/Elo only decide the NEXT experiments; they never set weights,
permissions or lifecycle (see strategy/promotion.py - its evidence has no league input).
Invariants:
  * pairing happens only inside a comparable group: same role, horizon, universe,
    forecast_target, EvaluationContext hash and resource budget; across groups = 'incomparable';
  * the pairing seed is saved: round seed = sha256(base_seed|round_no); round 1 is a seeded
    shuffle (no hand-picked opponents) with a soft same-family avoidance; a candidate appears
    once per round; an opponent repeats at most ``max_repeat`` times; identical code/param/data
    identities are merged first (dedup recorded, not paired against themselves);
  * odd count -> a BYE for the candidate with the fewest byes; a bye earns no points;
  * results: win / loss / draw / insufficient_evidence / pending / execution_failed / incomparable - insufficient evidence is not
    a loss, a pending label (holding period not over) grants nobody a win, an execution failure
    is not a real zero return.
Not covered: running experiments, deciding promotion, persistence (research_repo).
"""
from __future__ import annotations

import hashlib
import random
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

from pulse.contracts import to_wire
from pulse.domain.strategy.canonical import stable_uuid

RESULTS = ("win", "loss", "draw", "insufficient_evidence", "pending", "execution_failed", "incomparable")
POINTS = {"win": Decimal(1), "draw": Decimal("0.5"), "loss": Decimal(0)}


class LeagueError(ValueError):
    pass


@dataclass(frozen=True)
class Candidate:
    instance_id: str
    role: str
    horizon_id: str
    universe: str
    forecast_target: str
    context_hash: str
    budget_id: str
    family: str  # cluster of near-identical strategies (parameter variants)
    identity: str  # code+parameter+data identity: equal identities are merged

    @property
    def group_key(self) -> tuple[str, ...]:
        return (self.role, self.horizon_id, self.universe, self.forecast_target, self.context_hash, self.budget_id)


@dataclass(frozen=True)
class PairingPolicy:
    version: str = "swiss-v1"
    max_repeat: int = 1
    max_same_family_pairings: int = 1  # per league, across rounds


@dataclass
class LeagueHistory:
    """Opponent history and scheduling points (points are NOT a ranking for weights)."""

    base_seed: str
    opponents: Counter = field(default_factory=Counter)  # frozenset({a,b}) -> times
    byes: Counter = field(default_factory=Counter)
    points: dict[str, Decimal] = field(default_factory=dict)
    same_family_pairings: int = 0
    rounds_done: int = 0

    def record(self, a: str, b: str, result: str, same_family: bool) -> None:
        self.opponents[frozenset((a, b))] += 1
        if same_family:
            self.same_family_pairings += 1
        if result == "win":
            self.points[a] = self.points.get(a, Decimal(0)) + POINTS["win"]
        elif result == "loss":
            self.points[b] = self.points.get(b, Decimal(0)) + POINTS["win"]
        elif result == "draw":
            for x in (a, b):
                self.points[x] = self.points.get(x, Decimal(0)) + POINTS["draw"]
        # pending / insufficient_evidence: no points


@dataclass(frozen=True)
class PlannedPairing:
    pairing_id: UUID
    round_no: int
    left: str
    right: str | None  # None = bye
    outcome: str  # pending | bye | incomparable
    reason: str


@dataclass(frozen=True)
class RoundPlan:
    round_no: int
    seed: int
    pairings: tuple[PlannedPairing, ...]
    merged_duplicates: tuple[tuple[str, str], ...]


def round_seed(base_seed: str, round_no: int) -> int:
    return int(hashlib.sha256(f"{base_seed}|{round_no}".encode()).hexdigest()[:16], 16)


def group_comparable(candidates: list[Candidate]) -> dict[tuple[str, ...], list[Candidate]]:
    out: dict[tuple[str, ...], list[Candidate]] = {}
    for c in sorted(candidates, key=lambda c: c.instance_id):
        out.setdefault(c.group_key, []).append(c)
    return out


def _dedupe(group: list[Candidate]) -> tuple[list[Candidate], list[tuple[str, str]]]:
    seen: dict[str, str] = {}
    keep: list[Candidate] = []
    merged: list[tuple[str, str]] = []
    for c in sorted(group, key=lambda c: c.instance_id):
        if c.identity in seen:
            merged.append((c.instance_id, seen[c.identity]))
        else:
            seen[c.identity] = c.instance_id
            keep.append(c)
    return keep, merged


def pair_round(
    group: list[Candidate], history: LeagueHistory, round_no: int, policy: PairingPolicy = PairingPolicy()
) -> RoundPlan:
    """Pair one comparable group for one round. Deterministic given the saved base seed."""
    if not group:
        raise LeagueError("empty group")
    if len({c.group_key for c in group}) != 1:
        raise LeagueError("candidates are not comparable (role/horizon/universe/target/context/budget differ)")
    cands, merged = _dedupe(group)
    seed = round_seed(history.base_seed, round_no)
    rng = random.Random(seed)
    by_id = {c.instance_id: c for c in cands}
    ids = sorted(by_id)
    if round_no == 1 or not history.points:
        rng.shuffle(ids)
    else:  # near-by points first; stable order: points desc, fewer comparisons, id
        comparisons = Counter()
        for pair, n in history.opponents.items():
            for x in pair:
                comparisons[x] += n
        ids.sort(key=lambda i: (-history.points.get(i, Decimal(0)), comparisons[i], i))
    plans: list[PlannedPairing] = []
    pool = list(ids)
    if len(pool) % 2:  # bye goes to the least-byed candidate (first in ``pool`` order breaks ties)
        bye_id = min(pool, key=lambda i: (history.byes[i], pool.index(i)))
        pool.remove(bye_id)
        plans.append(PlannedPairing(stable_uuid("pairing", history.base_seed, round_no, bye_id, "bye"),
                                    round_no, bye_id, None, "bye", "odd_group_bye_no_points"))
    unpaired = list(pool)
    while unpaired:
        a = unpaired.pop(0)
        eligible = [
            b for b in unpaired
            if history.opponents[frozenset((a, b))] < policy.max_repeat
        ]
        if round_no == 1:
            diff = [b for b in eligible if by_id[b].family != by_id[a].family]
            eligible = diff or eligible
        if eligible and history.same_family_pairings >= policy.max_same_family_pairings:
            cross = [b for b in eligible if by_id[b].family != by_id[a].family]
            eligible = cross  # family budget exhausted: only cross-family pairings remain
        if not eligible:
            plans.append(PlannedPairing(stable_uuid("pairing", history.base_seed, round_no, a, "nobye"),
                                        round_no, a, None, "bye", "no_eligible_opponent_bye_no_points"))
            continue
        if round_no == 1 or not history.points:
            b = eligible[0]
        else:
            pa = history.points.get(a, Decimal(0))
            b = min(eligible, key=lambda x: (abs(history.points.get(x, Decimal(0)) - pa), x))
        unpaired.remove(b)
        plans.append(PlannedPairing(stable_uuid("pairing", history.base_seed, round_no, a, b),
                                    round_no, a, b, "pending", "scheduled_same_context_and_budget"))
    return RoundPlan(round_no, seed, tuple(plans), tuple(merged))


def incomparable(a: Candidate, b: Candidate, round_no: int, base_seed: str) -> PlannedPairing:
    diff = [n for n, x, y in zip(("role", "horizon", "universe", "target", "context", "budget"), a.group_key, b.group_key) if x != y]
    return PlannedPairing(stable_uuid("pairing", base_seed, round_no, a.instance_id, b.instance_id, "incomparable"),
                          round_no, a.instance_id, b.instance_id, "incomparable", "differs:" + ",".join(diff))


# ------------------------------------------------------------------ results


@dataclass(frozen=True)
class SideEvidence:
    """Paired cost-adjusted difference (left minus right) from the two completed runs."""

    both_completed: bool
    execution_failed: bool
    labels_matured: bool
    delta_lower: Decimal | None
    delta_upper: Decimal | None
    sample_count: int


@dataclass(frozen=True)
class ComparisonPolicy:
    version: str
    min_sample_count: int
    tie_margin: Decimal  # interval fully inside +-margin => draw


def classify_pairing(ev: SideEvidence, policy: ComparisonPolicy) -> tuple[str, str]:
    """Return (result, reason). Never inflates a tiny difference into a win."""
    if ev.execution_failed:
        return "execution_failed", "execution_failed_not_a_zero_return"
    if not ev.both_completed:
        return "pending", "experiments_not_completed"
    if not ev.labels_matured:
        return "pending", "holding_period_labels_not_matured"
    if ev.delta_lower is None or ev.delta_upper is None or ev.sample_count < policy.min_sample_count:
        return "insufficient_evidence", "interval_or_sample_missing"
    if ev.delta_lower > 0:
        return "win", "left_interval_above_zero"
    if ev.delta_upper < 0:
        return "loss", "left_interval_below_zero"
    if -policy.tie_margin <= ev.delta_lower and ev.delta_upper <= policy.tie_margin:
        return "draw", "interval_inside_tie_margin"
    return "insufficient_evidence", "interval_straddles_zero_wider_than_tie_margin"


def to_db_outcome(result: str) -> str:
    """Map to research.league_pairing.outcome (migration 0009 added insufficient_evidence and execution_failed:
    they are stored as themselves, so a settled "no verdict" is not confused with a still-pending pairing)."""
    return {"win": "left", "loss": "right", "draw": "tie", "pending": "pending", "insufficient_evidence": "insufficient_evidence",
            "execution_failed": "execution_failed", "incomparable": "incomparable"}[result]


#: League contract enum (v3.schema.json) lacks the two values above: contract gap, wire maps to the nearest value.
CONTRACT_OUTCOME_MAP = {"insufficient_evidence": "pending", "execution_failed": "incomparable"}


def league_wire(
    *, league_id: UUID, league_version: int, evaluation_context_id: UUID, display_name: str,
    pairing_policy_version: str, promotion_policy_version: str, rounds: list[dict[str, Any]],
) -> dict[str, Any]:
    """The ``League`` contract payload; ``rounds`` items follow the contract round shape."""
    return to_wire(
        {"league_id": league_id, "league_version": league_version, "evaluation_context_id": evaluation_context_id,
         "display_name": display_name, "pairing_policy_version": pairing_policy_version,
         "promotion_policy_version": promotion_policy_version, "rounds": rounds}
    )
