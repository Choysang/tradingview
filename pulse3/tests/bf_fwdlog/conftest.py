"""Fixtures of the fwdlog tests: a private migrated database, a lake in tmp_path, a movable clock, a legacy recorder tree and daily bars that are injected
(the real bars come from market.bar_projection; the stub keeps the test about the lane's rules and its persistence)."""
from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta, timezone

import psycopg
import pytest

from pulse.adapters.postgres import migrate

UTC = timezone.utc
ADMIN = os.environ.get("PULSE_TEST_ADMIN_DSN", "postgresql://postgres@127.0.0.1:55432/postgres")


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += timedelta(**kw)


@pytest.fixture
def fresh_dsn(pg_dsn):
    name = f"pulse_fwd_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f'create database "{name}"')
    dsn = psycopg.conninfo.make_conninfo(ADMIN, dbname=name)
    migrate.apply(dsn)
    yield dsn
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f'drop database if exists "{name}" with (force)')


@pytest.fixture
def lake(tmp_path):
    from pulse.adapters.lake import LakeStore

    return LakeStore(tmp_path / "lake")


@pytest.fixture
def clock():
    return Clock(datetime(2026, 11, 13, 10, 0, tzinfo=UTC))          # 18:00 CST on the second session, after both recorders ended


@pytest.fixture
def legacy_root(tmp_path):
    root = tmp_path / "legacy"
    root.mkdir()
    return root


@pytest.fixture
def conn(fresh_dsn):
    from pulse.adapters.postgres import db

    c = db.connect(fresh_dsn, autocommit=True)
    yield c
    c.close()
