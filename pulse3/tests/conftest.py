"""Shared test fixtures.

``pg_dsn`` creates a fresh database per test session on the server given by
PULSE_TEST_ADMIN_DSN (default: local PG18 on 55432) and runs every migration.
Tests marked ``pg`` are skipped when no server answers; they are never faked.
"""
from __future__ import annotations

import os
import uuid

import psycopg
import pytest

from pulse.adapters.postgres import migrate

ADMIN = os.environ.get("PULSE_TEST_ADMIN_DSN", "postgresql://postgres@127.0.0.1:55432/postgres")


def _admin_ok() -> bool:
    try:
        with psycopg.connect(ADMIN, connect_timeout=3):
            return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def pg_dsn():
    if not _admin_ok():
        pytest.skip("PostgreSQL not reachable at PULSE_TEST_ADMIN_DSN")
    name = f"pulse_test_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f'create database "{name}"')
    dsn = psycopg.conninfo.make_conninfo(ADMIN, dbname=name)
    migrate.apply(dsn)
    yield dsn
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f'drop database if exists "{name}" with (force)')
