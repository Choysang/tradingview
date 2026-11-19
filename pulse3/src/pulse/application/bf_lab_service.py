"""Backfill lane ``lab``, part 2: the tear sheet served for replays and paper books, and the batch comparison job (LEGACY_TRIAGE #160 #161 #36 #77).

Tear sheet (#160 / #161): ``domain.lab.tearsheet`` computes; this module feeds it from what pulse3 already stores and puts it on the wire.
  * playbook replay: the stored x1 replay of ``bf_research_service`` (daily NAV net of the user cost profile, trades with gross prices). Trade pnl is
    quantity x (exit - entry) price, GROSS of fees (the fees are inside the NAV); an open trade has no pnl. Read through ``load_replay``; nothing
    is recomputed or stored here.
  * paper book: ``execution.tracked_book_day.nav_cny`` of a forward book the principal may read (a read grant, like ``/v1/x/c/forward-books``).
    The book stores NAV per day but no trade list, so the trade figures and exposure are ``trade_list_unavailable``, never 0.
  * wire values are decimal strings with at most 12 fraction digits; a value outside the contract's range becomes ``null`` with reason ``out_of_range``.
Batch comparison (#36 / #77): ONE playbook replayed on the top N (at most 10) TRUE rows of a screen run, one instrument at a time.
  * a registered manual job (``LAB_JOB``, ops.job_definition, ``manual_trigger``) queued by a command with an Idempotency-Key; the worker claims it,
    replays, stores an immutable owner-scoped result artifact (``lab-batch-result``) and records it as the job's output. The archive is the list of
    the principal's runs.
  * point in time: the panel is loaded at the screen run's ``decision_as_of`` (``screen_service.load_panel``: only bars with available_at <= the cutoff
    and a research snapshot READY by then), the replay window ends at the last session visible then, the playbook's cross-sectional columns are
    computed on the screen's whole universe at that cutoff. The result says whether the research snapshot is the one the screen run used.
  * each instrument is replayed ALONE with the whole account (max 1 position), through the same ``replay`` / ExecutionKernel / cost profile as the
    playbook experiments (``playbook_service`` machinery, not changed). A playbook with a permanent data gap (``requires``) is refused at submit time.
  * The instruments were chosen with data up to the cutoff and the replay window ends at the cutoff: this measures how the rule did on names that look
    strong NOW, it is not an out-of-sample test. The result says so.
Not covered: portfolio-level combination of the N replays, the x2 / x3 cost stress, parameter sweeps, blind slices.
"""
from __future__ import annotations

import base64
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from pulse import contracts
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres._canon import sha256_of
from pulse.adapters.postgres.jobs import PgJobStore
from pulse.application import bf_research_service as BFR
from pulse.application import ops_service as OPS
from pulse.application import playbook_service as PS
from pulse.application import portfolio_service as PORT
from pulse.application import screen_service as S
from pulse.application.ports.jobs import JobDefinition
from pulse.domain.lab import tearsheet as T
from pulse.domain.market_rules.costs import user_account_2026
from pulse.domain.playbooks import exits as EX
from pulse.domain.playbooks import library as L
from pulse.domain.playbooks.builder import prepare, run_signal
from pulse.domain.playbooks.replay import PRICE_BASIS, ReplayConfig, replay

D = Decimal
LANE = "lab"
CONTRACT_ID = contracts.backfill_id(LANE)
SCHEMA_VERSION = "1.0.0"
RESULT_VERSION = "lab-batch-result-v1"
RESULT_KIND = "lab-batch-result"
PARAM_KIND = "lab-batch-param"
LAB_JOB = UUID("3a7f0c52-6e1d-4b8a-9d24-5c0e8b71f3a6")
JOB_OPERATION = "LabService.run_batch_compare"
OPERATION_ID = "labBatchCompare"
RESOURCE_TYPE = "lab_batch_run"
MAX_TOP_N = 10
DEFAULT_WINDOW = 250
MIN_WINDOW, MAX_WINDOW = 60, 750
WIRE_LIMIT = D(10) ** 26
log = logging.getLogger(__name__)


class LabError(Exception):
    """Maps to an API error: (http status, code, message)."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# --------------------------------------------------------------------------------------------------------
# wire form of a tear sheet
# --------------------------------------------------------------------------------------------------------
def q12(v: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 60
        return v.quantize(D("1e-12"), rounding=ROUND_HALF_EVEN)


def metric_wire(m: T.Metric) -> dict[str, Any]:
    if m.value is None:
        return {"value": None, "reason": m.reason or "not_computable"}
    if abs(m.value) >= WIRE_LIMIT:
        return {"value": None, "reason": "out_of_range"}
    return {"value": q12(m.value), "reason": None}


def tearsheet_wire(ts: T.TearSheet) -> dict[str, Any]:
    dd = ts.longest_drawdown
    return {"version": ts.version, "periods_per_year": ts.periods_per_year, "sessions": ts.sessions, "returns": ts.returns,
            "missing_nav_sessions": ts.missing_nav_sessions, "first_session": ts.first_session, "last_session": ts.last_session,
            "trades": None if ts.trades_closed is None else {"closed": ts.trades_closed, "open": ts.trades_open, "breakeven": ts.trades_breakeven},
            "longest_drawdown": None if dd is None else {"sessions": dd.sessions, "peak_session": dd.peak_session, "recovery_session": dd.recovery_session,
                                                          "recovered": dd.recovered},
            "metrics": {k: metric_wire(v) for k, v in ts.metrics.items()}}


# --------------------------------------------------------------------------------------------------------
# tear sheet of a stored replay / a paper book
# --------------------------------------------------------------------------------------------------------
def trades_from_replay(trades: list[dict[str, Any]]) -> list[T.TradeIn]:
    out = []
    for t in trades:
        closed = t["status"] == "closed" and t["exit_price"] is not None
        pnl = D(t["quantity"]) * (D(str(t["exit_price"])) - D(str(t["entry_price"]))) if closed else None
        out.append(T.TradeIn(t["entry_day"], t["exit_day"] if closed else None, pnl, D(str(t["gross_return"])) if closed and t["gross_return"] is not None else None))
    return out


def nav_from_replay(nav: list[dict[str, Any]]) -> list[tuple[str, Decimal | None]]:
    return [(p["d"], None if p["nav"] is None else D(str(p["nav"]))) for p in nav]


def replay_tearsheet(conn: psycopg.Connection, *, catalog_id: str | None, experiment_id: UUID | None) -> dict[str, Any]:
    stored = BFR.load_replay(conn, experiment_id=experiment_id) if experiment_id is not None else BFR.load_replay(conn, catalog_id=catalog_id)
    if stored is None:
        raise LabError(404, "NOT_FOUND", "还没有这个战法的回放结果：运行 ext-b-run 后才有净值曲线和成交明细")
    body = stored["body"]
    initial = D(str(body["initial_cash"]))
    ts = T.compute(nav_from_replay(body["nav"]), trades_from_replay(body["trades"]), initial=initial)
    return {"subject": {"kind": "replay", "id": body["catalog_id"], "label": f"战法回放 {body['playbook_id']}", "catalog_id": body["catalog_id"],
                        "experiment_id": body["experiment_id"]},
            "window": {"start": body["window"]["start"], "end": body["window"]["end"], "sessions": body["window"]["sessions"]},
            "basis": {"initial_nav": q12(initial), "trade_pnl": "quantity x (exit price - entry price), gross of fees (fees are inside the NAV)",
                      "nav_basis": f"daily NAV of the x1 replay, net of the user cost profile {body['cost_policy_version']}; prices {body['price_basis']}"},
            "tearsheet": tearsheet_wire(ts),
            "caveats": ["净值曲线是执行内核按用户费用口径回放的结果，已扣费；价格为前复权研究序列，只用于研究。",
                        "盈亏比、胜率、盈亏因子按成交均价之差计算（毛收益，未扣费）；费用只体现在净值里。",
                        "年化按 252 个交易日；样本只有几十个交易日时，夏普、索提诺、卡玛不能当作证据。"]}


def book_tearsheet(conn: psycopg.Connection, principal: UUID, book_key: str) -> dict[str, Any]:
    acct = conn.execute("SELECT account_id, label FROM execution.tracked_account WHERE book_key=%s", (book_key,)).fetchone()
    if acct is None or not PORT.has_grant(conn, principal, acct["account_id"], "read"):        # no existence leak across grants
        raise LabError(404, "NOT_FOUND", "没有这本前向账本，或你没有读取权限")
    days = conn.execute("SELECT trade_date, nav_cny FROM execution.tracked_book_day WHERE account_id=%s ORDER BY trade_date", (acct["account_id"],)).fetchall()
    ts = T.compute([(d["trade_date"].isoformat(), d["nav_cny"]) for d in days], None)
    return {"subject": {"kind": "book", "id": book_key, "label": acct["label"], "catalog_id": None, "experiment_id": None},
            "window": {"start": ts.first_session, "end": ts.last_session, "sessions": ts.sessions},
            "basis": {"initial_nav": None, "trade_pnl": None,
                      "nav_basis": "execution.tracked_book_day.nav_cny at the close of each decided session; the first NAV is the base (no initial capital is stored)"},
            "tearsheet": tearsheet_wire(ts),
            "caveats": ["前向账本只保存每日净值，没有成交明细：胜率、盈亏因子、盈亏比、持仓占比显示为缺失（不是 0）。",
                        "账本从首个可见发布日起算，不回填；检验中，不参与排名权重。",
                        "年化按 252 个交易日；样本很短时，比率类指标不能当作证据。"]}


# --------------------------------------------------------------------------------------------------------
# batch comparison: job registration, command, worker
# --------------------------------------------------------------------------------------------------------
def job_definition() -> JobDefinition:
    return JobDefinition(
        job_id=LAB_JOB, job_version=1, application_operation=JOB_OPERATION,
        schedule_document={"trigger": "manual", "manual_trigger": True, "timezone": "Asia/Shanghai"}, parameter_contract_id=CONTRACT_ID,
        parameter_schema_version=SCHEMA_VERSION, enabled=True, concurrency_key="lab-batch", max_concurrency=1, timeout_seconds=3600, retry_policy={"max_attempts": 1})


_JOB_READY: set[str] = set()


def ensure_job(conn: psycopg.Connection) -> None:
    """Register the job definition once per process and database (idempotent in the database)."""
    key = str(conn.info.dbname)
    if key not in _JOB_READY:
        PgJobStore(conn).register_definition(job_definition())
        _JOB_READY.add(key)


def ensure_command_binding(conn: psycopg.Connection) -> None:
    """``ops.command_receipt`` references ``core.api_binding``: register the command once (a row that exists is only read)."""
    if conn.execute("SELECT 1 FROM core.api_binding WHERE operation_id=%s", (OPERATION_ID,)).fetchone():
        return
    conn.execute("INSERT INTO core.api_binding(operation_id, http_method, path_template, response_contract_id, response_schema_version,"
                 " application_service, read_model, freshness_policy_version) VALUES (%s,%s,%s,%s,%s,%s,'none','backfill-v1') ON CONFLICT (operation_id) DO NOTHING",
                 (OPERATION_ID, "POST", "/v1/x/bf/lab/batch-compare", CONTRACT_ID, SCHEMA_VERSION, JOB_OPERATION))


def resolve_playbook(text: str) -> L.Playbook:
    pid = text[3:] if text.startswith("PB:") else text
    pb = L.REGISTRY.get(pid)
    if pb is None:
        raise LabError(404, "NOT_FOUND", f"没有这个战法：{text}")
    for req in pb.requires:
        if req in PS.PERMANENT_MISSING:
            raise LabError(422, "PLAYBOOK_UNAVAILABLE", f"战法 {pid} 缺少数据：{PS.PERMANENT_MISSING[req]}")
        if req == "float_cap":
            raise LabError(422, "PLAYBOOK_UNAVAILABLE", f"战法 {pid} 缺少按时点保存的流通市值，不能回放")
    return pb


def _true_rows(conn: psycopg.Connection, screen_run_id: UUID, top_n: int) -> list[dict[str, Any]]:
    return conn.execute("SELECT sort_position, instrument_id FROM research.screen_run_row WHERE screen_run_id=%s AND condition_value='true'"
                        " ORDER BY sort_position LIMIT %s", (screen_run_id, top_n)).fetchall()


def create_batch(conn: psycopg.Connection, pid: UUID, req: dict[str, Any], key: str, now: datetime) -> dict[str, Any]:
    """Command handler (inside the idempotent command transaction): validate, store the parameters, queue the job. Returns the receipt."""
    try:
        run = S.get_run(conn, pid, UUID(req["screen_run_id"]))
    except S.ScreenError as e:
        raise LabError(e.status, e.code, e.message) from e
    if run["state"] != "ready":
        raise LabError(409, "RESULT_NOT_READY", f"筛选运行还不是 ready（{run['state']}）")
    pb = resolve_playbook(req["playbook_id"])
    top_n = req.get("top_n", MAX_TOP_N)
    if not _true_rows(conn, run["screen_run_id"], 1):
        raise LabError(422, "NO_MATCHING_ROWS", "这次筛选没有条件为真的股票，没有可对比的对象")
    params = {"screen_run_id": req["screen_run_id"], "playbook_id": pb.id, "top_n": top_n, "window_sessions": req.get("window_sessions", DEFAULT_WINDOW)}
    ensure_job(conn)
    stored = OPS.put_artifact(conn, kind=PARAM_KIND, contract_id=CONTRACT_ID, schema_version=SCHEMA_VERSION, body=params, owner=pid)
    run_id = uuid.uuid5(LAB_JOB, f"{pid}:{key}")
    store = PgJobStore(conn)
    store.enqueue(job_run_id=run_id, job_id=LAB_JOB, job_version=1, idempotency_key=f"{pid}:{key}", parameter_hash=sha256_of(params), scheduled_for=now,
                  resource_type=RESOURCE_TYPE, resource_id=run_id)
    conn.execute("INSERT INTO ops.job_run_argument(job_run_id, principal_id, parameter_contract_id, parameter_schema_version, parameter_artifact, parameter_hash,"
                 " input_snapshot_refs, release_id) VALUES (%s,%s,%s,%s,%s,%s,%s,NULL) ON CONFLICT (job_run_id) DO NOTHING",
                 (run_id, pid, CONTRACT_ID, SCHEMA_VERSION, stored["ref"], stored["content_hash"], Jsonb([])))
    return {"receipt": {**store.receipt(run_id), "poll_operation_id": "getJobRun"}}


def run_batch(conn: psycopg.Connection, lake: LakeStore, params: dict[str, Any], pid: UUID) -> dict[str, Any]:
    """The replay itself: returns the ``BatchResult`` body. Raises on a problem that makes the whole run meaningless (the job then fails)."""
    run = S.get_run(conn, pid, UUID(params["screen_run_id"]))
    doc = run["input_document"]
    asof = datetime.fromisoformat(doc["decision_as_of"].replace("Z", "+00:00"))
    board = S.UNIVERSES[doc["universe_policy_version"]]
    pb = resolve_playbook(params["playbook_id"])
    picked = _true_rows(conn, run["screen_run_id"], params["top_n"])
    panel = S.load_panel(conn, lake, asof, board=board, with_caps=False)
    sessions = panel.sessions
    end = len(sessions)
    if end < 3:
        raise LabError(422, "NOT_ENOUGH_HISTORY", "决策时点可见的交易日太少，无法回放")
    start = max(1, end - params["window_sessions"])
    prepare(panel.views, sessions, [pb])
    pparams = pb.default_params()
    specs = PS._specs(panel)
    by_id = panel.by_id()
    base = user_account_2026()
    rules = EX.ExitRules.from_spec(pb.exits)
    rows = []
    for r in picked:
        iid = str(r["instrument_id"])
        meta = panel.metas.get(iid)
        head = {"sort_position": r["sort_position"], "instrument_id": iid, "code": None if meta is None else meta.code, "name": None if meta is None else meta.display_name}
        view = by_id.get(iid)
        if view is None or iid not in specs:
            rows.append({**head, "status": "skipped", "skip_reason": "not_in_panel" if view is None else "no_instrument_spec", "tearsheet": None})
            continue
        try:
            sig = run_signal(pb, view, pparams)
            cfg = ReplayConfig(PS.DEFAULT_CASH, base, start, end, 1, 1, rules)
            res = replay(signals={iid: sig}, views={iid: view}, specs={iid: specs[iid]}, sessions=sessions, cfg=cfg, run_id=f"lab:{pb.id}:{iid}")
        except Exception as exc:  # noqa: BLE001 - one instrument's failure is recorded on its row, the others still run
            log.exception("batch replay of %s on %s failed", pb.id, iid)
            rows.append({**head, "status": "skipped", "skip_reason": f"replay_error:{type(exc).__name__}", "tearsheet": None})
            continue
        trades = [T.TradeIn(t.entry_day, t.exit_day if t.exit_price is not None else None,
                            None if t.exit_price is None else D(t.quantity) * (t.exit_price - t.entry_price), t.gross_return) for t in res.trades]
        ts = T.compute(res.nav, trades, initial=PS.DEFAULT_CASH)
        rows.append({**head, "status": "ok" if res.trades else "no_trades", "skip_reason": None, "tearsheet": tearsheet_wire(ts)})
    matches = panel.manifest["content_hash"] == doc["panel"]["research_content_hash"]
    return {
        "version": RESULT_VERSION,
        "screen": {"screen_run_id": str(run["screen_run_id"]), "decision_as_of": doc["decision_as_of"], "universe_policy_version": doc["universe_policy_version"],
                   "true_rows": run["true_count"] or 0, "selected": len(picked), "panel_matches_screen": matches},
        "playbook": {"id": pb.id, "catalog_id": f"PB:{pb.id}", "name": pb.name, "family": pb.family},
        "window": {"start": sessions[start], "end": sessions[end - 1], "sessions": end - start}, "initial_cash": q12(PS.DEFAULT_CASH),
        "cost_policy_version": base.version, "price_basis": PRICE_BASIS, "rows": rows,
        "caveats": [
            "每只股票单独回放：整个账户只交易这一只（最多 1 个持仓），走同一个执行内核和用户费用口径；列是这一只的净值与成交统计。",
            "这 N 只股票是用决策时点的数据选出来的，回放窗口又截止在同一时点：它衡量的是这条规则在「现在看起来强」的股票上过去的表现，不是样本外检验。",
            ("回放只用决策时点已可见的数据（研究快照与筛选运行使用的一致）。" if matches else
             "注意：决策时点可见的研究快照与筛选运行当时用的不是同一份（之后有修订）；本次回放按决策时点重新取数。"),
            "价格为前复权研究序列，只用于研究；年化按 252 个交易日，样本短时比率类指标不能当作证据。"]}


def _store_result(conn: psycopg.Connection, pid: UUID, body: dict[str, Any]) -> dict[str, Any]:
    return OPS.put_artifact(conn, kind=RESULT_KIND, contract_id=CONTRACT_ID, schema_version=SCHEMA_VERSION, body=body, owner=pid)


def execute_pending(conn: psycopg.Connection, lake: LakeStore, *, clock=lambda: datetime.now(timezone.utc)) -> tuple[UUID, str] | None:
    """Claim the next queued batch job and run it to ``succeeded`` or ``failed``. Returns (job_run_id, state) or None when nothing is queued."""
    if conn.execute("SELECT 1 FROM ops.job_run WHERE job_id=%s AND run_state IN ('queued','running') LIMIT 1", (LAB_JOB,)).fetchone() is None:
        return None                                                    # nothing to do: also works on a database whose contracts are not seeded yet
    ensure_job(conn)
    store = PgJobStore(conn)
    job = store.claim(job_id=LAB_JOB, now=clock())
    if job is None:
        return None
    epoch = job.lease_epoch
    arg = conn.execute("SELECT a.principal_id, e.body FROM ops.job_run_argument a JOIN ops.ext_artifact e ON e.ref=a.parameter_artifact WHERE a.job_run_id=%s",
                       (job.job_run_id,)).fetchone()
    try:
        if arg is None:
            raise LabError(500, "NO_PARAMETERS", "任务没有参数记录")
        body = run_batch(conn, lake, arg["body"], arg["principal_id"])
        art = _store_result(conn, arg["principal_id"], body)
        store.succeed(job.job_run_id, epoch, output_ref=art["ref"], output_hash=art["content_hash"], now=clock())
        return job.job_run_id, "succeeded"
    except Exception as exc:  # noqa: BLE001 - a job failure is recorded on the run, never swallowed
        code = (exc.code if isinstance(exc, LabError) else type(exc).__name__)[:60]
        log.exception("batch compare %s failed", job.job_run_id)
        store.fail(job.job_run_id, epoch, error_code=code, now=clock())
        return job.job_run_id, "failed"


# --------------------------------------------------------------------------------------------------------
# archive reads (owner scoped)
# --------------------------------------------------------------------------------------------------------
_RUN_SQL = ("SELECT r.job_run_id, r.run_state, r.error_code, r.scheduled_for, r.completed_at, x.output_ref, p.body AS params"
            " FROM ops.job_run r JOIN ops.job_run_argument a ON a.job_run_id=r.job_run_id JOIN ops.ext_artifact p ON p.ref=a.parameter_artifact"
            " LEFT JOIN ops.job_run_resource x ON x.job_run_id=r.job_run_id WHERE r.job_id=%s AND a.principal_id=%s")


def _brief(row: dict[str, Any], result: dict[str, Any] | None) -> dict[str, Any]:
    p = row["params"]
    summary = None
    if result is not None:
        rs = result["rows"]
        summary = {"instruments": len(rs), "with_trades": sum(1 for x in rs if x["status"] == "ok"), "skipped": sum(1 for x in rs if x["status"] == "skipped")}
    return {"job_run_id": str(row["job_run_id"]), "run_state": row["run_state"], "error_code": row["error_code"], "scheduled_for": row["scheduled_for"],
            "completed_at": row["completed_at"],
            "request": {"screen_run_id": p["screen_run_id"], "playbook_id": p["playbook_id"], "top_n": p["top_n"], "window_sessions": p["window_sessions"]},
            "summary": summary}


def _result_of(conn: psycopg.Connection, pid: UUID, ref: str | None) -> dict[str, Any] | None:
    if ref is None:
        return None
    art = OPS.get_artifact(conn, ref, pid)
    return None if art is None else art["body"]


def list_runs(conn: psycopg.Connection, pid: UUID, limit: int, cursor: str | None) -> dict[str, Any]:
    if not 1 <= limit <= 100:
        raise LabError(400, "INVALID_CONTRACT", "limit 必须在 1 到 100 之间")
    sql, args = _RUN_SQL, [LAB_JOB, pid]
    if cursor:
        try:
            ts, rid = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            args += [datetime.fromisoformat(ts.replace("Z", "+00:00")), UUID(rid)]
        except (ValueError, TypeError) as e:
            raise LabError(400, "INVALID_CONTRACT", "游标无效") from e
        sql += " AND (r.scheduled_for, r.job_run_id) < (%s, %s)"
    rows = conn.execute(sql + " ORDER BY r.scheduled_for DESC, r.job_run_id DESC LIMIT %s", (*args, limit + 1)).fetchall()
    page = rows[:limit]
    items = [_brief(r, _result_of(conn, pid, r["output_ref"])) for r in page]
    nxt = None
    if len(rows) > limit:
        last = page[-1]
        nxt = base64.urlsafe_b64encode(json.dumps([contracts.utc_z(last["scheduled_for"]), str(last["job_run_id"])]).encode()).decode()
    return {"items": items, "page": {"next_cursor": nxt, "limit": limit}, "order": "按排队时间由新到旧；只显示你自己发起的对比"}


def get_run(conn: psycopg.Connection, pid: UUID, job_run_id: UUID) -> dict[str, Any]:
    row = conn.execute(_RUN_SQL + " AND r.job_run_id=%s", (LAB_JOB, pid, job_run_id)).fetchone()
    if row is None:                                                    # no existence leak across owners
        raise LabError(404, "NOT_FOUND", "没有这次对比运行")
    result = _result_of(conn, pid, row["output_ref"])
    return {"run": _brief(row, result), "result": result, "result_ref": row["output_ref"]}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def default_ttl() -> timedelta:
    return timedelta(days=7)
