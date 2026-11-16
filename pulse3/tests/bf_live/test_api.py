"""The two reads over HTTP on real PostgreSQL (envelope + payload contract), and the web fixtures against the same contract."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pulse import contracts
from pulse.adapters.sources.legacy_recordings import LegacyRecordings
from pulse.application import bf_live_service as B
from pulse.domain.instruments.identity import instrument_uuid, venue_for_code

from . import support as T

WEB_FIXTURES = Path(__file__).resolve().parents[2] / "apps" / "web" / "src" / "pages" / "bf" / "live" / "fixtures"


@pytest.fixture
def client(fresh_dsn, lake, legacy_root, clock, conn, monkeypatch):
    q, hot = T.scenario_rows()
    T.write_quotes(legacy_root, "quotes", "part-0001.parquet", q)
    T.write_quotes(legacy_root, "hot", "part-0001.parquet", hot)
    svc = B.LiveService(conn, lake, LegacyRecordings(legacy_root), clock=clock)
    svc.bootstrap()
    svc.import_intraday(T.DAY)
    svc.publish_seal_facts(T.DAY)
    monkeypatch.setenv("PULSE_DSN", fresh_dsn)
    monkeypatch.setenv("PULSE_LAKE", str(lake.root))
    monkeypatch.setenv("PULSE_ENV", "dev")
    monkeypatch.setenv("PULSE_API_HOST", "127.0.0.1")
    monkeypatch.setenv("PULSE_DEV_PRINCIPAL", "00000000-0000-0000-0000-000000000001")
    from pulse.api import deps
    from pulse.api.app import create_app

    deps.close_pool()
    with TestClient(create_app()) as c:
        yield c
    deps.close_pool()


@pytest.mark.pg
def test_seal_clock_route(client):
    r = client.get("/v1/x/bf/live/seal-clock", params={"session_date": "2026-11-12"})
    assert r.status_code == 200
    body = r.json()
    contracts.check("SealClock", body["data"], contracts.backfill_id("live"))
    contracts.check("ResponseMeta", body["meta"])
    d = body["data"]
    assert d["sealed_ever_count"] == 2 and d["estimation"] == "estimated_from_60s_snapshots" and body["meta"]["warnings"][0]["code"] == "ESTIMATED_FROM_60S_SNAPSHOTS"
    assert body["meta"]["quality_status"] == "partial" and d["buckets"][0]["clock"] == "09:25"
    assert client.get("/v1/x/bf/live/seal-clock").json()["data"]["session_date"] == "2026-11-12"          # latest session by default
    assert client.get("/v1/x/bf/live/seal-clock", params={"session_date": "2026-11-11"}).status_code == 404
    assert client.get("/v1/x/bf/live/seal-clock", params={"bucket_minutes": 7}).status_code == 400


@pytest.mark.pg
def test_seal_facts_route(client):
    iid = instrument_uuid(venue_for_code("000001"), "000001")
    r = client.get("/v1/x/bf/live/seal-facts", params={"instrument_id": str(iid), "session_date": "2026-11-12"})
    assert r.status_code == 200
    d = r.json()["data"]
    contracts.check("SealFacts", d, contracts.backfill_id("live"))
    assert d["status"] == "ok" and d["fact"]["first_seal_at"] == "2026-11-12T01:35:00Z" and d["fact"]["close_seal_order_amount_cny"] == "8800000"
    other = client.get("/v1/x/bf/live/seal-facts", params={"instrument_id": "11111111-1111-4111-8111-111111111111"}).json()["data"]
    contracts.check("SealFacts", other, contracts.backfill_id("live"))
    assert other["status"] == "not_observed" and other["fact"] is None
    assert client.get("/v1/x/bf/live/seal-facts", params={"instrument_id": "nope"}).status_code == 400


def test_web_fixtures_pass_the_contract():
    seen = 0
    for name, definition in (("seal-clock.json", "SealClock"), ("seal-facts.json", "SealFacts")):
        doc = json.loads((WEB_FIXTURES / name).read_text(encoding="utf-8"))
        contracts.check(definition, doc["data"], contracts.backfill_id("live"))
        contracts.check("ResponseMeta", doc["meta"])
        seen += 1
    assert seen == 2
