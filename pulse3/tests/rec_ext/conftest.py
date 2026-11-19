"""Fixtures of the native ext recorder tests: a private migrated database, a lake in tmp_path, a world clock shared by service / pacing / sleep."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

from pulse.adapters.postgres import migrate

UTC = timezone.utc
ADMIN = os.environ.get("PULSE_TEST_ADMIN_DSN", "postgresql://postgres@127.0.0.1:55432/postgres")
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "ext" / "recorded"


@pytest.fixture
def fresh_dsn(pg_dsn):
    name = f"pulse_ext_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f'create database "{name}"')
    dsn = psycopg.conninfo.make_conninfo(ADMIN, dbname=name)
    migrate.apply(dsn)
    yield dsn
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f'drop database if exists "{name}" with (force)')


@pytest.fixture
def conn(fresh_dsn):
    from pulse.adapters.postgres import db

    c = db.connect(fresh_dsn, autocommit=True)
    yield c
    c.close()


@pytest.fixture
def lake(tmp_path):
    from pulse.adapters.lake import LakeStore

    return LakeStore(tmp_path / "lake")


@pytest.fixture
def legacy_root(tmp_path):
    root = tmp_path / "legacy"
    root.mkdir()
    return root


class World:
    """One fake clock for the service, the clients' pacing and the recorder's sleep (start given in Asia/Shanghai)."""

    def __init__(self, cst: datetime):
        self.t = cst.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(UTC)
        self.mono = 0.0

    def now(self) -> datetime:
        return self.t

    def sleep(self, secs: float) -> None:
        self.t += timedelta(seconds=secs)
        self.mono += secs

    def monotonic(self) -> float:
        return self.mono

    def cst(self) -> datetime:
        return self.t.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)


@pytest.fixture
def make_world():
    return World
