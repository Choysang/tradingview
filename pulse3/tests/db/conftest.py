"""Fixtures for tests/db: one fresh connection per test; committed work is visible to other connections."""
from __future__ import annotations

import pytest

from pulse.adapters.postgres import db


@pytest.fixture
def conn(pg_dsn):
    c = db.connect(pg_dsn)
    try:
        yield c
    finally:
        c.rollback()
        c.close()


@pytest.fixture
def dsn(pg_dsn):
    return pg_dsn
