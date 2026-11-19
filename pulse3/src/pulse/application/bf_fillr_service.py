"""Backfill lane ``fillr`` (fill-research): the research features the coverage audit of 2026-11-18 found only partly ported.

Rows (docs/LEGACY_TRIAGE.md, audit ``legacy_coverage_2026-11-18``): #16 / #82 / #150 playbook board (fitness, verdict, out-of-sample average,
sparkline), #21 / #87 custom run, #35 monthly returns, #37 / #80 factor daily IC and long-short curve, #42 / #68 / #158 analog study on demand.
Contract: ``backfill_fillr.schema.json`` (every payload leaves through ``respond_backfill`` and is validated first).
Board (#16 #82 #150): ``domain.playbooks.evaluation`` turns the STORED replay of each playbook (``bf_research_service`` artifact: daily NAV net of the
user cost profile + trades with gross returns and entry regime) into the old board's figures. The out-of-sample split is the one the experiment's
sealed evaluation context records (``train_end``); nothing is re-chosen. Nothing is recomputed that was not stored, nothing is written on read.
Jobs (#21 #37 #42): ONE registered manual job ``FILLR_JOB`` queued by commands with an Idempotency-Key, executed by ``pulse.workers.fillr run``
(NOT by the API process: the integrator schedules the worker; every read says ``queued`` until it ran). ``kind`` in the stored parameters picks:
  * ``custom_run``: a playbook with the user's parameter / exit / sizing overrides replayed through the same ``evaluate_playbook`` machinery and sealed
    context as the experiments. The command first REGISTERS A DRAFT through ``playbook_service.register_strategy_draft`` (the registerStrategyDraft
    handler: immutable definition, lineage to the base playbook instance, status "未检验") so the variant is recorded, then queues the run. The result is an
    owner-scoped EXPLORATORY artifact: it is not an experiment of the league, it never changes a playbook's status.
  * ``factor_series``: re-runs ``factor_lab.run_factor`` over the SAME windows as a completed factor experiment (deterministic given the sealed context)
    and stores the daily IC and the long-short cumulative curve as an immutable artifact keyed by the experiment.
  * ``analog_study``: ``playbook_service.build_analog_study`` for one instrument, release and policy (the study is a shared derived dataset keyed by
    its identity; a second request for the same study returns the same job).
Invariants: point in time (the context's decision_as_of; the blind slice is never read); money / ratios are decimal strings with at most 12 fraction
digits; a missing figure is null with a reason; a failed job keeps its error code; the same Idempotency-Key replays the same answer.
Not covered: instant (synchronous) custom runs, parameter sweeps, a style-matched baseline in the verdict (needs per-day pool returns that a replay does
not store; the utility is ``domain.research.baseline``).
"""
from __future__ import annotations

import base64
import dataclasses
import json
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from pulse import contracts
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres._canon import sha256_of
from pulse.adapters.postgres.jobs import PgJobStore
from pulse.application import bf_research_service as BFR
from pulse.application import bindings as B
from pulse.application import ops_service as OPS
from pulse.application import playbook_service as PS
from pulse.application import screen_service as S
from pulse.application.ports.jobs import JobDefinition
from pulse.domain.lab import tearsheet as T
from pulse.domain.playbooks import attribution as AT
from pulse.domain.playbooks import evaluation as EV
from pulse.domain.playbooks import exits as EX
from pulse.domain.playbooks import library as L
from pulse.domain.playbooks.replay import PRICE_BASIS
from pulse.domain.playbooks.spec import ExitSpec
from pulse.domain.screening import factor_lab as FL
from pulse.domain.strategy.canonical import canonical_hash, stable_uuid

D = Decimal
LANE = "fillr"
CONTRACT_ID = contracts.backfill_id(LANE)
SCHEMA_VERSION = "1.0.0"
FILLR_JOB = UUID("5b1e2d9c-7a43-4f08-b6d1-2c9e8a0f3d57")
JOB_OPERATION = "FillrService.run_job"
PARAM_KIND = "fillr-param"
RESULT_KIND = "fillr-custom-result"
SERIES_KIND = "fillr-factor-series"
SERIES_VERSION = "fillr-factor-series-v1"
RESULT_VERSION = "fillr-custom-result-v1"
BOARD_VERSION = "playbook-board-v1"
MONTHLY_VERSION = "monthly-returns-v1"
WORKER_HINT = "uv run python -m pulse.workers.fillr run"
OPERATIONS = {"custom_run": ("fillrCustomRun", "/v1/x/bf/fillr/custom-run"), "factor_series": ("fillrFactorSeries", "/v1/x/bf/fillr/factor-series"),
              "analog_study": ("fillrAnalogStudy", "/v1/x/bf/fillr/analog-study")}
RESOURCE_TYPES = {"custom_run": "fillr_custom_run", "factor_series": "fillr_factor_series", "analog_study": "fillr_analog_study"}
SPARK_POINTS = 60
EQUITY_POINTS = 400
TRADES_SHOWN = 200
PLACES = D("1E-12")
log = logging.getLogger(__name__)


class FillrError(Exception):
    """Maps to an API error: (http status, code, message)."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def q12(v: Decimal | None) -> Decimal | None:
    return None if v is None else v.quantize(PLACES, rounding=ROUND_HALF_EVEN)


def q4(v: float | None) -> Decimal | None:
    return None if v is None else D(str(round(v, 4)))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------------------------------------
# job registration and queueing
# --------------------------------------------------------------------------------------------------------
def job_definition() -> JobDefinition:
    return JobDefinition(
        job_id=FILLR_JOB, job_version=1, application_operation=JOB_OPERATION,
        schedule_document={"trigger": "manual", "manual_trigger": True, "timezone": "Asia/Shanghai"}, parameter_contract_id=CONTRACT_ID,
        parameter_schema_version=SCHEMA_VERSION, enabled=True, concurrency_key="fillr", max_concurrency=1, timeout_seconds=3600, retry_policy={"max_attempts": 1})


_JOB_READY: set[str] = set()


def ensure_job(conn: psycopg.Connection) -> None:
    key = str(conn.info.dbname)
    if key not in _JOB_READY:
        PgJobStore(conn).register_definition(job_definition())
        _JOB_READY.add(key)


def ensure_command_binding(conn: psycopg.Connection, kind: str) -> str:
    op, path = OPERATIONS[kind]
    if conn.execute("SELECT 1 FROM core.api_binding WHERE operation_id=%s", (op,)).fetchone() is None:
        conn.execute("INSERT INTO core.api_binding(operation_id, http_method, path_template, response_contract_id, response_schema_version,"
                     " application_service, read_model, freshness_policy_version) VALUES (%s,'POST',%s,%s,%s,%s,'none','backfill-v1') ON CONFLICT (operation_id) DO NOTHING",
                     (op, path, CONTRACT_ID, SCHEMA_VERSION, JOB_OPERATION))
    return op


def _enqueue(conn: psycopg.Connection, pid: UUID, *, key: str, kind: str, params: dict[str, Any], now: datetime) -> UUID:
    ensure_job(conn)
    stored = OPS.put_artifact(conn, kind=PARAM_KIND, contract_id=CONTRACT_ID, schema_version=SCHEMA_VERSION, body=params, owner=pid)
    run_id = uuid.uuid5(FILLR_JOB, key)
    PgJobStore(conn).enqueue(job_run_id=run_id, job_id=FILLR_JOB, job_version=1, idempotency_key=key, parameter_hash=sha256_of(params), scheduled_for=now,
                             resource_type=RESOURCE_TYPES[kind], resource_id=run_id)
    conn.execute("INSERT INTO ops.job_run_argument(job_run_id, principal_id, parameter_contract_id, parameter_schema_version, parameter_artifact, parameter_hash,"
                 " input_snapshot_refs, release_id) VALUES (%s,%s,%s,%s,%s,%s,%s,NULL) ON CONFLICT (job_run_id) DO NOTHING",
                 (run_id, pid, CONTRACT_ID, SCHEMA_VERSION, stored["ref"], stored["content_hash"], Jsonb([])))
    return run_id


def _shared_key(conn: psycopg.Connection, base: str) -> str:
    """Idempotency key of a SHARED job: one per identity, a new attempt only after the previous ones failed."""
    n = conn.execute("SELECT count(*) AS n FROM ops.job_run WHERE job_id=%s AND idempotency_key LIKE %s AND run_state IN ('failed','cancelled')",
                     (FILLR_JOB, base + ":%")).fetchone()["n"]
    return f"{base}:{n}"


def _latest_job(conn: psycopg.Connection, base: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT job_run_id, run_state, error_code FROM ops.job_run WHERE job_id=%s AND idempotency_key LIKE %s"
                       " ORDER BY scheduled_for DESC, job_run_id DESC LIMIT 1", (FILLR_JOB, base + ":%")).fetchone()
    return None if row is None else {"job_run_id": str(row["job_run_id"]), "run_state": row["run_state"], "error_code": row["error_code"]}


def _receipt(conn: psycopg.Connection, run_id: UUID) -> dict[str, Any]:
    return {**PgJobStore(conn).receipt(run_id), "poll_operation_id": "getJobRun"}


# --------------------------------------------------------------------------------------------------------
# board (#16 #82 #150)
# --------------------------------------------------------------------------------------------------------
_ARTIFACT_SQL = (
    "SELECT DISTINCT ON (a.body->>'catalog_id') a.body, a.ref, r.experiment_id, r.completed_at, c.train_end FROM ops.ext_artifact a"
    " JOIN research.experiment_run r ON r.experiment_id = (a.body->>'experiment_id')::uuid AND r.run_state = 'completed'"
    " JOIN research.evaluation_context c ON c.evaluation_context_id = r.evaluation_context_id"
    " WHERE a.contract_id = %s AND a.body->>'version' = %s AND a.body->>'catalog_id' LIKE 'PB:%%'"
    " ORDER BY a.body->>'catalog_id', r.completed_at DESC, r.experiment_id DESC")
_LATEST_RUN_SQL = (
    "SELECT DISTINCT ON (d.specification->>'catalog_id') d.specification->>'catalog_id' AS catalog_id, r.run_state, r.error_code, r.experiment_id"
    " FROM research.experiment_run r JOIN strategy.instance i ON i.instance_id = r.instance_id"
    " JOIN strategy.definition d ON d.strategy_id = i.strategy_id AND d.strategy_version = i.strategy_version"
    " WHERE d.specification->>'catalog_id' LIKE 'PB:%%'"
    " ORDER BY d.specification->>'catalog_id', COALESCE(r.completed_at, r.started_at) DESC NULLS LAST, r.experiment_id DESC")


def regime_now(conn: psycopg.Connection, lake: LakeStore, now: datetime) -> dict[str, Any]:
    """The newest regime point (labels only count when their dimension basis is ``ok``), or why there is none."""
    tl = BFR.regime_timeline(conn, lake, now)
    if not tl["available"] or not tl["points"]:
        return {"available": False, "reason": tl.get("reason") or "NO_REGIME_POINTS", "release_id": tl.get("release_id"), "trend": None, "sentiment": None, "through": None}
    p = tl["points"][-1]
    tag = AT.regime_tag(p)
    return {"available": True, "reason": None, "release_id": tl.get("release_id"), "trend": EV.regime_bucket(tag, "trend"),
            "sentiment": EV.regime_bucket(tag, "sentiment"), "through": str(p["date"])}


def split_day_of(nav_days: list[str], train_end: datetime | None) -> str | None:
    """First replay session on or after the context's ``train_end`` (the validation window starts there); None when the window ends before it."""
    if train_end is None:
        return None
    cutoff = train_end.astimezone(B.CST).date().isoformat()
    return next((d for d in nav_days if d >= cutoff), None)


def evaluate_body(body: dict[str, Any], train_end: datetime | None, trend: str | None, sentiment: str | None) -> EV.Evaluation:
    nav = [(p["d"], None if p["nav"] is None else D(str(p["nav"]))) for p in body["nav"]]
    trades = [EV.TradeIn(t["entry_day"], D(str(t["gross_return"])), t.get("holding_sessions"), t.get("regime_at_entry"))
              for t in body["trades"] if t["status"] == "closed" and t.get("gross_return") is not None]
    return EV.evaluate(trades, nav, D(str(body["initial_cash"])), split_day_of([d for d, _ in nav], train_end), trend=trend, sentiment=sentiment)


def _sample_wire(s: EV.Sample | None) -> dict[str, Any] | None:
    if s is None:
        return None
    return {"trades": s.trades, "win_rate": q12(s.win_rate), "avg_return": q12(s.avg_return), "t_stat": q12(s.t_stat), "sharpe": q12(s.sharpe), "sessions": s.sessions}


def evaluation_wire(ev: EV.Evaluation) -> dict[str, Any]:
    blk = None if ev.block is None else {"p_value": q4(ev.block.p_value), "blocks": ev.block.blocks, "block": ev.block.block, "reliable": ev.block.reliable,
                                         "reason": ev.block.reason}
    return {"version": ev.version, "split_day": ev.split_day, "sample_label": "out_of_sample" if ev.split_day else "in_sample_only",
            "full": _sample_wire(ev.full), "in_sample": _sample_wire(ev.in_sample), "out_of_sample": _sample_wire(ev.out_of_sample), "recent": _sample_wire(ev.recent),
            "verdict": ev.verdict, "verdict_reason": ev.verdict_reason, "fitness": ev.fitness, "fitness_reasons": list(ev.reasons), "block": blk,
            "missing": list(ev.missing)}


def _unavailable_reason(pb: L.Playbook) -> str | None:
    for req in pb.requires:
        if req in PS.PERMANENT_MISSING:
            return f"data_not_available:{req}（{PS.PERMANENT_MISSING[req]}）"
    return pb.unavailable_reason          # float_cap is NOT a permanent gap any more: the evaluator reads point-in-time cap versions (bf_fillm_caps) and a run without them fails with its own code


_EVALUATED: dict[tuple, tuple[dict[str, Any], list[dict[str, Any]]]] = {}


def _evaluated(art: dict[str, Any], reg: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Evaluation wire + sparkline points of one stored replay. The artifact is immutable (content-addressed ref) and the evaluation is a pure function of it,
    the sealed split and the current regime labels, so the answer is cached on exactly those (a few ms per playbook saved on every board read)."""
    key = (art["ref"], art["train_end"], reg["trend"], reg["sentiment"])
    hit = _EVALUATED.get(key)
    if hit is None:
        body = art["body"]
        navs = [p for p in body["nav"] if p["nav"] is not None]
        hit = (evaluation_wire(evaluate_body(body, art["train_end"], reg["trend"], reg["sentiment"])), EV.downsample(navs, SPARK_POINTS))
        if len(_EVALUATED) >= 512:
            _EVALUATED.clear()
        _EVALUATED[key] = hit
    return hit


def board(conn: psycopg.Connection, lake: LakeStore, now: datetime) -> dict[str, Any]:
    """Wire ``PlaybookBoard``: the 52 playbooks in library order with their latest stored replay evaluated."""
    reg = regime_now(conn, lake, now)
    arts = {r["body"]["catalog_id"]: r for r in conn.execute(_ARTIFACT_SQL, (BFR.CONTRACT_ID, BFR.ARTIFACT_VERSION)).fetchall()}
    runs = {r["catalog_id"]: r for r in conn.execute(_LATEST_RUN_SQL).fetchall()}
    items: list[dict[str, Any]] = []
    for pid in L.ORDER:
        pb = L.REGISTRY[pid]
        cid = f"PB:{pid}"
        head = {"catalog_id": cid, "playbook_id": pid, "name": pb.name, "family": pb.family, "horizon": pb.horizon, "reason": None, "experiment_id": None,
                "completed_at": None, "evaluation": None, "sparkline": [], "sparkline_start": None, "sparkline_end": None}
        art = arts.get(cid)
        if art is not None:
            ev_wire, spark = _evaluated(art, reg)
            items.append({**head, "state": "evaluated", "experiment_id": art["experiment_id"], "completed_at": art["completed_at"], "evaluation": ev_wire,
                          "sparkline": [p["nav"] for p in spark], "sparkline_start": spark[0]["d"] if spark else None, "sparkline_end": spark[-1]["d"] if spark else None})
            continue
        why = _unavailable_reason(pb)
        run = runs.get(cid)
        if why:
            items.append({**head, "state": "unavailable", "reason": why})
        elif run is not None and run["run_state"] == "failed":
            items.append({**head, "state": "failed", "reason": run["error_code"] or "failed", "experiment_id": run["experiment_id"]})
        else:
            items.append({**head, "state": "not_run", "reason": "no_completed_experiment"})
    counts = {k: sum(1 for i in items if i["state"] == k) for k in ("evaluated", "not_run", "unavailable", "failed")}
    return {"version": BOARD_VERSION, "regime_now": reg, "items": items, "counts": {"total": len(items), **counts},
            "notes": [EV.SPLIT_NOTE, "适配度与判定都由服务器从已保存的回放计算；回放净值已扣用户费用口径，单笔收益为毛收益；价格为前复权研究序列。",
                      "当前环境取最新发布的市场环境点（未校准的描述标签）。块自助（E082）块数不足 10 时只供参考，不改判定。"]}


# --------------------------------------------------------------------------------------------------------
# monthly returns (#35)
# --------------------------------------------------------------------------------------------------------
def monthly_returns(conn: psycopg.Connection, *, catalog_id: str | None, experiment_id: UUID | None) -> dict[str, Any]:
    stored = BFR.load_replay(conn, experiment_id=experiment_id) if experiment_id is not None else BFR.load_replay(conn, catalog_id=catalog_id)
    if stored is None:
        raise FillrError(404, "NOT_FOUND", "还没有这个战法的回放结果：运行 ext-b-run 后才有净值曲线")
    body = stored["body"]
    nav = [(p["d"], None if p["nav"] is None else D(str(p["nav"]))) for p in body["nav"]]
    months = T.monthly_returns(nav, D(str(body["initial_cash"])))
    days = [d for d, _ in nav]
    return {"version": MONTHLY_VERSION,
            "subject": {"label": body["playbook_id"], "catalog_id": body["catalog_id"], "experiment_id": body["experiment_id"]},
            "window": {"start": days[0] if days else None, "end": days[-1] if days else None, "sessions": len(days)},
            "months": [{"year": m.year, "month": m.month, "value": q12(m.value), "sessions": m.sessions, "reason": m.reason} for m in months],
            "basis": "月收益 = 当月最后一个有净值的交易日净值 / 上月最后一个有净值的净值 - 1；首月以期初资金为基准；净值已扣用户费用口径。",
            "caveats": ["首月、末月可能只有部分交易日（见 sessions）；没有净值的月份显示缺失，不补 0。", "价格为前复权研究序列，只用于研究。"]}


# --------------------------------------------------------------------------------------------------------
# custom run (#21 #87)
# --------------------------------------------------------------------------------------------------------
def resolve_playbook(text: str) -> L.Playbook:
    pid = text[3:] if text.startswith("PB:") else text
    pb = L.REGISTRY.get(pid)
    if pb is None:
        raise FillrError(404, "NOT_FOUND", f"没有这个战法：{text}")
    why = _unavailable_reason(pb)
    if why:
        raise FillrError(422, "PLAYBOOK_UNAVAILABLE", f"战法 {pid} 不能回放：{why}")
    return pb


def _rules_of(pb: L.Playbook, params: dict[str, float] | None = None) -> dict[str, Any]:
    e = pb.exits
    return {"params": pb.default_params(params), "holding_days": e.holding_days, "stop_loss_pct": e.stop_loss_pct, "take_profit_pct": e.take_profit_pct,
            "trailing_stop_pct": e.trailing_stop_pct, "max_positions": pb.max_positions, "max_new_per_day": pb.max_new_per_day}


def apply_overrides(pb: L.Playbook, ov: dict[str, Any]) -> L.Playbook:
    """The playbook with the user's exit / sizing overrides (signal parameters are passed to the signal separately). Validates ranges."""
    known = {p.key: p for p in pb.params}
    for k, v in (ov.get("params") or {}).items():
        p = known.get(k)
        if p is None:
            raise FillrError(400, "INVALID_CONTRACT", f"战法 {pb.id} 没有参数 {k}")
        if not p.low <= float(v) <= p.high:
            raise FillrError(400, "INVALID_CONTRACT", f"参数 {p.label}({k}) 必须在 {p.low:g} 到 {p.high:g} 之间")
    e = pb.exits
    exits = ExitSpec(
        holding_days=int(ov.get("holding_days", e.holding_days)),
        stop_loss_pct=ov["stop_loss_pct"] if "stop_loss_pct" in ov else e.stop_loss_pct,
        take_profit_pct=ov["take_profit_pct"] if "take_profit_pct" in ov else e.take_profit_pct,
        trailing_stop_pct=ov["trailing_stop_pct"] if "trailing_stop_pct" in ov else e.trailing_stop_pct)
    return dataclasses.replace(pb, exits=exits, max_positions=int(ov.get("max_positions", pb.max_positions)),
                               max_new_per_day=int(ov.get("max_new_per_day", pb.max_new_per_day)))


def create_custom_run(conn: psycopg.Connection, pid: UUID, req: dict[str, Any], key: str, now: datetime) -> dict[str, Any]:
    """Command handler (inside the idempotent command transaction): validate, register the draft, queue the run. Returns ``CustomRunReceipt``."""
    pb = resolve_playbook(req["playbook_id"])
    ov = req["overrides"]
    modified = apply_overrides(pb, ov)
    base = conn.execute("SELECT i.instance_id, i.strategy_id, i.strategy_version FROM strategy.instance i JOIN strategy.definition d USING (strategy_id, strategy_version)"
                        " WHERE d.specification->>'catalog_id' = %s ORDER BY i.created_at LIMIT 1", (f"PB:{pb.id}",)).fetchone()
    if base is None:
        raise FillrError(409, "NOT_REGISTERED", "战法登记库里还没有这个战法：先运行 uv run python -m pulse.workers.research ext-b-register")
    eff = _rules_of(modified, ov.get("params"))
    doc = {"playbook_id": pb.id, "overrides": ov, "effective": eff}
    digest = canonical_hash(doc)
    sid = stable_uuid("fillr-draft", str(pid), key)
    label = req.get("label") or f"{pb.name}（自定义）"
    draft = {"display_name": label[:80], "role": "selection", "method_family": "rule", "forecast_target": PS.FORECAST_TARGET,
             "horizon_id": f"{modified.exits.holding_days}td", "specification_contract_id": "pulse.fillr_custom_playbook", "specification_schema_version": "1.0",
             "specification_artifact": {"ref": f"fillr-custom:{digest[:32]}", "content_hash": digest, "contract_id": "pulse.fillr_custom_playbook", "schema_version": "1.0"},
             "parent_instances": [{"instance_id": str(base["instance_id"]), "strategy_id": str(base["strategy_id"]), "strategy_version": base["strategy_version"]}]}
    try:
        data, _warn = PS.register_strategy_draft(conn, pid, {"strategy_id": str(sid), "expected_latest_version": 0, "draft": draft}, now)
    except PS.StrategyError as e:
        raise FillrError(e.status, e.code, e.message) from e
    params = {"kind": "custom_run", "playbook_id": pb.id, "overrides": ov, "label": label, "strategy_id": str(sid), "strategy_version": 1}
    run_id = _enqueue(conn, pid, key=f"custom:{pid}:{key}", kind="custom_run", params=params, now=now)
    return {"receipt": _receipt(conn, run_id),
            "draft": {"strategy_id": str(sid), "strategy_version": data["strategy"]["strategy_version"], "instance_id": data["instance_id"]},
            "worker_hint": WORKER_HINT}


def _sealed_context(conn: psycopg.Connection) -> SimpleNamespace:
    row = conn.execute("SELECT evaluation_context_id, decision_as_of, train_start, train_end, validation_start, validation_end, cost_policy_version"
                       " FROM research.evaluation_context WHERE sealed_at IS NOT NULL ORDER BY created_at DESC LIMIT 1").fetchone()
    if row is None:
        raise FillrError(409, "NO_SEALED_CONTEXT", "还没有封存的评价上下文：先运行一次研究实验（research ext-b-run）")
    return SimpleNamespace(**row)


CUSTOM_METRICS = (("net_return", "overall"), ("net_return", "train"), ("net_return", "validation"), ("net_return_cost_x2", "validation"),
                  ("net_return_cost_x3", "validation"), ("excess_return_vs_csi300", "validation"), ("max_drawdown", "overall"), ("win_rate_gross", "overall"),
                  ("avg_trade_return_gross", "overall"), ("trade_count", "overall"), ("fees_paid", "overall"))


def run_custom(conn: psycopg.Connection, lake: LakeStore, params: dict[str, Any], job_run_id: UUID) -> dict[str, Any]:
    """The replay of a customised playbook through the experiments' own evaluator and sealed context. Returns the ``CustomRunResult`` body."""
    pb0 = resolve_playbook(params["playbook_id"])
    ov = params["overrides"]
    pb = apply_overrides(pb0, ov)
    ctx = _sealed_context(conn)
    values = pb.default_params(ov.get("params"))
    capture: dict[str, Any] = {}
    metrics = PS.evaluate_playbook(conn, lake, ctx, pb, values, capture)
    timeline = BFR.regime_timeline(conn, lake, ctx.decision_as_of)
    body = BFR.build_replay_artifact(experiment_id=job_run_id, instance_id=job_run_id, playbook_id=pb.id, rules=EX.ExitRules.from_spec(pb.exits), run=capture["run"],
                                     sessions=capture["sessions"], a=capture["a"], c=capture["c"], views=capture["views"], timeline=timeline)
    reg = regime_now(conn, lake, ctx.decision_as_of)
    ev = evaluate_body(body, ctx.train_end, reg["trend"], reg["sentiment"])
    by_key = {(m["metric_name"], m["slice_key"]): m for m in metrics}
    wire_metrics = []
    for name, sl in CUSTOM_METRICS:
        m = by_key.get((name, sl))
        if m is not None:
            wire_metrics.append({"name": name, "slice": sl, "value": q12(m["value"]), "unit": m["unit"], "missing_reason": m["missing_reason"]})
    closed = [t for t in body["trades"] if t["status"] == "closed"]
    newest = sorted(body["trades"], key=lambda t: (t["entry_day"], t["instrument_id"]), reverse=True)[:TRADES_SHOWN]
    equity = EV.downsample(body["nav"], EQUITY_POINTS)
    return {
        "version": RESULT_VERSION, "exploratory": True, "playbook": {"id": pb.id, "catalog_id": f"PB:{pb.id}", "name": pb.name}, "request": ov,
        "effective": _rules_of(pb, ov.get("params")), "base": _rules_of(pb0),
        "window": {**body["window"], "decision_as_of": ctx.decision_as_of}, "initial_cash": body["initial_cash"], "cost_policy_version": body["cost_policy_version"],
        "evaluation": evaluation_wire(ev), "metrics": wire_metrics, "equity": [{"d": p["d"], "nav": p["nav"]} for p in equity],
        "trades": {"total": len(body["trades"]), "closed": len(closed), "shown": len(newest), "items": [
            {"code": t["code"], "entry_day": t["entry_day"], "exit_day": t["exit_day"], "entry_price": t["entry_price"], "exit_price": t["exit_price"],
             "gross_return": t["gross_return"], "status": t["status"], "exit_reason": t["exit_reason"]} for t in newest]},
        "caveats": ["自定义回测是探索性的：参数由您事后选择，不计入研究联赛的试验，也不改变战法的证据状态；多次调参后挑出的最好结果会高估真实表现。",
                    EV.SPLIT_NOTE, f"价格基准：{PRICE_BASIS}（前复权研究序列，只用于研究）；净值已扣用户费用口径。"]}


# --------------------------------------------------------------------------------------------------------
# factor series (#37 #80)
# --------------------------------------------------------------------------------------------------------
_FACTOR_EXP_SQL = (
    "SELECT r.experiment_id, r.run_state, d.specification->>'catalog_id' AS catalog_id, c.decision_as_of, c.train_start, c.train_end, c.validation_start, c.validation_end"
    " FROM research.experiment_run r JOIN research.evaluation_context c ON c.evaluation_context_id = r.evaluation_context_id"
    " JOIN strategy.instance i ON i.instance_id = r.instance_id"
    " JOIN strategy.definition d ON d.strategy_id = i.strategy_id AND d.strategy_version = i.strategy_version")


def _factor_experiment(conn: psycopg.Connection, *, experiment_id: UUID | None, factor_id: str | None) -> dict[str, Any] | None:
    if experiment_id is not None:
        row = conn.execute(_FACTOR_EXP_SQL + " WHERE r.experiment_id=%s", (experiment_id,)).fetchone()
        if row is not None and not str(row["catalog_id"]).startswith("FL:"):
            raise FillrError(400, "INVALID_CONTRACT", "这个实验不是因子实验")
        return row
    cid = factor_id if (factor_id or "").startswith("FL:") else f"FL:{factor_id}"
    return conn.execute(_FACTOR_EXP_SQL + " WHERE d.specification->>'catalog_id'=%s AND r.run_state='completed' ORDER BY r.completed_at DESC, r.experiment_id DESC LIMIT 1",
                        (cid,)).fetchone()


def _series_artifact(conn: psycopg.Connection, experiment_id: UUID) -> dict[str, Any] | None:
    row = conn.execute("SELECT body FROM ops.ext_artifact WHERE contract_id=%s AND body->>'version'=%s AND body->>'experiment_id'=%s LIMIT 1",
                       (CONTRACT_ID, SERIES_VERSION, str(experiment_id))).fetchone()
    return None if row is None else row["body"]


def factor_series_status(conn: psycopg.Connection, *, experiment_id: UUID | None, factor_id: str | None) -> dict[str, Any]:
    exp = _factor_experiment(conn, experiment_id=experiment_id, factor_id=factor_id)
    base = {"reason": None, "experiment_id": None, "factor_id": factor_id if factor_id else None, "job": None, "series": None, "worker_hint": WORKER_HINT}
    if exp is None:
        if experiment_id is not None:
            raise FillrError(404, "NOT_FOUND", "没有这个实验")
        return {**base, "state": "no_experiment", "reason": "no_completed_factor_experiment"}
    base.update(experiment_id=exp["experiment_id"], factor_id=exp["catalog_id"])
    if exp["run_state"] != "completed":
        return {**base, "state": "no_experiment", "reason": f"experiment_{exp['run_state']}"}
    body = _series_artifact(conn, exp["experiment_id"])
    if body is not None:
        return {**base, "state": "ready", "series": {"version": body["version"], "computed_from": body["computed_from"], "items": body["items"]}}
    job = _latest_job(conn, f"factor-series:{exp['experiment_id']}")
    state = "not_computed"
    if job is not None:
        state = {"queued": "queued", "running": "running", "failed": "failed"}.get(job["run_state"], "not_computed")
    return {**base, "state": state, "job": job}


def create_factor_series(conn: psycopg.Connection, pid: UUID, req: dict[str, Any], now: datetime) -> dict[str, Any]:
    exp = _factor_experiment(conn, experiment_id=UUID(req["experiment_id"]), factor_id=None)
    if exp is None:
        raise FillrError(404, "NOT_FOUND", "没有这个实验")
    if exp["run_state"] != "completed":
        raise FillrError(409, "RESULT_NOT_READY", f"因子实验还不是 completed（{exp['run_state']}）")
    if _series_artifact(conn, exp["experiment_id"]) is not None:
        return {"state": "ready", "receipt": None, "worker_hint": WORKER_HINT}
    base = f"factor-series:{exp['experiment_id']}"
    run_id = _enqueue(conn, pid, key=_shared_key(conn, base), kind="factor_series", params={"kind": "factor_series", "experiment_id": str(exp["experiment_id"])}, now=now)
    rec = _receipt(conn, run_id)
    return {"state": "running" if rec["run_state"] == "running" else "queued", "receipt": rec, "worker_hint": WORKER_HINT}


def run_factor_series(conn: psycopg.Connection, lake: LakeStore, params: dict[str, Any]) -> dict[str, Any]:
    exp = _factor_experiment(conn, experiment_id=UUID(params["experiment_id"]), factor_id=None)
    if exp is None or exp["run_state"] != "completed":
        raise FillrError(409, "RESULT_NOT_READY", "因子实验不存在或未完成")
    factor = FL.BY_ID[exp["catalog_id"][3:]]
    spec = SimpleNamespace(**{k: exp[k] for k in ("decision_as_of", "train_start", "train_end", "validation_start", "validation_end")})
    panel0 = S.load_panel(conn, lake, spec.decision_as_of, with_caps=False)
    a, b, c = PS.slice_bounds(panel0.sessions, spec)
    until = date.fromisoformat(panel0.sessions[c]) if c < len(panel0.sessions) else None
    panel = S.load_panel(conn, lake, spec.decision_as_of, until=until, board="main", with_caps=False) if until else panel0
    items = []
    for h in (5, 20):
        for sl, lo, hi in (("train", a, b), ("validation", b, c)):
            r = FL.run_factor(panel.views, panel.sessions, factor, horizon=h, start=lo, end=hi)
            items.append({"horizon": h, "slice": sl, "days": r.days, "ic_mean": PS._q(r.ic_mean), "icir": PS._q(r.icir), "periods": r.long_short_periods,
                          "long_short_return": PS._q(r.long_short_return),
                          "ic": [{"d": d, "ic": PS._q(v)} for d, v in r.ic_series],
                          "long_short": [{"d": d, "period_return": PS._q(p), "cumulative": PS._q(cum)} for d, p, cum in r.long_short_series],
                          "notes": list(r.notes)})
    return {"version": SERIES_VERSION, "experiment_id": str(exp["experiment_id"]), "factor_id": exp["catalog_id"],
            "computed_from": f"factor_lab.run_factor over the sealed context windows of the experiment (train {a}..{b - 1}, validation {b}..{c - 1} by session index)",
            "items": items}


# --------------------------------------------------------------------------------------------------------
# analog study on demand (#42 #68 #158)
# --------------------------------------------------------------------------------------------------------
def _analog_identity(conn: psycopg.Connection, instrument_id: UUID, release_id: UUID, policy: str) -> tuple[str, str, dict[str, Any]]:
    try:
        PS.parse_policy(policy)
        rel = PS.release_row(conn, release_id)
        if conn.execute("SELECT 1 FROM core.instrument WHERE instrument_id=%s", (instrument_id,)).fetchone() is None:
            raise FillrError(404, "NOT_FOUND", "没有这个标的")
        qkey, _ = PS.analog_query_key(conn, instrument_id, rel)
    except PS.StrategyError as e:
        raise FillrError(e.status, e.code, e.message) from e
    return PS.study_identity(qkey, policy, rel["decision_as_of"]), qkey, rel


def analog_status(conn: psycopg.Connection, instrument_id: UUID, release_id: UUID, policy: str) -> dict[str, Any]:
    ident, qkey, rel = _analog_identity(conn, instrument_id, release_id, policy)
    out = {"state": "not_built", "instrument_id": instrument_id, "release_id": release_id, "selection_policy_version": policy, "feature_key_hash": qkey,
           "decision_as_of": rel["decision_as_of"], "job": None, "study": None, "worker_hint": WORKER_HINT}
    man = B.find_derived(conn, PS.ANALOG_DATASET, "identity", ident)
    if man is not None:
        cov = man["coverage_document"] or {}
        return {**out, "state": "ready", "study": {"snapshot_id": man["snapshot_id"], "content_hash": man["content_hash"],
                                                   "horizon_sessions": cov.get("horizon_sessions", 0), "neighbors": cov.get("neighbors", 0)}}
    job = _latest_job(conn, f"analog:{ident}")
    if job is not None:
        out["job"] = job
        out["state"] = {"queued": "queued", "running": "running", "failed": "failed"}.get(job["run_state"], "not_built")
    return out


def create_analog_study(conn: psycopg.Connection, pid: UUID, req: dict[str, Any], now: datetime) -> dict[str, Any]:
    iid, rid, policy = UUID(req["instrument_id"]), UUID(req["release_id"]), req["selection_policy_version"]
    ident, _qkey, _rel = _analog_identity(conn, iid, rid, policy)
    if B.find_derived(conn, PS.ANALOG_DATASET, "identity", ident) is not None:
        return {"state": "ready", "receipt": None, "worker_hint": WORKER_HINT}
    params = {"kind": "analog_study", "instrument_id": str(iid), "release_id": str(rid), "policy": policy}
    run_id = _enqueue(conn, pid, key=_shared_key(conn, f"analog:{ident}"), kind="analog_study", params=params, now=now)
    rec = _receipt(conn, run_id)
    return {"state": "running" if rec["run_state"] == "running" else "queued", "receipt": rec, "worker_hint": WORKER_HINT}


def run_analog(conn: psycopg.Connection, lake: LakeStore, params: dict[str, Any]) -> dict[str, Any]:
    from pulse.application.data_service import DataService

    try:
        return PS.build_analog_study(DataService(conn, lake), UUID(params["instrument_id"]), UUID(params["release_id"]), params["policy"])
    except PS.StrategyError as e:
        raise FillrError(e.status, e.code, e.message) from e


# --------------------------------------------------------------------------------------------------------
# worker
# --------------------------------------------------------------------------------------------------------
def execute_pending(conn: psycopg.Connection, lake: LakeStore, *, clock=utc_now) -> tuple[UUID, str, str] | None:
    """Claim the next queued fillr job and run it to ``succeeded`` or ``failed``. Returns (job_run_id, kind, state) or None when nothing is queued."""
    if conn.execute("SELECT 1 FROM ops.job_run WHERE job_id=%s AND run_state IN ('queued','running') LIMIT 1", (FILLR_JOB,)).fetchone() is None:
        return None
    ensure_job(conn)
    store = PgJobStore(conn)
    job = store.claim(job_id=FILLR_JOB, now=clock())
    if job is None:
        return None
    epoch = job.lease_epoch
    arg = conn.execute("SELECT a.principal_id, e.body FROM ops.job_run_argument a JOIN ops.ext_artifact e ON e.ref=a.parameter_artifact WHERE a.job_run_id=%s",
                       (job.job_run_id,)).fetchone()
    kind = "unknown"
    try:
        if arg is None:
            raise FillrError(500, "NO_PARAMETERS", "任务没有参数记录")
        params = arg["body"]
        kind = params["kind"]
        if kind == "custom_run":
            art = OPS.put_artifact(conn, kind=RESULT_KIND, contract_id=CONTRACT_ID, schema_version=SCHEMA_VERSION,
                                   body=run_custom(conn, lake, params, job.job_run_id), owner=arg["principal_id"])
            out_ref, out_hash = art["ref"], art["content_hash"]
        elif kind == "factor_series":
            art = OPS.put_artifact(conn, kind=SERIES_KIND, contract_id=CONTRACT_ID, schema_version=SCHEMA_VERSION, body=run_factor_series(conn, lake, params))
            out_ref, out_hash = art["ref"], art["content_hash"]
        elif kind == "analog_study":
            man = run_analog(conn, lake, params)
            out_ref, out_hash = f"analog-study:{man['snapshot_id']}", man["content_hash"]
        else:
            raise FillrError(500, "UNKNOWN_KIND", f"未知任务类型 {kind}")
        store.succeed(job.job_run_id, epoch, output_ref=out_ref, output_hash=out_hash, now=clock())
        return job.job_run_id, kind, "succeeded"
    except Exception as exc:  # noqa: BLE001 - a job failure is recorded on the run, never swallowed
        code = (exc.code if isinstance(exc, FillrError) else getattr(exc, "code", None) or type(exc).__name__)
        log.exception("fillr job %s (%s) failed", job.job_run_id, kind)
        store.fail(job.job_run_id, epoch, error_code=str(code)[:60], now=clock())
        return job.job_run_id, kind, "failed"


# --------------------------------------------------------------------------------------------------------
# custom run archive (owner scoped)
# --------------------------------------------------------------------------------------------------------
_RUN_SQL = ("SELECT r.job_run_id, r.run_state, r.error_code, r.scheduled_for, r.completed_at, x.output_ref, p.body AS params"
            " FROM ops.job_run r JOIN ops.job_run_argument a ON a.job_run_id=r.job_run_id JOIN ops.ext_artifact p ON p.ref=a.parameter_artifact"
            " LEFT JOIN ops.job_run_resource x ON x.job_run_id=r.job_run_id WHERE r.job_id=%s AND a.principal_id=%s AND p.body->>'kind'='custom_run'")


def _result_of(conn: psycopg.Connection, pid: UUID, ref: str | None) -> dict[str, Any] | None:
    if ref is None or not ref.startswith(RESULT_KIND):
        return None
    art = OPS.get_artifact(conn, ref, pid)
    return None if art is None else art["body"]


def _brief(row: dict[str, Any], result: dict[str, Any] | None) -> dict[str, Any]:
    p = row["params"]
    summary = None
    if result is not None:
        net = next((m["value"] for m in result["metrics"] if m["name"] == "net_return" and m["slice"] == "overall"), None)
        summary = {"verdict": result["evaluation"]["verdict"], "fitness": result["evaluation"]["fitness"], "net_return": net, "trades": result["trades"]["total"]}
    return {"job_run_id": row["job_run_id"], "run_state": row["run_state"], "error_code": row["error_code"], "scheduled_for": row["scheduled_for"],
            "completed_at": row["completed_at"], "playbook_id": p["playbook_id"], "label": p.get("label"), "request": p["overrides"],
            "strategy": {"strategy_id": p["strategy_id"], "strategy_version": p["strategy_version"]} if p.get("strategy_id") else None, "summary": summary}


def list_custom_runs(conn: psycopg.Connection, pid: UUID, limit: int, cursor: str | None) -> dict[str, Any]:
    if not 1 <= limit <= 100:
        raise FillrError(400, "INVALID_CONTRACT", "limit 必须在 1 到 100 之间")
    sql, args = _RUN_SQL, [FILLR_JOB, pid]
    if cursor:
        try:
            ts, rid = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            args += [datetime.fromisoformat(ts.replace("Z", "+00:00")), UUID(rid)]
        except (ValueError, TypeError) as e:
            raise FillrError(400, "INVALID_CONTRACT", "游标无效") from e
        sql += " AND (r.scheduled_for, r.job_run_id) < (%s, %s)"
    rows = conn.execute(sql + " ORDER BY r.scheduled_for DESC, r.job_run_id DESC LIMIT %s", (*args, limit + 1)).fetchall()
    page = rows[:limit]
    items = [_brief(r, _result_of(conn, pid, r["output_ref"])) for r in page]
    nxt = None
    if len(rows) > limit:
        last = page[-1]
        nxt = base64.urlsafe_b64encode(json.dumps([contracts.utc_z(last["scheduled_for"]), str(last["job_run_id"])]).encode()).decode()
    return {"items": items, "page": {"next_cursor": nxt, "limit": limit}, "order": "按排队时间由新到旧；只显示您自己发起的自定义回测", "worker_hint": WORKER_HINT}


def get_custom_run(conn: psycopg.Connection, pid: UUID, job_run_id: UUID) -> dict[str, Any]:
    row = conn.execute(_RUN_SQL + " AND r.job_run_id=%s", (FILLR_JOB, pid, job_run_id)).fetchone()
    if row is None:
        raise FillrError(404, "NOT_FOUND", "没有这次自定义回测")
    result = _result_of(conn, pid, row["output_ref"])
    return {"run": _brief(row, result), "result": result, "result_ref": row["output_ref"], "worker_hint": WORKER_HINT}


def default_ttl() -> timedelta:
    return timedelta(days=7)
