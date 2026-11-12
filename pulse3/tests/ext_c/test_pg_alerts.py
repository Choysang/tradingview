"""PEX13 on real PostgreSQL: rule CAS, news-tier scan from claims, dedup/supersede, backfill, delivery attempts, listing."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from pulse.contracts import utc_z
from pulse.application import alert_service as AS
from pulse.application import portfolio_service as PS

from . import support as S

pytestmark = pytest.mark.pg
UTC = timezone.utc


def rule_body(tier="P0", dest=None, enabled=True, **kw):
    return {"scope": {"scope_kind": "news_tier", "scope_key": tier, "membership_snapshot_id": None},
            "condition": {"node": "compare", "clock": {"kind": "event_available"}, "operator": "eq", "left": {"kind": "constant", "value": "1", "unit": "count"},
                          "right": {"kind": "constant", "value": "1", "unit": "count"}},
            "policy_version": "alert-rule-v1", "annotation_id": None, "annotation_version": None, "destination_ids": [str(d) for d in dest], "enabled": enabled, **kw}


@pytest.fixture()
def world(conn, principal):
    held, watched, other = (S.seed_instrument(conn, c, n) for c, n in (("600801", "持仓股"), ("600802", "自选股"), ("600803", "路人股")))
    acct = None
    wl = uuid.uuid4()
    conn.execute("INSERT INTO ui.watchlist(watchlist_id,principal_id,watchlist_version,display_name,created_at,updated_at) VALUES (%s,%s,1,'w',now(),now())", (wl, principal))
    conn.execute("INSERT INTO ui.watchlist_member(watchlist_id,instrument_id,sort_position,added_at) VALUES (%s,%s,1,now())", (wl, watched))
    return {"held": held, "watched": watched, "other": other, "acct": acct}


def holding_world(conn, principal, held):
    """An account that really holds ``held`` (opening position through the unified ledger)."""
    acct = S.open_account(conn, positions={str(held): {"quantity": 1000, "cost": "10000"}})
    PS.grant(conn, principal, acct, "read", datetime.now(UTC) - timedelta(days=1))
    return acct


def test_rule_cas_owner_and_disable(conn, principal):
    dests = AS.ensure_destinations(conn, principal)
    inbox = next(d["destination_id"] for d in dests if d["channel"] == "in_app")
    rid = uuid.uuid4()
    v1 = AS.save_rule(conn, principal, {"rule_id": rid, "expected_rule_version": 0, "body": rule_body("P0", [inbox])})
    assert v1["version"] == 1
    with pytest.raises(AS.AlertError) as e:
        AS.save_rule(conn, principal, {"rule_id": rid, "expected_rule_version": 0, "body": rule_body("P0", [inbox])})
    assert e.value.code == "VERSION_CONFLICT"
    other = uuid.uuid4()
    with pytest.raises(AS.AlertError) as e:                      # another user's rule does not exist for you
        AS.save_rule(conn, other, {"rule_id": rid, "expected_rule_version": 1, "body": rule_body("P0", [])})
    assert e.value.status == 404
    with pytest.raises(AS.AlertError) as e:                      # a destination of someone else
        AS.save_rule(conn, principal, {"rule_id": uuid.uuid4(), "expected_rule_version": 0, "body": rule_body("P0", [AS.destination_id(other, "in_app")])})
    assert e.value.status == 403
    v2 = AS.save_rule(conn, principal, {"rule_id": rid, "expected_rule_version": 1, "body": rule_body("P0", [inbox], enabled=False)})
    assert v2["version"] == 2
    n = conn.execute("SELECT count(*) n FROM notification.alert_rule WHERE rule_id=%s", (rid,)).fetchone()["n"]
    assert n == 2                                               # disabling appended a version; nothing was deleted
    with pytest.raises(Exception):
        conn.execute("UPDATE notification.alert_rule SET updated_at=now() WHERE rule_id=%s", (rid,))


def test_scan_p0_p1_p2_dedup_supersede_and_backfill(conn, principal, world):
    now = datetime.now(UTC) + timedelta(days=11)
    acct = holding_world(conn, principal, world["held"])
    AS.init_default_rules(conn, principal, now - timedelta(days=1))
    avail = now - timedelta(minutes=5)
    p0 = S.add_claim(conn, headline="持仓股被证监会立案调查", category="company.ma_governance", polarity="-", instruments=[world["held"]], available_at=avail)
    S.add_claim(conn, headline="路人股获大单", category="company.contract_order", polarity="+", instruments=[world["other"]], available_at=avail)
    S.add_claim(conn, headline="自选股发布业绩预减", category="company.earnings_guidance", polarity="-", instruments=[world["watched"]], available_at=avail)
    S.add_claim(conn, headline="旧闻：持仓股曾被立案", category="company.ma_governance", polarity="-", instruments=[world["held"]], available_at=avail, published_at=avail - timedelta(days=3))
    s = AS.scan(conn, now, principals=[principal], since=now - timedelta(hours=1), until=now)
    assert s["intents"] == 3 and s["skipped_backfill"] == 1
    rows = conn.execute("SELECT tier, evidence_artifact FROM notification.alert_intent WHERE principal_id=%s ORDER BY tier", (principal,)).fetchall()
    assert [r["tier"] for r in rows] == ["P0", "P2", "P2"]
    ev = conn.execute("SELECT body FROM ops.ext_artifact WHERE ref=%s", (rows[0]["evidence_artifact"],)).fetchone()["body"]
    assert ev["headline"].startswith("持仓股") and ev["claim_status"] == "unverified" and ev["instruments"][0]["name"] == "持仓股"
    # re-scan of the same content adds nothing
    assert AS.scan(conn, now, principals=[principal], since=now - timedelta(hours=1), until=now)["intents"] == 0
    # a changed verification status is a new intent that supersedes the old one
    S.add_claim(conn, headline="持仓股被证监会立案调查", category="company.ma_governance", polarity="-", instruments=[world["held"]], available_at=avail + timedelta(minutes=1),
                published_at=avail, status="supported", claim_id=p0["claim_id"], revision=2)
    assert AS.scan(conn, now, principals=[principal], since=now - timedelta(hours=1), until=now)["intents"] == 1
    new = conn.execute("SELECT alert_id, supersedes FROM notification.alert_intent WHERE principal_id=%s AND tier='P0' ORDER BY raised_at DESC, alert_id", (principal,)).fetchall()
    assert len(new) == 2 and any(r["supersedes"] for r in new)
    # in-app attempts exist and are confirmed; intents + outbox events were written together
    att = conn.execute("SELECT status, count(*) n FROM notification.delivery_attempt GROUP BY 1").fetchall()
    assert {a["status"] for a in att} == {"confirmed"}
    assert conn.execute("SELECT count(*) n FROM ops.outbox WHERE event_type='alert.raised' AND aggregate_id=%s", (str(principal),)).fetchone()["n"] == 4


def test_p1_policy_negative_in_holding_industry(conn, principal, world):
    now = datetime.now(UTC) + timedelta(days=12)
    node = conn.execute("SELECT taxonomy_version, node_id FROM core.taxonomy_node WHERE taxonomy_id='legacy_boards_industry' AND display_name='房地产'").fetchone()
    if node is None:
        conn.execute("INSERT INTO core.taxonomy(taxonomy_id,taxonomy_version,taxonomy_kind,definition,available_at) VALUES ('legacy_boards_industry',1,'sector','{}',now()) ON CONFLICT DO NOTHING")
        conn.execute("INSERT INTO core.taxonomy_node(taxonomy_id,taxonomy_version,node_id,parent_node_id,display_name,definition) VALUES ('legacy_boards_industry',1,'new_fdc',NULL,'房地产','{}') ON CONFLICT DO NOTHING")
        node = {"taxonomy_version": 1, "node_id": "new_fdc"}
    conn.execute("INSERT INTO core.instrument_tag(assignment_id,instrument_id,taxonomy_id,taxonomy_version,node_id,effective_from,available_at,assertion_kind,evidence_ref)"
                 " VALUES (%s,%s,'legacy_boards_industry',%s,%s,'2020-01-01Z','2020-01-01Z','observed','t')", (uuid.uuid4(), world["held"], node["taxonomy_version"], node["node_id"]))
    holding_world(conn, principal, world["held"])
    AS.init_default_rules(conn, principal, now - timedelta(days=1))
    S.add_claim(conn, headline="多地收紧房地产限购政策", category="policy.industry", polarity="-", instruments=[], available_at=now - timedelta(minutes=3))
    s = AS.scan(conn, now, principals=[principal], since=now - timedelta(hours=1), until=now)
    assert s["intents"] == 1
    ev = conn.execute("SELECT i.tier, a.body FROM notification.alert_intent i JOIN ops.ext_artifact a ON a.ref=i.evidence_artifact WHERE i.principal_id=%s", (principal,)).fetchone()
    assert ev["tier"] == "P1" and ev["body"]["industries"] == ["房地产"] and ev["body"]["instruments"] == []


def test_disabled_rule_and_older_claims_do_not_alert(conn, principal, world):
    now = datetime.now(UTC) + timedelta(days=13)
    holding_world(conn, principal, world["held"])
    AS.init_default_rules(conn, principal, now - timedelta(days=1))
    for r in AS.current_rules(conn, principal):
        AS.save_rule(conn, principal, {"rule_id": r["rule_id"], "expected_rule_version": r["rule_version"], "body": {**r["body"], "enabled": False}}, now)
    S.add_claim(conn, headline="持仓股被立案", category="company.ma_governance", polarity="-", instruments=[world["held"]], available_at=now - timedelta(minutes=2))
    assert AS.scan(conn, now, principals=[principal], since=now - timedelta(hours=1), until=now)["intents"] == 0
    # re-enable: a claim that predates the new rule version still does not alert (history is not 'just happened')
    for r in AS.current_rules(conn, principal):
        AS.save_rule(conn, principal, {"rule_id": r["rule_id"], "expected_rule_version": r["rule_version"], "body": {**r["body"], "enabled": True}}, now)
    assert AS.scan(conn, now, principals=[principal], since=now - timedelta(hours=1), until=now)["intents"] == 0


def test_delivery_attempts_webhook_never_sent_unconfigured_and_retries_only_failed(conn, principal, world, monkeypatch):
    now = datetime.now(UTC) + timedelta(days=14)
    holding_world(conn, principal, world["held"])
    monkeypatch.delenv("PULSE_PUSH_WEBHOOK", raising=False)
    monkeypatch.delenv("PULSE_PUSH_WEBHOOK_FILE", raising=False)
    monkeypatch.delenv("PULSE_SECRETS_DIR", raising=False)
    assert [d["enabled"] for d in AS.ensure_destinations(conn, principal) if d["channel"] == "webhook"] == [False]
    monkeypatch.setenv("PULSE_PUSH_WEBHOOK", "https://hook.example.invalid/send?key=SECRETVALUE")
    monkeypatch.setenv("PULSE_PUSH_KIND", "wecom")
    dests = {d["channel"]: d["destination_id"] for d in AS.ensure_destinations(conn, principal)}
    assert conn.execute("SELECT endpoint_secret_ref FROM notification.destination WHERE destination_id=%s", (dests["webhook"],)).fetchone()["endpoint_secret_ref"] == "env:PULSE_PUSH_WEBHOOK"
    rid = uuid.uuid4()
    AS.save_rule(conn, principal, {"rule_id": rid, "expected_rule_version": 0, "body": rule_body("P0", [dests["in_app"], dests["webhook"]])}, now - timedelta(days=1))
    S.add_claim(conn, headline="持仓股被立案", category="company.ma_governance", polarity="-", instruments=[world["held"]], available_at=now - timedelta(minutes=2))
    assert AS.scan(conn, now, principals=[principal], since=now - timedelta(hours=1), until=now)["intents"] == 1
    assert {r["status"] for r in conn.execute("SELECT status FROM notification.delivery_attempt WHERE destination_id=%s", (dests["webhook"],))} == {"pending"}
    with pytest.raises(Exception):                                    # no transport: the real send is refused under pytest
        AS.deliver(conn, now)
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        return httpx.Response(200, json={"errcode": 93000})            # provider refuses -> failed -> a second attempt is queued
    conn.execute("UPDATE notification.delivery_attempt SET status='pending' WHERE status='sending'")
    s = AS.deliver(conn, now, transport=httpx.MockTransport(handler))
    assert s["failed"] == 1 and s["retried"] == 1 and len(calls) == 1
    n = conn.execute("SELECT attempt_number, status FROM notification.delivery_attempt WHERE destination_id=%s ORDER BY attempt_number", (dests["webhook"],)).fetchall()
    assert [(a["attempt_number"], a["status"]) for a in n] == [(1, "failed"), (2, "pending")]
    s = AS.deliver(conn, now, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"errcode": 0})))
    assert s["accepted"] == 1 and s["retried"] == 0
    def timeout(r):
        raise httpx.ReadTimeout("x", request=r)
    conn.execute("INSERT INTO notification.delivery_attempt(attempt_id,alert_id,destination_id,attempt_number,status,started_at) SELECT gen_random_uuid(),alert_id,%s,9,'pending',now() FROM notification.alert_intent WHERE principal_id=%s", (dests["webhook"], principal))
    s = AS.deliver(conn, now, transport=httpx.MockTransport(timeout))
    assert s["unknown"] == 1 and s["retried"] == 0                    # unknown is never auto-resent
    s = AS.deliver(conn, now + timedelta(days=2), transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    assert s["sent"] == 0
    # the secret never reached the database
    assert conn.execute("SELECT count(*) n FROM notification.destination WHERE endpoint_secret_ref ILIKE '%%SECRETVALUE%%'").fetchone()["n"] == 0


def test_api_save_rule_idempotent_and_list_alerts(make_api, conn, principal, world):
    api = make_api(principal)
    dests = AS.ensure_destinations(conn, principal)
    inbox = next(str(d["destination_id"]) for d in dests if d["channel"] == "in_app")
    rid = str(uuid.uuid4())
    body = {"rule_id": rid, "expected_rule_version": 0, "body": rule_body("P0", [inbox])}
    r1 = api.post("save-alert-rule", body, key="k1")
    assert r1.status_code == 200, r1.text
    assert r1.json()["data"]["rule"]["version"] == 1
    assert api.post("save-alert-rule", body, key="k1").json()["data"] == r1.json()["data"]          # same key: the original answer
    assert api.post("save-alert-rule", {**body, "expected_rule_version": 5}, key="k1").status_code == 409   # same key, other payload
    assert api.post("save-alert-rule", body, key="k2").json()["error"]["code"] == "VERSION_CONFLICT"      # stale CAS
    assert api.post("save-alert-rule", {**body, "body": {**body["body"], "scope": {"scope_kind": "news_tier", "scope_key": "P9", "membership_snapshot_id": None}}}, key="k3").status_code == 400
    holding_world(conn, principal, world["held"])
    now = datetime.now(UTC) + timedelta(days=15)
    c = S.add_claim(conn, headline="持仓股被立案", category="company.ma_governance", polarity="-", instruments=[world["held"]], available_at=now + timedelta(seconds=1))
    AS.scan(conn, now + timedelta(seconds=5), principals=[principal], since=now - timedelta(hours=1), until=now + timedelta(minutes=1))
    win = {"start": utc_z(now - timedelta(hours=1)), "end": utc_z(now + timedelta(hours=1))}
    r = api.get("list-user-alerts", window=win, page={"limit": 10})
    assert r.status_code == 200, r.text
    alerts = r.json()["data"]["alerts"]
    assert len(alerts) == 1 and alerts[0]["delivery_states"] == ["in_app:confirmed"]
    art = api.c.get("/v1/x/c/artifact", params={"ref": alerts[0]["evidence_artifact"]["ref"]}, cookies=api._cookies()).json()["data"]
    assert art["body"]["tier"] == "P0" and art["body"]["headline"] == "持仓股被立案"
    # another user sees nothing and cannot read the artifact
    stranger = uuid.uuid4()
    api2 = make_api(principal, stranger).as_(stranger)
    assert api2.get("list-user-alerts", window=win, page={"limit": 10}).json()["data"]["alerts"] == []
    assert api2.c.get("/v1/x/c/artifact", params={"ref": alerts[0]["evidence_artifact"]["ref"]}, cookies=api2._cookies()).status_code == 404
    assert api.c.get("/v1/x/list-user-alerts", params={"window": "{}"}).status_code == 403            # no session
