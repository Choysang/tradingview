"""Per-request database connection from a shared pool."""
from __future__ import annotations

from collections.abc import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from pulse import config

_pool: ConnectionPool | None = None


def _configure(conn: psycopg.Connection) -> None:
    conn.execute("SET TIME ZONE 'UTC'")
    conn.commit()


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        # check: a connection broken by a database restart is replaced before it serves a request (M5-E02 drill:
        # the first request after `Restart-Service pulse3-pg` returned 500 without it).
        _pool = ConnectionPool(config.load().dsn, min_size=1, max_size=8, open=True, configure=_configure,
                               check=ConnectionPool.check_connection, kwargs={"row_factory": dict_row, "autocommit": True})
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def conn_dep() -> Iterator[psycopg.Connection]:
    with pool().connection() as conn:
        yield conn
