"""PostgreSQL connection and transaction helpers (the only place psycopg is opened).

Domain code never imports this module; application services receive a
connection or a repository through their ports.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from pulse import config


def connect(dsn: str | None = None, *, autocommit: bool = False) -> psycopg.Connection:
    conn = psycopg.connect(dsn or config.load().dsn, autocommit=autocommit, row_factory=dict_row)
    with conn.cursor() as cur:
        cur.execute("SET TIME ZONE 'UTC'")
    if not autocommit:
        conn.commit()
    return conn


@contextmanager
def transaction(conn: psycopg.Connection) -> Iterator[psycopg.Connection]:
    """READ COMMITTED transaction (declared isolation for release/ranking writes, see DB02)."""
    with conn.transaction():
        yield conn
