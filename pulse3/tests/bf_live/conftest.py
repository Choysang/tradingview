"""Fixtures of the live-lane tests: a private migrated database, a lake in tmp_path, a fixed clock, registered instruments."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from pulse.adapters.postgres import instrument_repo, migrate
from pulse.domain.instruments.identity import InstrumentRecord

UTC = timezone.utc
ADMIN = os.environ.get("PULSE_TEST_ADMIN_DSN", "postgresql://postgres@127.0.0.1:55432/postgres")
NAMES = {"000001": "平安银行", "600000": "浦发银行", "000002": "万科A", "600519": "测试酒业", "605136": "丽人丽妆"}


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += timedelta(**kw)


@pytest.fixture
def fresh_dsn(pg_dsn):
    name = f"pulse_live_{uuid.uuid4().hex[:10]}"
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
    return Clock(datetime(2026, 11, 12, 8, 0, tzinfo=UTC))          # 16:00 CST, after the close


@pytest.fixture
def legacy_root(tmp_path):
    root = tmp_path / "legacy"
    root.mkdir()
    return root


@pytest.fixture
def conn(fresh_dsn):
    from pulse.adapters.postgres import db

    c = db.connect(fresh_dsn, autocommit=True)
    recs = [InstrumentRecord.build(code, name) for code, name in NAMES.items()]
    with c.transaction():
        instrument_repo.upsert_instruments(c, recs, datetime(2026, 11, 1, tzinfo=UTC), (), "live-tests")
    yield c
    c.close()
