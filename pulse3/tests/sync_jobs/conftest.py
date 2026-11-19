"""Fixtures of the sync-job tests: a migrated empty PostgreSQL database (no market data needed) and API clients on it."""
from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from pulse.adapters.postgres import db, migrate

ADMIN = os.environ.get("PULSE_TEST_ADMIN_DSN", "postgresql://postgres@127.0.0.1:55432/postgres")
OPS = uuid.UUID(int=7)
OUTSIDER = uuid.UUID(int=8)


@pytest.fixture(scope="module")
def dsn():
    try:
        with psycopg.connect(ADMIN, connect_timeout=3):
            pass
    except Exception:
        pytest.skip("PostgreSQL not reachable")
    name = f"pulse_sync_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f'create database "{name}"')
    out = psycopg.conninfo.make_conninfo(ADMIN, dbname=name)
    migrate.apply(out)
    yield out
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f'drop database if exists "{name}" with (force)')


@pytest.fixture()
def conn(dsn):
    """A connection on a database with no sync runs (every test starts clean)."""
    c = db.connect(dsn, autocommit=True)
    c.execute("DELETE FROM ops.sync_job_log")      # job runs are permanent: leftovers are cancelled so they never count as active
    c.execute("UPDATE ops.job_run SET run_state='cancelled', completed_at=now() WHERE run_state IN ('queued','running')"
              " AND job_id IN (SELECT job_id FROM ops.job_definition WHERE application_operation='SyncJobService.run_job')")
    yield c
    c.close()


def _client(monkeypatch, dsn, tmp_path, **env):
    from pulse.api import auth, deps
    from pulse.api.app import create_app

    monkeypatch.setenv("PULSE_DSN", dsn)
    monkeypatch.setenv("PULSE_LAKE", str(tmp_path))
    monkeypatch.setenv("PULSE_API_HOST", "127.0.0.1")
    for k in ("PULSE_DEV_PRINCIPAL", "PULSE_SESSIONS", "PULSE_OPS_PRINCIPALS", "PULSE_ENV", "PULSE_SYNC_EXECUTOR"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    auth._sessions.cache_clear()
    deps.close_pool()
    return create_app(), deps


@pytest.fixture()
def client(dsn, conn, monkeypatch, tmp_path):
    """Session of an ops principal (the dev principal is ops when PULSE_ENV=dev)."""
    app, deps = _client(monkeypatch, dsn, tmp_path, PULSE_ENV="dev", PULSE_DEV_PRINCIPAL=str(OPS), PULSE_SYNC_EXECUTOR="1")
    with TestClient(app) as c:
        yield c
    deps.close_pool()


@pytest.fixture()
def client_no_executor(dsn, conn, monkeypatch, tmp_path):
    """Ops session on a host where no sync worker is scheduled (before the cutover)."""
    app, deps = _client(monkeypatch, dsn, tmp_path, PULSE_ENV="dev", PULSE_DEV_PRINCIPAL=str(OPS))
    with TestClient(app) as c:
        yield c
    deps.close_pool()


@pytest.fixture()
def anonymous(dsn, conn, monkeypatch, tmp_path):
    app, deps = _client(monkeypatch, dsn, tmp_path)
    with TestClient(app) as c:
        yield c
    deps.close_pool()


@pytest.fixture()
def outsider(dsn, conn, monkeypatch, tmp_path):
    """A valid session that is NOT an ops principal."""
    app, deps = _client(monkeypatch, dsn, tmp_path, PULSE_SESSIONS=f"tok={OUTSIDER}", PULSE_OPS_PRINCIPALS=str(OPS), PULSE_SYNC_EXECUTOR="1")
    with TestClient(app) as c:
        c.cookies.set("pulse_session", "tok")
        yield c
    deps.close_pool()
