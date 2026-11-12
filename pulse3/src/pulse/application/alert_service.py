"""Alerts: user rules, news-tier intents, delivery attempts (PEX13; D18-D21). Old site E075 P0/P1/P2 on the I3 claim pipeline.

Why: an alert is a business INTENT (``notification.alert_intent``: why, on which evidence, until when) that is separate from
any SEND ATTEMPT (``notification.delivery_attempt``): a webhook timeout must not change what the user should know.
Invariants:
* a rule is an append-only version with a CAS head (``saveAlertRule``: expected_rule_version 0 creates, otherwise must equal the
  current version; the owner is checked together with the version; disabling stops NEW alerts and keeps every old row);
* an intent is unique per ``(principal, dedup_key)``; the key holds tier, claim id, target and the claim CONTENT version, so a
  re-scan of the same claim is a no-op while a changed verification status is a new intent that ``supersedes`` the old one;
* history re-collected late never alerts (``domain.alerts.tiers.is_backfill``) and a claim older than the rule version never
  alerts for that version;
* the in-app inbox entry is the intent itself (attempt ``in_app:confirmed``); a webhook attempt exists only when a webhook URL is
  configured (``adapters.notify.webhook``), ``unknown`` (timeout) is never auto-resent, ``failed`` is retried up to three attempts,
  an attempt past ``expires_at`` becomes ``expired``;
* the intent, its attempts and the ``alert.raised`` outbox event are written in one transaction; evidence artifacts are owned
  by the user and immutable.
Not covered: price-line (``Condition``) evaluation - a rule's condition is stored and pinned (annotation id + version) but only the
scopes ``news_tier`` / ``instrument`` / ``sector`` are evaluated; LLM fallback tiers; intraday "already reacted" magnitude.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from pulse import contracts
from pulse.adapters.notify import webhook
from pulse.adapters.postgres._canon import sha256_of
from pulse.adapters.postgres.outbox import PgOutbox
from pulse.application import ops_service
from pulse.application.ports.delivery import OutboxEvent
from pulse.domain.alerts import tiers as T

SCOPE_KINDS = ("news_tier", "instrument", "sector")
EVIDENCE_CONTRACT, EVIDENCE_VERSION = "pulse.ext_c.alert_evidence", "1"
DEST_NS = uuid.UUID("7d1e5a90-3c42-4f6b-a1d8-0b9c2e4f6a31")
MAX_ATTEMPTS = 3
TAXONOMY = "legacy_boards_industry"
NEWS_TOKEN = "pulse.intel.news_claim@1"
SCAN_CURSOR = "news-tier-scan-v1"
SCAN_LIMIT = 5000


class AlertError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# ---- destinations --------------------------------------------------------------------------------------------------------------
def destination_id(principal: UUID, channel: str) -> UUID:
    return uuid.uuid5(DEST_NS, f"{principal}|{channel}")


def ensure_destinations(conn: psycopg.Connection, principal: UUID, now: datetime | None = None) -> list[dict]:
    """The user's two destinations: in_app (always on) and webhook (enabled only while a URL is configured). Versioned on change."""
    now = now or ops_service.now_utc()
    cfg = webhook.load_config()
    wanted = [("in_app", "in_app:inbox", True, "站内收件箱"),
              ("webhook", webhook.SECRET_REFS[0], cfg is not None, f"{webhook.KIND_NAMES[cfg.kind]}推送" if cfg else "Webhook 推送（未配置）")]
    for channel, ref, enabled, label in wanted:
        did = destination_id(principal, channel)
        cur = conn.execute("SELECT * FROM notification.destination WHERE destination_id=%s ORDER BY destination_version DESC LIMIT 1", (did,)).fetchone()
        if cur is None or (cur["enabled"], cur["label"]) != (enabled, label):
            conn.execute("INSERT INTO notification.destination(destination_id, principal_id, destination_version, channel, endpoint_secret_ref,"
                         " enabled, label, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                         (did, principal, 1 if cur is None else cur["destination_version"] + 1, channel, ref, enabled, label, now))
    return destinations(conn, principal)


def destinations(conn: psycopg.Connection, principal: UUID) -> list[dict]:
    return conn.execute("SELECT DISTINCT ON (destination_id) * FROM notification.destination WHERE principal_id=%s"
                        " ORDER BY destination_id, destination_version DESC", (principal,)).fetchall()


# ---- rules (saveAlertRule) -------------------------------------------------------------------------------------------------------
def _check_body(conn: psycopg.Connection, principal: UUID, body: dict) -> None:
    scope = body["scope"]
    kind, key = scope["scope_kind"], scope["scope_key"]
    if kind not in SCOPE_KINDS:
        raise AlertError(400, "INVALID_CONTRACT", f"不支持的告警范围 {kind}（支持 {', '.join(SCOPE_KINDS)}）")
    if kind == "news_tier" and key not in ("P0", "P1", "P2"):
        raise AlertError(400, "INVALID_CONTRACT", "news_tier 范围只能是 P0/P1/P2")
    if kind == "instrument":
        try:
            ok = conn.execute("SELECT 1 FROM core.instrument WHERE instrument_id=%s", (UUID(key),)).fetchone()
        except ValueError:
            ok = None
        if not ok:
            raise AlertError(400, "INVALID_CONTRACT", "证券不存在")
    if kind == "sector" and not conn.execute("SELECT 1 FROM core.taxonomy_node WHERE taxonomy_id=%s AND node_id=%s", (TAXONOMY, key)).fetchone():
        raise AlertError(400, "INVALID_CONTRACT", "板块节点不存在")
    mine = {d["destination_id"] for d in destinations(conn, principal)}
    foreign = [d for d in body["destination_ids"] if UUID(str(d)) not in mine]
    if foreign:
        raise AlertError(403, "FORBIDDEN", "目的地不属于当前用户")
    if (body.get("annotation_id") is None) != (body.get("annotation_version") is None):
        raise AlertError(400, "INVALID_CONTRACT", "annotation_id 与 annotation_version 必须成对")
    if body.get("annotation_id") is not None:
        row = conn.execute("SELECT annotation_version FROM ui.annotation WHERE annotation_id=%s AND principal_id=%s",
                           (body["annotation_id"], principal)).fetchone()
        rev = conn.execute("SELECT 1 FROM ui.workspace_revision WHERE principal_id=%s AND object_kind='annotation' AND object_id=%s AND object_version=%s",
                           (principal, body["annotation_id"], body["annotation_version"])).fetchone()
        if row is None and rev is None:
            raise AlertError(404, "NOT_FOUND", "画线不存在")
        if not rev and row["annotation_version"] != body["annotation_version"]:
            raise AlertError(409, "VERSION_CONFLICT", "画线版本已变化，需要重新编译规则")


def save_rule(conn: psycopg.Connection, principal: UUID, req: dict, now: datetime | None = None) -> dict:
    now = now or ops_service.now_utc()
    ensure_destinations(conn, principal, now)
    body = contracts.to_wire(req["body"])
    _check_body(conn, principal, body)
    rule_id, expected = UUID(str(req["rule_id"])), req["expected_rule_version"]
    head = conn.execute("SELECT * FROM notification.alert_rule_head WHERE rule_id=%s FOR UPDATE", (rule_id,)).fetchone()
    if head is not None and head["principal_id"] != principal:
        raise AlertError(404, "NOT_FOUND", "规则不存在")
    if head is None:
        if expected != 0:
            raise AlertError(409, "VERSION_CONFLICT", "规则不存在，创建时 expected_rule_version 必须为 0")
        version = 1
    else:
        if head["current_version"] != expected:
            raise AlertError(409, "VERSION_CONFLICT", f"规则当前版本是 {head['current_version']}")
        version = expected + 1
    digest = sha256_of(body)
    conn.execute("INSERT INTO notification.alert_rule(rule_id, principal_id, rule_version, body, content_hash, updated_at) VALUES (%s,%s,%s,%s,%s,%s)",
                 (rule_id, principal, version, Jsonb(body), digest, now))
    if head is None:
        conn.execute("INSERT INTO notification.alert_rule_head(rule_id, principal_id, current_version) VALUES (%s,%s,%s)", (rule_id, principal, version))
    else:
        cur = conn.execute("UPDATE notification.alert_rule_head SET current_version=%s WHERE rule_id=%s AND current_version=%s", (version, rule_id, expected))
        if cur.rowcount != 1:
            raise AlertError(409, "VERSION_CONFLICT", "规则被并发修改")
    return {"object_id": rule_id, "version": version, "content_hash": digest}


def init_default_rules(conn: psycopg.Connection, principal: UUID, now: datetime | None = None) -> list[dict]:
    """P0/P1/P2 rules on the in-app destination, enabled (CLI convenience; idempotent per tier)."""
    ensure_destinations(conn, principal, now)
    out = []
    for tier in ("P0", "P1", "P2"):
        rid = uuid.uuid5(DEST_NS, f"{principal}|default-rule|{tier}")
        if conn.execute("SELECT 1 FROM notification.alert_rule_head WHERE rule_id=%s", (rid,)).fetchone():
            continue
        body = {"scope": {"scope_kind": "news_tier", "scope_key": tier, "membership_snapshot_id": None},
                "condition": {"node": "compare", "clock": {"kind": "event_available"}, "operator": "eq",
                              "left": {"kind": "constant", "value": "1", "unit": "count"},
                              "right": {"kind": "constant", "value": "1", "unit": "count"}},
                "policy_version": "alert-rule-v1", "annotation_id": None, "annotation_version": None,
                "destination_ids": [destination_id(principal, "in_app")], "enabled": True}
        out.append(save_rule(conn, principal, {"rule_id": rid, "expected_rule_version": 0, "body": body}, now))
    return out


def current_rules(conn: psycopg.Connection, principal: UUID) -> list[dict]:
    return conn.execute(
        "SELECT r.rule_id, r.rule_version, r.body, r.updated_at FROM notification.alert_rule_head h JOIN notification.alert_rule r"
        " ON r.rule_id=h.rule_id AND r.rule_version=h.current_version WHERE h.principal_id=%s ORDER BY r.updated_at, r.rule_id", (principal,)).fetchall()


def rules_wire(conn: psycopg.Connection, principal: UUID) -> dict:
    dests = ensure_destinations(conn, principal)
    cfg = webhook.status()
    return {"rules": [{"rule_id": r["rule_id"], "rule_version": r["rule_version"], "updated_at": r["updated_at"], "body": r["body"]} for r in current_rules(conn, principal)],
            "destinations": [{"destination_id": d["destination_id"], "channel": d["channel"], "enabled": d["enabled"],
                              "endpoint": "configured" if d["enabled"] else "not_configured", "label": d["label"]} for d in dests],
            "webhook": cfg}


# ---- listing (listUserAlerts) ---------------------------------------------------------------------------------------------------
def list_alerts(conn: psycopg.Connection, principal: UUID, window: dict, limit: int, after: tuple | None, now: datetime | None = None) -> tuple[list[dict], tuple | None]:
    now = now or ops_service.now_utc()
    sql = ("SELECT * FROM notification.alert_intent WHERE principal_id=%s AND raised_at >= %s AND raised_at < %s")
    args: list = [principal, window["start"], window["end"]]
    if after is not None:
        sql += " AND (raised_at, alert_id) < (%s, %s)"
        args += list(after)
    rows = conn.execute(sql + " ORDER BY raised_at DESC, alert_id DESC LIMIT %s", args + [limit + 1]).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    out = []
    for r in rows:
        att = conn.execute(
            "SELECT DISTINCT ON (a.destination_id) a.destination_id, a.status, d.channel FROM notification.delivery_attempt a"
            " JOIN LATERAL (SELECT channel FROM notification.destination x WHERE x.destination_id=a.destination_id ORDER BY destination_version DESC LIMIT 1) d ON true"
            " WHERE a.alert_id=%s ORDER BY a.destination_id, a.attempt_number DESC", (r["alert_id"],)).fetchall()
        states = []
        for a in sorted(att, key=lambda x: x["channel"]):
            st = a["status"]
            if st in ("pending", "sending") and now > r["expires_at"]:
                st = "expired"
            states.append(f"{a['channel']}:{st}")
        ev = conn.execute("SELECT content_hash, contract_id, schema_version FROM ops.ext_artifact WHERE ref=%s", (r["evidence_artifact"],)).fetchone()
        outcome = None
        if r["outcome_artifact"]:
            o = conn.execute("SELECT content_hash, contract_id, schema_version FROM ops.ext_artifact WHERE ref=%s", (r["outcome_artifact"],)).fetchone()
            outcome = {"ref": r["outcome_artifact"], **o}
        out.append({"alert_id": r["alert_id"], "rule_id": r["rule_id"], "rule_version": r["rule_version"], "raised_at": r["raised_at"],
                    "evidence_artifact": {"ref": r["evidence_artifact"], **ev}, "delivery_states": states, "outcome_artifact": outcome})
    last = (rows[-1]["raised_at"], rows[-1]["alert_id"]) if more and rows else None
    return out, last


# ---- scan: claims -> intents ----------------------------------------------------------------------------------------------------
def _context(conn: psycopg.Connection, principal: UUID, now: datetime) -> T.Context:
    from pulse.adapters.postgres.ledger_repo import LedgerRepo
    from pulse.application.portfolio_service import granted_accounts

    holdings: set[str] = set()
    tracked = {r["account_id"] for r in conn.execute("SELECT account_id FROM execution.tracked_account")}
    for acct in granted_accounts(conn, principal, "read", now):
        if acct in tracked:                                   # forward books are paper tests under evaluation, not the user's holdings
            continue
        led = LedgerRepo(conn).load(acct)
        holdings |= {s for s, h in led.holdings.items() if h.quantity}
    watch = {str(r["instrument_id"]) for r in conn.execute(
        "SELECT m.instrument_id FROM ui.watchlist_member m JOIN ui.watchlist w USING (watchlist_id) WHERE w.principal_id=%s", (principal,))}
    main = {str(r["instrument_id"]) for r in conn.execute(
        "SELECT DISTINCT ON (instrument_id) instrument_id, board_code FROM core.instrument_state ORDER BY instrument_id, state_version DESC") if r["board_code"] == "main"}
    inds: dict[str, set[str]] = {}
    if holdings:
        for r in conn.execute(
                "SELECT t.instrument_id, n.display_name FROM core.instrument_tag t JOIN core.taxonomy_node n ON n.taxonomy_id=t.taxonomy_id"
                " AND n.taxonomy_version=t.taxonomy_version AND n.node_id=t.node_id WHERE t.taxonomy_id=%s AND t.instrument_id = ANY(%s)"
                " AND t.effective_from <= %s AND (t.effective_to IS NULL OR t.effective_to > %s) AND t.available_at <= %s",
                (TAXONOMY, [UUID(h) for h in holdings if _is_uuid(h)], now, now, now)):
            inds.setdefault(r["display_name"], set()).add(str(r["instrument_id"]))
    return T.Context(frozenset(holdings), frozenset(watch), frozenset(main), {k: frozenset(v) for k, v in inds.items()})


def _is_uuid(s: str) -> bool:
    try:
        UUID(s)
        return True
    except ValueError:
        return False


def _claim_views(conn: psycopg.Connection, since: datetime, until: datetime | None = None) -> list[T.ClaimView]:
    rows = conn.execute(
        "SELECT * FROM (SELECT DISTINCT ON (claim_id) * FROM intelligence.claim_revision WHERE claim_schema_version=%s AND available_at > %s"
        " AND (%s::timestamptz IS NULL OR available_at <= %s) ORDER BY claim_id, revision DESC) c ORDER BY available_at, claim_id LIMIT %s",
        (NEWS_TOKEN, since, until, until, SCAN_LIMIT)).fetchall()
    if not rows:
        return []
    ids = [r["claim_revision_id"] for r in rows]
    cids = [r["claim_id"] for r in rows]
    ex = {r["claim_revision_id"]: r for r in conn.execute(
        "SELECT ec.claim_revision_id, e.analysis_payload->'exposed_instruments' AS ex, e.analysis_payload->'sector_hints' AS sh"
        " FROM intelligence.event_claim ec JOIN intelligence.event_revision e USING (event_revision_id)"
        " WHERE ec.claim_revision_id = ANY(%s) AND e.analysis_schema_version ILIKE '%%impact%%'", (ids,))}
    pub = {r["claim_revision_id"]: r["p"] for r in conn.execute(
        "SELECT ce.claim_revision_id, min(d.published_at) p FROM intelligence.claim_evidence ce JOIN intelligence.document_revision d"
        " USING (document_revision_id) WHERE ce.claim_revision_id = ANY(%s) GROUP BY 1", (ids,))}
    first = {r["claim_id"]: r["a"] for r in conn.execute(
        "SELECT claim_id, min(available_at) a FROM intelligence.claim_revision WHERE claim_id = ANY(%s) GROUP BY 1", (cids,))}
    out = []
    for r in rows:
        p = r["object_payload"]
        e = ex.get(r["claim_revision_id"])
        inst = tuple(sorted({i["instrument_id"] for i in (e["ex"] or [])})) if e else ()
        hints = tuple(h.get("hint", "") for h in (e["sh"] or [])) if e else tuple(p.get("sector_hints") or ())
        published = pub.get(r["claim_revision_id"])
        lag = None if published is None else (first[r["claim_id"]] - published).total_seconds()
        out.append(T.ClaimView(str(r["claim_id"]), str(r["claim_revision_id"]), r["revision"], p.get("headline") or "", p.get("category"),
                               p.get("polarity_hint") or "none", r["claim_status"], r["assertion_type"], r["available_at"].isoformat(),
                               r["event_time"].isoformat() if r["event_time"] else None, lag, inst, hints,
                               (p.get("cluster") or {}).get("seed")))
    return out


def _names(conn: psycopg.Connection, ids: set[str]) -> dict[str, dict]:
    if not ids:
        return {}
    uid = [UUID(i) for i in ids if _is_uuid(i)]
    out: dict[str, dict] = {}
    for r in conn.execute("SELECT DISTINCT ON (instrument_id) instrument_id, display_name FROM core.instrument_state WHERE instrument_id = ANY(%s)"
                          " ORDER BY instrument_id, state_version DESC", (uid,)):
        out[str(r["instrument_id"])] = {"code": None, "name": r["display_name"]}
    for r in conn.execute("SELECT instrument_id, source_symbol FROM core.instrument_alias WHERE instrument_id = ANY(%s) ORDER BY source_symbol DESC", (uid,)):
        out.setdefault(str(r["instrument_id"]), {"code": None, "name": None})["code"] = r["source_symbol"]
    return out


def _rule_index(rules: list[dict]) -> dict[str, list[dict]]:
    """Enabled rules by scope kind."""
    idx: dict[str, list[dict]] = {k: [] for k in SCOPE_KINDS}
    for r in rules:
        b = r["body"]
        if b["enabled"]:
            idx[b["scope"]["scope_kind"]].append(r)
    return idx


def _new_intent(conn: psycopg.Connection, principal: UUID, rule: dict, hit: T.TierHit, claim: T.ClaimView, names: dict, now: datetime,
                tier_label: str, target: str) -> UUID | None:
    key = T.dedup_key(tier_label, claim, target)
    if conn.execute("SELECT 1 FROM notification.alert_intent WHERE principal_id=%s AND dedup_key=%s", (principal, key)).fetchone():
        return None
    prefix = key.rsplit(":", 1)[0] + ":"
    prev = conn.execute("SELECT alert_id FROM notification.alert_intent WHERE principal_id=%s AND dedup_key LIKE %s ORDER BY raised_at DESC LIMIT 1",
                        (principal, prefix.replace("%", r"\%").replace("_", r"\_") + "%")).fetchone()
    body = T.evidence_body(hit, claim, rule_id=str(rule["rule_id"]), names=names)
    body["tier"] = tier_label
    art = ops_service.put_artifact(conn, kind="alert-evidence", contract_id=EVIDENCE_CONTRACT, schema_version=EVIDENCE_VERSION, body=body, owner=principal)
    alert_id = uuid.uuid5(DEST_NS, f"{principal}|{key}")
    conn.execute(
        "INSERT INTO notification.alert_intent(alert_id, principal_id, rule_id, rule_version, tier, dedup_key, based_on_release_id, evidence_artifact,"
        " raised_at, expires_at, supersedes, source_claim_revision_id) VALUES (%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s,%s,%s)",
        (alert_id, principal, rule["rule_id"], rule["rule_version"], tier_label, key, art["ref"], now,
         now + timedelta(seconds=T.ALERT_LIFETIME_SECONDS), prev["alert_id"] if prev else None, UUID(claim.claim_revision_id)))
    dests = {d["destination_id"]: d for d in destinations(conn, principal)}
    for did in rule["body"]["destination_ids"]:
        d = dests.get(UUID(str(did)))
        if d is None or not d["enabled"]:
            continue
        inbox = d["channel"] == "in_app"
        conn.execute("INSERT INTO notification.delivery_attempt(attempt_id, alert_id, destination_id, attempt_number, status, started_at, completed_at)"
                     " VALUES (%s,%s,%s,1,%s,%s,%s)", (uuid.uuid4(), alert_id, d["destination_id"], "confirmed" if inbox else "pending", now, now if inbox else None))
    n = conn.execute("SELECT count(*) n FROM notification.alert_intent WHERE principal_id=%s", (principal,)).fetchone()["n"]
    PgOutbox(conn).append(OutboxEvent(
        event_id=uuid.uuid5(DEST_NS, f"outbox|{alert_id}"), event_type="alert.raised", schema_version="1", aggregate_type="alert_inbox",
        aggregate_id=str(principal), aggregate_version=n, occurred_at=now, producer_version="alert-service-v1",
        payload={"alert_id": str(alert_id), "tier": tier_label, "raised_at": now.isoformat()}))
    return alert_id


def scan(conn: psycopg.Connection, now: datetime | None = None, *, principals: list[UUID] | None = None, since: datetime | None = None,
         until: datetime | None = None, limit_principals: int = 100) -> dict:
    """Turn new news claims into alert intents for every user with enabled rules. Idempotent."""
    now = now or ops_service.now_utc()
    cur = conn.execute("SELECT last_available_at FROM notification.scan_cursor WHERE cursor_name=%s", (SCAN_CURSOR,)).fetchone()
    start = since or (cur["last_available_at"] if cur else now - timedelta(hours=6))
    claims = _claim_views(conn, start, until)
    summary = {"claims_read": len(claims), "intents": 0, "skipped_backfill": 0, "principals": 0}
    if principals is None:
        principals = [r["principal_id"] for r in conn.execute(
            "SELECT DISTINCT principal_id FROM notification.alert_rule_head LIMIT %s", (limit_principals,))]
    names_needed = {i for c in claims for i in c.instrument_ids}
    names = _names(conn, names_needed)
    for p in principals:
        rules = current_rules(conn, p)
        idx = _rule_index(rules)
        if not any(idx.values()):
            continue
        summary["principals"] += 1
        ctx = _context(conn, p, now)
        tier_rules = {r["body"]["scope"]["scope_key"]: r for r in idx["news_tier"]}
        for c in claims:
            if T.is_backfill(c):
                summary["skipped_backfill"] += 1
                continue
            avail = datetime.fromisoformat(c.available_at)
            for hit in T.decide(c, ctx):
                rule = tier_rules.get(hit.tier)
                if rule is None or avail < rule["updated_at"]:
                    continue
                target = ",".join(hit.instrument_ids or hit.industries)
                with conn.transaction():
                    if _new_intent(conn, p, rule, hit, c, names, now, hit.tier, target):
                        summary["intents"] += 1
            for kind, scope_hit in (("instrument", lambda r, c=c: r["body"]["scope"]["scope_key"] in c.instrument_ids),
                                    ("sector", lambda r, c=c: _sector_matches(conn, r["body"]["scope"]["scope_key"], c))):
                for rule in idx[kind]:
                    if avail < rule["updated_at"] or not scope_hit(rule):
                        continue
                    hit = T.TierHit("RULE", f"用户规则命中（{kind}）", c.instrument_ids if kind == "instrument" else (), trigger={"rule_scope": kind})
                    with conn.transaction():
                        if _new_intent(conn, p, rule, hit, c, names, now, "RULE", rule["body"]["scope"]["scope_key"]):
                            summary["intents"] += 1
    if claims:
        newest = max(datetime.fromisoformat(c.available_at) for c in claims)
        conn.execute("INSERT INTO notification.scan_cursor(cursor_name, last_available_at, updated_at) VALUES (%s,%s,%s)"
                     " ON CONFLICT (cursor_name) DO UPDATE SET last_available_at=GREATEST(notification.scan_cursor.last_available_at, EXCLUDED.last_available_at), updated_at=EXCLUDED.updated_at",
                     (SCAN_CURSOR, newest, now))
    return summary


def _sector_matches(conn: psycopg.Connection, node_id: str, c: T.ClaimView) -> bool:
    n = conn.execute("SELECT display_name FROM core.taxonomy_node WHERE taxonomy_id=%s AND node_id=%s", (TAXONOMY, node_id)).fetchone()
    return n is not None and n["display_name"] in T.industries_for(c.headline, c.sector_hints)


# ---- deliver ---------------------------------------------------------------------------------------------------------------------
def deliver(conn: psycopg.Connection, now: datetime | None = None, *, transport=None, limit: int = 100) -> dict:
    """Send pending webhook attempts. Never sends without a configured URL; tests inject ``transport``."""
    now = now or ops_service.now_utc()
    cfg = webhook.load_config()
    summary = {"sent": 0, "accepted": 0, "failed": 0, "unknown": 0, "expired": 0, "retried": 0, "skipped_unconfigured": 0}
    rows = conn.execute(
        "SELECT a.*, i.expires_at, i.evidence_artifact FROM notification.delivery_attempt a JOIN notification.alert_intent i USING (alert_id)"
        " WHERE a.status='pending' ORDER BY a.started_at LIMIT %s", (limit,)).fetchall()
    for a in rows:
        if now > a["expires_at"]:
            conn.execute("UPDATE notification.delivery_attempt SET status='expired', completed_at=%s WHERE attempt_id=%s", (now, a["attempt_id"]))
            summary["expired"] += 1
            continue
        if cfg is None:
            summary["skipped_unconfigured"] += 1
            continue
        ev = conn.execute("SELECT body FROM ops.ext_artifact WHERE ref=%s", (a["evidence_artifact"],)).fetchone()["body"]
        conn.execute("UPDATE notification.delivery_attempt SET status='sending' WHERE attempt_id=%s AND status='pending'", (a["attempt_id"],))
        res = webhook.send(cfg, ev, transport=transport)
        conn.execute("UPDATE notification.delivery_attempt SET status=%s, completed_at=%s, provider_message_id=%s, error_code=%s WHERE attempt_id=%s",
                     (res.status, now, res.provider_message_id, res.error_code, a["attempt_id"]))
        summary["sent"] += 1
        summary[res.status] += 1
        if res.status == "failed" and a["attempt_number"] < MAX_ATTEMPTS:
            conn.execute("INSERT INTO notification.delivery_attempt(attempt_id, alert_id, destination_id, attempt_number, status, started_at)"
                         " VALUES (%s,%s,%s,%s,'pending',%s)", (uuid.uuid4(), a["alert_id"], a["destination_id"], a["attempt_number"] + 1, now))
            summary["retried"] += 1
    return summary


@ops_service.register_handler("AlertService.scan")
def _job_scan(conn, run, args) -> dict:
    return scan(conn, ops_service.now_utc())


@ops_service.register_handler("AlertService.deliver")
def _job_deliver(conn, run, args) -> dict:
    return deliver(conn, ops_service.now_utc())
