"""Fixtures for the data-plane tests."""
from __future__ import annotations

import os
import uuid

import psycopg
import pytest

from pulse.adapters.postgres import migrate


@pytest.fixture
def lake(tmp_path):
    from pulse.adapters.lake import LakeStore

    return LakeStore(tmp_path / "lake")


@pytest.fixture
def fresh_dsn(pg_dsn):
    """A private migrated database per test: ingest tests mutate shared tables."""
    admin = os.environ.get("PULSE_TEST_ADMIN_DSN", "postgresql://postgres@127.0.0.1:55432/postgres")
    name = f"pulse_data_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f'create database "{name}"')
    dsn = psycopg.conninfo.make_conninfo(admin, dbname=name)
    migrate.apply(dsn)
    yield dsn
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f'drop database if exists "{name}" with (force)')
