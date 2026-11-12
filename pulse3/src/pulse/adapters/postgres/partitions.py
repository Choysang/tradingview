"""Monthly partition maintenance for market.bar_projection (W02, database_design.md section 2).

Why: the table has no default partition on purpose, so a missing month is a loud insert error. The
deployment job calls ``ensure_bar_partitions`` ahead of the calendar (and after back-filling history).
Invariants: idempotent; concurrent callers are serialized inside the SQL function.
Not covered: detaching/dropping old months (retention is a separate, explicit decision).
"""
from __future__ import annotations

from datetime import date

import psycopg


def ensure_bar_partitions(conn: psycopg.Connection, from_date: date, through_date: date) -> int:
    """Create missing monthly partitions covering the closed date range; returns how many were created."""
    row = conn.execute(
        "SELECT market.ensure_bar_projection_partitions(%s, %s) AS n", (from_date, through_date)).fetchone()
    return int(row["n"] if isinstance(row, dict) else row[0])


def bar_partition_months(conn: psycopg.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid"
        " WHERE i.inhparent='market.bar_projection'::regclass ORDER BY c.relname").fetchall()
    return [r["relname"] if isinstance(r, dict) else r[0] for r in rows]
