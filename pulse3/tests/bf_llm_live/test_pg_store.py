"""E092 on real PostgreSQL: the lake log keeps rows append-only across processes, the advisory lock excludes a second writer, and the registry rows are written once.

Skipped when no PostgreSQL answers (``pg_dsn``); never faked. The vendor is a mock transport: no key, no network.
"""
from __future__ import annotations

from datetime import datetime, timezone

import polars as pl
import pytest

from pulse.adapters.postgres import db
from pulse.application import bf_fusion_service as S
from pulse.application import bf_llm_live_registry as REG
from pulse.application import bindings as B
from pulse.application.bf_llm_live_store import LakeLogStore, make_row
from pulse.application.data_service import DataService
from pulse.domain.llm_live import rules as R

pytestmark = pytest.mark.pg
NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


def test_rows_survive_a_new_store_object_and_are_never_rewritten(conn, lake):
    store = LakeLogStore(DataService(conn, lake, clock=lambda: NOW))
    store.append([make_row("2026-11-23", "call", "k1", "r1", {"n": 1, "text": "中文 ok"}, at=NOW)])
    store.append([make_row("2026-11-23", "answer", "k1", "r1", {"p_outperform": 55}, at=NOW)])
    other = LakeLogStore(DataService(conn, lake, clock=lambda: NOW))
    rows = other.rows()
    assert [r["kind"] for r in rows] == ["call", "answer"] and rows[0]["body"]["text"] == "中文 ok"
    n = conn.execute("SELECT count(*) AS n FROM market.dataset_manifest WHERE dataset_id='llm_live_log' AND manifest_state='READY'").fetchone()["n"]
    assert n == 2                                                                         # one delta snapshot per append, nothing replaced


def test_the_advisory_lock_excludes_a_second_connection(conn, lake, fresh_dsn):
    first = LakeLogStore(DataService(conn, lake, clock=lambda: NOW))
    c2 = db.connect(fresh_dsn, autocommit=True)
    try:
        second = LakeLogStore(DataService(c2, lake, clock=lambda: NOW))
        with first.exclusive() as a:
            assert a is True
            with second.exclusive() as b:
                assert b is False
        with second.exclusive() as b:
            assert b is True
    finally:
        c2.close()


def test_registered_once_as_shadow_with_the_frozen_hash(conn, lake):
    svc = DataService(conn, lake, clock=lambda: NOW)
    frame = pl.DataFrame({"code": ["600000"], "date": [NOW.date()], "score": [1.0]}, schema=S.CURRENT_SCHEMA)
    B.publish_derived(svc, S.DS_CURRENT, frame, {"dataset": S.DS_CURRENT}, contract_id="pulse.analysis_artifact", watermark=NOW)
    first = REG.register(conn, NOW)
    second = REG.register(conn, NOW)
    assert first["experiment_id"] == second["experiment_id"]
    runs = conn.execute("SELECT run_purpose, run_state, run_manifest FROM research.experiment_run WHERE run_manifest->>'lane'='llm_live'").fetchall()
    assert len(runs) == 1 and runs[0]["run_purpose"] == "shadow" and runs[0]["run_state"] == "running" and R.SPEC_SHA256 in str(runs[0]["run_manifest"])


def test_a_decide_day_end_to_end_on_the_lake_log_with_the_real_registry_hook(conn, lake):
    import httpx

    from pulse.adapters.models.responses_client import ResponsesClient
    from pulse.application.bf_llm_live_service import LlmLive
    from bf_llm_live.support import DAY, KEY, Recorder, plan

    svc = DataService(conn, lake, clock=lambda: NOW)
    frame = pl.DataFrame({"code": ["600000"], "date": [NOW.date()], "score": [1.0]}, schema=S.CURRENT_SCHEMA)
    B.publish_derived(svc, S.DS_CURRENT, frame, {"dataset": S.DS_CURRENT}, contract_id="pulse.analysis_artifact", watermark=NOW)
    vendor = Recorder()
    live = LlmLive(LakeLogStore(svc), context=lambda d: plan(d, n_top=2, n_ctl=1), labels=lambda ds: {}, key_loader=lambda: KEY,
                   client_factory=lambda key: ResponsesClient(key, transport=httpx.MockTransport(vendor)), clock=lambda: NOW, switch_on=lambda: True,
                   register=lambda now: REG.register(conn, now), workers=1)
    out = live.decide(DAY)
    assert out["status"] == "done" and out["asked"] == 3 and len(vendor.requests) == 3
    reopened = LakeLogStore(DataService(conn, lake, clock=lambda: NOW)).rows()
    assert [r["kind"] for r in reopened].count("call") == 3 and [r["kind"] for r in reopened].count("answer") == 3 and KEY not in str(reopened)
    assert conn.execute("SELECT count(*) AS n FROM research.experiment_run WHERE run_manifest->>'lane'='llm_live'").fetchone()["n"] == 1
    assert live.decide(DAY)["status"] == "complete" and len(vendor.requests) == 3
