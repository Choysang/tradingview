"""Research use cases: seed candidates, schedule a league round, record promotion.

Why: M3 (W09). Orchestrates the domain league/promotion logic and the registry repos while
keeping the separation the spec demands: the league schedules experiments, promotion is a
different decision with its own evidence, and neither writes ranking weights.
Invariants: seeding never produces an ``active`` strategy; ``apply`` of a promotion is a
separate step from ``record``; league points feed only ``pair_round``.
Not covered: executing experiments, computing metrics/evidence (research workers).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from uuid import UUID

import psycopg

from pulse.adapters.postgres import research_repo, strategy_repo
from pulse.domain.research.league import (
    Candidate,
    LeagueHistory,
    PairingPolicy,
    RoundPlan,
    group_comparable,
    pair_round,
)
from pulse.domain.strategy.canonical import stable_uuid
from pulse.domain.strategy.promotion import PromotionDecisionRecord
from pulse.domain.strategy.seed import CatalogSeed, seed_factors, seed_strategies


def load_catalog_seed(catalog_dir: Path) -> tuple[CatalogSeed, CatalogSeed]:
    strat = json.loads((catalog_dir / "strategy_catalog_v3.json").read_text(encoding="utf-8"))
    fact = json.loads((catalog_dir / "factor_catalog_v3.json").read_text(encoding="utf-8"))
    return seed_strategies(strat), seed_factors(fact, strat)


def seed_registry(conn: psycopg.Connection, catalog_dir: Path, *, now: datetime) -> dict[str, dict[str, int]]:
    strategies, factors = load_catalog_seed(catalog_dir)
    return {
        "strategies": strategy_repo.seed_candidates(conn, strategies, now=now),
        "factors": strategy_repo.seed_candidates(conn, factors, now=now),
    }


def schedule_league_round(
    conn: psycopg.Connection,
    league: tuple[UUID, int],
    candidates: list[Candidate],
    history: LeagueHistory,
    round_no: int,
    *,
    scheduled_at: datetime,
    policy: PairingPolicy = PairingPolicy(),
) -> list[RoundPlan]:
    """Pair every comparable group (incomparable groups never meet) and store the round."""
    plans = [pair_round(g, history, round_no, policy) for _, g in sorted(group_comparable(candidates).items())]
    research_repo.schedule_round(conn, league, round_no, [p for plan in plans for p in plan.pairings], scheduled_at=scheduled_at)
    return plans


def record_and_maybe_apply(
    conn: psycopg.Connection, d: PromotionDecisionRecord, *, decided_key: str, apply_now: bool, now: datetime
) -> UUID:
    pid = stable_uuid(
        "promotion", d.instance_id, d.evaluation_experiment_id, d.policy_version, d.from_state, d.to_state, decided_key
    )
    research_repo.record_promotion(conn, pid, d)
    if apply_now and d.outcome == "approved":
        research_repo.apply_promotion(conn, pid, applied_at=now)
    return pid
