"""M5-E02 drill: after the database drops every pooled connection, the next request is served (not a 500)."""
from __future__ import annotations

import psycopg


def test_request_after_backends_are_terminated_succeeds(client, pg_dsn):
    assert client.get("/app-config.json").status_code == 200
    with psycopg.connect(pg_dsn, autocommit=True) as admin:          # what a database restart does to the pool
        admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                      " WHERE datname = current_database() AND pid <> pg_backend_pid()")
    assert client.get("/app-config.json").status_code == 200
