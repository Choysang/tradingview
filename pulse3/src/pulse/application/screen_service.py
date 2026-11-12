"""Screen use cases (PEX10): research panel, parse, run (frozen scan), query run, save / list screens.

Why: LEGACY inventory "问句选股", "Filter rail", "Presets", "Results table + CSV". The route layer only translates HTTP; the
rules live in ``pulse.domain.screening`` and the shared kernels in ``pulse.domain.features``. A screen run is a frozen, owner-
scoped scan: the request (AST, universe policy, snapshot refs, decision_as_of, strategy instances) is hashed and stored with the
job; the result rows (TRUE and UNKNOWN only, FALSE counted) are immutable once the run is ``ready``.
Invariants: inputs are point-in-time (``bindings.load_research_inputs`` keeps bars with ``available_at <= decision_as_of``, latest
visible revision); a missing field value is UNKNOWN in the three-valued evaluation and stays unknown in the result (never 0, never
silently false); a run whose data cannot be loaded is ``failed`` with an error code, never an empty success; screens are
authorised by the authenticated owner only; a saved screen is a row, not browser storage or a layout blob.
Prices: the research series is forward adjusted (see ``replay.PRICE_BASIS``): relative fields are exact, price LEVELS are
exact only for the newest session. ``float_cap`` comes from the legacy cap snapshot (scaled by close ratio) and is used only when
its ``available_at <= decision_as_of``.
Not covered: themes/sector membership, turnover, valuation (catalog ``UNSUPPORTED``), sharing a screen with another user.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import polars as pl
import psycopg
from psycopg.types.json import Jsonb

from pulse import contracts
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import market_repo
from pulse.adapters.postgres._canon import sha256_of
from pulse.adapters.postgres.jobs import PgJobStore
from pulse.application import bindings as B
from pulse.application.bootstrap import SCHEMA_ID
from pulse.application.ports.errors import FencedOut
from pulse.application.ports.jobs import JobDefinition
from pulse.domain.features.key import FeatureKey, key_hash, parameter_digest
from pulse.domain.playbooks.builder import RawSeries, build_views
from pulse.domain.playbooks.view import InstrumentView
from pulse.domain.screening import ast as A
from pulse.domain.screening import catalog as K
from pulse.domain.screening import parser

SCREEN_JOB = UUID("5c3d8e2a-91b4-4f67-a0e5-3b7d2c9f1a48")
CAP_DATASET = B.CAP_DATASET
UNIVERSES = {"screen-universe-v1": None, "screen-universe-main-v1": "main"}
PRICE_BASIS_NOTE = "前复权研究序列：相对指标精确，价位仅最新一根等于不复权价"
PANEL_VERSION = "screen-panel-v1"


class ScreenError(Exception):
    """Maps to an API error: (http status, code, message)."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# --------------------------------------------------------------------------------------------------------
# panel
# --------------------------------------------------------------------------------------------------------
@dataclass
class Panel:
    views: list[InstrumentView]
    sessions: list[str]
    metas: dict[str, B.InstrumentMeta]
    manifest: dict
    decision_as_of: datetime
    cap_latest: dict[str, float]                 # instrument id -> latest float cap CNY (PIT-visible at decision_as_of)
    cap_manifest: dict | None
    benchmark: dict[date, float]
    notes: list[str]
    fields: dict[str, dict[str, float | None]] = field(default_factory=dict)

    def by_id(self) -> dict[str, InstrumentView]:
        return {v.instrument_id: v for v in self.views}


_CACHE: dict[tuple, Panel] = {}


def load_panel(conn: psycopg.Connection, lake: LakeStore, decision_as_of: datetime, *, until: date | None = None,
               board: str | None = None, with_caps: bool = True) -> Panel:
    """Research panel visible at ``decision_as_of``; ``until`` (exclusive session date) hides later bars entirely."""
    inputs = B.load_research_inputs(conn, lake, decision_as_of)
    key = (inputs.manifest["content_hash"], decision_as_of.date(), until, board, with_caps)
    if key in _CACHE:
        return _CACHE[key]
    metas_u, _ = B.load_instrument_meta(conn, decision_as_of)
    metas = {str(k): m for k, m in metas_u.items()}
    df = inputs.bars
    if until is not None:
        df = df.filter(pl.col("trade_date") < until)
    sessions = [d.isoformat() for d in inputs.sessions if until is None or d < until]
    keep = {i for i, m in metas.items() if board is None or m.board_code == board}
    cap_latest: dict[str, float] = {}
    cap_man = None
    if with_caps:
        cap_man = B._ready_manifest(conn, CAP_DATASET, decision_as_of)
        if cap_man is not None:
            cf = lake.read_snapshot_frames(cap_man["content_hash"])
            if cf is not None and cf.height:
                cf = cf.filter(pl.col("available_at") <= decision_as_of).sort("available_at").unique("instrument_id", keep="last")
                cap_latest = {r["instrument_id"]: float(r["float_cap_cny"]) for r in cf.iter_rows(named=True) if r["float_cap_cny"] is not None}
    views: list[InstrumentView] = []
    raws: list[RawSeries] = []
    for (iid,), g in df.partition_by("instrument_id", as_dict=True, maintain_order=True).items():
        if iid not in keep:
            continue
        g = g.sort("trade_date")
        raws.append(RawSeries(iid, metas[iid].board_code, tuple(d.isoformat() for d in g["trade_date"].to_list()),
                              tuple(g["open"].to_list()), tuple(g["high"].to_list()), tuple(g["low"].to_list()),
                              tuple(g["close"].to_list()), tuple(g["volume"].to_list()), metas[iid].code, cap_latest.get(iid)))
    views = build_views(raws, sessions)
    panel = Panel(views, sessions, metas, inputs.manifest, decision_as_of, cap_latest, cap_man, inputs.benchmark, list(inputs.notes))
    if len(_CACHE) >= 2:
        _CACHE.clear()
    _CACHE[key] = panel
    return panel


def facts_of(panel: Panel, iid: str) -> K.InstrumentFacts:
    m = panel.metas.get(iid)
    return K.InstrumentFacts(None if m is None else m.board_code, None if m is None else m.is_risk_warning, panel.cap_latest.get(iid))


def field_row(panel: Panel, v: InstrumentView) -> dict[str, float | None]:
    row = panel.fields.get(v.instrument_id)
    if row is None:
        row = panel.fields[v.instrument_id] = K.field_values(v, facts_of(panel, v.instrument_id))
    return row


# --------------------------------------------------------------------------------------------------------
# typed feature wire (FeatureResult is a reference contract: the value rides in the artifact ref, see B_evidence.md)
# --------------------------------------------------------------------------------------------------------
def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def feature_key(field_id: str, iid: str, run: dict) -> FeatureKey:
    inp = run["input_document"]
    snap = inp["panel"]["research_snapshot_id"]
    return FeatureKey(
        feature_id=field_id, definition_version=K.FIELD_VERSION, implementation_digest=_sha(f"{K.CATALOG_VERSION}:{field_id}:shared-kernels-v1"),
        parameter_digest=parameter_digest({}), instrument_ids=(iid,), timeframe="1d", adjustment_mode="forward_adjusted", snapshot_id=snap,
        decision_as_of=datetime.fromisoformat(inp["decision_as_of"].replace("Z", "+00:00")),
        availability_policy_version=B.AVAILABILITY_POLICY, session_policy_version=B.SESSION_POLICY,
        input_revision=f"manifest:{inp['panel']['research_content_hash'][:16]}", input_schema_version=PANEL_VERSION,
        dependency_digest=_sha("dependencies:" + field_id), universe_policy_version=inp["universe_policy_version"],
        calendar_version="observed-from-bars", adjustment_version="legacy-qfq-unversioned", market_rules_version="not-applicable",
        numeric_policy_version="float64-v1")


def typed_feature(run: dict, iid: str, f: dict) -> dict[str, Any]:
    """TypedFeature wire for one stored cell {field, value, unit, state}."""
    key = feature_key(f["field"], iid, run)
    value = f["value"]
    ref = f"screen-value://{run['screen_run_id']}/{iid}/{f['field']}?" + (f"value={value}&unit={f['unit']}" if value is not None else f"missing={f.get('reason', 'unknown')}")
    kh = key_hash(key)
    return {
        "feature_id": f["field"], "definition_version": K.FIELD_VERSION, "key_hash": kh,
        "result": {"feature_key_hash": kh, "key": key.to_wire(), "result_state": "READY",
                   "artifact": {"ref": ref, "content_hash": _sha(ref), "contract_id": "pulse.screen_feature_value", "schema_version": "1.0"},
                   "computed_at": run["completed_at"] or run["created_at"], "quality_status": "valid" if value is not None else "partial"}}


# --------------------------------------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------------------------------------
_JOBS_READY = False


def ensure_jobs(conn: psycopg.Connection) -> None:
    """Register this group's job definitions once per process (idempotent in the database)."""
    global _JOBS_READY
    if not _JOBS_READY:
        seed_jobs(conn)
        _JOBS_READY = True


def seed_jobs(conn: psycopg.Connection) -> None:
    PgJobStore(conn).register_definition(JobDefinition(
        job_id=SCREEN_JOB, job_version=1, application_operation="ScreenService.execute_screen_run",
        schedule_document={"trigger": "command", "timezone": "Asia/Shanghai"}, parameter_contract_id=SCHEMA_ID,
        parameter_schema_version=contracts.CONTRACT_VERSION, enabled=True, concurrency_key="screen", max_concurrency=1,
        timeout_seconds=1800, retry_policy={"max_attempts": 2}))


def receipt_of(store: PgJobStore, job_run_id: UUID, poll: str) -> dict:
    r = store.receipt(job_run_id)
    r["poll_operation_id"] = poll
    return r


# --------------------------------------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------------------------------------
def parse_text(text: str, field_catalog_version: str, locale: str) -> dict:
    if locale not in ("zh-CN", "zh"):
        raise ScreenError(400, "INVALID_CONTRACT", "locale 目前只支持 zh-CN")
    try:
        return parser.parse(text, field_catalog_version)
    except ValueError as e:
        raise ScreenError(400, "INVALID_CONTRACT", str(e)) from e


def _extra_fields(conn: psycopg.Connection, instances: list[dict]) -> dict[str, str]:
    extra: dict[str, str] = {}
    for ref in instances:
        row = conn.execute(
            "SELECT d.specification FROM strategy.instance i JOIN strategy.definition d USING (strategy_id, strategy_version)"
            " WHERE i.instance_id=%s AND i.strategy_id=%s AND i.strategy_version=%s",
            (ref["instance_id"], ref["strategy_id"], ref["strategy_version"])).fetchone()
        if row is None:
            raise ScreenError(404, "NOT_FOUND", f"策略实例不存在：{ref['instance_id']}")
        pid = (row["specification"].get("playbook") or {}).get("id")
        if pid:
            extra[f"signal.{pid}"] = "flag"
    return extra


def create_run(conn: psycopg.Connection, pid: UUID, body: dict, key: str, now: datetime) -> dict:
    """Handler of runScreen (inside the command transaction): validate, freeze the request, enqueue the job."""
    try:
        A.validate_ast(body["ast"], _extra_fields(conn, body["strategy_instances"]))
    except A.ScreenAstError as e:
        raise ScreenError(400, e.code, str(e)) from e
    if body["universe_policy_version"] not in UNIVERSES:
        raise ScreenError(400, "INVALID_CONTRACT", f"未知股票池：{body['universe_policy_version']}")
    asof = datetime.fromisoformat(body["decision_as_of"].replace("Z", "+00:00"))
    if asof > now + timedelta(minutes=5):
        raise ScreenError(409, "STALE_INPUT", "decision_as_of 在未来")
    refs = body["input_snapshot_refs"]
    research = None
    for r in refs:
        man = market_repo.get_manifest(conn, UUID(r["snapshot_id"]))
        if man is None or man["manifest_state"] != "READY" or man["content_hash"] != r["content_hash"] or man["dataset_id"] != r["dataset_id"]:
            raise ScreenError(409, "STALE_INPUT", f"输入快照已变化或不可用：{r['dataset_id']}")
        if man["ready_at"] > asof and man["dataset_id"] == B.RESEARCH_DATASET:
            raise ScreenError(409, "STALE_INPUT", "decision_as_of 早于研究快照发布时间")
        if man["dataset_id"] == B.RESEARCH_DATASET:
            research = man
    if research is None:
        raise ScreenError(409, "STALE_INPUT", f"input_snapshot_refs 必须包含 {B.RESEARCH_DATASET}")
    newer = B._ready_manifest(conn, B.RESEARCH_DATASET, None)
    warnings = [] if newer["snapshot_id"] == research["snapshot_id"] else ["research snapshot is not the newest READY version"]
    doc = {"universe_policy_version": body["universe_policy_version"], "input_snapshot_refs": refs, "decision_as_of": body["decision_as_of"],
           "strategy_instances": body["strategy_instances"], "field_catalog_version": body["ast"]["allowed_field_catalog_version"],
           "panel": {"research_snapshot_id": str(research["snapshot_id"]), "research_content_hash": research["content_hash"]}, "warnings": warnings}
    run_id = uuid.uuid5(SCREEN_JOB, f"{pid}:{key}")
    ah = A.ast_hash(body["ast"])
    ensure_jobs(conn)
    store = PgJobStore(conn)
    job = store.enqueue(job_run_id=run_id, job_id=SCREEN_JOB, job_version=1, idempotency_key=f"screen:{run_id}", parameter_hash=sha256_of(
        {"ast": ah, "input": doc}), scheduled_for=now, resource_type="screen_run", resource_id=run_id)
    conn.execute(
        "INSERT INTO research.screen_run(screen_run_id, principal_id, ast, ast_hash, input_document, input_hash, job_run_id, state, created_at)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,'queued',%s) ON CONFLICT DO NOTHING",
        (run_id, pid, Jsonb(body["ast"]), ah, Jsonb(doc), sha256_of(doc), job.job_run_id, now))
    return {"receipt": receipt_of(store, run_id, "queryScreenRun")}


def save_screen(conn: psycopg.Connection, pid: UUID, body: dict, now: datetime) -> dict:
    try:
        A.validate_ast(body["ast"])
    except A.ScreenAstError as e:
        raise ScreenError(400, e.code, str(e)) from e
    if body["universe_policy_version"] not in UNIVERSES:
        raise ScreenError(400, "INVALID_CONTRACT", f"未知股票池：{body['universe_policy_version']}")
    sid = UUID(body["screen_id"])
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 11))", (str(sid),))
    head = conn.execute("SELECT max(screen_version) AS v, min(principal_id::text) AS owner FROM ui.saved_screen WHERE screen_id=%s", (sid,)).fetchone()
    if head["v"] is not None and head["owner"] != str(pid):
        raise ScreenError(403, "FORBIDDEN", "无权修改该筛选")
    cur = head["v"] or 0
    if body["expected_screen_version"] != cur:
        raise ScreenError(409, "VERSION_CONFLICT", f"筛选当前版本为 {cur}，请求基于 {body['expected_screen_version']}")
    version = cur + 1
    ah = A.ast_hash(body["ast"])
    content = sha256_of({"name": body["display_name"], "ast": ah, "universe": body["universe_policy_version"]})
    conn.execute(
        "INSERT INTO ui.saved_screen(screen_id, screen_version, principal_id, display_name, ast, ast_hash, strategy_instances,"
        " universe_policy_version, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,'[]'::jsonb,%s,%s,%s)",
        (sid, version, pid, body["display_name"], Jsonb(body["ast"]), ah, body["universe_policy_version"], now, now))
    return {"screen": {"object_id": str(sid), "version": version, "content_hash": content}}


def list_saved(conn: psycopg.Connection, pid: UUID, limit: int, cursor: str | None) -> tuple[list[dict], dict]:
    rows = conn.execute(
        "SELECT DISTINCT ON (screen_id) screen_id, screen_version, display_name, ast, universe_policy_version, updated_at"
        " FROM ui.saved_screen WHERE principal_id=%s ORDER BY screen_id, screen_version DESC", (pid,)).fetchall()
    rows.sort(key=lambda r: (-r["updated_at"].timestamp(), str(r["screen_id"])))
    snap = sha256_of([[str(r["screen_id"]), r["screen_version"]] for r in rows])
    start = 0
    if cursor:
        try:
            c = json.loads(cursor)
            if c["snap"] != snap:
                raise ScreenError(409, "CURSOR_EXPIRED", "保存的筛选已变化，请从头翻页")
            start = int(c["off"])
        except (ValueError, KeyError, TypeError) as e:
            raise ScreenError(409, "CURSOR_EXPIRED", "游标无效") from e
    page = rows[start:start + limit]
    nxt = json.dumps({"snap": snap, "off": start + limit}) if start + limit < len(rows) else None
    items = [{"screen_id": str(r["screen_id"]), "screen_version": r["screen_version"], "display_name": r["display_name"], "ast": r["ast"],
              "universe_policy_version": r["universe_policy_version"]} for r in page]
    return items, {"next_cursor": nxt, "snapshot_hash": snap}


def get_run(conn: psycopg.Connection, pid: UUID, run_id: UUID) -> dict:
    row = conn.execute("SELECT * FROM research.screen_run WHERE screen_run_id=%s", (run_id,)).fetchone()
    if row is None or row["principal_id"] != pid:                    # no existence leak across owners
        raise ScreenError(404, "NOT_FOUND", "筛选运行不存在")
    return row


def query_run(conn: psycopg.Connection, pid: UUID, run_id: UUID, limit: int, cursor: str | None) -> tuple[dict, list[dict], dict]:
    run = get_run(conn, pid, run_id)
    if run["state"] in ("queued", "running"):
        raise ScreenError(409, "RESULT_NOT_READY", "筛选仍在运行，请稍后重试")
    if run["state"] != "ready":
        raise ScreenError(422, "SCREEN_RUN_FAILED", f"筛选运行未成功：{run['error_code'] or run['state']}")
    start = 0
    if cursor:
        try:
            c = json.loads(cursor)
            if c["run"] != str(run_id) or c["hash"] != run["result_hash"]:
                raise ScreenError(409, "CURSOR_EXPIRED", "游标不属于该次运行")
            start = int(c["pos"])
        except (ValueError, KeyError, TypeError) as e:
            raise ScreenError(409, "CURSOR_EXPIRED", "游标无效") from e
    rows = conn.execute("SELECT * FROM research.screen_run_row WHERE screen_run_id=%s AND sort_position>%s ORDER BY sort_position LIMIT %s",
                        (run_id, start, limit + 1)).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    out = [{"instrument_id": str(r["instrument_id"]), "condition_value": r["condition_value"],
            "features": [typed_feature(run, str(r["instrument_id"]), f) for f in r["features"]], "sort_position": r["sort_position"]} for r in rows]
    nxt = json.dumps({"run": str(run_id), "hash": run["result_hash"], "pos": rows[-1]["sort_position"]}) if more and rows else None
    return run, out, {"next_cursor": nxt, "snapshot_hash": run["result_hash"]}


# --------------------------------------------------------------------------------------------------------
# execution (worker / background)
# --------------------------------------------------------------------------------------------------------
def execute_screen_run(conn: psycopg.Connection, lake: LakeStore, *, clock=lambda: datetime.now(timezone.utc)) -> tuple[UUID, str] | None:
    """Claim the next queued screen job and run it to ``ready`` or ``failed``. Returns (screen_run_id, state) or None."""
    store = PgJobStore(conn)
    job = store.claim(job_id=SCREEN_JOB, now=clock())
    if job is None:
        return None
    run_id, epoch = job.resource_id, job.lease_epoch
    run = conn.execute("SELECT * FROM research.screen_run WHERE screen_run_id=%s", (run_id,)).fetchone()
    if run is None or run["state"] not in ("queued", "running"):
        store.fail(job.job_run_id, epoch, error_code="screen_run_not_open", retryable=False, now=clock())
        return run_id, "failed"
    conn.execute("UPDATE research.screen_run SET state='running' WHERE screen_run_id=%s AND state IN ('queued','running')", (run_id,))
    try:
        doc = run["input_document"]
        asof = datetime.fromisoformat(doc["decision_as_of"].replace("Z", "+00:00"))
        board = UNIVERSES[doc["universe_policy_version"]]
        panel = load_panel(conn, lake, asof, board=board)
        ast = run["ast"]
        extra = _extra_fields(conn, doc["strategy_instances"])
        signal_fns = _signal_columns(conn, panel, doc["strategy_instances"]) if extra else {}
        used = A.used_fields(ast)
        sort = ast["sort"] or A.DEFAULT_SORT
        show = list(dict.fromkeys(used + [s["field_id"] for s in sort]))[:6]
        trues, unknowns, false_n = [], [], 0
        for v in panel.views:
            vals = dict(field_row(panel, v))
            for name, fn in signal_fns.items():
                vals[name] = fn(v)
            res = A.evaluate_row(ast, vals, asof)
            if res.value.value == "TRUE":
                trues.append((v.instrument_id, vals))
            elif res.value.value == "UNKNOWN":
                unknowns.append((v.instrument_id, vals))
            else:
                false_n += 1
        ordered = [(i, "true") for i in A.order_rows(sort, trues)] + [(i, "unknown") for i in A.order_rows(sort, unknowns)]
        vals_by = dict(trues + unknowns)
        batch = []
        for pos, (iid, truth) in enumerate(ordered, start=1):
            cells = []
            for fid in show:
                f = K.BY_ID.get(fid)
                v = vals_by[iid].get(fid)
                cells.append({"field": fid, "value": A.wire_value(v), "unit": f.unit if f else "flag",
                              **({} if v is not None else {"reason": "input_missing_or_history_short"})})
            batch.append((run_id, pos, uuid.UUID(iid), truth, Jsonb(cells)))
        gaps = []
        for fid in used:
            if fid == "float_cap" and panel.cap_manifest is None:
                gaps.append({"dataset_id": CAP_DATASET, "requirement": "float_cap", "reason_code": "dataset_missing_at_decision_as_of"})
            elif panel.views and fid in K.BY_ID and all(field_row(panel, v).get(fid) is None for v in panel.views):
                gaps.append({"dataset_id": B.RESEARCH_DATASET, "requirement": fid, "reason_code": "field_unknown_for_every_instrument"})
        digest = sha256_of([[p, truth, str(iid)] for p, (iid, truth) in enumerate(ordered, start=1)] + [run["ast_hash"]])
        with conn.transaction():
            with conn.cursor() as cur:
                cur.executemany("INSERT INTO research.screen_run_row(screen_run_id, sort_position, instrument_id, condition_value, features)"
                                " VALUES (%s,%s,%s,%s,%s)", batch)
            done = clock()
            conn.execute(
                "UPDATE research.screen_run SET state='ready', result_hash=%s, true_count=%s, unknown_count=%s, false_count=%s,"
                " evaluated_count=%s, capability_gaps=%s, completed_at=%s WHERE screen_run_id=%s",
                (digest, len(trues), len(unknowns), false_n, len(panel.views), Jsonb(gaps), done, run_id))
        store.succeed(job.job_run_id, epoch, output_ref=f"screen_run:{run_id}", output_hash=digest, now=done)
        return run_id, "ready"
    except FencedOut:
        return run_id, "fenced_out"
    except Exception as exc:                                         # a run that cannot finish is FAILED, not empty
        code = f"{type(exc).__name__}:{str(exc)[:120]}"
        conn.execute("UPDATE research.screen_run SET state='failed', error_code=%s, completed_at=%s WHERE screen_run_id=%s AND state IN ('queued','running')",
                     (code, clock(), run_id))
        try:
            store.fail(job.job_run_id, epoch, error_code=code[:100], retryable=False, now=clock())
        except FencedOut:
            pass
        return run_id, "failed"


def _signal_columns(conn: psycopg.Connection, panel: Panel, instances: list[dict]):
    """``signal.<playbook>`` -> fn(view) = 1.0/0.0 at the latest completed bar (research use: any lifecycle may screen)."""
    from pulse.domain.playbooks import library as L
    from pulse.domain.playbooks.builder import prepare, run_signal
    out = {}
    pbs = []
    for ref in instances:
        row = conn.execute("SELECT d.specification, i.parameters FROM strategy.instance i JOIN strategy.definition d USING (strategy_id, strategy_version)"
                           " WHERE i.instance_id=%s", (ref["instance_id"],)).fetchone()
        pid = (row["specification"].get("playbook") or {}).get("id")
        if pid in L.REGISTRY:
            pbs.append((L.REGISTRY[pid], dict(row["parameters"])))
    prepare(panel.views, panel.sessions, [p for p, _ in pbs])
    for pb, params in pbs:
        out[f"signal.{pb.id}"] = (lambda v, pb=pb, params=params: None if not v.n else (1.0 if run_signal(pb, v, params)[-1] else 0.0))
    return out
