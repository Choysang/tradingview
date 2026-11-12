"""Clean-build and upgrade-build of the migration sequence on real PostgreSQL (M0-E03, DB-A21)."""
from __future__ import annotations

import os
import uuid

import psycopg
import pytest

from pulse.adapters.postgres import migrate

pytestmark = pytest.mark.pg
ADMIN = os.environ.get("PULSE_TEST_ADMIN_DSN", "postgresql://postgres@127.0.0.1:55432/postgres")


@pytest.fixture
def scratch_db():
    name = f"pulse_mig_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f'create database "{name}"')
    yield psycopg.conninfo.make_conninfo(ADMIN, dbname=name)
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f'drop database if exists "{name}" with (force)')


def test_migration_order_is_core_ledger_sources_integration_then_w02(pg_dsn):
    names = [p.name for p in migrate.files()]
    assert names[:4] == ["0001_core_schema.sql", "0002_ledger_schema.sql", "0003_sources_sectors_schema.sql",
                         "0004_integration_schema.sql"]
    assert names[4:] == sorted(names[4:]) and len(names) >= 7
    with psycopg.connect(pg_dsn) as c:
        applied = [r[0] for r in c.execute("select name from public.schema_migration order by name")]
    assert applied == names


def test_clean_build_is_repeatable_and_checksum_guarded(scratch_db):
    first = migrate.apply(scratch_db)
    assert first == [p.name for p in migrate.files()]
    assert migrate.apply(scratch_db) == []                                    # second run is a no-op
    with psycopg.connect(scratch_db, autocommit=True) as c:
        c.execute("update public.schema_migration set sha256=repeat('0',64) where name like '0006%'")
    with pytest.raises(RuntimeError, match="changed after it was applied"):
        migrate.apply(scratch_db)


def test_upgrade_from_package_baseline_backfills_delivery_log(scratch_db, monkeypatch):
    all_files = migrate.files()
    monkeypatch.setattr(migrate, "files", lambda: all_files[:4])             # a database that only has the package DDL
    assert len(migrate.apply(scratch_db)) == 4
    eid = uuid.uuid4()
    with psycopg.connect(scratch_db, autocommit=True) as c:
        c.execute(
            "insert into ops.outbox(event_id,event_type,schema_version,aggregate_type,aggregate_id,aggregate_version,"
            " occurred_at,recorded_at,producer_version,payload,payload_hash)"
            " values (%s,'decision.published','3.0.0','release_slot','legacy-slot',1,now(),now(),'old','{}',repeat('a',64))",
            (eid,))
    monkeypatch.undo()
    upgraded = migrate.apply(scratch_db)
    assert upgraded == [p.name for p in all_files[4:]]
    with psycopg.connect(scratch_db) as c:
        row = c.execute("select stream_name, stream_epoch, stream_sequence from ops.outbox_position where event_id=%s",
                        (eid,)).fetchone()
        parts = c.execute("select count(*) from pg_inherits where inhparent='market.bar_projection'::regclass").fetchone()[0]
    assert row == ("release_slot:legacy-slot", "1", 1)
    assert parts == 48
