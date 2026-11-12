"""Plans, accounts and portfolio: grants, trade-plan proposals, broker fill import, order/performance read models, opportunities,
forward books (PEX05, PEX12; D04, D05, D15, D16, D17, D27).

Why: every money number comes from the ONE ledger (``pulse.domain.execution``); this module adds ownership (who may read/plan/import
an account), the raw-facts-first import of broker fills, immutable read models keyed by their full query context, and the
LEGACY_FORWARD paper books computed by the same execution kernel.
Invariants:
* ``account_head`` has no owner: access is ``access.account_grant`` (read / plan / import, versioned, revocable); an ungranted
  account is invisible (404, never 403) and a revoked grant stops access immediately (cursors do not bypass it);
* ``importBrokerFills`` stores the normalized facts first (``execution.broker_fill_fact``), then applies them through the kernel; a
  duplicate (same account/source/execution id and content) never books twice; a changed redelivery is a ``conflict`` fact for
  reconciliation; a fill the ledger cannot take (missing cash/inventory, wrong day, reservation exceeded) stays
  ``pending_reconciliation`` - never silently dropped and never turned into "unfilled"; unknown fees are never booked as 0;
* a read model row is identified by ``(account, version, kind, query_context_hash)``; a query whose context was never built is
  ``RESULT_NOT_READY`` (nothing is computed inside the GET);
* a plan proposal is pinned to the release and (optional) account version it was written against; compile re-checks both and ends
  ``rejected``/``superseded`` with a reason instead of producing a stale plan; a user edit is a separate plan chain whose first
  version ``supersedes`` the system version, so the system plan is never rewritten.
Not covered: order placement (plans are never orders; reservations stay in the kernel), margin, multi-currency, raw upload of broker
files (a raw object must already exist: ``store_broker_raw`` is the CLI/test helper).
"""
from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from pulse import contracts
from pulse.adapters.postgres import decision_repo
from pulse.adapters.postgres._canon import sha256_of
from pulse.adapters.postgres.jobs import PgJobStore
from pulse.adapters.postgres.ledger_repo import EventContext, LedgerRepo, stable_uuid
from pulse.application import ops_service
from pulse.application.ports.errors import Conflict
from pulse.domain.execution import KERNEL_VERSION, ExecutionKernel, ExecutionReport, OrderIntent
from pulse.domain.execution.comparison import compare_with_hold
from pulse.domain.market_rules import (MARKET_RULES_VERSION, Board, CostProfileRegistry, Exchange, InstrumentSpec, PriceLimitRegistry)
from pulse.domain.market_rules.costs import scaled, user_account_2026
from pulse.domain.portfolio import forward_book as FB
from pulse.domain.portfolio import readmodels as RM

CST = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc
FWD_NS = uuid.UUID("2c9e41d7-85a3-4f0b-b7c6-9a1d3e5f7b20")
PERMISSIONS = ("read", "plan", "import")
MAPPING_VERSIONS = ("broker-fill-v1",)
REPORT_FILL, REPORT_CANCEL, REPORT_FEE = ("fill", "trade", "partial_fill"), ("cancel", "cancelled"), ("fee_correction", "correction")
FEE_MODES = ("actual", "pending_estimate")
PERF_POLICY_RE = r"^twr-cost-x([1-3])-v1$"
VALUATION_POLICY = "last-raw-close-in-snapshot-v1"
READMODEL_CONTRACT = {"orders": "pulse.ext_c.order_views", "performance": "pulse.ext_c.account_performance"}
PLAN_CONTRACT, PLAN_VERSION = "pulse.ext_c.plan_proposal_request", "1"


class PortfolioError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# ---- grants (D15) -----------------------------------------------------------------------------------------------------------------
def grant(conn: psycopg.Connection, principal: UUID, account_id: UUID, permission: str, now: datetime | None = None, *, revoke: bool = False) -> int:
    if permission not in PERMISSIONS:
        raise ValueError(permission)
    now = now or ops_service.now_utc()
    cur = conn.execute("SELECT max(grant_version) v FROM access.account_grant WHERE principal_id=%s AND account_id=%s AND permission=%s",
                       (principal, account_id, permission)).fetchone()["v"] or 0
    conn.execute("INSERT INTO access.account_grant(principal_id, account_id, permission, grant_version, valid_from, revoked_at) VALUES (%s,%s,%s,%s,%s,%s)",
                 (principal, account_id, permission, cur + 1, now, now if revoke else None))
    return cur + 1


def has_grant(conn: psycopg.Connection, principal: UUID, account_id: UUID, permission: str, at: datetime | None = None) -> bool:
    at = at or ops_service.now_utc()
    row = conn.execute("SELECT revoked_at, valid_from FROM access.account_grant WHERE principal_id=%s AND account_id=%s AND permission=%s"
                       " ORDER BY grant_version DESC LIMIT 1", (principal, account_id, permission)).fetchone()
    return bool(row) and row["revoked_at"] is None and row["valid_from"] <= at


def granted_accounts(conn: psycopg.Connection, principal: UUID, permission: str, at: datetime | None = None) -> list[UUID]:
    at = at or ops_service.now_utc()
    rows = conn.execute("SELECT DISTINCT ON (account_id) account_id, revoked_at, valid_from FROM access.account_grant WHERE principal_id=%s AND permission=%s"
                        " ORDER BY account_id, grant_version DESC", (principal, permission)).fetchall()
    return [r["account_id"] for r in rows if r["revoked_at"] is None and r["valid_from"] <= at]


def require_grant(conn: psycopg.Connection, principal: UUID, account_id: UUID, permission: str) -> None:
    if not has_grant(conn, principal, account_id, permission):
        raise PortfolioError(404, "NOT_FOUND", "账户不存在")          # an account you cannot use does not exist for you


def list_authorized_accounts(conn: psycopg.Connection, principal: UUID, limit: int, after: UUID | None) -> tuple[list[dict], UUID | None]:
    ids = sorted(granted_accounts(conn, principal, "read"), key=str)
    if after is not None:
        ids = [i for i in ids if str(i) > str(after)]
    more = len(ids) > limit
    ids = ids[:limit]
    heads = {r["account_id"]: r for r in conn.execute("SELECT account_id, current_sequence, base_currency FROM execution.account_head WHERE account_id = ANY(%s)", (ids,))}
    out = [{"account_id": i, "account_version": heads[i]["current_sequence"], "base_currency": heads[i]["base_currency"]} for i in ids if i in heads]
    return out, (ids[-1] if more else None)


# ---- plans (PEX05) ------------------------------------------------------------------------------------------------------------------
def _public_release(conn: psycopg.Connection, release_id: UUID) -> dict:
    r = conn.execute("SELECT * FROM decision.release WHERE release_id=%s", (release_id,)).fetchone()
    if r is None or r["release_state"] not in ("READY", "SUPERSEDED"):
        raise PortfolioError(404, "NOT_FOUND", "发布不存在")
    return r


def _origin_of(conn: psycopg.Connection, plan_id: UUID, version: int) -> dict:
    m = conn.execute("SELECT * FROM decision.plan_revision_metadata WHERE plan_id=%s AND plan_version=%s", (plan_id, version)).fetchone()
    if m is None:
        return {"origin": "system", "change_reason": "release_generated", "trigger_event_id": None, "principal_id": None}
    return {"origin": m["origin"], "change_reason": m["change_reason"], "trigger_event_id": m["trigger_event_id"], "principal_id": m["principal_id"]}


def _visible(conn: psycopg.Connection, principal: UUID, row: dict) -> bool:
    if row["account_id"] is not None and not has_grant(conn, principal, row["account_id"], "read"):
        return False
    o = _origin_of(conn, row["plan_id"], row["plan_version"])
    if o["origin"] == "user_edit" and o["principal_id"] != principal:
        return False
    rel = conn.execute("SELECT release_state FROM decision.release WHERE release_id=%s", (row["based_on_release_id"],)).fetchone()
    return o["origin"] == "user_edit" or (rel is not None and rel["release_state"] in ("READY", "SUPERSEDED"))


def _plan_item(conn: psycopg.Connection, row: dict) -> tuple[dict, dict]:
    o = _origin_of(conn, row["plan_id"], row["plan_version"])
    return row["plan_payload"], {"plan": {"plan_id": row["plan_id"], "plan_version": row["plan_version"]}, "origin": o["origin"],
                                 "change_reason": o["change_reason"], "trigger_event_id": o["trigger_event_id"]}


def query_plan_versions(conn: psycopg.Connection, principal: UUID, plan_id: UUID, limit: int, after_version: int) -> tuple[list, list, int | None]:
    rows = [r for r in conn.execute("SELECT * FROM decision.trade_plan WHERE plan_id=%s AND plan_version > %s ORDER BY plan_version",
                                    (plan_id, after_version)).fetchall() if _visible(conn, principal, r)]
    if not rows and not conn.execute("SELECT 1 FROM decision.trade_plan WHERE plan_id=%s AND plan_version <= %s", (plan_id, after_version)).fetchone():
        raise PortfolioError(404, "NOT_FOUND", "计划不存在")
    more = len(rows) > limit
    rows = rows[:limit]
    items = [_plan_item(conn, r) for r in rows]
    return [i[0] for i in items], [i[1] for i in items], (rows[-1]["plan_version"] if more and rows else None)


def list_trade_plans(conn: psycopg.Connection, principal: UUID, instrument_id: UUID, account_id: UUID | None, limit: int, after: UUID | None) -> tuple[list, list, UUID | None]:
    if account_id is not None:
        require_grant(conn, principal, account_id, "read")
    sql = ("SELECT DISTINCT ON (plan_id) * FROM decision.trade_plan WHERE instrument_id=%s")
    args: list[Any] = [instrument_id]
    sql += " AND account_id=%s" if account_id is not None else " AND account_id IS NULL"
    if account_id is not None:
        args.append(account_id)
    rows = conn.execute(sql + " ORDER BY plan_id, plan_version DESC", args).fetchall()
    # latest VISIBLE version of each chain (a hidden newer edit of someone else must not hide the system version)
    chains: dict[UUID, dict] = {}
    for r in conn.execute("SELECT * FROM decision.trade_plan WHERE plan_id = ANY(%s) ORDER BY plan_id, plan_version DESC", ([r["plan_id"] for r in rows],)):
        if r["plan_id"] not in chains and _visible(conn, principal, r):
            chains[r["plan_id"]] = r
    ids = sorted(chains, key=str)
    if after is not None:
        ids = [i for i in ids if str(i) > str(after)]
    more = len(ids) > limit
    ids = ids[:limit]
    items = [_plan_item(conn, chains[i]) for i in ids]
    return [i[0] for i in items], [i[1] for i in items], (ids[-1] if more else None)


def propose_trade_plan(conn: psycopg.Connection, principal: UUID, req: dict, idempotency_key: str, now: datetime | None = None) -> dict:
    now = now or ops_service.now_utc()
    _public_release(conn, req["based_on_release_id"])
    if not conn.execute("SELECT 1 FROM core.instrument WHERE instrument_id=%s", (req["instrument_id"],)).fetchone():
        raise PortfolioError(404, "NOT_FOUND", "证券不存在")
    base = req["base_plan"]
    if base is None:
        if req["expected_plan_version"] != 0:
            raise PortfolioError(409, "VERSION_CONFLICT", "创建计划时 expected_plan_version 必须为 0")
    else:
        latest = conn.execute("SELECT max(plan_version) v, min(instrument_id::text) i FROM decision.trade_plan WHERE plan_id=%s", (base["plan_id"],)).fetchone()
        row = conn.execute("SELECT * FROM decision.trade_plan WHERE plan_id=%s AND plan_version=%s", (base["plan_id"], base["plan_version"])).fetchone()
        if row is None or not _visible(conn, principal, row):
            raise PortfolioError(404, "NOT_FOUND", "基础计划不存在")
        if row["instrument_id"] != req["instrument_id"]:
            raise PortfolioError(400, "INVALID_CONTRACT", "基础计划与证券不一致")
        if req["expected_plan_version"] != base["plan_version"] or latest["v"] != base["plan_version"]:
            raise PortfolioError(409, "VERSION_CONFLICT", f"基础计划已有更新版本 {latest['v']}")
    if req["account_id"] is not None:
        require_grant(conn, principal, req["account_id"], "plan")
        head = conn.execute("SELECT current_sequence FROM execution.account_head WHERE account_id=%s", (req["account_id"],)).fetchone()
        if head["current_sequence"] != req["expected_account_version"]:
            raise PortfolioError(409, "VERSION_CONFLICT", f"账户当前版本是 {head['current_sequence']}")
    fixed = {k: v for k, v in req.items() if k != "idempotency_key"}
    request_hash = sha256_of(fixed)
    existing = conn.execute("SELECT proposal_id, job_run_id FROM decision.plan_proposal WHERE principal_id=%s AND request_hash=%s", (principal, request_hash)).fetchone()
    if existing:                                            # same request under another key: same proposal, no second plan
        return {**PgJobStore(conn).receipt(existing["job_run_id"]), "resource_id": existing["proposal_id"], "poll_operation_id": "queryPlanProposal"}
    proposal_id = uuid.uuid5(FWD_NS, f"proposal|{principal}|{request_hash}")
    art = ops_service.put_artifact(conn, kind="plan-proposal-request", contract_id=PLAN_CONTRACT, schema_version=PLAN_VERSION, body=fixed, owner=principal)
    run_id = uuid.uuid5(FWD_NS, f"proposal-run|{proposal_id}")
    ops_service.ensure_jobs(conn)
    d = ops_service.DEFINITIONS["PortfolioService.compile_proposal"]
    PgJobStore(conn).enqueue(job_run_id=run_id, job_id=d.job_id, job_version=d.job_version, idempotency_key=f"proposal:{proposal_id}",
                             parameter_hash=request_hash, scheduled_for=now, resource_type="plan_proposal", resource_id=proposal_id)
    conn.execute("INSERT INTO decision.plan_proposal(proposal_id, principal_id, instrument_id, based_on_release_id, base_plan_id, base_plan_version,"
                 " account_id, expected_account_version, request_hash, request_artifact, job_run_id, state, updated_at)"
                 " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'queued',%s)",
                 (proposal_id, principal, req["instrument_id"], req["based_on_release_id"], base["plan_id"] if base else None,
                  base["plan_version"] if base else None, req["account_id"], req["expected_account_version"], request_hash, art["ref"], run_id, now))
    conn.execute("INSERT INTO ops.job_run_argument(job_run_id, principal_id, parameter_contract_id, parameter_schema_version, parameter_artifact,"
                 " parameter_hash, input_snapshot_refs, release_id) VALUES (%s,%s,%s,%s,%s,%s,'[]',%s)",
                 (run_id, principal, PLAN_CONTRACT, PLAN_VERSION, art["ref"], art["content_hash"], req["based_on_release_id"]))
    return {**PgJobStore(conn).receipt(run_id), "poll_operation_id": "queryPlanProposal"}


def _finish(conn: psycopg.Connection, proposal_id: UUID, state: str, reason: str | None = None, plan: tuple[UUID, int] | None = None) -> dict:
    conn.execute("UPDATE decision.plan_proposal SET state=%s, reason_code=%s, compiled_plan_id=%s, compiled_plan_version=%s, updated_at=%s WHERE proposal_id=%s",
                 (state, reason, plan[0] if plan else None, plan[1] if plan else None, ops_service.now_utc(), proposal_id))
    return {"proposal_id": str(proposal_id), "state": state, "reason_code": reason}


@ops_service.register_handler("PortfolioService.compile_proposal")
def compile_proposal(conn: psycopg.Connection, run, args: dict) -> dict:
    """The one PlanCompiler step: re-check the pinned inputs, then append a user-edit version. Never an order.

    One transaction from the proposal row lock to the new version: the base-version check, the version number and the
    write cannot interleave with another compile of the same plan, and a failure leaves no partial state."""
    with conn.transaction():
        return _compile_proposal(conn, run)


def _compile_proposal(conn: psycopg.Connection, run) -> dict:
    p = conn.execute("SELECT * FROM decision.plan_proposal WHERE job_run_id=%s FOR UPDATE", (run.job_run_id,)).fetchone()
    if p is None:
        raise LookupError("proposal for job run not found")
    if p["state"] in ("compiled", "rejected", "superseded"):
        return {"proposal_id": str(p["proposal_id"]), "state": p["state"], "replayed": True}
    conn.execute("UPDATE decision.plan_proposal SET state='running' WHERE proposal_id=%s", (p["proposal_id"],))
    req = conn.execute("SELECT body FROM ops.ext_artifact WHERE ref=%s", (p["request_artifact"],)).fetchone()["body"]
    if p["account_id"] is not None:
        head = conn.execute("SELECT current_sequence FROM execution.account_head WHERE account_id=%s", (p["account_id"],)).fetchone()
        if head["current_sequence"] != p["expected_account_version"]:
            return _finish(conn, p["proposal_id"], "rejected", "ACCOUNT_VERSION_STALE")
        if not has_grant(conn, p["principal_id"], p["account_id"], "plan"):
            return _finish(conn, p["proposal_id"], "rejected", "ACCOUNT_GRANT_REVOKED")
    base_row = None
    if p["base_plan_id"] is not None:
        latest = conn.execute("SELECT max(plan_version) v FROM decision.trade_plan WHERE plan_id=%s", (p["base_plan_id"],)).fetchone()["v"]
        if latest != p["base_plan_version"]:
            return _finish(conn, p["proposal_id"], "superseded", "BASE_PLAN_SUPERSEDED")
        base_row = conn.execute("SELECT * FROM decision.trade_plan WHERE plan_id=%s AND plan_version=%s", (p["base_plan_id"], p["base_plan_version"])).fetchone()
    else:                                                    # creation: inherit the zones/sizing of the system plan of that release
        base_row = conn.execute("SELECT * FROM decision.trade_plan WHERE instrument_id=%s AND based_on_release_id=%s AND account_id IS NULL"
                                " AND NOT EXISTS (SELECT 1 FROM decision.plan_revision_metadata m WHERE m.plan_id=trade_plan.plan_id AND m.plan_version=trade_plan.plan_version)"
                                " ORDER BY plan_version DESC LIMIT 1", (p["instrument_id"], p["based_on_release_id"])).fetchone()
        if base_row is None:
            return _finish(conn, p["proposal_id"], "rejected", "NO_BASELINE_PLAN")
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"plan:{base_row['plan_id']}",))
    src = base_row["plan_payload"]
    user_chain = conn.execute("SELECT 1 FROM decision.plan_revision_metadata WHERE plan_id=%s AND plan_version=%s AND origin='user_edit'",
                              (base_row["plan_id"], base_row["plan_version"])).fetchone() is not None
    if user_chain:
        plan_id, version = base_row["plan_id"], base_row["plan_version"] + 1
    else:
        plan_id, version = uuid.uuid5(FWD_NS, f"user-plan|{p['principal_id']}|{base_row['plan_id']}"), 1
        prev = conn.execute("SELECT max(plan_version) v FROM decision.trade_plan WHERE plan_id=%s", (plan_id,)).fetchone()["v"]
        version = (prev or 0) + 1
    now = ops_service.now_utc()
    plan = deepcopy(src)
    plan.update({"plan_id": str(plan_id), "plan_version": version, "based_on_release_id": str(p["based_on_release_id"]),
                 "account_id": None if p["account_id"] is None else str(p["account_id"]),
                 "expected_account_version": p["expected_account_version"], "state": "watching",
                 "entry_conditions": req["requested_conditions"], "available_at": contracts.utc_z(now),
                 "supersedes": {"plan_id": str(base_row["plan_id"]), "plan_version": base_row["plan_version"]}})
    if req["strategy_instances"]:
        plan["strategy_refs"] = req["strategy_instances"]
    try:
        contracts.check("TradePlan", contracts.to_wire(plan))
    except contracts.ContractViolation as e:
        return _finish(conn, p["proposal_id"], "rejected", "INVALID_COMPILED_PLAN:" + e.errors[0][:80])
    with conn.transaction():
        decision_repo.append_plan_version(conn, plan)
        conn.execute("INSERT INTO decision.plan_revision_metadata(plan_id, plan_version, principal_id, origin, change_reason, trigger_event_id, source_proposal_id)"
                     " VALUES (%s,%s,%s,'user_edit','user_requested_entry_conditions',NULL,%s)", (plan_id, version, p["principal_id"], p["proposal_id"]))
        return _finish(conn, p["proposal_id"], "compiled", None, (plan_id, version))


def query_plan_proposal(conn: psycopg.Connection, principal: UUID, proposal_id: UUID) -> dict:
    p = conn.execute("SELECT * FROM decision.plan_proposal WHERE proposal_id=%s", (proposal_id,)).fetchone()
    if p is None or p["principal_id"] != principal:
        raise PortfolioError(404, "NOT_FOUND", "提案不存在")
    job = PgJobStore(conn).job_run_wire(p["job_run_id"])
    state = p["state"]
    if state == "queued" and job["run_state"] == "running":
        state = "running"
    if job["run_state"] == "failed" and state in ("queued", "running"):
        state, reason = "rejected", p["reason_code"] or job["error_code"] or "JOB_FAILED"
    else:
        reason = p["reason_code"]
    plan = None if p["compiled_plan_id"] is None else {"plan_id": p["compiled_plan_id"], "plan_version": p["compiled_plan_version"]}
    return {"job": job, "proposal_state": state, "compiled_plan": plan, "reason_code": reason}


# ---- shared market helpers --------------------------------------------------------------------------------------------------------
_EXCH = {"XSHG": Exchange.SSE, "XSHE": Exchange.SZSE, "XBSE": Exchange.BSE}
_BOARD = {"main": Board.MAIN, "chinext": Board.CHINEXT, "star": Board.STAR, "bse": Board.BSE}


def instrument_specs(conn: psycopg.Connection, ids: set[str]) -> dict[str, InstrumentSpec]:
    uid = [UUID(i) for i in ids]
    rows = conn.execute("SELECT i.instrument_id, i.venue_code, s.board_code, s.is_risk_warning FROM core.instrument i JOIN LATERAL"
                        " (SELECT * FROM core.instrument_state WHERE instrument_id=i.instrument_id ORDER BY state_version DESC LIMIT 1) s ON true"
                        " WHERE i.instrument_id = ANY(%s)", (uid,)).fetchall()
    return {str(r["instrument_id"]): InstrumentSpec(str(r["instrument_id"]), _EXCH[r["venue_code"]], _BOARD[r["board_code"]], r["is_risk_warning"]) for r in rows}


def calendar(conn: psycopg.Connection) -> FB.HybridCalendar:
    days = [r["trade_date"] for r in conn.execute("SELECT DISTINCT trade_date FROM market.bar_projection WHERE timeframe='1d'")]
    if not days:
        days = [date(2020, 1, 2)]
    return FB.HybridCalendar(days)


COSTS = CostProfileRegistry((user_account_2026(),))


def marks_provider(conn: psycopg.Connection, snapshot_ids: list[UUID] | None):
    """(day, symbols) -> latest non-null raw close on or before ``day`` (inside the given snapshots, or any READY raw snapshot)."""
    def marks(day: date, symbols: set[str]) -> dict[str, Decimal | None]:
        out: dict[str, Decimal | None] = {}
        for s in symbols:
            try:
                iid = UUID(s)
            except ValueError:
                out[s] = None
                continue
            sql = ("SELECT close_price_raw FROM market.bar_projection WHERE instrument_id=%s AND timeframe='1d' AND close_price_raw IS NOT NULL"
                   " AND trade_date <= %s")
            args: list[Any] = [iid, day]
            if snapshot_ids is not None:
                sql += " AND snapshot_id = ANY(%s)"
                args.append(snapshot_ids)
            r = conn.execute(sql + " ORDER BY trade_date DESC, bar_open_time DESC LIMIT 1", args).fetchone()
            out[s] = None if r is None else r["close_price_raw"]
        return out
    return marks


def _kernel_session(conn: psycopg.Connection, account_id: UUID, source: str, manifest: str, specs: dict, cal, action):
    repo = LedgerRepo(conn)
    with repo.locked(account_id) as session:
        ctx = EventContext(source, MARKET_RULES_VERSION, COSTS.resolve(session.ledger.day).version, KERNEL_VERSION, manifest)
        applied: list[UUID] = []

        def sink(e: dict) -> None:
            r = session.append(e, ctx)
            if r.applied:
                applied.append(stable_uuid(account_id, "event", e["event_id"]))

        kernel = ExecutionKernel(deepcopy(session.ledger), specs, cal, COSTS, PriceLimitRegistry(), sink=sink, require_price_band=False)
        out = action(kernel, session)
        return out, applied, session.version


def _advance_to(kernel: ExecutionKernel, cal, day: date) -> None:
    while kernel.ledger.day < day:
        nxt = cal.next_trading_day(kernel.ledger.day)
        if nxt > day:
            break
        kernel.advance_session(nxt, datetime.combine(nxt, time(8, 0), CST))


# ---- broker fill import (importBrokerFills, D16) ------------------------------------------------------------------------------------
def store_broker_raw(conn: psycopg.Connection, *, source_id: str, content: bytes, now: datetime | None = None, lake=None) -> UUID:
    """Persist a broker report file as an immutable raw object (CLI/test helper; the API takes its id)."""
    import hashlib

    from pulse.adapters.postgres import ingest_repo
    now = now or ops_service.now_utc()
    digest = hashlib.sha256(content).hexdigest()
    uri = lake.put_bytes(content, owner=f"broker-raw:{digest[:12]}", kind="raw", suffix=".bin").rel_path if lake is not None else f"inline://{digest}"
    conn.execute("INSERT INTO core.source(source_id, display_name, provider_uri, created_at) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                 (source_id, source_id, f"urn:broker:{source_id}", now))
    conn.execute("INSERT INTO core.contract_version(contract_id, schema_version, schema_document, schema_hash, compatibility_policy, created_at)"
                 " VALUES ('pulse.broker_report','1','{}'::jsonb,%s,'strict',%s) ON CONFLICT DO NOTHING", (digest, now))
    conn.execute("INSERT INTO core.source_endpoint(source_id, endpoint_id, source_schema_version, mapping_version, contract_id, schema_version,"
                 " mapping_hash, enabled, transport_kind, stale_after_seconds, policy_document, created_at) VALUES (%s,'fills','1','broker-fill-v1',"
                 "'pulse.broker_report','1',%s,false,'batch',86400,'{}'::jsonb,%s) ON CONFLICT DO NOTHING", (source_id, digest, now))
    rid, _ = ingest_repo.insert_raw_object(conn, source_id=source_id, endpoint_id="fills", source_schema_version="1", source_record_key=digest[:24],
                                           content_hash=digest, storage_uri=uri, mime_type="application/octet-stream", published_at=None,
                                           source_time_precision="unknown", first_seen_at=now, ingested_at=now)
    return rid


def _fee_total(fills: list[dict]) -> Decimal | None:
    total = Decimal(0)
    for f in fills:
        for m in f["fees"]:
            if m["value"] is None or m["unit"] != "CNY":
                return None
            total += Decimal(str(m["value"]))
    return total


def import_broker_fills(conn: psycopg.Connection, principal: UUID, req: dict, now: datetime | None = None) -> dict:
    now = now or ops_service.now_utc()
    account_id = req["account_id"]
    require_grant(conn, principal, account_id, "import")
    if not conn.execute("SELECT 1 FROM ingest.raw_object WHERE raw_object_id=%s", (req["raw_object_id"],)).fetchone():
        raise PortfolioError(404, "NOT_FOUND", "原始回报不存在（必须先落地原始对象）")
    fixed = {k: v for k, v in req.items() if k not in ("idempotency_key", "expected_account_version")}    # the version is a guard, not content
    request_hash = sha256_of(fixed)
    prior = conn.execute("SELECT * FROM execution.broker_import WHERE account_id=%s AND raw_object_id=%s AND mapping_version=%s AND request_hash=%s",
                         (account_id, req["raw_object_id"], req["mapping_version"], request_hash)).fetchone()
    if prior and prior["status"] in ("applied", "invalid_format"):
        return _import_wire(prior)
    import_id = prior["import_id"] if prior else uuid.uuid5(FWD_NS, f"import|{account_id}|{request_hash}")
    if prior is None:
        conn.execute("INSERT INTO execution.broker_import(import_id, account_id, principal_id, raw_object_id, mapping_version, request_hash, status, received_at)"
                     " VALUES (%s,%s,%s,%s,%s,%s,'received',%s)", (import_id, account_id, principal, req["raw_object_id"], req["mapping_version"], request_hash, now))
    bad = _format_problems(req)
    if bad:
        conn.execute("UPDATE execution.broker_import SET status='invalid_format', detail=%s WHERE import_id=%s", (Jsonb({"problems": bad}), import_id))
        return _import_wire(conn.execute("SELECT * FROM execution.broker_import WHERE import_id=%s", (import_id,)).fetchone())
    head = conn.execute("SELECT current_sequence FROM execution.account_head WHERE account_id=%s", (account_id,)).fetchone()
    version_ok = head["current_sequence"] == req["expected_account_version"]
    facts, results = [], {"applied": 0, "duplicate": 0, "pending": 0, "conflict": 0}
    fills = sorted(req["fills"], key=lambda f: (f["effective_at"], f["source_execution_id"]))
    pending: list[tuple[dict, str, str]] = []
    todo: list[dict] = []
    for f in fills:
        h = sha256_of(f)
        ex = conn.execute("SELECT content_hash, status FROM execution.broker_fill_fact WHERE account_id=%s AND source_id=%s AND source_execution_id=%s ORDER BY received_at",
                          (account_id, f["source_id"], f["source_execution_id"])).fetchall()
        if any(x["content_hash"] == h and x["status"] in ("applied", "correction") for x in ex):
            results["duplicate"] += 1
            continue
        if ex and not any(x["content_hash"] == h for x in ex) and f["correction_of_source_execution_id"] is None and f["report_kind"] not in REPORT_FEE:
            _fact(conn, account_id, f, h, import_id, "conflict", "SAME_ID_CHANGED_CONTENT", now)
            results["conflict"] += 1
            pending.append((f, "conflict", "SAME_ID_CHANGED_CONTENT"))
            continue
        todo.append(f)
    if todo and not version_ok:
        for f in todo:
            _fact(conn, account_id, f, sha256_of(f), import_id, "pending_reconciliation", "ACCOUNT_VERSION_CHANGED", now)
            results["pending"] += 1
            pending.append((f, "pending_reconciliation", "ACCOUNT_VERSION_CHANGED"))
        todo = []
    applied_ids: list[UUID] = []
    new_version = head["current_sequence"]
    if todo:
        specs = instrument_specs(conn, {str(f["instrument_id"]) for f in todo})
        cal = calendar(conn)
        outcome, applied_ids, new_version = _kernel_session(conn, account_id, f"broker:{todo[0]['source_id']}", str(import_id), specs, cal,
                                                            lambda k, s: _apply_fills(k, cal, todo, specs, account_id))
        for f, status, reason, eid in outcome:
            _fact(conn, account_id, f, sha256_of(f), import_id, status, reason, now, eid)
            if status in ("applied", "correction"):
                results["applied"] += 1
            else:
                results["pending"] += 1
                pending.append((f, status, reason or "UNKNOWN"))
    status = "reconciliation_pending" if pending else "applied"
    recon = None
    if pending:
        recon = uuid.uuid5(FWD_NS, f"recon|{import_id}")
        conn.execute("INSERT INTO ops.reconciliation_result(reconciliation_id, reconciliation_status, field_path, alignment_policy_version, tolerance_policy_version,"
                     " chosen_observation_id, normalization_trace, independent_origin_count, decided_at) VALUES (%s,'unverifiable','execution.broker_import.fills',"
                     "'broker-fill-align-v1','exact-v1',NULL,%s,1,%s) ON CONFLICT DO NOTHING",
                     (recon, Jsonb({"import_id": str(import_id), "pending": [{"source_execution_id": f["source_execution_id"], "status": s, "reason": r} for f, s, r in pending]}), now))
    conn.execute("UPDATE execution.broker_import SET status=%s, applied_event_ids=%s, reconciliation_result_id=%s, new_account_version=%s, detail=%s WHERE import_id=%s",
                 (status, applied_ids, recon, new_version if applied_ids else None, Jsonb(results), import_id))
    return _import_wire(conn.execute("SELECT * FROM execution.broker_import WHERE import_id=%s", (import_id,)).fetchone())


def _import_wire(row: dict) -> dict:
    return {"import_id": row["import_id"], "status": row["status"],
            "new_account_version": row["new_account_version"] if row["status"] == "applied" or row["applied_event_ids"] else None,
            "reconciliation_result_id": row["reconciliation_result_id"]}


def _fact(conn: psycopg.Connection, account_id: UUID, f: dict, h: str, import_id: UUID, status: str, reason: str | None, now: datetime,
          event_id: UUID | None = None) -> None:
    conn.execute("INSERT INTO execution.broker_fill_fact(account_id, source_id, source_execution_id, content_hash, import_id, fill, status, reason_code,"
                 " account_event_id, received_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (account_id, source_id, source_execution_id, content_hash)"
                 " DO UPDATE SET status=EXCLUDED.status, reason_code=EXCLUDED.reason_code, account_event_id=EXCLUDED.account_event_id, import_id=EXCLUDED.import_id",
                 (account_id, f["source_id"], f["source_execution_id"], h, import_id, Jsonb(contracts.to_wire(f)), status, reason, event_id, now))


def _format_problems(req: dict) -> list[str]:
    out = []
    if req["mapping_version"] not in MAPPING_VERSIONS:
        out.append(f"unsupported mapping_version {req['mapping_version']}")
    for f in req["fills"]:
        k = f["report_kind"]
        if k in REPORT_FILL:
            if f["last_fill_quantity"] is None or f["last_fill_price_raw"] is None:
                out.append(f"{f['source_execution_id']}: fill without quantity or price")
            if f["side"] not in ("buy", "sell"):
                out.append(f"{f['source_execution_id']}: unknown side {f['side']!r}")
        elif k not in REPORT_CANCEL + REPORT_FEE:
            out.append(f"{f['source_execution_id']}: unsupported report_kind {k!r}")
        if f["fee_effect_mode"] not in FEE_MODES:
            out.append(f"{f['source_execution_id']}: unknown fee_effect_mode {f['fee_effect_mode']!r}")
    return out


def _apply_fills(kernel: ExecutionKernel, cal, fills: list[dict], specs: dict, account_id: UUID) -> list[tuple[dict, str, str | None, UUID | None]]:
    out: list[tuple[dict, str, str | None, UUID | None]] = []
    by_order: dict[str, list[dict]] = {}
    for f in fills:
        if f["report_kind"] in REPORT_FILL:
            by_order.setdefault(f["source_order_id"], []).append(f)
    reserved: set[str] = set()
    for f in fills:
        oid = f"broker:{f['source_id']}:{f['source_order_id']}"
        at = datetime.fromisoformat(f["effective_at"].replace("Z", "+00:00")) if isinstance(f["effective_at"], str) else f["effective_at"]
        day = at.astimezone(CST).date()
        iid = str(f["instrument_id"])
        try:
            if iid not in specs:
                out.append((f, "pending_reconciliation", "INSTRUMENT_UNKNOWN", None))
                continue
            if day < kernel.ledger.day:
                out.append((f, "pending_reconciliation", "FILL_BEFORE_LEDGER_DAY", None))
                continue
            _advance_to(kernel, cal, day)
            if f["report_kind"] in REPORT_CANCEL:
                if oid not in kernel.ledger.orders:
                    out.append((f, "pending_order", "ORDER_NOT_KNOWN", None))
                else:
                    kernel.confirm_cancel(oid, f["cumulative_quantity"] or kernel.ledger.orders[oid].filled, at)
                    out.append((f, "applied", None, stable_uuid(account_id, "event", f"cancel:{oid}")))
                continue
            if f["report_kind"] in REPORT_FEE:
                out.append((f, "pending_reconciliation", "FEE_CORRECTION_NEEDS_ORIGINAL_FILL", None))
                continue
            price, qty = Decimal(str(f["last_fill_price_raw"])), int(f["last_fill_quantity"])
            if oid not in kernel.ledger.orders and oid not in reserved:
                group = by_order[f["source_order_id"]]
                total = max([g["cumulative_quantity"] or 0 for g in group] + [sum(g["last_fill_quantity"] for g in group)])
                prices = [Decimal(str(g["last_fill_price_raw"])) for g in group]
                limit = max(prices) if f["side"] == "buy" else min(prices)
                res = kernel.submit(OrderIntent(oid, iid, f["side"], total, limit), expected_account_version=kernel.ledger.account_version, at=at)
                if not res.accepted:
                    out.append((f, "pending_reconciliation", f"RESERVE_REJECTED:{res.reason}", None))
                    continue
                reserved.add(oid)
            actual = None
            if f["fee_effect_mode"] == "actual":
                actual = _fee_total([f])
            rep = ExecutionReport(f"{f['source_id']}:{f['source_execution_id']}", oid, qty, price, at, fee=actual)
            if oid in kernel.ledger.orders and kernel.ledger.orders[oid].remaining < qty:
                out.append((f, "pending_reconciliation", "ORDER_RESERVATION_EXCEEDED", None))
                continue
            r = kernel.ingest_execution(rep)
            out.append((f, "applied", None, stable_uuid(account_id, "event", f"fill:{rep.source_fill_id}")))
        except (ValueError, KeyError, AssertionError, LookupError, InvalidOperation) as exc:
            out.append((f, "pending_reconciliation", f"KERNEL_REJECTED:{type(exc).__name__}:{str(exc)[:80]}", None))
    return out


# ---- read models: orders / performance (D27) -------------------------------------------------------------------------------------
def _events_upto(conn: psycopg.Connection, account_id: UUID, version: int) -> tuple[dict, list[dict]]:
    head = conn.execute("SELECT current_sequence FROM execution.account_head WHERE account_id=%s", (account_id,)).fetchone()
    if head is None or version > head["current_sequence"]:
        raise PortfolioError(409, "ACCOUNT_VERSION_UNKNOWN", f"账户没有版本 {version}")
    ev = LedgerRepo(conn).events(account_id)
    return ev[0]["payload"], [e for e in ev[1:] if e["sequence"] <= version]


def _ctx_hash(**kw) -> str:
    return sha256_of(kw)


def orders_context(account_id: UUID, version: int, window: dict, knowledge_as_of: datetime) -> str:
    return _ctx_hash(kind="orders", account_id=account_id, account_version=version, window=window, knowledge_as_of=knowledge_as_of, policy=RM.ORDER_POLICY)


def performance_context(account_id: UUID, version: int, window: dict, knowledge_as_of: datetime, refs: list[dict], perf: str, val: str) -> str:
    return _ctx_hash(kind="performance", account_id=account_id, account_version=version, window=window, knowledge_as_of=knowledge_as_of,
                     refs=sorted(refs, key=lambda r: str(r["snapshot_id"])), perf=perf, val=val)


def _id_of(account_id: UUID):
    return lambda kind, text: stable_uuid(account_id, kind, text)


def build_readmodels(conn: psycopg.Connection, account_id: UUID, version: int, window: dict, knowledge_as_of: datetime, refs: list[dict],
                     perf_policy: str, val_policy: str = VALUATION_POLICY, kinds: tuple[str, ...] = ("orders", "performance")) -> dict:
    import re
    out: dict[str, str] = {}
    initial, events = _events_upto(conn, account_id, version)
    start, end = window["start"], window["end"]
    if "orders" in kinds:
        orders, cov = RM.orders_from_events([{**e} for e in events], until_sequence=version, knowledge_as_of=knowledge_as_of, window_start=start,
                                            window_end=end, id_of=_id_of(account_id))
        art = ops_service.put_artifact(conn, kind="account-orders", contract_id=READMODEL_CONTRACT["orders"], schema_version="1",
                                       body=contracts.to_wire({"orders": orders, "coverage": cov}), scope_account=account_id)
        h = orders_context(account_id, version, window, knowledge_as_of)
        _readmodel_row(conn, account_id, version, "orders", h, window, knowledge_as_of, [], sha256_of({"p": RM.ORDER_POLICY}), art)
        out["orders"] = h
    if "performance" in kinds:
        m = re.match(PERF_POLICY_RE, perf_policy)
        if not m or val_policy != VALUATION_POLICY:
            raise PortfolioError(400, "INVALID_CONTRACT", "未知的绩效或估值政策版本")
        snap_ids = [r["snapshot_id"] for r in refs] or None
        for r in refs:
            man = conn.execute("SELECT manifest_state, content_hash FROM market.dataset_manifest WHERE snapshot_id=%s", (r["snapshot_id"],)).fetchone()
            if man is None or man["manifest_state"] != "READY" or man["content_hash"] != r["content_hash"]:
                raise PortfolioError(409, "STALE_INPUT", "估值快照不存在、未就绪或哈希不符")
        factor = int(m.group(1))
        profile = None if factor == 1 else scaled(user_account_2026(), factor)
        perf = RM.compute_performance(initial, events, start=start.astimezone(CST).date(), end=end.astimezone(CST).date(),
                                      marks_at=marks_provider(conn, snap_ids), profile=profile)
        miss = ("mark_missing:" + ",".join(sorted(perf.missing))) if perf.missing else (perf.reasons[0] if perf.reasons else None)
        d = lambda v: None if v is None else Decimal(v)  # noqa: E731
        metrics = [
            {"name": "time_weighted_return", "definition_version": perf_policy, "value": perf.twr, "unit": "ratio", "sample_count": perf.segments,
             "missing_reason": None if perf.twr is not None else (miss or "no_capital_in_window")},
            {"name": "nav_end_cny", "definition_version": val_policy, "value": perf.nav_end, "unit": "CNY", "sample_count": perf.held_at_end,
             "missing_reason": None if perf.nav_end is not None else (miss or "nav_unavailable")},
            {"name": "transaction_cost_cny", "definition_version": perf_policy, "value": d(perf.fees), "unit": "CNY", "sample_count": perf.trades, "missing_reason": None},
            {"name": "realized_gross_cny", "definition_version": perf_policy, "value": d(perf.realized), "unit": "CNY", "sample_count": perf.trades, "missing_reason": None},
            {"name": "net_external_capital_cny", "definition_version": perf_policy, "value": d(perf.net_capital), "unit": "CNY", "sample_count": 1, "missing_reason": None}]
        cov = {"policy_version": val_policy, "expected_count": perf.held_at_end, "observed_count": perf.held_at_end - len(perf.missing),
               "missing_count": len(perf.missing), "unverifiable_reasons": list(perf.reasons)}
        body = {"account_id": account_id, "account_version": version, "window": window, "performance_policy_version": perf_policy,
                "valuation_policy_version": val_policy, "valuation_snapshot_refs": refs, "metrics": metrics, "coverage": cov}
        art = ops_service.put_artifact(conn, kind="account-performance", contract_id=READMODEL_CONTRACT["performance"], schema_version="1",
                                       body=contracts.to_wire(body), scope_account=account_id)
        h = performance_context(account_id, version, window, knowledge_as_of, refs, perf_policy, val_policy)
        _readmodel_row(conn, account_id, version, "performance", h, window, knowledge_as_of, refs, sha256_of({"p": perf_policy, "v": val_policy}), art)
        out["performance"] = h
    return out


def _readmodel_row(conn, account_id, version, kind, h, window, kao, refs, policy_hash, art) -> None:
    conn.execute("INSERT INTO execution.account_readmodel(account_id, account_version, projection_kind, query_context_hash, projection_contract_id,"
                 " projection_schema_version, window_start, window_end, knowledge_as_of, input_snapshot_refs, policy_hash, result_artifact)"
                 " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                 (account_id, version, kind, h, READMODEL_CONTRACT[kind], "1", window["start"], window["end"], kao, Jsonb(contracts.to_wire(refs)), policy_hash, art["ref"]))


def _readmodel_body(conn: psycopg.Connection, account_id: UUID, version: int, kind: str, h: str) -> dict:
    row = conn.execute("SELECT a.body, a.ref, a.content_hash, a.contract_id, a.schema_version FROM execution.account_readmodel m JOIN ops.ext_artifact a ON a.ref=m.result_artifact"
                       " WHERE m.account_id=%s AND m.account_version=%s AND m.projection_kind=%s AND m.query_context_hash=%s", (account_id, version, kind, h)).fetchone()
    if row is None:
        raise PortfolioError(409, "RESULT_NOT_READY", "该账户读模型尚未生成")
    return row


def query_account_orders(conn: psycopg.Connection, principal: UUID, req: dict, limit: int, offset: int) -> tuple[list, dict, int | None]:
    require_grant(conn, principal, req["account_id"], "read")
    h = orders_context(req["account_id"], req["account_version"], req["window"], req["knowledge_as_of"])
    body = _readmodel_body(conn, req["account_id"], req["account_version"], "orders", h)["body"]
    orders = body["orders"]
    return orders[offset:offset + limit], body["coverage"], (offset + limit if offset + limit < len(orders) else None)


def query_account_performance(conn: psycopg.Connection, principal: UUID, req: dict) -> dict:
    require_grant(conn, principal, req["account_id"], "read")
    h = performance_context(req["account_id"], req["account_version"], req["window"], req["knowledge_as_of"], req["valuation_snapshot_refs"],
                            req["performance_policy_version"], req["valuation_policy_version"])
    row = _readmodel_body(conn, req["account_id"], req["account_version"], "performance", h)
    return {**row["body"], "artifact": {"ref": row["ref"], "content_hash": row["content_hash"], "contract_id": row["contract_id"], "schema_version": row["schema_version"]}}


def readmodel_contexts(conn: psycopg.Connection, principal: UUID, account_id: UUID) -> list[dict]:
    """Built contexts of one account (support route): the exact parameters under which the typed queries answer."""
    require_grant(conn, principal, account_id, "read")
    return [{"projection_kind": r["projection_kind"], "account_version": r["account_version"], "window": {"start": r["window_start"], "end": r["window_end"]},
             "knowledge_as_of": r["knowledge_as_of"], "valuation_snapshot_refs": r["input_snapshot_refs"]}
            for r in conn.execute("SELECT * FROM execution.account_readmodel WHERE account_id=%s ORDER BY created_at DESC LIMIT 50", (account_id,))]


@ops_service.register_handler("PortfolioService.build_readmodels")
def _job_build_readmodels(conn, run, args) -> dict:
    p = args["params"]
    win = {"start": datetime.fromisoformat(p["window_start"].replace("Z", "+00:00")), "end": datetime.fromisoformat(p["window_end"].replace("Z", "+00:00"))}
    kao = datetime.fromisoformat(p["knowledge_as_of"].replace("Z", "+00:00"))
    return build_readmodels(conn, UUID(p["account_id"]), int(p["account_version"]), win, kao, p.get("valuation_snapshot_refs") or [],
                            p.get("performance_policy_version", "twr-cost-x1-v1"), p.get("valuation_policy_version", VALUATION_POLICY))


# ---- opportunities (queryOpportunities, D17) ----------------------------------------------------------------------------------------
def query_opportunities(conn: psycopg.Connection, principal: UUID, account_id: UUID | None, experiment_id: UUID | None, as_of: datetime,
                        limit: int, offset: int) -> tuple[dict, int | None]:
    if account_id is None and experiment_id is None:
        raise PortfolioError(400, "INVALID_CONTRACT", "account_id 与 experiment_id 至少给一个")
    if account_id is not None:
        require_grant(conn, principal, account_id, "read")
    elif not conn.execute("SELECT 1 FROM research.experiment_run WHERE experiment_id=%s", (experiment_id,)).fetchone():
        raise PortfolioError(404, "NOT_FOUND", "实验不存在")
    rows = conn.execute(
        "SELECT DISTINCT ON (opportunity_id) * FROM research.opportunity_event WHERE (%s::uuid IS NULL OR account_id=%s) AND (%s::uuid IS NULL OR experiment_id=%s)"
        " AND available_at <= %s ORDER BY opportunity_id, opportunity_version DESC", (account_id, account_id, experiment_id, experiment_id, as_of)).fetchall()
    if account_id is not None and experiment_id is not None and any(r["account_id"] != account_id or r["experiment_id"] != experiment_id for r in rows):
        raise PortfolioError(400, "INVALID_CONTRACT", "账户与实验不关联")
    rows.sort(key=lambda r: (r["available_at"], str(r["opportunity_id"])))
    page = rows[offset:offset + limit]
    items = [{"opportunity_id": r["opportunity_id"], "opportunity_version": r["opportunity_version"], "instrument_id": r["instrument_id"], "side": r["side"],
              "scenario_id": r["scenario_id"], "state": r["state"], "target_quantity": r["target_quantity"], "filled_quantity": r["filled_quantity"],
              "reason_code": r["reason_code"], "account_event_ids": r["account_event_ids"], "available_at": r["available_at"], "intent_artifact": r["intent_artifact"]}
             for r in page]
    states = {}
    for r in rows:
        states[r["state"]] = states.get(r["state"], 0) + 1
    body = {"as_of": as_of, "account_id": account_id, "experiment_id": experiment_id, "opportunities": items,
            "denominators": {"total": len(rows), "by_state": states},
            "incremental_vs_hold": _hold_comparison(conn, account_id, as_of) if account_id is not None else None}
    art = ops_service.put_artifact(conn, kind="opportunities", contract_id="pulse.ext_c.opportunities", schema_version="1", body=contracts.to_wire(body),
                                   owner=None if account_id is None else None, scope_account=account_id)
    return art, (offset + limit if offset + limit < len(rows) else None)


def _hold_comparison(conn: psycopg.Connection, account_id: UUID, as_of: datetime) -> dict | None:
    """Active vs hold on the same account: sold-and-not-rebought inventory is priced at the latest raw close (shortfall counted)."""
    initial, events = _events_upto(conn, account_id, conn.execute("SELECT current_sequence FROM execution.account_head WHERE account_id=%s", (account_id,)).fetchone()["current_sequence"])
    events = [e for e in events if datetime.fromisoformat(e["effective_at"]) <= as_of]
    if not any(e["kind"] == "fill.booked" for e in events):
        return {"status": "no_trades", "incremental_pnl_cny": None}
    syms = {s for e in events if e["kind"] == "order.reserve" for s in [e["payload"]["instrument_id"]]} | set(initial.get("positions", {}))
    marks = marks_provider(conn, None)(as_of.astimezone(CST).date(), syms)
    if any(v is None for v in marks.values()):
        return {"status": "mark_missing", "incremental_pnl_cny": None, "missing": sorted(s for s, v in marks.items() if v is None)}
    try:
        c = compare_with_hold(initial, events, marks)
    except (ValueError, AssertionError) as exc:
        return {"status": "not_comparable", "incremental_pnl_cny": None, "reason": str(exc)[:120]}
    return {"status": "ok", "incremental_pnl_cny": c.incremental_pnl, "active_fees_cny": c.active_fees, "baseline_fees_cny": c.baseline_fees,
            "inventory_shortfall": dict(c.inventory_shortfall), "inventory_value_delta_cny": c.inventory_value_delta, "bridge_holds": c.bridge_holds()}


# ---- forward books ---------------------------------------------------------------------------------------------------------------
def forward_account_id(book_key: str) -> UUID:
    return uuid.uuid5(FWD_NS, f"forward-book|{book_key}")


def register_forward_books(conn: psycopg.Connection, principals: list[UUID], now: datetime | None = None) -> list[UUID]:
    from pulse.application.account_service import open_paper_account
    now = now or ops_service.now_utc()
    out = []
    for key, rule in FB.RULES.items():
        aid = forward_account_id(key)
        open_paper_account(conn, trading_day=FB.FROZEN_ON, account_id=aid, initial_cash=FB.OPENING_CASH)
        conn.execute("INSERT INTO execution.tracked_account(account_id, book_key, tracking_kind, label, rule, frozen_on) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                     (aid, key, FB.TRACKING_KIND, FB.LABELS[key], Jsonb(rule), FB.FROZEN_ON))
        for p in principals:
            if not has_grant(conn, p, aid, "read"):
                grant(conn, p, aid, "read", now)
        out.append(aid)
    return out


def _current_ranking(conn: psycopg.Connection, horizon: str) -> tuple[dict, UUID, list[tuple[str, int]]]:
    slot = conn.execute("SELECT current_release_id FROM decision.release_slot WHERE slot_key='main'").fetchone()
    if slot is None or slot["current_release_id"] is None:
        raise LookupError("no published release in slot main")
    rel = conn.execute("SELECT * FROM decision.release WHERE release_id=%s", (slot["current_release_id"],)).fetchone()
    run = conn.execute("SELECT ranking_id FROM decision.ranking_run WHERE release_id=%s AND ranking_scope='market' AND horizon_id=%s", (rel["release_id"], horizon)).fetchone()
    if run is None:
        raise LookupError(f"release has no market ranking for {horizon}")
    ent = [(str(r["instrument_id"]), r["alpha_rank"]) for r in conn.execute("SELECT instrument_id, alpha_rank FROM decision.ranking_entry WHERE ranking_id=%s ORDER BY alpha_rank", (run["ranking_id"],))]
    return rel, run["ranking_id"], ent


def _bar(conn: psycopg.Connection, iid: str, day: date) -> dict | None:
    return conn.execute("SELECT open_price_raw, close_price_raw, volume_shares, exchange_reference_price_raw FROM market.bar_projection WHERE instrument_id=%s AND timeframe='1d'"
                        " AND trade_date=%s AND open_price_raw IS NOT NULL ORDER BY is_final DESC, bar_open_time DESC LIMIT 1", (UUID(iid), day)).fetchone()


def _holdings_with_entry(conn: psycopg.Connection, account_id: UUID) -> list[FB.Holding]:
    led = LedgerRepo(conn).load(account_id)
    entry: dict[str, date] = {}
    for e in LedgerRepo(conn).events(account_id):
        if e["kind"] == "fill.booked":
            oid = e["payload"]["order_id"]
            if oid.endswith(":buy"):
                entry[oid.split(":")[3]] = datetime.fromisoformat(e["effective_at"]).astimezone(CST).date()
    return [FB.Holding(s, h.quantity, entry.get(s, led.day)) for s, h in led.holdings.items() if h.quantity]


def _execute_day(conn: psycopg.Connection, account_id: UUID, book_key: str, row: dict, exec_day: date, cal, per_name: Decimal) -> dict:
    """Run the orders decided on ``row['trade_date']`` at the raw open of ``exec_day`` through the kernel."""
    planned = row["planned_buys"]
    specs = instrument_specs(conn, {b["instrument_id"] for b in planned["buys"]} | set(planned["sells"]))
    results = {"buys": [], "sells": []}

    def action(kernel: ExecutionKernel, session):
        _advance_to(kernel, cal, exec_day)
        at = datetime.combine(exec_day, time(9, 30), CST)
        lim = PriceLimitRegistry()
        for side, ids in (("sell", planned["sells"]), ("buy", [b["instrument_id"] for b in planned["buys"]])):
            for iid in ids:
                bar = _bar(conn, iid, exec_day)
                rec = {"instrument_id": iid, "filled": 0, "reason": None}
                results["sells" if side == "sell" else "buys"].append(rec)
                if bar is None or not bar["volume_shares"]:
                    rec["reason"] = "NO_BAR_OR_SUSPENDED"
                    continue
                price = bar["open_price_raw"]
                ref = bar["exchange_reference_price_raw"]
                if ref is not None:
                    try:
                        band = lim.band(specs[iid], exec_day, ref)
                        if side == "buy" and price >= band.limit_up:
                            rec["reason"] = "LIMIT_UP_OPEN"
                            continue
                        if side == "sell" and price <= band.limit_down:
                            rec["reason"] = "LIMIT_DOWN_OPEN"
                            continue
                    except Exception as exc:  # noqa: BLE001 - unverified rule: record, do not guess
                        rec["reason"] = f"PRICE_RULE_UNVERIFIED:{type(exc).__name__}"
                        continue
                if side == "sell":
                    h = kernel.ledger.holdings.get(iid)
                    qty = h.quantity if h else 0
                else:                                        # the quantity decided on the signal close is the intent; the open only sets the price
                    qty = next((b["target_quantity"] for b in planned["buys"] if b["instrument_id"] == iid), 0)
                if qty <= 0:
                    rec["reason"] = "ZERO_QUANTITY"
                    continue
                oid = f"fwd:{book_key}:{exec_day.isoformat()}:{iid}:{side}"
                res = kernel.submit(OrderIntent(oid, iid, side, qty, price), expected_account_version=kernel.ledger.account_version, at=at)
                if not res.accepted:
                    rec["reason"] = f"SUBMIT_REJECTED:{res.reason}"
                    continue
                kernel.ingest_execution(ExecutionReport(f"{oid}:fill", oid, qty, price, at))
                rec["filled"], rec["price"] = qty, str(price)
        return results

    _, applied, version = _kernel_session(conn, account_id, f"forward:{book_key}", f"exec:{exec_day}", specs, cal, action)
    return {**results, "event_ids": [str(x) for x in applied], "account_version": version}


def _opportunity(conn: psycopg.Connection, account_id: UUID, book_key: str, signal_day: date, b: dict, state: str, filled: int, reason: str | None,
                 version: int, now: datetime, events: list[str]) -> None:
    oid = uuid.uuid5(FWD_NS, f"opp|{book_key}|{signal_day}|{b['instrument_id']}")
    art = ops_service.put_artifact(conn, kind="opportunity-intent", contract_id="pulse.ext_c.opportunity_intent", schema_version="1",
                                   body={"book_key": book_key, "signal_day": signal_day.isoformat(), "instrument_id": b["instrument_id"],
                                         "alpha_rank": b["alpha_rank"], "rule": "forward-book-v1"}, scope_account=account_id)
    conn.execute("INSERT INTO research.opportunity_event(opportunity_id, opportunity_version, account_id, instrument_id, side, intent_artifact, scenario_id, state,"
                 " target_quantity, filled_quantity, reason_code, account_event_ids, available_at) VALUES (%s,%s,%s,%s,'buy',%s,'next_open_no_queue_model',%s,%s,%s,%s,%s,%s)"
                 " ON CONFLICT DO NOTHING", (oid, version, account_id, UUID(b["instrument_id"]), art["ref"], state, b["target_quantity"], filled, reason,
                                              [UUID(x) for x in events], now))


def forward_daily(conn: psycopg.Connection, now: datetime | None = None) -> dict:
    """One pass over every registered forward book: settle yesterday's decisions when the next bar exists, then decide the new session."""
    now = now or ops_service.now_utc()
    books = conn.execute("SELECT * FROM execution.tracked_account ORDER BY book_key").fetchall()
    if not books:
        return {"books": 0, "note": "no forward book registered"}
    cal = calendar(conn)
    out: dict[str, Any] = {}
    for b in books:
        out[b["book_key"]] = _forward_one(conn, b, cal, now)
    return out


def _forward_one(conn: psycopg.Connection, book: dict, cal: FB.HybridCalendar, now: datetime) -> dict:
    rule, aid, key = book["rule"], book["account_id"], book["book_key"]
    per_name = Decimal(rule["per_name_cny"])
    rel, ranking_id, ranking = _current_ranking(conn, rule["horizon_id"])
    last_bar_day = conn.execute("SELECT max(trade_date) d FROM market.bar_projection WHERE timeframe='1d' AND close_price_raw IS NOT NULL"
                                " AND available_at <= %s", (rel["decision_as_of"],)).fetchone()["d"]
    if last_bar_day is None:
        return {"status": "no_bars"}
    signal_day = last_bar_day
    prev = conn.execute("SELECT * FROM execution.tracked_book_day WHERE account_id=%s ORDER BY trade_date DESC LIMIT 1", (aid,)).fetchone()
    if prev is not None and prev["trade_date"] >= signal_day:
        return {"status": "already_decided", "trade_date": prev["trade_date"].isoformat()}
    notes: list[str] = []
    if prev is not None and not prev["planned_buys"].get("executed"):
        exec_day = cal.next_trading_day(prev["trade_date"])
        latest_any = conn.execute("SELECT max(trade_date) d FROM market.bar_projection WHERE timeframe='1d'").fetchone()["d"]
        if latest_any is None or exec_day > latest_any:
            return {"status": "awaiting_bar", "exec_day": exec_day.isoformat()}
        res = _execute_day(conn, aid, key, prev, exec_day, cal, per_name)
        planned = {**prev["planned_buys"], "executed": True, "exec_day": exec_day.isoformat(), "results": {"buys": res["buys"], "sells": res["sells"]}}
        conn.execute("UPDATE execution.tracked_book_day SET planned_buys=%s WHERE account_id=%s AND trade_date=%s", (Jsonb(planned), aid, prev["trade_date"]))
        got = {r["instrument_id"]: r for r in res["buys"]}
        for bb in prev["planned_buys"]["buys"]:
            r = got.get(bb["instrument_id"], {"filled": 0, "reason": "NOT_EXECUTED"})
            filled = r["filled"]
            _opportunity(conn, aid, key, prev["trade_date"], bb, "filled" if filled else "unfilled", min(filled, bb["target_quantity"]), r["reason"], 2, now, res["event_ids"] if filled else [])
        notes.append(f"executed decisions of {prev['trade_date']} at {exec_day} open")
    holdings = _holdings_with_entry(conn, aid)
    plan = FB.plan_day(rule, signal_day, ranking, holdings, set(), cal)
    buys = []
    for iid, rank in plan.buys:
        bar = _bar(conn, iid, signal_day)
        qty = FB.lot_quantity(per_name, bar["close_price_raw"]) if bar else 0
        if qty > 0:
            buys.append({"instrument_id": iid, "alpha_rank": rank, "target_quantity": qty})
        else:
            notes.append(f"{iid}: no close on {signal_day}, not planned")
    led = LedgerRepo(conn).load(aid)
    marks = marks_provider(conn, None)(signal_day, {s for s, h in led.holdings.items() if h.quantity})
    absent = [s for s, v in marks.items() if v is None]
    snap = led.snapshot_partial(marks)
    hold_wire = [{"instrument_id": s, "quantity": h.quantity, "cost_cny": str(snap["positions"][s]["cost"]) if s in snap["positions"] else None,
                  "mark_price": None if marks.get(s) is None else str(marks[s])} for s, h in led.holdings.items() if h.quantity]
    planned = {"buys": buys, "sells": list(plan.sells), "executed": False, "reasons": plan.reasons, "assumed_calendar_after": cal.assumed_after.isoformat()}
    head = conn.execute("SELECT current_sequence FROM execution.account_head WHERE account_id=%s", (aid,)).fetchone()
    conn.execute("INSERT INTO execution.tracked_book_day(account_id, trade_date, release_id, ranking_id, planned_buys, holdings, nav_cny, cash_cny, missing_marks,"
                 " account_version, computed_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                 (aid, signal_day, rel["release_id"], ranking_id, Jsonb(planned), Jsonb(hold_wire), snap["nav"], snap["cash_settled"], len(absent), head["current_sequence"], now))
    for bb in buys:
        _opportunity(conn, aid, key, signal_day, bb, "triggered", 0, None, 1, now, [])
    return {"status": "decided", "trade_date": signal_day.isoformat(), "buys": len(buys), "sells": len(plan.sells), "nav_cny": None if snap["nav"] is None else str(snap["nav"]),
            "missing_marks": len(absent), "notes": notes}


def forward_books_wire(conn: psycopg.Connection, principal: UUID) -> dict:
    out = []
    for b in conn.execute("SELECT * FROM execution.tracked_account ORDER BY book_key"):
        if not has_grant(conn, principal, b["account_id"], "read"):
            continue
        days = conn.execute("SELECT * FROM execution.tracked_book_day WHERE account_id=%s ORDER BY trade_date", (b["account_id"],)).fetchall()
        head = conn.execute("SELECT current_sequence, base_currency FROM execution.account_head WHERE account_id=%s", (b["account_id"],)).fetchone()
        ids = {h["instrument_id"] for d in days[-1:] for h in d["holdings"]} | {x["instrument_id"] for d in days[-1:] for x in d["planned_buys"]["buys"]}
        names = _names(conn, ids)
        latest = None
        if days:
            d = days[-1]
            latest = {"trade_date": d["trade_date"], "release_id": d["release_id"], "nav_cny": d["nav_cny"], "cash_cny": d["cash_cny"], "missing_marks": d["missing_marks"],
                      "holdings": [{**h, **names.get(h["instrument_id"], {"code": None, "name": None})} for h in d["holdings"]],
                      "planned_buys": [{"instrument_id": x["instrument_id"], "alpha_rank": x["alpha_rank"], **names.get(x["instrument_id"], {"code": None, "name": None})} for x in d["planned_buys"]["buys"]]}
        out.append({"account_id": b["account_id"], "book_key": b["book_key"], "label": b["label"], "status_label": b["status_label"], "tracking_kind": b["tracking_kind"],
                    "ranking_weight": b["ranking_weight"], "rule": b["rule"], "account_version": head["current_sequence"], "base_currency": head["base_currency"],
                    "frozen_on": b["frozen_on"], "latest": latest,
                    "history": [{"trade_date": d["trade_date"], "nav_cny": d["nav_cny"], "positions": len(d["holdings"])} for d in days],
                    "notes": ["本书仅用于前向检验，不参与排名权重", "权益曲线闸门未建模（预登记比较为无闸门对无闸门）",
                              "成交按次日原始开盘价，无排队模型；涨停开盘买入记为未成交", "从首个可见发布日起算，不回填"]})
    return {"books": out}


def _names(conn: psycopg.Connection, ids: set[str]) -> dict[str, dict]:
    from pulse.application.alert_service import _names as nm
    return nm(conn, ids)


@ops_service.register_handler("PortfolioService.forward_daily")
def _job_forward(conn, run, args) -> dict:
    return forward_daily(conn)
