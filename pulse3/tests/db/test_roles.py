"""Runtime roles and grants on real PostgreSQL (M0-E03 "角色权限", 0004 note on pointer DML)."""
from __future__ import annotations

from datetime import date
from uuid import uuid4

import psycopg
import pytest
from w02_helpers import h, new_release, now, ready_all, seed_api_binding

from pulse.adapters.postgres.outbox import PgOutbox
from pulse.adapters.postgres.partitions import ensure_bar_partitions
from pulse.adapters.postgres.release import PgAnalysisRelease
from pulse.application.ports.delivery import OutboxEvent

pytestmark = pytest.mark.pg
DENIED = psycopg.errors.InsufficientPrivilege
ROLES = ("pulse_migrator", "pulse_api", "pulse_worker", "pulse_coordinator")
SCHEMAS = ("core", "ingest", "market", "strategy", "decision", "ops", "ui", "execution", "intelligence",
           "research", "analysis")


def as_role(conn, role):
    conn.execute(f"SET LOCAL ROLE {role}")


def test_roles_exist_as_nologin_group_roles(conn):
    rows = conn.execute("SELECT rolname, rolcanlogin, rolsuper FROM pg_roles WHERE rolname = ANY(%s)",
                        (list(ROLES),)).fetchall()
    assert {r["rolname"] for r in rows} == set(ROLES)
    assert not any(r["rolcanlogin"] or r["rolsuper"] for r in rows)


def test_every_table_is_readable_by_runtime_roles(conn):
    """Guards against a future migration adding a table without the standard SELECT grant."""
    tables = conn.execute(
        "SELECT n.nspname || '.' || c.relname AS t FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace"
        " WHERE n.nspname = ANY(%s) AND c.relkind IN ('r','p') AND NOT c.relispartition", (list(SCHEMAS),)).fetchall()
    # 87 = 80 package tables + 4 (0006) + 3 (0008/0009); extension migrations (0020+) add more. The expectation is derived from the
    # migration files (every ``CREATE TABLE <schema>.<name>`` in these schemas must exist), never a hard-coded total.
    import re
    from pathlib import Path

    names = {t["t"] for t in tables}
    declared: set[str] = set()
    pattern = re.compile(r"CREATE TABLE (?:IF NOT EXISTS )?(" + "|".join(SCHEMAS) + r")\.(\w+)", re.I)
    for f in sorted((Path(__file__).resolve().parents[2] / "migrations").glob("[0-9][0-9][0-9][0-9]_*.sql")):
        declared |= {f"{s}.{n}" for s, n in pattern.findall(f.read_text(encoding="utf-8"))}
    assert declared and declared <= names, sorted(declared - names)
    assert len(tables) >= 87 and len(names - declared) <= 40      # nothing unexplained appears (partition parents/functions-created tables are few)
    for role in ("pulse_api", "pulse_worker", "pulse_coordinator"):
        missing = [t["t"] for t in tables if not conn.execute(
            "SELECT has_table_privilege(%s, %s, 'SELECT') AS ok", (role, t["t"])).fetchone()["ok"]]
        assert not missing, (role, missing)


def test_api_is_read_mostly(conn):
    seed_api_binding(conn, "roleop")
    conn.commit()
    as_role(conn, "pulse_api")
    assert conn.execute("SELECT count(*) AS n FROM decision.release").fetchone()["n"] >= 0
    for sql in ("INSERT INTO decision.release_slot(slot_key, updated_at) VALUES ('x', now())",
                "UPDATE decision.release SET release_state='FAILED'",
                "INSERT INTO market.dataset_manifest(snapshot_id) VALUES (gen_random_uuid())",
                "DELETE FROM ops.outbox",
                "SELECT * FROM decision.publish_release_stage(gen_random_uuid(), repeat('a',64), gen_random_uuid())"):
        with pytest.raises(DENIED):
            conn.execute(sql)
        conn.rollback()
        as_role(conn, "pulse_api")
    # but it may append outbox events (command handlers) and keep command receipts
    PgOutbox(conn).append(OutboxEvent(
        event_id=uuid4(), event_type="account.updated", schema_version="3.0.0", aggregate_type="role",
        aggregate_id=uuid4().hex, aggregate_version=1, occurred_at=now(), producer_version="t", payload={}))
    conn.rollback()


def test_worker_builds_but_cannot_publish_or_move_pointer(conn):
    as_role(conn, "pulse_worker")
    rel = PgAnalysisRelease(conn)
    rid = new_release(rel)                                          # INSERT release BUILDING
    ready_all(rel, rid, ["ranking", "plans"])                       # component writes (lock needs UPDATE privilege)
    with pytest.raises(DENIED):
        conn.execute("UPDATE decision.release SET release_hash=%s, published_at=now() WHERE release_id=%s",
                     (h("x"), rid))
    conn.rollback()
    as_role(conn, "pulse_worker")
    for sql in ("INSERT INTO decision.release_slot(slot_key, updated_at) VALUES ('w', now())",
                "UPDATE decision.release_slot SET pointer_generation = pointer_generation",
                "INSERT INTO decision.release_publication VALUES (gen_random_uuid())",
                "SELECT * FROM decision.begin_release_stage(gen_random_uuid(),'s',gen_random_uuid(),repeat('a',64))",
                "SELECT * FROM decision.publish_release_stage(gen_random_uuid(), repeat('a',64), gen_random_uuid())",
                "DELETE FROM ops.outbox",
                "SELECT ops.rotate_outbox_epoch('x')"):
        with pytest.raises(DENIED):
            conn.execute(sql)
        conn.rollback()
        as_role(conn, "pulse_worker")
    conn.rollback()


def test_worker_provisions_partitions_through_definer_function(conn):
    as_role(conn, "pulse_worker")
    assert ensure_bar_partitions(conn, date(2035, 1, 1), date(2035, 1, 31)) == 1
    with pytest.raises(DENIED):
        conn.execute("CREATE TABLE market.rogue(x int)")
    conn.rollback()
    as_role(conn, "pulse_api")
    with pytest.raises(DENIED):
        ensure_bar_partitions(conn, date(2036, 1, 1), date(2036, 1, 31))
    conn.rollback()


def test_worker_builds_and_coordinator_publishes_end_to_end(dsn):
    from pulse.adapters.postgres import db
    slot = f"role:{uuid4().hex[:8]}"
    c = db.connect(dsn)
    as_role(c, "pulse_worker")
    rel = PgAnalysisRelease(c)
    rid = new_release(rel)
    ready_all(rel, rid, ["ranking", "plans"])
    c.commit()
    as_role(c, "pulse_coordinator")
    rel = PgAnalysisRelease(c)
    rel.ensure_slot(slot)
    stage = rel.begin_stage(stage_id=uuid4(), slot_key=slot, release_id=rid, input_context_hash=h("r"))
    res = rel.publish(stage.stage_id, producer_version="coordinator-test")      # outbox + delivery log via definer trigger
    c.commit()
    assert rel.current(slot).release_id == rid and res.pointer_generation == 1
    seq = c.execute("SELECT stream_sequence FROM ops.outbox_position WHERE event_id=%s", (res.event_id,)).fetchone()
    assert seq["stream_sequence"] == 1
    c.close()
