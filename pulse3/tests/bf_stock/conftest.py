"""Fixtures for the stock backfill lane: a real PostgreSQL (fresh, migrated), the API client on it and the market seed helpers
of tests/api (direct SQL rows in the package DDL). SYNTHETIC rows: they prove wiring, point-in-time cutoffs and null policy."""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from pulse.adapters.postgres import migrate

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))
import api_seed  # noqa: E402


ADMIN = os.environ.get("PULSE_TEST_ADMIN_DSN", "postgresql://postgres@127.0.0.1:55432/postgres")


@pytest.fixture()
def fresh_dsn(pg_dsn):
    """A private migrated database per test: cap datasets and late bars are global rows, tests must not see each other's."""
    name = f"pulse_bfstock_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f'create database "{name}"')
    dsn = psycopg.conninfo.make_conninfo(ADMIN, dbname=name)
    migrate.apply(dsn)
    yield dsn
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f'drop database if exists "{name}" with (force)')


@pytest.fixture()
def client(fresh_dsn, monkeypatch, tmp_path):
    monkeypatch.setenv("PULSE_DSN", fresh_dsn)
    monkeypatch.setenv("PULSE_ENV", "dev")
    monkeypatch.setenv("PULSE_API_HOST", "127.0.0.1")
    monkeypatch.setenv("PULSE_DEV_PRINCIPAL", str(uuid.UUID(int=7)))
    monkeypatch.setenv("PULSE_LAKE", str(tmp_path / "lake"))
    from pulse.api import deps
    from pulse.api.app import create_app

    deps.close_pool()
    with TestClient(create_app()) as c:
        yield c
    deps.close_pool()


@pytest.fixture()
def db(fresh_dsn):
    conn = psycopg.connect(fresh_dsn, row_factory=dict_row)
    api_seed.seed_sources(conn)
    yield conn
    conn.close()
