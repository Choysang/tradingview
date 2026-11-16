"""Agent access (#63/#64) and test push (#97) on a real PostgreSQL."""
from __future__ import annotations

import json
import re
import uuid

import httpx
import pytest

from pulse import contracts

pytestmark = pytest.mark.pg
SECRET = "https://hooks.example.test/robot/send?key=SECRET-TOKEN-123"


# ---- manifest + llms.txt ------------------------------------------------------------------------------------------------------------
def test_manifest_lists_every_bound_operation_passes_its_contract_and_needs_no_session(fresh, api_on):
    conn, dsn = fresh
    api = api_on(dsn, uuid.uuid4())
    r = api.c.get("/v1/x/bf/ops/manifest")                                     # no cookie: the manifest is public
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    contracts.check("ManifestData", data, contracts.backfill_id("ops"))
    bound = conn.execute("SELECT operation_id, http_method, path_template FROM core.api_binding").fetchall()
    assert len(bound) >= 72                                                    # 31 core + 41 extension
    listed = {(o["operation_id"], o["method"], o["path"]) for o in data["operations"]}
    assert {(b["operation_id"], b["http_method"], b["path_template"]) for b in bound} <= listed
    assert data["generated_from"]["core_api_binding_rows"] == len(bound) and data["operation_count"] == len(data["operations"])
    by = {o["operation_id"]: o for o in data["operations"]}
    assert by["getCurrentRelease"]["source"] == "core" and by["getCurrentRelease"]["auth"] == "public"
    assert by["saveAlertRule"]["source"] == "ext" and by["saveAlertRule"]["auth"] == "session" and by["saveAlertRule"]["idempotency_key_required"] is True
    assert by["queryMarketToday"]["summary"]
    # backfill routes come from the mounted route table, this lane's own included
    assert {"getAgentManifest", "sendTestPush", "getForwardMonitor", "getLlmsTxt"} <= set(by)
    assert by["sendTestPush"]["auth"] == "session" and by["getAgentManifest"]["auth"] == "public"
    assert all(o["path"].startswith("/v1/x/bf/") or o["path"] == "/llms.txt" for o in data["operations"] if o["source"] == "backfill")


def test_llms_txt_is_plain_text_generated_from_the_same_manifest(fresh, api_on):
    conn, dsn = fresh
    api = api_on(dsn, uuid.uuid4())
    manifest = api.c.get("/v1/x/bf/ops/manifest").json()["data"]
    r = api.c.get("/llms.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    ops = [ln for ln in r.text.splitlines() if re.match(r"- (GET|POST|PUT|PATCH|DELETE) /", ln)]
    assert len(ops) == manifest["operation_count"]
    for o in manifest["operations"]:
        assert f"{o['method']} {o['path']} - {o['operation_id']}" in r.text
    assert "/v1/x/bf/ops/manifest" in r.text and "Idempotency-Key" in r.text and "pulse_session" in r.text


# ---- test push ------------------------------------------------------------------------------------------------------------------------
def destinations(api) -> dict:
    d = api.get("/v1/x/c/alert-rules").json()["data"]["destinations"]
    return {x["channel"]: x for x in d}


def test_push_to_the_in_app_inbox_is_recorded_once_per_key(fresh, api_on):
    conn, dsn = fresh
    me = uuid.uuid4()
    api = api_on(dsn, me)
    inbox = destinations(api)["in_app"]
    body = {"destination_id": inbox["destination_id"]}
    r1 = api.post("/v1/x/bf/ops/test-push", body, key="k1")
    assert r1.status_code == 200, r1.text
    d = r1.json()["data"]
    assert d["channel"] == "in_app" and d["status"] == "confirmed" and d["is_test"] is True and d["error_code"] is None
    assert api.post("/v1/x/bf/ops/test-push", body, key="k1").json()["data"] == d                  # replay: same answer, nothing new
    assert conn.execute("SELECT count(*) n FROM notification.alert_intent WHERE principal_id=%s", (me,)).fetchone()["n"] == 1
    assert conn.execute("SELECT count(*) n FROM notification.delivery_attempt WHERE alert_id=%s", (d["alert_id"],)).fetchone()["n"] == 1
    assert api.post("/v1/x/bf/ops/test-push", {"destination_id": str(uuid.uuid4())}, key="k1").status_code == 409   # same key, different request
    r2 = api.post("/v1/x/bf/ops/test-push", body, key="k2")                                         # a new key is a new test
    assert r2.json()["data"]["alert_id"] != d["alert_id"]
    listed = api.get("/v1/x/list-user-alerts", window={"start": "2020-01-01T00:00:00Z", "end": "2100-01-01T00:00:00Z"}, page={"limit": 10})
    assert listed.status_code == 200 and len(listed.json()["data"]["alerts"]) == 2                  # recorded like other deliveries
    ev = api.get("/v1/x/c/artifact", ref=listed.json()["data"]["alerts"][0]["evidence_artifact"]["ref"]).json()["data"]["body"]
    assert ev["is_test"] is True and ev["tier"] == "RULE"


def test_webhook_test_push_uses_the_injected_transport_never_leaks_the_url_and_is_idempotent(fresh, api_on, monkeypatch):
    conn, dsn = fresh
    me = uuid.uuid4()
    monkeypatch.setenv("PULSE_PUSH_WEBHOOK", SECRET)
    monkeypatch.setenv("PULSE_PUSH_KIND", "wecom")
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})
    import pulse.api.routes_bf_ops as R
    monkeypatch.setattr(R, "PUSH_TRANSPORT", httpx.MockTransport(handler))
    api = api_on(dsn, me)
    hook = destinations(api)["webhook"]
    assert hook["enabled"] is True
    body = {"destination_id": hook["destination_id"]}
    r = api.post("/v1/x/bf/ops/test-push", body, key="w1")
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["channel"] == "webhook" and d["status"] == "accepted" and len(calls) == 1
    assert str(calls[0].url) == SECRET and "测试" in calls[0].content.decode("utf-8")                # the fake transport saw it, nobody else
    assert api.post("/v1/x/bf/ops/test-push", body, key="w1").json()["data"] == d and len(calls) == 1   # replay does not send again
    att = conn.execute("SELECT status FROM notification.delivery_attempt WHERE alert_id=%s", (d["alert_id"],)).fetchall()
    assert [a["status"] for a in att] == ["accepted"]
    dump = json.dumps([r.text, conn.execute("SELECT * FROM notification.destination").fetchall(), conn.execute("SELECT body FROM ops.ext_artifact").fetchall(),
                       conn.execute("SELECT response_document FROM ops.command_receipt").fetchall(), conn.execute("SELECT * FROM notification.delivery_attempt").fetchall()],
                      default=str, ensure_ascii=False)
    assert "SECRET-TOKEN-123" not in dump and "hooks.example.test" not in dump


def test_provider_failure_is_recorded_as_failed_with_a_code(fresh, api_on, monkeypatch):
    conn, dsn = fresh
    monkeypatch.setenv("PULSE_PUSH_WEBHOOK", SECRET)
    import pulse.api.routes_bf_ops as R
    monkeypatch.setattr(R, "PUSH_TRANSPORT", httpx.MockTransport(lambda req: httpx.Response(500)))
    api = api_on(dsn, uuid.uuid4())
    d = api.post("/v1/x/bf/ops/test-push", {"destination_id": destinations(api)["webhook"]["destination_id"]}).json()["data"]
    assert d["status"] == "failed" and d["error_code"] == "HTTP_500"


def test_real_sends_stay_refused_under_pytest_and_leave_no_record(fresh, api_on, monkeypatch):
    conn, dsn = fresh
    me = uuid.uuid4()
    monkeypatch.setenv("PULSE_PUSH_WEBHOOK", SECRET)
    api = api_on(dsn, me)                                                       # PUSH_TRANSPORT stays None
    r = api.post("/v1/x/bf/ops/test-push", {"destination_id": destinations(api)["webhook"]["destination_id"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "SEND_DISABLED"
    assert conn.execute("SELECT count(*) n FROM notification.alert_intent WHERE principal_id=%s", (me,)).fetchone()["n"] == 0
    assert "SECRET-TOKEN-123" not in r.text


def test_unconfigured_foreign_and_malformed_destinations_are_refused(fresh, api_on):
    conn, dsn = fresh
    me, other = uuid.uuid4(), uuid.uuid4()
    api = api_on(dsn, me)
    dests = destinations(api)
    hook = dests["webhook"]                                                     # no URL configured: disabled
    assert hook["enabled"] is False
    r = api.post("/v1/x/bf/ops/test-push", {"destination_id": hook["destination_id"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "DESTINATION_NOT_CONFIGURED"
    assert api.post("/v1/x/bf/ops/test-push", {"destination_id": str(uuid.uuid4())}).status_code == 404       # unknown
    assert api.post("/v1/x/bf/ops/test-push", {"destination_id": "nope"}).status_code == 400
    assert api.post("/v1/x/bf/ops/test-push", {"destination_id": hook["destination_id"], "url": "https://x"}).status_code == 400
    assert api.c.post("/v1/x/bf/ops/test-push", json={"destination_id": hook["destination_id"]}, cookies=api.cookies()).status_code in (400, 422)   # no Idempotency-Key
    assert api.c.post("/v1/x/bf/ops/test-push", json={"destination_id": hook["destination_id"]}, headers={"Idempotency-Key": "z"}).status_code == 403   # no session
    stranger = api_on(dsn, other)                                               # someone else's destination id reads as unknown
    assert stranger.post("/v1/x/bf/ops/test-push", {"destination_id": dests["in_app"]["destination_id"]}).status_code == 404
