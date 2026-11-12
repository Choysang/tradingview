"""Fixtures for the ext-B tests: the I1 synthetic world (real DataService import, real lake, real PG), one published release,
an API client on the same database and a second principal for ownership checks. SYNTHETIC data: proves wiring and invariants."""
from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from integration import world as W
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db, migrate

ADMIN = os.environ.get("PULSE_TEST_ADMIN_DSN", "postgresql://postgres@127.0.0.1:55432/postgres")


@pytest.fixture(scope="session")
def world(tmp_path_factory):
    try:
        with psycopg.connect(ADMIN, connect_timeout=3):
            pass
    except Exception:
        pytest.skip("PostgreSQL not reachable")
    name = f"pulse_extb_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f'create database "{name}"')
    dsn = psycopg.conninfo.make_conninfo(ADMIN, dbname=name)
    migrate.apply(dsn)
    w = W.build_world(dsn, tmp_path_factory.mktemp("extb"))
    yield w
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f'drop database if exists "{name}" with (force)')


@pytest.fixture(scope="session")
def built(world):
    from pulse.application.release_service import BuildConfig, build_release

    conn = db.connect(world["dsn"], autocommit=True)
    res = build_release(conn, LakeStore(world["lake_root"]),
                        cfg=BuildConfig(legacy_warehouse=str(world["warehouse"]), top_n_plans=5, workspace_page=8, sector_min_members=5),
                        clock=W.clock())
    yield res, conn
    conn.close()


@pytest.fixture()
def conn(world):
    c = db.connect(world["dsn"], autocommit=True)
    yield c
    c.close()


@pytest.fixture()
def lake(world):
    return LakeStore(world["lake_root"])


@pytest.fixture()
def client(world, built, monkeypatch):
    monkeypatch.setenv("PULSE_DSN", world["dsn"])
    monkeypatch.setenv("PULSE_LAKE", str(world["lake_root"]))
    monkeypatch.setenv("PULSE_ENV", "dev")
    monkeypatch.setenv("PULSE_API_HOST", "127.0.0.1")
    monkeypatch.setenv("PULSE_DEV_PRINCIPAL", str(uuid.UUID(int=7)))
    from pulse.api import deps
    from pulse.api.app import create_app

    deps.close_pool()
    with TestClient(create_app()) as c:
        yield c
    deps.close_pool()
