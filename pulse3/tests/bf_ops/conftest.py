"""Fixtures for the ops backfill lane: a private migrated database per test, an API client on it, the I1 synthetic world."""
from __future__ import annotations

import json
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


@pytest.fixture()
def fresh():
    """(connection, dsn) on a private migrated database."""
    try:
        with psycopg.connect(ADMIN, connect_timeout=3):
            pass
    except Exception:
        pytest.skip("PostgreSQL not reachable")
    from ext_c import support as S

    name, dsn = _make_db("pulse_bfops")
    c = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
    S.seed_sources(c)
    yield c, dsn
    c.close()
    _drop(name)


class Api:
    def __init__(self, client: TestClient, principal: uuid.UUID):
        self.c, self.principal = client, principal

    def cookies(self) -> dict:
        return {"pulse_session": f"tok-{self.principal}"}

    def get(self, path: str, **params):
        q = {k: (json.dumps(v) if isinstance(v, (dict, list)) else str(v)) for k, v in params.items() if v is not None}
        return self.c.get(path, params=q, cookies=self.cookies())

    def post(self, path: str, body: dict, key: str | None = None):
        return self.c.post(path, json=body, headers={"Idempotency-Key": key or str(uuid.uuid4())}, cookies=self.cookies())


@pytest.fixture()
def api_on(monkeypatch):
    """Factory ``api_on(dsn, principal)``: API client with one session token, bound to ``dsn``."""
    clients: list[TestClient] = []

    def build(dsn: str, principal: uuid.UUID) -> Api:
        monkeypatch.setenv("PULSE_SESSIONS", f"tok-{principal}={principal}")
        monkeypatch.delenv("PULSE_DEV_PRINCIPAL", raising=False)
        monkeypatch.setenv("PULSE_DSN", dsn)
        monkeypatch.setenv("PULSE_API_HOST", "127.0.0.1")
        from pulse.api import auth, deps
        from pulse.api.app import create_app

        auth._sessions.cache_clear()
        deps.close_pool()
        client = TestClient(create_app())
        client.__enter__()
        clients.append(client)
        return Api(client, principal)

    yield build
    for c in clients:
        c.__exit__(None, None, None)
    from pulse.api import auth, deps
    deps.close_pool()
    auth._sessions.cache_clear()


@pytest.fixture(scope="session")
def world(tmp_path_factory):
    try:
        with psycopg.connect(ADMIN, connect_timeout=3):
            pass
    except Exception:
        pytest.skip("PostgreSQL not reachable")
    from integration import world as W

    name = f"pulse_bfopsw_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN, autocommit=True) as a:
        a.execute(f'create database "{name}"')
    dsn = psycopg.conninfo.make_conninfo(ADMIN, dbname=name)
    migrate.apply(dsn)
    w = W.build_world(dsn, tmp_path_factory.mktemp("bfops"))
    yield w
    _drop(name)
