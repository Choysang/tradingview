"""Snapshot manifest state machine guards of 0001 exercised on real PostgreSQL (M1-E03).

Covers staged -> COMMITTED_UNPUBLISHED -> READY, immutability of READY manifests and their dependency
closure, and the deferred "READY needs READY inputs" check. Object pins / GC leases are not part of the
package schema and are therefore not covered here.
"""
from __future__ import annotations

import psycopg
import pytest
from w02_helpers import h, now, seed_manifest

pytestmark = pytest.mark.pg


def make_ready(conn, snap):
    conn.execute(
        "UPDATE market.dataset_manifest SET manifest_state='READY', storage_uri='file:///x', content_hash=%s,"
        " row_count=0, ready_at=%s WHERE snapshot_id=%s", (h(str(snap)), now(), snap))


def test_unpublished_snapshot_becomes_ready_once_inputs_are_ready(conn):
    base = seed_manifest(conn, "READY")
    derived = seed_manifest(conn, "COMMITTED_UNPUBLISHED")
    conn.execute("INSERT INTO market.dataset_dependency VALUES (%s,%s,'bars')", (derived, base))
    make_ready(conn, derived)
    conn.commit()
    assert conn.execute("SELECT manifest_state FROM market.dataset_manifest WHERE snapshot_id=%s",
                        (derived,)).fetchone()["manifest_state"] == "READY"


def test_ready_requires_ready_inputs_at_commit(conn):
    base = seed_manifest(conn, "COMMITTED_UNPUBLISHED")
    derived = seed_manifest(conn, "COMMITTED_UNPUBLISHED")
    conn.execute("INSERT INTO market.dataset_dependency VALUES (%s,%s,'bars')", (derived, base))
    make_ready(conn, derived)
    with pytest.raises(psycopg.errors.RaiseException, match="unpublished input"):
        conn.commit()
    conn.rollback()


def test_ready_manifest_and_its_dependency_closure_are_immutable(conn):
    base = seed_manifest(conn, "READY")
    derived = seed_manifest(conn, "COMMITTED_UNPUBLISHED")
    conn.execute("INSERT INTO market.dataset_dependency VALUES (%s,%s,'bars')", (derived, base))
    make_ready(conn, derived)
    conn.commit()
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        conn.execute("UPDATE market.dataset_manifest SET row_count=5 WHERE snapshot_id=%s", (derived,))
    conn.rollback()
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        conn.execute("DELETE FROM market.dataset_dependency WHERE snapshot_id=%s", (derived,))
    conn.rollback()
    other = seed_manifest(conn, "READY")
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        conn.execute("INSERT INTO market.dataset_dependency VALUES (%s,%s,'calendar')", (derived, other))
    conn.rollback()


def test_ready_state_needs_content_identity(conn):
    snap = seed_manifest(conn, "COMMITTED_UNPUBLISHED")
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE market.dataset_manifest SET manifest_state='READY', ready_at=%s WHERE snapshot_id=%s",
                     (now(), snap))
    conn.rollback()
