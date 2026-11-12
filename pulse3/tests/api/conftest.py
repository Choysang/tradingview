import uuid

import psycopg
import pytest
from psycopg.rows import dict_row
from fastapi.testclient import TestClient

import api_seed


@pytest.fixture()
def client(pg_dsn, monkeypatch):
    monkeypatch.setenv("PULSE_DSN", pg_dsn)
    monkeypatch.setenv("PULSE_ENV", "dev")
    monkeypatch.setenv("PULSE_API_HOST", "127.0.0.1")
    monkeypatch.setenv("PULSE_DEV_PRINCIPAL", str(uuid.UUID(int=7)))
    from pulse.api import deps
    from pulse.api.app import create_app

    deps.close_pool()
    with TestClient(create_app()) as c:
        yield c
    deps.close_pool()



@pytest.fixture()
def db(pg_dsn):
    conn = psycopg.connect(pg_dsn, row_factory=dict_row)
    api_seed.seed_sources(conn)
    yield conn
    conn.close()


