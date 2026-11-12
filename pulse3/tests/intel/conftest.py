"""Fixtures for the intelligence (I3) tests: private migrated DB, lake, fixed clock, seeded instruments, recorded transport."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import psycopg
import pytest

from pulse.adapters.postgres import instrument_repo, migrate
from pulse.domain.instruments.identity import InstrumentRecord

UTC = timezone.utc
ADMIN = os.environ.get("PULSE_TEST_ADMIN_DSN", "postgresql://postgres@127.0.0.1:55432/postgres")
NAMES = {"000001": "平安银行", "000530": "冰山冷热", "605136": "丽人丽妆", "300302": "同有科技", "600000": "浦发银行", "002415": "海康威视"}


@pytest.fixture
def fresh_dsn(pg_dsn):
    name = f"pulse_intel_{uuid.uuid4().hex[:10]}"
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


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> None:
        from datetime import timedelta

        self.t += timedelta(**kw)


@pytest.fixture
def clock():
    return Clock(datetime(2026, 11, 11, 12, 45, tzinfo=UTC))  # 20:45 CST, just after the recorded captures


@pytest.fixture
def conn(fresh_dsn):
    from pulse.adapters.postgres import db

    c = db.connect(fresh_dsn, autocommit=True)
    recs = [InstrumentRecord.build(code, name) for code, name in NAMES.items()]
    with c.transaction():
        instrument_repo.upsert_instruments(c, recs, datetime(2026, 11, 1, tzinfo=UTC), (), "intel-tests")
    yield c
    c.close()


@pytest.fixture
def svc(conn, lake, clock):
    from pulse.application.intelligence_service import IntelligenceService

    s = IntelligenceService(conn, lake, clock=clock)
    s.bootstrap()
    return s
