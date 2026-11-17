"""Legacy #135 daily report push: built from the current main release only, delivered once per (principal, release)."""
from __future__ import annotations

import uuid

import httpx
import pytest

from pulse.application import alert_service as A
from pulse.application import bf_report_service as R

from . import support as S

pytestmark = pytest.mark.pg
SECRET = "https://hook.invalid/send?key=SECRET-REPORT-KEY"


def test_no_release_means_no_report(conn):
    assert R.push(conn)["status"] == "skipped"


def test_report_repeats_the_release_and_is_sent_once(conn, monkeypatch):
    monkeypatch.setenv("PULSE_PUSH_WEBHOOK", SECRET)
    monkeypatch.setenv("PULSE_PUSH_KIND", "wecom")
    a, b = S.seed_instrument(conn, "600001", "甲股份"), S.seed_instrument(conn, "600002", "乙股份")
    rid = S.publish(conn, slot="main", ranking=[b, a])
    me = uuid.uuid4()
    A.ensure_destinations(conn, me)
    calls: list[httpx.Request] = []
    transport = httpx.MockTransport(lambda req: (calls.append(req), httpx.Response(200, json={"errcode": 0}))[1])

    out = R.push(conn, transport=transport, only=[me])
    assert out["status"] == "done" and out["release_id"] == str(rid) and out["sent"] == 2          # in-app + webhook
    text = calls[0].content.decode("utf-8")
    assert "乙股份(600002-" in text and text.index("乙股份") < text.index("甲股份") and "不构成投资建议" in text
    assert SECRET not in str(conn.execute("SELECT body FROM ops.ext_artifact").fetchall())
    again = R.push(conn, transport=transport, only=[me])
    assert again["sent"] == 0 and again["already_sent"] == 1 and len(calls) == 1                  # same release: nothing sent again
    rows = conn.execute("SELECT status FROM notification.delivery_attempt d JOIN notification.alert_intent i USING (alert_id)"
                        " WHERE i.principal_id=%s ORDER BY status", (me,)).fetchall()
    assert [r["status"] for r in rows] == ["accepted", "confirmed"]


def test_a_known_principal_without_destinations_gets_the_report_in_app(conn, monkeypatch):
    """The single-user site's principal never opened the alert page: the report still reaches its in-app inbox."""
    monkeypatch.delenv("PULSE_PUSH_WEBHOOK", raising=False)
    a = S.seed_instrument(conn, "600003", "丙股份")
    S.publish(conn, slot="main", ranking=[a])
    me = uuid.uuid4()
    out = R.push(conn, only=[me], ensure_for=[me])
    assert out["status"] == "done" and out["sent"] == 1
    rows = conn.execute("SELECT d.status, x.channel FROM notification.delivery_attempt d JOIN notification.alert_intent i USING (alert_id)"
                        " JOIN notification.destination x ON x.destination_id=d.destination_id WHERE i.principal_id=%s", (me,)).fetchall()
    assert [(r["status"], r["channel"]) for r in rows] == [("confirmed", "in_app")]
