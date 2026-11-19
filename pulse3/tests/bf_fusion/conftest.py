"""Fixtures of the fusion lane tests: a private migrated database per test, an API client on it, the I1 synthetic world (module scope)."""
from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from pulse.adapters.postgres import migrate

ADMIN = os.environ.get("PULSE_TEST_ADMIN_DSN", "postgresql://postgres@127.0.0.1:55432/postgres")


def _make_db(prefix: str) -> tuple[str, str]:
    name = f"{prefix}_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN, autocommit=True) as a:
        a.execute(f'create database "{name}"')
    dsn = psycopg.conninfo.make_conninfo(ADMIN, dbname=name)
    migrate.apply(dsn)
    return name, dsn


def _drop(name: str) -> None:
    with psycopg.connect(ADMIN, autocommit=True) as a:
        a.execute(f'drop database if exists "{name}" with (force)')


def _need_pg() -> None:
    try:
        with psycopg.connect(ADMIN, connect_timeout=3):
            pass
    except Exception:
        pytest.skip("PostgreSQL not reachable")


@pytest.fixture()
def fresh(tmp_path):
    """(connection, dsn, lake root) on a private migrated database; the core contracts and sources are seeded."""
    _need_pg()
    from ext_c import support as S

    name, dsn = _make_db("pulse_bffz")
    c = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
    S.seed_sources(c)
    yield c, dsn, tmp_path / "lake"
    c.close()
    _drop(name)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    _need_pg()
    from integration import world as W

    name = f"pulse_bffzw_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN, autocommit=True) as a:
        a.execute(f'create database "{name}"')
    dsn = psycopg.conninfo.make_conninfo(ADMIN, dbname=name)
    migrate.apply(dsn)
    w = W.build_world(dsn, tmp_path_factory.mktemp("bffz"))
    yield w
    _drop(name)


@pytest.fixture()
def api_on(monkeypatch):
    """Factory ``api_on(dsn, lake_root, principal)``: an API client with one session token, bound to ``dsn`` and the lake."""
    clients: list[TestClient] = []

    def build(dsn: str, lake_root, principal: uuid.UUID) -> TestClient:
        monkeypatch.setenv("PULSE_SESSIONS", f"tok-{principal}={principal}")
        monkeypatch.delenv("PULSE_DEV_PRINCIPAL", raising=False)
        monkeypatch.setenv("PULSE_DSN", dsn)
        monkeypatch.setenv("PULSE_LAKE", str(lake_root))
        monkeypatch.setenv("PULSE_API_HOST", "127.0.0.1")
        from pulse.api import auth, deps
        from pulse.api.app import create_app

        auth._sessions.cache_clear()
        deps.close_pool()
        client = TestClient(create_app(), cookies={"pulse_session": f"tok-{principal}"})
        client.__enter__()
        clients.append(client)
        return client

    yield build
    for c in clients:
        c.__exit__(None, None, None)
    from pulse.api import auth, deps

    deps.close_pool()
    auth._sessions.cache_clear()
