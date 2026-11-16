"""Operator health probe and the API/SPA boundary (an unknown /v1 path must not return the web page with 200)."""
from __future__ import annotations


def test_healthz_reports_database(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok", "database": True}


def test_unknown_api_path_is_a_json_404(client):
    r = client.get("/v1/does-not-exist")
    assert r.status_code in (404, 405)
    assert r.headers["content-type"].startswith("application/json")
