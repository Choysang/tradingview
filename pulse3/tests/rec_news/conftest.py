"""Fixtures of the native news recorder PostgreSQL tests: private migrated database, lake, fixed clock, registered instruments."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import psycopg
import pytest

from pulse.adapters.postgres import instrument_repo, migrate
from pulse.domain.instruments.identity import InstrumentRecord

from .support import Clock

UTC = timezone.utc
ADMIN = os.environ.get("PULSE_TEST_ADMIN_DSN", "postgresql://postgres@127.0.0.1:55432/postgres")
NAMES = {"000001": "平安银行", "600000": "浦发银行", "000002": "万科A", "605136": "丽人丽妆", "300302": "同有科技"}


@pytest.fixture
def fresh_dsn(pg_dsn):
    name = f"pulse_recnews_{uuid.uuid4().hex[:10]}"
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
    return Clock(datetime(2026, 11, 12, 8, 0, tzinfo=UTC))          # 16:00 CST


@pytest.fixture
def conn(fresh_dsn):
    from pulse.adapters.postgres import db

    c = db.connect(fresh_dsn, autocommit=True)
    recs = [InstrumentRecord.build(code, name) for code, name in NAMES.items()]
    with c.transaction():
        instrument_repo.upsert_instruments(c, recs, datetime(2026, 11, 1, tzinfo=UTC), (), "rec-news-tests")
    yield c
    c.close()
