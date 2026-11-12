"""Monthly partitions of market.bar_projection on real PostgreSQL (M0-E03 "月分区", DB-A21)."""
from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import psycopg
import pytest
from w02_helpers import seed_manifest

from pulse.adapters.postgres import db
from pulse.adapters.postgres.partitions import bar_partition_months, ensure_bar_partitions

pytestmark = pytest.mark.pg


def instrument(conn):
    iid = uuid4()
    conn.execute(
        "INSERT INTO core.instrument(instrument_id, instrument_type, venue_code, currency, exchange_timezone, created_at)"
        " VALUES (%s,'equity','XSHG','CNY','Asia/Shanghai',clock_timestamp())", (iid,))
    return iid


def bar(conn, iid, snap, day: date, minute=0):
    opened = datetime(day.year, day.month, day.day, 1, minute, tzinfo=timezone.utc)
    conn.execute(
        "INSERT INTO market.bar_projection(trade_date, instrument_id, timeframe, session_scope, bar_open_time,"
        " bar_close_time, snapshot_id, currency, open_price_raw, high_price_raw, low_price_raw, close_price_raw,"
        " volume_shares, turnover_amount_cny, is_final, available_at, quality_status)"
        " VALUES (%s,%s,'1m','regular',%s,%s,%s,'CNY',10,11,9,10.5,1000,10500,true,%s,'valid')",
        (day, iid, opened, opened + timedelta(minutes=1), snap, opened + timedelta(minutes=2)))


def test_initial_hot_window_is_provisioned(conn):
    months = bar_partition_months(conn)
    assert months[0] == "bar_projection_202401" and months[-1] == "bar_projection_202712"
    assert len(months) == 48


def test_rows_route_to_their_month_and_boundaries_hold(conn):
    iid, snap = instrument(conn), seed_manifest(conn)
    bar(conn, iid, snap, date(2026, 11, 30))
    bar(conn, iid, snap, date(2026, 12, 1))
    rows = conn.execute("SELECT trade_date, tableoid::regclass::text AS part FROM market.bar_projection"
                        " WHERE instrument_id=%s ORDER BY trade_date", (iid,)).fetchall()
    assert [r["part"] for r in rows] == ["market.bar_projection_202611", "market.bar_projection_202612"]
    with pytest.raises(psycopg.errors.UniqueViolation):                 # unique key includes the partition key
        bar(conn, iid, snap, date(2026, 12, 1))
    conn.rollback()


def test_missing_month_is_an_explicit_error_until_provisioned(conn):
    iid, snap = instrument(conn), seed_manifest(conn)
    conn.commit()
    far = date(2031, 3, 15)
    with pytest.raises(psycopg.Error, match="no partition"):
        bar(conn, iid, snap, far)
    conn.rollback()
    created = ensure_bar_partitions(conn, date(2031, 3, 1), date(2031, 4, 30))
    conn.commit()
    assert created == 2
    bar(conn, iid, snap, far)
    assert ensure_bar_partitions(conn, date(2031, 3, 1), date(2031, 4, 30)) == 0       # idempotent
    with pytest.raises(psycopg.errors.RaiseException, match="reversed"):
        ensure_bar_partitions(conn, date(2031, 5, 1), date(2031, 4, 1))
    conn.rollback()


def test_date_range_query_prunes_to_one_partition(conn):
    plan = conn.execute(
        "EXPLAIN (FORMAT JSON) SELECT * FROM market.bar_projection"
        " WHERE trade_date BETWEEN '2026-11-02' AND '2026-11-20' AND instrument_id=%s", (uuid4(),)).fetchone()
    text = json.dumps(list(plan.values())[0])
    assert "bar_projection_202611" in text
    assert "bar_projection_202610" not in text and "bar_projection_202612" not in text


def test_concurrent_deployers_do_not_collide(dsn):
    errors = []
    barrier = threading.Barrier(3)

    def deploy():
        c = db.connect(dsn)
        try:
            barrier.wait()
            ensure_bar_partitions(c, date(2033, 1, 1), date(2033, 6, 30))
            c.commit()
        except Exception as e:      # noqa: BLE001
            errors.append(e)
        finally:
            c.close()

    ts = [threading.Thread(target=deploy) for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors, errors
    c = db.connect(dsn)
    assert sum(1 for m in bar_partition_months(c) if m.startswith("bar_projection_2033")) == 6
    c.close()
