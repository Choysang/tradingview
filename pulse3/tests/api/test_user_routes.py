import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.mark.pg
def test_layout_create_update_conflict_and_idempotency(client):
    lid = str(uuid.uuid4())
    body = {"expected_layout_version": 0, "display_name": "日线+MACD", "layout_document": {"panes": ["k", "macd"]}}
    r1 = client.put(f"/v1/workspace-layouts/{lid}", json=body, headers={"Idempotency-Key": "k1"})
    assert r1.status_code == 200, r1.text
    assert r1.json()["data"]["layout_version"] == 1
    # same key same body: replay, no second write
    r2 = client.put(f"/v1/workspace-layouts/{lid}", json=body, headers={"Idempotency-Key": "k1"})
    assert r2.json()["data"] == r1.json()["data"]
    # same key different body: conflict
    r3 = client.put(f"/v1/workspace-layouts/{lid}", json={**body, "display_name": "x"}, headers={"Idempotency-Key": "k1"})
    assert r3.status_code == 409 and r3.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    # stale version: conflict
    r4 = client.put(f"/v1/workspace-layouts/{lid}", json=body, headers={"Idempotency-Key": "k2"})
    assert r4.status_code == 409 and r4.json()["error"]["code"] == "VERSION_CONFLICT"
    upd = {**body, "expected_layout_version": 1, "display_name": "周线"}
    r5 = client.put(f"/v1/workspace-layouts/{lid}", json=upd, headers={"Idempotency-Key": "k3"})
    assert r5.json()["data"]["layout_version"] == 2
    g = client.get(f"/v1/workspace-layouts/{lid}")
    assert g.status_code == 200 and g.json()["data"]["display_name"] == "周线"


@pytest.mark.pg
def test_unknown_layout_is_structured_404(client):
    r = client.get(f"/v1/workspace-layouts/{uuid.uuid4()}")
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.pg
def test_watchlist_and_annotation_roundtrip(client):
    wid = str(uuid.uuid4())
    w = client.put(f"/v1/watchlists/{wid}", headers={"Idempotency-Key": "w1"},
                   json={"expected_watchlist_version": 0, "display_name": "观察", "description": None, "members": []})
    assert w.status_code == 200, w.text
    assert [x["watchlist_id"] for x in client.get("/v1/watchlists").json()["data"]["items"]] == [wid]
    aid = str(uuid.uuid4())
    a = client.put(f"/v1/annotations/{aid}", headers={"Idempotency-Key": "a1"},
                   json={"expected_annotation_version": 0, "instrument_id": None, "watchlist_id": wid,
                         "annotation_kind": "note", "anchor_release_id": None, "anchor_snapshot_id": None,
                         "body": {"text": "复盘"}})
    assert a.status_code == 200, a.text
    assert client.get("/v1/annotations", params={"watchlist_id": wid}).json()["data"]["items"][0]["body"] == {"text": "复盘"}


@pytest.mark.pg
def test_bad_body_rejected_by_contract(client):
    r = client.put(f"/v1/workspace-layouts/{uuid.uuid4()}", json={"display_name": "x"}, headers={"Idempotency-Key": "z"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "CONTRACT_VIOLATION"
