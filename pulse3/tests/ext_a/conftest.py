"""Fixtures for the extension-group-A tests: the I1 synthetic world with deterministic limit-up / broken-board paths,
one published release (with the optional ``market_state`` component) and an API client on the same database.

Why a patched walk: random walks within +-3% never touch a price limit, so the ladder / promotion / broken-board
facts would all be zero. The last sessions of five codes are replaced by exact limit-up and broken-board paths.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

from integration import world as W
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db, migrate

ADMIN = os.environ.get("PULSE_TEST_ADMIN_DSN", "postgresql://postgres@127.0.0.1:55432/postgres")
LIMIT_UP = ["600001", "600002"]            # two limit-ups on each of the last two sessions (1st then 2nd board)
FIRST_ONLY = ["600003"]                    # limit-up on the last session only
BROKEN = ["600004"]                        # touches the limit on the last session and closes below it
PATCHED = LIMIT_UP + FIRST_ONLY + BROKEN


def patched_walk(real_walk):
    def walk(code, ds, base):
        rows = real_walk(code, ds, base)
        if code not in PATCHED:
            return rows
        n = len(rows)
        flat = [(10.0, 10.0, 10.0, 10.0, 1_000_000.0)] * (n - 2)
        if code in LIMIT_UP:
            tail = [(10.0, 11.0, 10.0, 11.0, 5_000_000.0), (11.0, 12.1, 11.0, 12.1, 6_000_000.0)]
        elif code in FIRST_ONLY:
            tail = [(10.0, 10.1, 10.0, 10.0, 1_000_000.0), (10.0, 11.0, 10.0, 11.0, 5_000_000.0)]
        else:
            tail = [(10.0, 10.1, 10.0, 10.0, 1_000_000.0), (10.0, 11.0, 10.0, 10.4, 4_000_000.0)]
        return flat + tail
    return walk


@pytest.fixture(scope="session")
def world(tmp_path_factory):
    try:
        with psycopg.connect(ADMIN, connect_timeout=3):
            pass
    except Exception:
        pytest.skip("PostgreSQL not reachable")
    name = f"pulse_exta_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f'create database "{name}"')
    dsn = psycopg.conninfo.make_conninfo(ADMIN, dbname=name)
    migrate.apply(dsn)
    real = W.walk
    W.walk = patched_walk(real)
    try:
        w = W.build_world(dsn, tmp_path_factory.mktemp("exta"))
    finally:
        W.walk = real
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
    monkeypatch.setenv("PULSE_SSE_MAX_SECONDS", "2")
    monkeypatch.setenv("PULSE_SSE_POLL_SECONDS", "0.2")
    from pulse.api import deps
    from pulse.api.app import create_app

    deps.close_pool()
    with TestClient(create_app()) as c:
        yield c
    deps.close_pool()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
