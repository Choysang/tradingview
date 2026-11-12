"""PostgreSQL persistence for rankings, sector rankings, plans, candidate pool, actionability.

Why: M3 needs frozen, pinned, replayable decision objects (SPEC §6/§7, DB tables
decision.ranking_run/ranking_entry/sector_rank_entry/trade_plan/candidate_pool_event/
actionability_snapshot). The DDL guards (published release immutable, plan versions
append-only, entries match their run/plan) are the last line of defence; this module also
verifies what SQL cannot (contract validity of the payload before it is stored).
Invariants: every plan payload and actionability result is checked against the contract
before insert; the connection owns the transaction (callers wrap in ``conn.transaction()``);
nothing here publishes a release - the release writer (another package) does.
Not covered: release staging/publication, artifact storage for explanation bodies
(only their content-hash refs are stored in explanation_ref).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from pulse import contracts
from pulse.domain.ranking.run import RankingRun
from pulse.domain.sectors.pool import PoolEvent
from pulse.domain.strategy.canonical import canonical_hash


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ------------------------------------------------------------------ rankings


def insert_ranking_run(conn: psycopg.Connection, run: RankingRun, *, created_at: datetime) -> None:
    contracts.check("Ranking", run.to_wire())
    tax = run.taxonomy_node or {}
    conn.execute(
        """INSERT INTO decision.ranking_run(ranking_id,release_id,ranking_scope,scope_key,taxonomy_id,taxonomy_version,
               node_id,horizon_id,forecast_target,ranking_policy_version,membership_snapshot_id,created_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (run.ranking_id, run.release_id, run.ranking_scope, run.scope_key, tax.get("taxonomy_id"),
         tax.get("taxonomy_version"), tax.get("node_id"), run.horizon_id, run.forecast_target,
         run.ranking_policy_version, run.membership_snapshot_id, created_at),
    )
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO decision.ranking_entry(ranking_id,instrument_id,alpha_rank,action_rank,relative_rank_score,
                   calibrated_probability,action_status,explanation_ref,plan_id,plan_version)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            [
                (run.ranking_id, e.instrument_id, e.alpha_rank, e.action_rank, e.relative_rank_score,
                 e.calibrated_probability, e.action_status, e.explanation_ref,
                 None if e.plan_ref is None else e.plan_ref[0], None if e.plan_ref is None else e.plan_ref[1])
                for e in run.entries
            ],
        )


def insert_sector_overview(conn: psycopg.Connection, payload: dict[str, Any], *, created_at: datetime) -> None:
    """Store a ``SectorOverview`` (ranking_run scope sector_overview + sector_rank_entry rows)."""
    contracts.check("SectorOverview", payload)
    snapshots = {e["membership_snapshot_id"] for e in payload["entries"]}
    if len(snapshots) != 1:
        raise ValueError("a sector overview run is pinned to exactly one membership snapshot")
    conn.execute(
        """INSERT INTO decision.ranking_run(ranking_id,release_id,ranking_scope,scope_key,taxonomy_id,taxonomy_version,
               node_id,horizon_id,forecast_target,ranking_policy_version,membership_snapshot_id,created_at)
           VALUES (%s,%s,'sector_overview',%s,%s,%s,NULL,%s,%s,%s,%s,%s)""",
        (payload["ranking_id"], payload["release_id"], f"{payload['taxonomy_id']}/v{payload['taxonomy_version']}",
         payload["taxonomy_id"], payload["taxonomy_version"], payload["horizon_id"], payload["forecast_target"],
         payload["ranking_policy_version"], next(iter(snapshots)), created_at),
    )
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO decision.sector_rank_entry(ranking_id,taxonomy_id,taxonomy_version,node_id,rank_kind,rank,score,
                   member_count,observed_member_count,membership_snapshot_id,explanation_ref,quality_status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            [
                (payload["ranking_id"], e["taxonomy_node"]["taxonomy_id"], e["taxonomy_node"]["taxonomy_version"],
                 e["taxonomy_node"]["node_id"], e["rank_kind"], e["rank"], e["score"], e["member_count"],
                 e["observed_member_count"], e["membership_snapshot_id"], e["explanation_ref"], e["quality_status"])
                for e in payload["entries"]
            ],
        )


def load_ranking_wire(conn: psycopg.Connection, ranking_id: UUID, *, after_alpha_rank: int = 0, limit: int | None = None) -> dict[str, Any]:
    """Reload a stored stock ranking as a ``Ranking`` payload (keyset on alpha_rank)."""
    head = conn.execute("SELECT * FROM decision.ranking_run WHERE ranking_id=%s", (ranking_id,)).fetchone()
    if head is None:
        raise KeyError(f"unknown ranking {ranking_id}")
    if head["ranking_scope"] == "sector_overview":
        raise ValueError("a sector_overview run holds sector rows; use load_sector_overview_wire")
    rows = conn.execute(
        """SELECT * FROM decision.ranking_entry WHERE ranking_id=%s AND alpha_rank>%s ORDER BY alpha_rank LIMIT %s""",
        (ranking_id, after_alpha_rank, limit),
    ).fetchall()
    node = None
    if head["node_id"] is not None:
        node = {"taxonomy_id": head["taxonomy_id"], "taxonomy_version": head["taxonomy_version"], "node_id": head["node_id"]}
    return contracts.to_wire({
        "ranking_id": head["ranking_id"], "release_id": head["release_id"], "ranking_scope": head["ranking_scope"],
        "scope_key": head["scope_key"], "horizon_id": head["horizon_id"], "forecast_target": head["forecast_target"],
        "ranking_policy_version": head["ranking_policy_version"], "taxonomy_node": node,
        "membership_snapshot_id": head["membership_snapshot_id"],
        "entries": [
            {"instrument_id": r["instrument_id"], "alpha_rank": r["alpha_rank"], "action_rank": r["action_rank"],
             "relative_rank_score": r["relative_rank_score"], "calibrated_probability": r["calibrated_probability"],
             "action_status": r["action_status"], "explanation_ref": r["explanation_ref"],
             "plan_ref": None if r["plan_id"] is None else {"plan_id": r["plan_id"], "plan_version": r["plan_version"]}}
            for r in rows
        ],
    })


def load_sector_overview_wire(conn: psycopg.Connection, ranking_id: UUID) -> dict[str, Any]:
    head = conn.execute("SELECT * FROM decision.ranking_run WHERE ranking_id=%s AND ranking_scope='sector_overview'", (ranking_id,)).fetchone()
    if head is None:
        raise KeyError(f"unknown sector overview {ranking_id}")
    rows = conn.execute("SELECT * FROM decision.sector_rank_entry WHERE ranking_id=%s ORDER BY rank", (ranking_id,)).fetchall()
    return contracts.to_wire({
        "ranking_id": head["ranking_id"], "release_id": head["release_id"], "ranking_scope": "sector_overview",
        "taxonomy_id": head["taxonomy_id"], "taxonomy_version": head["taxonomy_version"], "horizon_id": head["horizon_id"],
        "forecast_target": head["forecast_target"], "ranking_policy_version": head["ranking_policy_version"],
        "entries": [
            {"taxonomy_node": {"taxonomy_id": r["taxonomy_id"], "taxonomy_version": r["taxonomy_version"], "node_id": r["node_id"]},
             "rank_kind": r["rank_kind"], "rank": r["rank"], "score": r["score"], "member_count": r["member_count"],
             "observed_member_count": r["observed_member_count"], "membership_snapshot_id": r["membership_snapshot_id"],
             "explanation_ref": r["explanation_ref"], "quality_status": r["quality_status"]}
            for r in rows
        ],
    })


# ------------------------------------------------------------------ plans


def append_plan_version(conn: psycopg.Connection, plan: dict[str, Any]) -> None:
    """Append one TradePlan version (contract-checked; DDL makes versions immutable)."""
    contracts.check("TradePlan", plan)
    conn.execute(
        """INSERT INTO decision.trade_plan(plan_id,plan_version,instrument_id,account_id,state,plan_payload,
               based_on_release_id,expected_account_version,available_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (plan["plan_id"], plan["plan_version"], plan["instrument_id"], plan["account_id"], plan["state"],
         Jsonb(plan), plan["based_on_release_id"], plan["expected_account_version"], _ts(plan["available_at"])),
    )


def load_plan_history(conn: psycopg.Connection, plan_id: UUID) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT plan_payload FROM decision.trade_plan WHERE plan_id=%s ORDER BY plan_version", (plan_id,)).fetchall()
    return [r["plan_payload"] for r in rows]


PUBLIC_RELEASE_STATES = ("READY", "SUPERSEDED")


def plan_state(conn: psycopg.Connection, plan_id: UUID, plan_version: int) -> str | None:
    """Stored state of one plan version (None when the version does not exist)."""
    row = conn.execute("SELECT state FROM decision.trade_plan WHERE plan_id=%s AND plan_version=%s", (plan_id, plan_version)).fetchone()
    return None if row is None else row["state"]


def load_visible_plan_history(conn: psycopg.Connection, plan_id: UUID, *, building_release_id: UUID | None = None
                              ) -> tuple[list[dict[str, Any]], set[int]]:
    """(visible versions, dead version numbers) of one plan.

    Visible = based on a public (READY/SUPERSEDED) release, or on the release currently being built. Dead = based on any
    other release (FAILED, or a BUILDING attempt that is not this one): the row stays, but it is not part of the chain.
    """
    rows = conn.execute(
        "SELECT t.plan_version, t.plan_payload, (r.release_state = ANY(%s) OR r.release_id = %s) AS visible"
        " FROM decision.trade_plan t JOIN decision.release r ON r.release_id=t.based_on_release_id"
        " WHERE t.plan_id=%s ORDER BY t.plan_version", (list(PUBLIC_RELEASE_STATES), building_release_id, plan_id)).fetchall()
    return [r["plan_payload"] for r in rows if r["visible"]], {r["plan_version"] for r in rows if not r["visible"]}


# ------------------------------------------------------------------ candidate pool


def append_pool_event(conn: psycopg.Connection, e: PoolEvent, *, recorded_at: datetime) -> bool:
    """Append a pool state event; returns False when this exact event was already stored.

    Redelivery is idempotent (deterministic ``pool_event_id``). A different event for the same
    (pool, instrument, sequence) is a lost CAS race and raises ``UniqueViolation``.
    """
    existing = conn.execute("SELECT 1 FROM decision.candidate_pool_event WHERE pool_event_id=%s", (e.pool_event_id,)).fetchone()
    if existing:
        return False
    conn.execute(
        """INSERT INTO decision.candidate_pool_event(pool_event_id,pool_id,instrument_id,event_sequence,policy_id,policy_version,
               taxonomy_id,taxonomy_version,node_id,membership_snapshot_id,release_id,previous_state,current_state,trigger_code,
               trigger_payload,plan_id,plan_version,decision_as_of,available_at,recorded_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (e.pool_event_id, e.pool_id, e.instrument_id, e.event_sequence, e.policy_id, e.policy_version, e.taxonomy_id,
         e.taxonomy_version, e.node_id, e.membership_snapshot_id, e.release_id, e.previous_state, e.current_state,
         e.trigger_code, Jsonb(e.trigger_payload), None if e.plan_ref is None else e.plan_ref[0],
         None if e.plan_ref is None else e.plan_ref[1], e.decision_as_of, e.available_at, recorded_at),
    )
    return True


def current_pool_states(conn: psycopg.Connection, pool_id: UUID) -> dict[UUID, dict[str, Any]]:
    rows = conn.execute(
        """SELECT DISTINCT ON (instrument_id) instrument_id,event_sequence,current_state,trigger_payload
             FROM decision.candidate_pool_event WHERE pool_id=%s ORDER BY instrument_id,event_sequence DESC""",
        (pool_id,),
    ).fetchall()
    return {r["instrument_id"]: r for r in rows}


def load_pool_wire(conn: psycopg.Connection, pool_id: UUID, release_id: UUID) -> dict[str, Any]:
    rows = conn.execute(
        """SELECT DISTINCT ON (instrument_id) * FROM decision.candidate_pool_event WHERE pool_id=%s
              ORDER BY instrument_id,event_sequence DESC""", (pool_id,)).fetchall()
    if not rows:
        raise KeyError(f"unknown pool {pool_id}")
    head = rows[0]
    wire = contracts.to_wire({
        "pool_id": pool_id, "release_id": release_id, "policy_id": head["policy_id"], "policy_version": head["policy_version"],
        "items": [
            {"pool_event_id": r["pool_event_id"], "instrument_id": r["instrument_id"], "event_sequence": r["event_sequence"],
             "taxonomy_node": {"taxonomy_id": r["taxonomy_id"], "taxonomy_version": r["taxonomy_version"], "node_id": r["node_id"]},
             "membership_snapshot_id": r["membership_snapshot_id"], "current_state": r["current_state"],
             "trigger_code": r["trigger_code"],
             "plan_ref": None if r["plan_id"] is None else {"plan_id": r["plan_id"], "plan_version": r["plan_version"]},
             "available_at": r["available_at"]}
            for r in sorted(rows, key=lambda r: str(r["instrument_id"]))
        ],
    })
    return contracts.check("CandidatePool", wire)


# ------------------------------------------------------------------ actionability


def insert_actionability(conn: psycopg.Connection, payload: dict[str, Any]) -> None:
    """Store an ActionabilitySnapshot. ``result_ref`` is the content address of the stored result."""
    contracts.check("ActionabilitySnapshot", payload)
    digest = canonical_hash(payload)
    conn.execute(
        """INSERT INTO decision.actionability_snapshot(actionability_id,release_id,ranking_id,account_id,account_version,
               quote_snapshot_id,actionability_policy_version,checked_at,valid_until,result_ref,result_hash)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (payload["actionability_id"], payload["release_id"], payload["ranking_id"], payload["account_id"],
         payload["account_version"], payload["quote_snapshot_id"], payload["actionability_policy_version"],
         _ts(payload["checked_at"]), _ts(payload["valid_until"]), f"actionability:{digest}", digest),
    )
    conn.execute("INSERT INTO decision.actionability_result(actionability_id,result_payload) VALUES (%s,%s)",
                 (payload["actionability_id"], Jsonb(payload)))


def load_actionability(conn: psycopg.Connection, actionability_id: UUID) -> dict[str, Any] | None:
    """Immutable ActionabilitySnapshot payload stored with its header (migration 0008)."""
    row = conn.execute("SELECT result_payload FROM decision.actionability_result WHERE actionability_id=%s",
                       (actionability_id,)).fetchone()
    return None if row is None else row["result_payload"]


def actionability_header(conn: psycopg.Connection, actionability_id: UUID) -> dict[str, Any] | None:
    return conn.execute("SELECT * FROM decision.actionability_snapshot WHERE actionability_id=%s", (actionability_id,)).fetchone()


__all__ = (
    "insert_ranking_run", "insert_sector_overview", "load_ranking_wire", "load_sector_overview_wire",
    "append_plan_version", "load_plan_history", "load_visible_plan_history", "PUBLIC_RELEASE_STATES", "plan_state", "append_pool_event", "current_pool_states", "load_pool_wire",
    "insert_actionability", "actionability_header", "load_actionability",
)
