"""Daily report push after the close (legacy triage row #135, docs/LEGACY_TRIAGE.md).

Why: the old site pushed an after-close summary. Here it is built only from the current published release (slot main):
session date, decision cutoff, the top names of each market ranking horizon and the degraded components, with the
research disclaimer. It is delivered through the same alert intent + delivery attempt records as alerts, once per
(principal, release): a rerun for the same release sends nothing again.
Invariants: no figure is computed here (the report repeats the release); a release that is not READY is never reported;
the webhook URL is never read into a result or a record; sends are refused under pytest unless a fake transport is given.
Not covered: intraday content (the report is after-close only); per-user filtering of the content.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from uuid import UUID

import psycopg

from pulse import contracts
from pulse.adapters.notify import webhook
from pulse.adapters.postgres.release import PgAnalysisRelease
from pulse.application import alert_service as A
from pulse.application import ops_service
from pulse.domain.alerts import tiers as T

REPORT_POLICY = "daily-report-v1"
TOP_N = 5
DISCLAIMER = "研究用途，不构成投资建议。排名与计划均来自当日固定发布。"


def _tops(conn: psycopg.Connection, release_id: UUID) -> list[dict]:
    out = []
    for run in conn.execute("SELECT ranking_id, horizon_id FROM decision.ranking_run WHERE release_id=%s AND ranking_scope='market'"
                            " ORDER BY horizon_id", (release_id,)).fetchall():
        rows = conn.execute(
            "SELECT e.alpha_rank, a.source_symbol code, (SELECT s.display_name FROM core.instrument_state s WHERE s.instrument_id=e.instrument_id"
            " ORDER BY s.effective_from DESC, s.state_version DESC LIMIT 1) name FROM decision.ranking_entry e"
            " LEFT JOIN LATERAL (SELECT source_symbol FROM core.instrument_alias x WHERE x.instrument_id=e.instrument_id"
            " ORDER BY (x.source_id='legacy_warehouse') DESC, x.effective_from DESC LIMIT 1) a ON true"
            " WHERE e.ranking_id=%s ORDER BY e.alpha_rank LIMIT %s", (run["ranking_id"], TOP_N)).fetchall()
        out.append({"horizon_id": run["horizon_id"], "top": [{"rank": r["alpha_rank"], "code": r["code"], "name": r["name"]} for r in rows]})
    return out


def compose(conn: psycopg.Connection) -> dict | None:
    """The report body for the current main release, or None when nothing is published."""
    cur = PgAnalysisRelease(conn).current("main")
    if cur is None:
        return None
    view = PgAnalysisRelease(conn).get_release(cur.release_id)
    if view.release_state != "READY":
        return None
    tops = _tops(conn, cur.release_id)
    lines = [f"{t['horizon_id']}: " + "、".join(f"{x['name'] or x['code']}({x['code']})" for x in t["top"]) for t in tops]
    return {"release_id": str(cur.release_id), "decision_as_of": cur.decision_as_of, "horizons": tops,
            "degraded": list(cur.degraded_components), "headline": "盘后日报：" + cur.decision_as_of.date().isoformat(),
            "text": "\n".join(lines + ([f"降级组件：{', '.join(cur.degraded_components)}"] if cur.degraded_components else []) + [DISCLAIMER])}


def push(conn: psycopg.Connection, *, now: datetime | None = None, transport=None, only: list[UUID] | None = None) -> dict:
    """Send the report for the current release to every principal's enabled destinations, once per release.

    `only` limits the run to the given principals (used by tests that share a database).
    """
    now = now or ops_service.now_utc()
    rep = compose(conn)
    if rep is None:
        return {"status": "skipped", "reason": "no READY release in slot main", "sent": 0}
    principals = [r["principal_id"] for r in conn.execute("SELECT DISTINCT principal_id FROM notification.destination").fetchall()]
    if only is not None:
        principals = [p for p in principals if p in set(only)]
    sent, skipped = 0, 0
    for p in principals:
        alert_id = uuid.uuid5(A.DEST_NS, f"{p}|daily-report|{rep['release_id']}")
        if conn.execute("SELECT 1 FROM notification.alert_intent WHERE alert_id=%s", (alert_id,)).fetchone():
            skipped += 1
            continue
        with conn.transaction():
            sent += _deliver_one(conn, p, alert_id, rep, now, transport)
    return {"status": "done", "release_id": rep["release_id"], "principals": len(principals), "sent": sent, "already_sent": skipped}


def _deliver_one(conn: psycopg.Connection, principal: UUID, alert_id: UUID, rep: dict, now: datetime, transport) -> int:
    body = {"tier": "RULE", "headline": rep["headline"], "category": "daily_report", "direction": "unclear", "reason": rep["text"],
            "policy_version": REPORT_POLICY, "rule_id": str(uuid.uuid5(A.DEST_NS, f"{principal}|{REPORT_POLICY}")), "instruments": [],
            "industries": [], "held_in_industry": [], "claim_status": "固定发布摘要", "assertion_type": "report",
            "verification_note": "内容复述当日发布，不含新计算", "source_claim_revision_ids": [], "claim_id": None, "available_at": now,
            "event_time": rep["decision_as_of"], "trigger": "after_close_report", "already_reacted": None, "is_test": False,
            "release_id": rep["release_id"]}
    art = ops_service.put_artifact(conn, kind="alert-evidence", contract_id=A.EVIDENCE_CONTRACT, schema_version=A.EVIDENCE_VERSION,
                                   body=body, owner=principal)
    conn.execute(
        "INSERT INTO notification.alert_intent(alert_id, principal_id, rule_id, rule_version, tier, dedup_key, based_on_release_id, evidence_artifact,"
        " raised_at, expires_at, supersedes, source_claim_revision_id) VALUES (%s,%s,%s,1,'RULE',%s,%s,%s,%s,%s,NULL,NULL)",
        (alert_id, principal, uuid.UUID(body["rule_id"]), f"daily-report:{rep['release_id']}", UUID(rep["release_id"]), art["ref"], now,
         now + timedelta(seconds=T.ALERT_LIFETIME_SECONDS)))
    n = 0
    for d in A.destinations(conn, principal):
        if not d["enabled"]:
            continue
        if d["channel"] == "in_app":
            status, provider_id, error = "confirmed", None, None
        else:
            cfg = webhook.load_config()
            if cfg is None:
                continue
            try:
                res = webhook.send(cfg, contracts.to_wire(body), transport=transport)
            except webhook.SendDisabled:
                continue
            status, provider_id, error = res.status, res.provider_message_id, res.error_code
        conn.execute("INSERT INTO notification.delivery_attempt(attempt_id, alert_id, destination_id, attempt_number, status, provider_message_id,"
                     " started_at, completed_at, error_code) VALUES (%s,%s,%s,1,%s,%s,%s,%s,%s)",
                     (uuid.uuid5(A.DEST_NS, f"{alert_id}|{d['destination_id']}|1"), alert_id, d["destination_id"], status, provider_id, now, now, error))
        n += 1
    return n
