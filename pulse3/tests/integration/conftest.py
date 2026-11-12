"""Fixtures for the I1 integration tests: one fresh migrated database with the synthetic world imported once."""
from __future__ import annotations

import os
import uuid

import psycopg
import pytest

from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db, migrate

from . import world as W

ADMIN = os.environ.get("PULSE_TEST_ADMIN_DSN", "postgresql://postgres@127.0.0.1:55432/postgres")


@pytest.fixture(scope="session")
def world(tmp_path_factory):
    try:
        with psycopg.connect(ADMIN, connect_timeout=3):
            pass
    except Exception:
        pytest.skip("PostgreSQL not reachable")
    name = f"pulse_i1_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f'create database "{name}"')
    dsn = psycopg.conninfo.make_conninfo(ADMIN, dbname=name)
    migrate.apply(dsn)
    w = W.build_world(dsn, tmp_path_factory.mktemp("i1"))
    yield w
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f'drop database if exists "{name}" with (force)')


@pytest.fixture()
def conn(world):
    c = db.connect(world["dsn"], autocommit=True)
    yield c
    c.close()


@pytest.fixture()
def lake(world):
    return LakeStore(world["lake_root"])
