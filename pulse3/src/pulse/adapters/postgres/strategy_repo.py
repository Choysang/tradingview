"""PostgreSQL strategy registry: seed candidates, tags, lifecycle CAS.

Why: SPEC §9 / STRATEGY_LEAGUE_SPEC §2: strategy.definition (immutable version),
strategy.instance (parameters + lifecycle), strategy.tag_assignment (versioned taxonomy
tags). The 61 strategy + 105 factor candidates are seeded as ``discovered``.
Invariants: seeding is idempotent (existing identical rows are left alone; a definition
whose hash changed is refused - change = new version); every seeded tag's taxonomy and node
exist first (created in the ``pulse.strategy_registry`` namespace, kind ``strategy_tag``);
lifecycle changes are compare-and-set on the old state, legality checked by the domain,
and ``active`` is never written by seeding.
Not covered: promotion evidence (research_service), persistent lifecycle-event history
(proposal migrations/proposals/decision_strategy_lifecycle_event.sql).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from pulse.domain.strategy.lifecycle import LifecycleTransition
from pulse.domain.strategy.seed import TAXONOMY_VERSION, CatalogSeed


class LifecycleConflict(RuntimeError):
    """The instance is no longer in the state the decision was made against."""


def _ensure_taxonomy(conn: psycopg.Connection, seed: CatalogSeed, now: datetime) -> None:
    for taxonomy_id, nodes in seed.taxonomy_nodes.items():
        conn.execute(
            """INSERT INTO core.taxonomy(taxonomy_id,taxonomy_version,taxonomy_kind,owner_source_id,definition,available_at)
               VALUES (%s,%s,'strategy_tag',NULL,%s,%s) ON CONFLICT DO NOTHING""",
            (taxonomy_id, TAXONOMY_VERSION, Jsonb({"origin": "catalog_seed", "display_labels_are_not_identifiers": True}), now),
        )
        conn.cursor().executemany(
            """INSERT INTO core.taxonomy_node(taxonomy_id,taxonomy_version,node_id,parent_node_id,display_name,definition)
               VALUES (%s,%s,%s,NULL,%s,%s) ON CONFLICT DO NOTHING""",
            [(taxonomy_id, TAXONOMY_VERSION, n, n, Jsonb({})) for n in nodes],
        )


def seed_candidates(conn: psycopg.Connection, seed: CatalogSeed, *, now: datetime) -> dict[str, int]:
    """Idempotently insert definitions, instances and tags. Returns counts of NEW rows."""
    _ensure_taxonomy(conn, seed, now)
    new = {"definitions": 0, "instances": 0, "tags": 0}
    for d in seed.definitions:
        row = conn.execute(
            "SELECT definition_hash FROM strategy.definition WHERE strategy_id=%s AND strategy_version=%s",
            (d.strategy_id, d.strategy_version),
        ).fetchone()
        if row is not None:
            if row["definition_hash"] != d.definition_hash:
                raise ValueError(f"{d.catalog_id}: definition changed under the same version; bump strategy_version")
            continue
        conn.execute(
            """INSERT INTO strategy.definition(strategy_id,strategy_version,display_name,role,method_family,forecast_target,
                   horizon_id,specification,definition_hash,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (d.strategy_id, d.strategy_version, d.display_name, d.role, d.method_family, d.forecast_target, d.horizon_id,
             Jsonb(dict(d.specification)), d.definition_hash, now),
        )
        new["definitions"] += 1
    for i in seed.instances:
        cur = conn.execute(
            """INSERT INTO strategy.instance(instance_id,strategy_id,strategy_version,parameters,parameter_hash,
                   universe_policy_version,lifecycle,implementation_status,reproduction_status,health,created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (instance_id) DO NOTHING""",
            (i.instance_id, i.strategy_id, i.strategy_version, Jsonb(dict(i.parameters)), i.parameter_hash,
             i.universe_policy_version, i.lifecycle, i.implementation_status, i.reproduction_status, i.health, now),
        )
        new["instances"] += cur.rowcount
    for t in seed.tags:
        cur = conn.execute(
            """INSERT INTO strategy.tag_assignment(strategy_id,strategy_version,taxonomy_id,taxonomy_version,node_id,reason_ref)
               VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (t.strategy_id, t.strategy_version, t.taxonomy_id, t.taxonomy_version, t.node_id, t.reason_ref),
        )
        new["tags"] += cur.rowcount
    return new


def apply_lifecycle(conn: psycopg.Connection, tr: LifecycleTransition) -> None:
    """Compare-and-set the instance lifecycle after the domain has validated the move."""
    tr.validate()
    cur = conn.execute(
        "UPDATE strategy.instance SET lifecycle=%s WHERE instance_id=%s AND lifecycle=%s",
        (tr.to_state, UUID(tr.instance_id), tr.from_state),
    )
    if cur.rowcount != 1:
        raise LifecycleConflict(f"instance {tr.instance_id} is not in state {tr.from_state!r}")


def lifecycle_counts(conn: psycopg.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT lifecycle,count(*) AS n FROM strategy.instance GROUP BY lifecycle").fetchall()
    return {r["lifecycle"]: r["n"] for r in rows}


def get_instance(conn: psycopg.Connection, instance_id: UUID) -> dict[str, Any] | None:
    return conn.execute("SELECT * FROM strategy.instance WHERE instance_id=%s", (instance_id,)).fetchone()
