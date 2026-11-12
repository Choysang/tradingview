"""Fixtures for the group-C tests: a real migrated PostgreSQL, a dev-principal API client, per-test principals."""
from __future__ import annotations

import json
import uuid
from urllib.parse import quote

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from . import support as S


@pytest.fixture()
def conn(pg_dsn):
    c = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=True)
    S.seed_sources(c)
    yield c
    c.close()


@pytest.fixture()
def principal() -> uuid.UUID:
    return uuid.uuid4()


class Api:
    """TestClient wrapper: queries as JSON-text params, commands with an Idempotency-Key."""

    def __init__(self, client: TestClient, principal: uuid.UUID):
        self.c, self.principal = client, principal
        self.sessions: dict[str, uuid.UUID] = {}

    def as_(self, who: uuid.UUID) -> "Api":
        a = Api(self.c, who)
        return a

    def _cookies(self) -> dict:
        return {"pulse_session": f"tok-{self.principal}"}

    def get(self, op_path: str, **params):
        q = {k: (json.dumps(v, default=str) if isinstance(v, (dict, list)) else str(v)) for k, v in params.items() if v is not None}
        return self.c.get(f"/v1/x/{op_path}", params=q, cookies=self._cookies())

    def post(self, op_path: str, body: dict, key: str | None = None):
        return self.c.post(f"/v1/x/{op_path}", json=body, headers={"Idempotency-Key": key or str(uuid.uuid4())}, cookies=self._cookies())


@pytest.fixture()
def make_api(pg_dsn, monkeypatch):
    """Factory: api(principal) with a session token per principal (no dev principal, so ownership is real)."""
    created: list[uuid.UUID] = []

    def build(*principals: uuid.UUID) -> Api:
        sess = ",".join(f"tok-{p}={p}" for p in principals)
        monkeypatch.setenv("PULSE_SESSIONS", sess)
        monkeypatch.delenv("PULSE_DEV_PRINCIPAL", raising=False)
        monkeypatch.setenv("PULSE_DSN", pg_dsn)
        monkeypatch.setenv("PULSE_API_HOST", "127.0.0.1")
        from pulse.api import auth, deps
        from pulse.api.app import create_app

        auth._sessions.cache_clear()
        deps.close_pool()
        client = TestClient(create_app())
        client.__enter__()
        created.append(client)
        return Api(client, principals[0])

    yield build
    for c in created:
        c.__exit__(None, None, None)
    from pulse.api import auth, deps
    deps.close_pool()
    auth._sessions.cache_clear()


@pytest.fixture()
def fresh_conn():
    """A private migrated database for tests whose result depends on global tables (calendar of all bars, the 'main' slot)."""
    import os

    from pulse.adapters.postgres import migrate

    admin = os.environ.get("PULSE_TEST_ADMIN_DSN", "postgresql://postgres@127.0.0.1:55432/postgres")
    name = f"pulse_extc_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(admin, autocommit=True) as a:
        a.execute(f'create database "{name}"')
    dsn = psycopg.conninfo.make_conninfo(admin, dbname=name)
    migrate.apply(dsn)
    c = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
    S.seed_sources(c)
    c.dsn_for_test = dsn                                   # type: ignore[attr-defined]
    yield c
    c.close()
    with psycopg.connect(admin, autocommit=True) as a:
        a.execute(f'drop database if exists "{name}" with (force)')
