"""Playbook / factor / tactic registry and the evaluators behind the unified experiment path (PEX11, PEX06, PEX04).

Why: the 52 old playbooks, the 6 factor-lab factors, the exit rules and the intraday-T tactics are REGISTERED as strategy
definitions (lifecycle ``specified``, evidence label ``未检验``) and earn status only through ``pulse.workers.research`` experiments:
EvaluationContext + ExecutionKernel replay with the user cost profile (x2 / x3 stress) and the CSI-300 benchmark. The old
backtest numbers are NOT carried over. This module holds registration, the strategy-draft command (D14), the strategy-version
query, the tactic-replay command (D06/D07), the analog study (D03), interval statistics and the evaluators the worker calls.
Invariants: an evaluator only sees bars strictly before the context's ``validation_end`` (the blind slice is never loaded by a
screening run); a playbook whose data is missing raises ``DataUnavailable`` -> the worker records a FAILED trial with that code;
parameter sweeps store every variant (slice ``variant:<key>`` / ``param:<a>-<b>``), never only the best; the cost profile is the
user's account plan (``user_account_2026``) and its x2 / x3 copies; prices are the forward-adjusted research series (metrics are
``partial`` quality and say so). Authorisation: the platform is single-tenant, scopes (screen:*, strategy:register, research:run)
map to "authenticated principal"; owner checks exist where a row has an owner.
Not covered: promotion (separate decision), blind-final runs, minute-level T, per-regime attribution.
"""
from __future__ import annotations

import math
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

import polars as pl
import psycopg
from psycopg.types.json import Jsonb

from pulse import contracts
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import market_repo, research_repo, strategy_repo
from pulse.adapters.postgres._canon import sha256_of
from pulse.adapters.postgres.jobs import PgJobStore
from pulse.application import bindings as B
from pulse.application import screen_service as S
from pulse.application.bootstrap import SCHEMA_ID
from pulse.application.ports.jobs import JobDefinition
from pulse.domain.analogs import analog as AN
from pulse.domain.analogs import interval as IV
from pulse.domain.execution import KERNEL_VERSION, PathScenario
from pulse.domain.features.key import key_hash
from pulse.domain.market_rules import Board, Exchange, InstrumentSpec
from pulse.domain.market_rules.costs import CostProfile, scaled, user_account_2026
from pulse.domain.playbooks import exits as EX
from pulse.domain.playbooks import library as L
from pulse.domain.playbooks.builder import prepare, run_signal
from pulse.domain.playbooks.replay import PRICE_BASIS, ReplayConfig, replay
from pulse.domain.playbooks.tactics import run_exit_rules, run_intraday_t
from pulse.domain.research import experiments as E
from pulse.domain.screening import factor_lab as FL
from pulse.domain.strategy.canonical import canonical_hash, stable_uuid
from pulse.domain.strategy.seed import TAXONOMY_VERSION, CatalogSeed, DefinitionSeed, InstanceSeed, TagSeed

TACTIC_JOB = UUID("8f1b6d3c-2a47-4e90-b5c8-6d0e9a3f7c12")
FORECAST_TARGET = "rel_return_20td_vs_universe_median_after_declared_costs_v1"   # same as the worker's fixed context
EVIDENCE_LABEL = "unverified"                                                    # displayed as 未检验
METRIC_VERSION = "playbook-replay-v1"
FACTOR_METRIC_VERSION = "factor-lab-v1"
TACTIC_METRIC_VERSION = "tactic-replay-v1"
DEFAULT_CASH = Decimal("1000000")
VENUE = {"XSHG": Exchange.SSE, "XSHE": Exchange.SZSE, "XBSE": Exchange.BSE}
BOARD = {"main": Board.MAIN, "chinext": Board.CHINEXT, "star": Board.STAR, "bse": Board.BSE}
PERMANENT_MISSING = {"fundamentals_pit": "没有 PIT 基本面", "sector_membership_history": "没有按时点保存的板块成员",
                     "instrument_name_history": "没有简称历史", "lhb_seat_trades_history": "席位成交未入库"}


class DataUnavailable(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class StrategyError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# --------------------------------------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------------------------------------
def _horizon_of(days: int) -> str:
    return f"{days}td"


def _seed(defs: list[DefinitionSeed], insts: list[InstanceSeed], tags: list[TagSeed]) -> CatalogSeed:
    nodes: dict[str, set[str]] = {}
    for t in tags:
        nodes.setdefault(t.taxonomy_id, set()).add(t.node_id)
    return CatalogSeed(tuple(defs), tuple(insts), tuple(tags), {k: tuple(sorted(v)) for k, v in sorted(nodes.items())})


def _dsl(kind: str, key: str, body: dict) -> dict:
    """D14 payload: the typed rule document (the code lives in the registered interpreter named by schema_id)."""
    art = canonical_hash(body)
    return {"schema_id": f"pulse.{kind}@0.1", "ast_artifact": {"ref": f"strategy-body:{kind}:{key}", "content_hash": art,
                                                               "contract_id": f"pulse.{kind}", "schema_version": "0.1"},
            "source_license_ref": None, "translation_manifest": None}


def playbook_seed() -> CatalogSeed:
    defs, insts, tags = [], [], []
    for pid in L.ORDER:
        pb = L.REGISTRY[pid]
        sid = stable_uuid("playbook", pid)
        params = pb.default_params()
        body = {"id": pid, "name": pb.name, "family": pb.family, "style": pb.style, "horizon": pb.horizon, "rule": pb.rule, "source": pb.source,
                "params": [p.__dict__ for p in pb.params], "exits": pb.exits.__dict__, "max_positions": pb.max_positions,
                "max_new_per_day": pb.max_new_per_day, "requires": list(pb.requires), "unavailable_reason": pb.unavailable_reason}
        spec = {"catalog_id": f"PB:{pid}", "family_id": f"playbook.{pb.family}", "playbook": body, "evidence_label": "未检验",
                "dsl": _dsl("playbook", pid, body), "origin": "legacy_playbook_library_v1 (rules only; old backtest numbers not carried)",
                "hypothesis": pb.rule[:400]}
        defs.append(DefinitionSeed(f"PB:{pid}", sid, 1, pb.name, "selection", "rule", FORECAST_TARGET, _horizon_of(pb.exits.holding_days), spec, canonical_hash(spec)))
        insts.append(InstanceSeed(stable_uuid("playbook-instance", pid, canonical_hash(params)), sid, 1, params, canonical_hash(params),
                                  "screen-universe-main-v1", "specified", "implemented", "not_started", "unavailable" if pb.unavailable_reason else "healthy"))
        for tax, node in (("playbook.family", pb.family), ("strategy.evidence_label", EVIDENCE_LABEL), ("playbook.style", pb.style)):
            tags.append(TagSeed(sid, 1, tax, node, f"playbook:{pid}", TAXONOMY_VERSION))
    return _seed(defs, insts, tags)


def factor_seed() -> CatalogSeed:
    defs, insts, tags = [], [], []
    for f in FL.FACTORS:
        sid = stable_uuid("lab-factor", f.id)
        body = {"id": f.id, "name": f.name, "description": f.description, "horizons": [5, 20], "quantiles": FL.QUANTILES}
        spec = {"catalog_id": f"FL:{f.id}", "family_id": "factor_lab", "factor": body, "evidence_label": "未检验", "dsl": _dsl("factor", f.id, body),
                "origin": "legacy_factor_lab_v1", "hypothesis": f.description}
        defs.append(DefinitionSeed(f"FL:{f.id}", sid, 1, f.name, "selection", "factor", FORECAST_TARGET, "20td", spec, canonical_hash(spec)))
        insts.append(InstanceSeed(stable_uuid("factor-instance", f.id), sid, 1, {"horizons": [5, 20]}, canonical_hash({"horizons": [5, 20]}),
                                  "screen-universe-main-v1", "specified", "implemented", "not_started", "healthy"))
        for tax, node in (("factor.lab", "lab6"), ("strategy.evidence_label", EVIDENCE_LABEL)):
            tags.append(TagSeed(sid, 1, tax, node, f"factor:{f.id}", TAXONOMY_VERSION))
    return _seed(defs, insts, tags)


T_GRID = ((1.0, 1.0), (2.0, 1.0), (3.0, 2.0))


def tactic_seed() -> CatalogSeed:
    defs, insts, tags = [], [], []
    base = EX.ExitRules(holding_days=20)
    for key, label, ch in EX.VARIANTS:
        sid = stable_uuid("exit-rule", key)
        rules = EX.variant(base, key)
        body = {"kind": "exit_rules", "variant": key, "label": label, "rules": rules.to_json()}
        spec = {"catalog_id": f"EX:{key}", "family_id": "exit_lab", "tactic": body, "evidence_label": "未检验", "dsl": _dsl("tactic", key, body),
                "origin": "legacy_exit_lab_v1", "hypothesis": f"卖法「{label}」相对持有不动有净增量"}
        defs.append(DefinitionSeed(f"EX:{key}", sid, 1, f"卖法：{label}", "exit", "rule", "incremental_nav_vs_hold_after_costs_v1", "replay", spec, canonical_hash(spec)))
        p = rules.to_json()
        insts.append(InstanceSeed(stable_uuid("exit-instance", key), sid, 1, p, canonical_hash(p), "replay-inventory", "specified", "implemented", "not_started", "healthy"))
        tags.append(TagSeed(sid, 1, "strategy.evidence_label", EVIDENCE_LABEL, f"exit:{key}", TAXONOMY_VERSION))
    for mode, name in (("reverse", "倒T"), ("forward", "正T")):
        sid = stable_uuid("t-rule", mode)
        body = {"kind": "intraday_t", "mode": mode, "grid": [list(g) for g in T_GRID]}
        spec = {"catalog_id": f"TT:{mode}", "family_id": "exit_lab", "tactic": body, "evidence_label": "未检验", "dsl": _dsl("tactic", mode, body),
                "origin": "legacy_exit_lab_v1", "hypothesis": f"{name}相对持有不动有净增量（日线需声明路径假设）"}
        defs.append(DefinitionSeed(f"TT:{mode}", sid, 1, f"做T：{name}", "execution", "rule", "incremental_nav_vs_hold_after_costs_v1", "replay", spec, canonical_hash(spec)))
        for up, rb in T_GRID:
            p = {"mode": mode, "up_pct": up, "rebuy_pct": rb, "share": 0.5}
            insts.append(InstanceSeed(stable_uuid("t-instance", mode, up, rb), sid, 1, p, canonical_hash(p), "replay-inventory", "specified", "implemented", "not_started", "healthy"))
        tags.append(TagSeed(sid, 1, "strategy.evidence_label", EVIDENCE_LABEL, f"t:{mode}", TAXONOMY_VERSION))
    return _seed(defs, insts, tags)


def tactic_hypothesis_id(instance_id: UUID) -> UUID:
    return stable_uuid("tactic-hypothesis", instance_id)


def register_all(conn: psycopg.Connection, now: datetime | None = None) -> dict[str, dict[str, int]]:
    now = now or datetime.now(timezone.utc)
    out = {}
    with conn.transaction():
        for name, seed in (("playbooks", playbook_seed()), ("factors", factor_seed()), ("tactics", tactic_seed())):
            out[name] = strategy_repo.seed_candidates(conn, seed, now=now)
        for d, i in ((d, i) for d in tactic_seed().definitions for i in tactic_seed().instances if i.strategy_id == d.strategy_id):
            research_repo.register_hypothesis(
                conn, hypothesis_id=tactic_hypothesis_id(i.instance_id), hypothesis_version=1, claim=str(d.specification["hypothesis"]),
                mechanism="declared exit / intraday-T rule evaluated against the same-inventory hold baseline", forecast_target=d.forecast_target,
                horizon_id=d.horizon_id, input_requirements={"replay_spec": "pulse.replay_spec@0.1", "baseline": "same-inventory-hold-v1"},
                falsification_conditions={"incremental_pnl_vs_hold_after_costs": "<= 0 on the replay window"}, available_at=now,
                strategy=(d.strategy_id, d.strategy_version))
        seed_jobs(conn)
    return out


def replay_context(conn: psycopg.Connection) -> dict:
    """What the tactic-replay form needs: the latest sealed context and the registered tactic instances with their hypotheses."""
    ctx = conn.execute("SELECT * FROM research.evaluation_context WHERE sealed_at IS NOT NULL ORDER BY created_at DESC LIMIT 1").fetchone()
    insts = conn.execute(
        "SELECT i.instance_id, i.strategy_id, i.strategy_version, d.display_name, d.specification->'tactic' AS tactic, i.parameters FROM strategy.instance i"
        " JOIN strategy.definition d USING (strategy_id, strategy_version) WHERE d.specification ? 'tactic' ORDER BY d.display_name, i.instance_id").fetchall()
    return contracts.to_wire({
        "evaluation_context": None if ctx is None else {
            "evaluation_context_id": ctx["evaluation_context_id"], "train_start": ctx["train_start"], "train_end": ctx["train_end"],
            "validation_start": ctx["validation_start"], "validation_end": ctx["validation_end"], "cost_policy_version": ctx["cost_policy_version"],
            "decision_as_of": ctx["decision_as_of"]},
        "cost_policy_versions": [user_account_2026().version, scaled(user_account_2026(), 2).version, scaled(user_account_2026(), 3).version],
        "instances": [{"instance_id": r["instance_id"], "strategy_id": r["strategy_id"], "strategy_version": r["strategy_version"], "display_name": r["display_name"],
                       "tactic": r["tactic"], "parameters": r["parameters"], "hypothesis_id": tactic_hypothesis_id(r["instance_id"]), "hypothesis_version": 1}
                      for r in insts]})


_TACTIC_JOBS_READY = False


def ensure_jobs(conn: psycopg.Connection) -> None:
    global _TACTIC_JOBS_READY
    if not _TACTIC_JOBS_READY:
        seed_jobs(conn)
        _TACTIC_JOBS_READY = True


def seed_jobs(conn: psycopg.Connection) -> None:
    S.seed_jobs(conn)
    PgJobStore(conn).register_definition(JobDefinition(
        job_id=TACTIC_JOB, job_version=1, application_operation="PlaybookService.run_tactic_replay",
        schedule_document={"trigger": "command", "timezone": "Asia/Shanghai"}, parameter_contract_id=SCHEMA_ID,
        parameter_schema_version=contracts.CONTRACT_VERSION, enabled=True, concurrency_key="tactic", max_concurrency=1,
        timeout_seconds=3600, retry_policy={"max_attempts": 1}))


# --------------------------------------------------------------------------------------------------------
# strategy draft / version (PEX11)
# --------------------------------------------------------------------------------------------------------
def _strategy_wire(conn: psycopg.Connection, sid: UUID, version: int) -> dict:
    d = conn.execute("SELECT * FROM strategy.definition WHERE strategy_id=%s AND strategy_version=%s", (sid, version)).fetchone()
    if d is None:
        raise StrategyError(404, "NOT_FOUND", "策略版本不存在")
    insts = conn.execute("SELECT * FROM strategy.instance WHERE strategy_id=%s AND strategy_version=%s ORDER BY created_at, instance_id", (sid, version)).fetchall()
    tags = conn.execute("SELECT * FROM strategy.tag_assignment WHERE strategy_id=%s AND strategy_version=%s ORDER BY taxonomy_id, node_id", (sid, version)).fetchall()
    return contracts.to_wire({
        "strategy_id": d["strategy_id"], "strategy_version": d["strategy_version"], "display_name": d["display_name"], "role": d["role"],
        "method_family": d["method_family"], "forecast_target": d["forecast_target"], "horizon_id": d["horizon_id"],
        "definition_hash": d["definition_hash"], "specification": d["specification"],
        "tags": [{"taxonomy_id": t["taxonomy_id"], "taxonomy_version": t["taxonomy_version"], "node_id": t["node_id"]} for t in tags],
        "instances": [{k: i[k] for k in ("instance_id", "strategy_id", "strategy_version", "parameter_hash", "universe_policy_version", "lifecycle",
                                         "implementation_status", "reproduction_status", "health")} for i in insts]})


def register_strategy_draft(conn: psycopg.Connection, pid: UUID, body: dict, now: datetime) -> tuple[dict, list[dict]]:
    """Handler of registerStrategyDraft. Definitions are immutable; a revision appends version+1 (CAS on the latest version)."""
    draft = body["draft"]
    sid = UUID(body["strategy_id"])
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 12))", (str(sid),))
    head = conn.execute("SELECT max(strategy_version) AS v FROM strategy.definition WHERE strategy_id=%s", (sid,)).fetchone()["v"] or 0
    if body["expected_latest_version"] != head:
        raise StrategyError(409, "VERSION_CONFLICT", f"策略最新版本为 {head}，请求基于 {body['expected_latest_version']}")
    if head:
        prev = conn.execute("SELECT specification FROM strategy.definition WHERE strategy_id=%s AND strategy_version=%s", (sid, head)).fetchone()
        if (prev["specification"].get("registration") or {}).get("owner_principal_id") not in (None, str(pid)):
            raise StrategyError(403, "FORBIDDEN", "只有登记人可以修订该策略")
    for pref in draft["parent_instances"]:
        ok_ = conn.execute("SELECT 1 FROM strategy.instance WHERE instance_id=%s AND strategy_id=%s AND strategy_version=%s",
                           (pref["instance_id"], pref["strategy_id"], pref["strategy_version"])).fetchone()
        if not ok_:
            raise StrategyError(404, "NOT_FOUND", f"父实例不存在：{pref['instance_id']}")
    art = draft["specification_artifact"]
    dup = conn.execute(
        "SELECT strategy_id, strategy_version FROM strategy.definition WHERE specification->'dsl'->'ast_artifact'->>'content_hash'=%s"
        " AND NOT (strategy_id=%s)", (art["content_hash"], sid)).fetchone()
    version = head + 1
    spec = {"dsl": {"schema_id": draft["specification_contract_id"], "schema_version": draft["specification_schema_version"], "ast_artifact": art,
                    "source_license_ref": None, "translation_manifest": None},
            "registration": {"owner_principal_id": str(pid), "status": "specified", "registered_at": contracts.utc_z(now),
                             "production_permission": False, "implementation_status": "not_implemented"},
            "evidence_label": "未检验", "parents": [dict(p) for p in draft["parent_instances"]]}
    dh = canonical_hash({"name": draft["display_name"], "role": draft["role"], "family": draft["method_family"], "target": draft["forecast_target"],
                         "horizon": draft["horizon_id"], "spec": spec["dsl"]})
    conn.execute(
        "INSERT INTO strategy.definition(strategy_id, strategy_version, display_name, role, method_family, forecast_target, horizon_id,"
        " specification, definition_hash, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (sid, version, draft["display_name"], draft["role"], draft["method_family"], draft["forecast_target"], draft["horizon_id"],
         Jsonb(spec), dh, now))
    for pref in draft["parent_instances"]:
        conn.execute("INSERT INTO research.strategy_parent(strategy_id, strategy_version, parent_strategy_id, parent_strategy_version,"
                     " contribution_role, composition_policy_version, evidence_ref) VALUES (%s,%s,%s,%s,'blend','draft-lineage-v1',%s)",
                     (sid, version, pref["strategy_id"], pref["strategy_version"], f"draft:{pid}"))
    iid = stable_uuid("draft-instance", sid, version)
    conn.execute(
        "INSERT INTO strategy.instance(instance_id, strategy_id, strategy_version, parameters, parameter_hash, universe_policy_version,"
        " lifecycle, implementation_status, reproduction_status, health, created_at) VALUES (%s,%s,%s,'{}'::jsonb,%s,'screen-universe-main-v1',"
        "'specified','not_implemented','not_started','unavailable',%s)", (iid, sid, version, canonical_hash({}), now))
    warnings = []
    if dup:
        warnings.append({"code": "DUPLICATE_FORMULA", "message": f"相同公式已登记为 {dup['strategy_id']} v{dup['strategy_version']}",
                         "affected_component": "strategy", "evidence_ref": f"{dup['strategy_id']}:{dup['strategy_version']}"})
    return {"strategy": _strategy_wire(conn, sid, version), "instance_id": str(iid)}, warnings


def query_strategy_version(conn: psycopg.Connection, pid: UUID, sid: UUID, version: int) -> dict:
    s = _strategy_wire(conn, sid, version)
    owner = (s["specification"].get("registration") or {}).get("owner_principal_id")
    if owner is not None and owner != str(pid):
        raise StrategyError(404, "NOT_FOUND", "策略版本不存在")        # private drafts are invisible to others
    ev = conn.execute(
        "SELECT r.experiment_id FROM research.experiment_run r JOIN strategy.instance i USING (instance_id)"
        " WHERE i.strategy_id=%s AND i.strategy_version=%s ORDER BY r.experiment_id", (sid, version)).fetchall()
    return {"strategy": s, "evaluation_ids": [str(r["experiment_id"]) for r in ev]}


# --------------------------------------------------------------------------------------------------------
# context windows and metrics
# --------------------------------------------------------------------------------------------------------
def _cst(dt: datetime) -> date:
    return dt.astimezone(B.CST).date()


def slice_bounds(sessions: list[str], spec) -> tuple[int, int, int]:
    """(train_start_idx, validation_start_idx, validation_end_idx): session indexes of the sealed context windows."""
    def first_ge(d: date) -> int:
        k = d.isoformat()
        return next((i for i, s in enumerate(sessions) if s >= k), len(sessions))
    return first_ge(_cst(spec.train_start)), first_ge(_cst(spec.train_end)), first_ge(_cst(spec.validation_end))


def _q(x: float | Decimal | None) -> Decimal | None:
    if x is None:
        return None
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return Decimal(repr(round(float(x), 10))) if not isinstance(x, Decimal) else x.quantize(Decimal("0.000000000001"))


def metric(name: str, value, unit: str, n: int, *, slice_key: str = "overall", version: str = METRIC_VERSION, quality: str = "partial",
           reason: str | None = None) -> dict:
    v = _q(value)
    if v is None and not reason:
        reason = "not_computable"
    return {"metric_name": name, "metric_definition_version": version, "slice_key": slice_key, "value": v, "unit": unit, "lower_bound": None,
            "upper_bound": None, "confidence_level": None, "sample_count": n, "quality_status": quality, "missing_reason": None if v is not None else reason}


def _specs(panel: S.Panel) -> dict[str, InstrumentSpec]:
    out = {}
    for v in panel.views:
        m = panel.metas[v.instrument_id]
        if m.venue_code in VENUE and m.board_code in BOARD:
            out[v.instrument_id] = InstrumentSpec(v.instrument_id, VENUE[m.venue_code], BOARD[m.board_code], bool(m.is_risk_warning))
    return out


def _bench(panel: S.Panel, sessions: list[str], a: int, b: int) -> Decimal | None:
    """CSI-300 return from the close before session a to the close of session b-1."""
    if b - 1 >= len(sessions) or b - 1 <= max(a - 1, 0):
        return None
    c0 = panel.benchmark.get(date.fromisoformat(sessions[max(a - 1, 0)]))     # a == 0: base is the first close (one session shorter)
    c1 = panel.benchmark.get(date.fromisoformat(sessions[b - 1]))
    return None if not c0 or not c1 else Decimal(repr(c1 / c0 - 1))


# --------------------------------------------------------------------------------------------------------
# evaluators (called by the research worker)
# --------------------------------------------------------------------------------------------------------
def evaluate_playbook(conn: psycopg.Connection, lake: LakeStore, spec, pb: L.Playbook, params: dict[str, float]) -> list[dict]:
    for req in pb.requires:
        if req in PERMANENT_MISSING:
            raise DataUnavailable(f"data_not_available:{req}")
        if req == "float_cap":
            raise DataUnavailable("data_not_available:float_cap_history_not_point_in_time")
    panel0 = S.load_panel(conn, lake, spec.decision_as_of, with_caps=False)
    a, b, c = slice_bounds(panel0.sessions, spec)
    until = date.fromisoformat(panel0.sessions[c]) if c < len(panel0.sessions) else None
    panel = S.load_panel(conn, lake, spec.decision_as_of, until=until, board="main", with_caps=False) if until else panel0
    sessions = panel.sessions
    prepare(panel.views, sessions, [pb])
    sigs = {v.instrument_id: run_signal(pb, v, params) for v in panel.views}
    nsig = sum(1 for s in sigs.values() for x in s[a:c] if x is not None)
    specs = _specs(panel)
    views = {v.instrument_id: v for v in panel.views if v.instrument_id in specs}
    base = user_account_2026()
    runs = {}
    for tag, profile in (("x1", base), ("x2", scaled(base, 2)), ("x3", scaled(base, 3))):
        cfg = ReplayConfig(DEFAULT_CASH, profile, a, c, pb.max_positions, pb.max_new_per_day, EX.ExitRules.from_spec(pb.exits))
        runs[tag] = replay(signals={k: v for k, v in sigs.items() if k in views}, views=views, specs=specs, sessions=sessions, cfg=cfg, run_id=f"{pb.id}:{tag}")
    return _replay_metrics(runs, panel, sessions, a, b, c, nsig)


def _nav_at(nav: list, idx_from_start: int) -> Decimal | None:
    return nav[idx_from_start][1] if 0 <= idx_from_start < len(nav) else None


def _replay_metrics(runs: dict, panel: S.Panel, sessions: list[str], a: int, b: int, c: int, nsig: int) -> list[dict]:
    r1 = runs["x1"]
    n_sess = c - a
    trades = [t for t in r1.trades]
    closed = [t for t in trades if t.gross_return is not None]
    m: list[dict] = []
    no_trade = len(trades) == 0

    def ret(run, lo: int, hi: int) -> Decimal | None:
        start = DEFAULT_CASH if lo == a else _nav_at(run.nav, lo - a - 1)
        end = _nav_at(run.nav, hi - a - 1)
        return None if start is None or end is None or start == 0 else end / start - 1

    for tag, run in runs.items():
        suffix = "" if tag == "x1" else f"_cost_{tag}"
        for sl, lo, hi in (("overall", a, c), ("train", a, b), ("validation", b, c)):
            if tag != "x1" and sl == "train":
                continue
            v = None if no_trade else ret(run, lo, hi)
            m.append(metric(f"net_return{suffix}", v, "ratio", max(hi - lo, 0), slice_key=sl, reason="no_trades_in_window"))
    for sl, lo, hi in (("overall", a, c), ("train", a, b), ("validation", b, c)):
        bench = _bench(panel, sessions, lo, hi)
        m.append(metric("benchmark_return_csi300", bench, "ratio", max(hi - lo, 0), slice_key=sl, reason="benchmark_missing"))
        net = None if no_trade else ret(r1, lo, hi)
        m.append(metric("excess_return_vs_csi300", None if net is None or bench is None else net - bench, "ratio", max(hi - lo, 0), slice_key=sl,
                        reason="no_trades_or_benchmark_missing"))
    navs = [n for _, n in r1.nav if n is not None]
    peak, mdd = DEFAULT_CASH, Decimal(0)
    for n in navs:
        peak = max(peak, n)
        mdd = min(mdd, n / peak - 1)
    m.append(metric("max_drawdown", None if no_trade else mdd, "ratio", n_sess, reason="no_trades_in_window"))
    m.append(metric("trade_count", Decimal(len(trades)), "count", len(trades), quality="valid"))
    m.append(metric("closed_trade_count", Decimal(len(closed)), "count", len(closed), quality="valid"))
    gr = [t.gross_return for t in closed]
    m.append(metric("win_rate_gross", Decimal(sum(1 for x in gr if x > 0)) / len(gr) if gr else None, "ratio", len(gr), reason="no_closed_trades"))
    m.append(metric("avg_trade_return_gross", sum(gr) / len(gr) if gr else None, "ratio", len(gr), reason="no_closed_trades"))
    m.append(metric("fees_paid", r1.fees, "CNY", len(trades), quality="valid"))
    m.append(metric("signal_count", Decimal(nsig), "count", n_sess, quality="valid"))
    for k in ("entry_filled", "entry_unfilled", "exit_filled", "exit_unfilled"):
        m.append(metric(k, Decimal(r1.counters.get(k, 0)), "count", n_sess, quality="valid"))
    rejected = sum(v for k, v in r1.counters.items() if ":" in k and k.split(":")[0].endswith("rejected"))
    m.append(metric("orders_rejected", Decimal(rejected), "count", n_sess, quality="valid"))
    m.append(metric("price_basis_forward_adjusted_research_only", Decimal(1), "flag", n_sess, quality="partial"))
    return m


def evaluate_factor(conn: psycopg.Connection, lake: LakeStore, spec, factor: FL.FactorDef) -> list[dict]:
    panel0 = S.load_panel(conn, lake, spec.decision_as_of, with_caps=False)
    a, b, c = slice_bounds(panel0.sessions, spec)
    until = date.fromisoformat(panel0.sessions[c]) if c < len(panel0.sessions) else None
    panel = S.load_panel(conn, lake, spec.decision_as_of, until=until, board="main", with_caps=False) if until else panel0
    out: list[dict] = []
    for h in (5, 20):
        for sl, lo, hi in (("train", a, b), ("validation", b, c)):
            r = FL.run_factor(panel.views, panel.sessions, factor, horizon=h, start=lo, end=hi)
            v = FACTOR_METRIC_VERSION
            n = r.days
            pairs = [("rank_ic_mean", r.ic_mean, "ratio"), ("rank_ic_std", r.ic_std, "ratio"), ("icir", r.icir, "ratio"), ("ic_t_stat", r.ic_t, "ratio"),
                     ("ic_positive_share", r.ic_positive_share, "ratio"), ("long_short_return", r.long_short_return, "ratio"),
                     ("top_group_turnover", r.top_turnover, "ratio")]
            for name, val, unit in pairs:
                out.append(metric(f"{name}_h{h}", val, unit, n, slice_key=sl, version=v, reason="cross_section_too_thin_or_window_too_short"))
            for q, val in enumerate(r.quantile_mean_return, start=1):
                out.append(metric(f"quantile_return_q{q}_h{h}", val, "ratio", r.quantile_count[q - 1], slice_key=sl, version=v, reason="empty_bucket"))
            out.append(metric(f"ic_days_h{h}", Decimal(n), "count", n, slice_key=sl, version=v, quality="valid"))
    return out


def window_of_replay(sessions: list[str], w: dict) -> tuple[int, int]:
    a = _cst(datetime.fromisoformat(w["start"].replace("Z", "+00:00"))).isoformat()
    z = _cst(datetime.fromisoformat(w["end"].replace("Z", "+00:00"))).isoformat()
    return (next((i for i, s in enumerate(sessions) if s >= a), len(sessions)), next((i for i, s in enumerate(sessions) if s >= z), len(sessions)))


def evaluate_tactic(conn: psycopg.Connection, lake: LakeStore, spec_row: dict, replay_spec: dict, tactic: dict) -> list[dict]:
    asof = spec_row["decision_as_of"]
    end_dt = datetime.fromisoformat(replay_spec["window"]["end"].replace("Z", "+00:00"))
    panel = S.load_panel(conn, lake, asof, until=_cst(end_dt), with_caps=False)
    sessions = panel.sessions
    a, z = window_of_replay(sessions, replay_spec["window"])
    if z - a < 2:
        raise DataUnavailable("replay_window_has_fewer_than_two_sessions")
    specs = _specs(panel)
    by = panel.by_id()
    ids = [i for i in replay_spec["instrument_ids"] if i in by and i in specs]
    if len(ids) != len(replay_spec["instrument_ids"]):
        raise DataUnavailable("replay_instrument_without_research_bars")
    views = {i: by[i] for i in ids}
    lots: dict[str, tuple[int, Decimal]] = {}
    for lot in replay_spec["opening_lots"]:
        q0, c0 = lots.get(lot["instrument_id"], (0, Decimal(0)))
        lots[lot["instrument_id"]] = (q0 + lot["quantity"], c0 + Decimal(lot["cost_cny"]))
    if any(i not in views for i in lots):
        raise DataUnavailable("opening_lot_instrument_not_in_replay_set")
    cash = Decimal(replay_spec["cash_cny"])
    profile = _profile_of(replay_spec["cost_policy_version"])
    common = dict(views=views, specs=specs, sessions=sessions, start=a, end=z, cash=cash, lots=lots, profile=profile)
    m: list[dict] = []
    V = TACTIC_METRIC_VERSION
    n = z - a

    def put(res, slice_key: str) -> None:
        c = res.comparison
        m.extend([
            metric("incremental_pnl_vs_hold", c.incremental_pnl, "CNY", n, slice_key=slice_key, version=V),
            metric("baseline_nav", c.baseline_nav, "CNY", n, slice_key=slice_key, version=V),
            metric("active_nav", c.active_nav, "CNY", n, slice_key=slice_key, version=V),
            metric("fees_active", c.active_fees, "CNY", n, slice_key=slice_key, version=V),
            metric("cash_delta", c.cash_delta, "CNY", n, slice_key=slice_key, version=V),
            metric("inventory_value_delta", c.inventory_value_delta, "CNY", n, slice_key=slice_key, version=V),
            metric("inventory_shortfall_shares", Decimal(sum(c.inventory_shortfall.values())), "shares", n, slice_key=slice_key, version=V, quality="valid"),
            metric("legs_attempted", Decimal(res.legs_attempted), "count", n, slice_key=slice_key, version=V, quality="valid"),
            metric("legs_completed", Decimal(res.legs_completed), "count", n, slice_key=slice_key, version=V, quality="valid")])

    if tactic["kind"] == "exit_rules":
        rules = EX.ExitRules.from_json(tactic["rules"])
        put(run_exit_rules(rules=rules, label=tactic["label"], run_id="tx", **common)[0], "replay")
        base = EX.ExitRules(holding_days=rules.holding_days)
        for key, label, _ in EX.VARIANTS:                                  # the whole family, not only the chosen member
            put(run_exit_rules(rules=EX.variant(base, key), label=label, run_id=f"tv{key}", **common)[0], f"variant:{key}")
    else:
        mode, share = tactic["mode"], float(tactic.get("share", 0.5))
        up, rb = float(tactic["up_pct"]), float(tactic["rebuy_pct"])
        res = {}
        for tag, path in (("path_ohlc", PathScenario.OHLC), ("path_olhc", PathScenario.OLHC)):
            res[tag] = run_intraday_t(mode=mode, up_pct=up, rebuy_pct=rb, share=share, path=path, **common)
            put(res[tag], tag)
        spread = abs(res["path_ohlc"].comparison.incremental_pnl - res["path_olhc"].comparison.incremental_pnl)
        m.append(metric("path_sensitivity", spread, "CNY", n, slice_key="path_spread", version=V))
        for gu, gr in T_GRID:                                              # sweep: every grid point is stored
            for tag, path in (("path_ohlc", PathScenario.OHLC), ("path_olhc", PathScenario.OLHC)):
                put(run_intraday_t(mode=mode, up_pct=gu, rebuy_pct=gr, share=share, path=path, **common), f"param:{gu:g}-{gr:g}:{tag}")
    return m


def _profile_of(version: str) -> CostProfile:
    base = user_account_2026()
    for k in (1, 2, 3):
        p = base if k == 1 else scaled(base, k)
        if p.version == version:
            return p
    raise DataUnavailable(f"cost_policy_not_registered:{version}")


# --------------------------------------------------------------------------------------------------------
# worker evaluator hook
# --------------------------------------------------------------------------------------------------------
def evaluate_instance(conn: psycopg.Connection, lake: LakeStore, ctx_spec, rec: E.ExperimentRecord) -> list[dict]:
    row = conn.execute("SELECT i.parameters, d.specification FROM strategy.instance i JOIN strategy.definition d USING (strategy_id, strategy_version)"
                       " WHERE i.instance_id=%s", (rec.instance_id,)).fetchone()
    spec = row["specification"]
    if "playbook" in spec:
        pb = L.REGISTRY[spec["playbook"]["id"]]
        return evaluate_playbook(conn, lake, ctx_spec, pb, {k: float(v) for k, v in row["parameters"].items()})
    if "factor" in spec:
        return evaluate_factor(conn, lake, ctx_spec, FL.BY_ID[spec["factor"]["id"]])
    raise DataUnavailable("no_evaluator_registered")


# --------------------------------------------------------------------------------------------------------
# scheduleTacticReplay (PEX06)
# --------------------------------------------------------------------------------------------------------
def schedule_tactic_replay(conn: psycopg.Connection, pid: UUID, body: dict, key: str, now: datetime) -> dict:
    rs = body["replay_spec"]
    if rs["training_mode"] == "no_fit":
        raise StrategyError(422, "UNSUPPORTED_CONTEXT_SHAPE", "no_fit 回放需要 D07 新上下文版本（train 窗口可为空）；未登记前不接受，也不伪造训练窗")
    if rs["training_mode"] not in ("fitted", "frozen"):
        raise StrategyError(400, "INVALID_CONTRACT", "training_mode 必须是 fitted / frozen / no_fit")
    if rs["benchmark_policy_version"] != "same-inventory-hold-v1":
        raise StrategyError(400, "INVALID_CONTRACT", "benchmark_policy_version 目前只支持 same-inventory-hold-v1")
    if rs["cost_policy_version"] not in {user_account_2026().version, scaled(user_account_2026(), 2).version, scaled(user_account_2026(), 3).version}:
        raise StrategyError(400, "INVALID_CONTRACT", f"未登记的费用口径：{rs['cost_policy_version']}")
    for lot in rs["opening_lots"]:
        if lot["instrument_id"] not in rs["instrument_ids"]:
            raise StrategyError(400, "INVALID_CONTRACT", "opening_lots 的标的必须在 instrument_ids 内")
        if lot["eligible_on"] > _cst(datetime.fromisoformat(rs["window"]["start"].replace("Z", "+00:00"))).isoformat():
            raise StrategyError(422, "UNSUPPORTED_CONTEXT_SHAPE", "底仓 eligible_on 晚于回放起点：账本只接受起点已可卖的底仓")
    inst = conn.execute("SELECT i.*, d.role, d.specification FROM strategy.instance i JOIN strategy.definition d USING (strategy_id, strategy_version)"
                        " WHERE i.instance_id=%s", (body["instance_id"],)).fetchone()
    if inst is None:
        raise StrategyError(404, "NOT_FOUND", "策略实例不存在")
    tactic = (inst["specification"] or {}).get("tactic")
    if tactic is None:
        raise StrategyError(409, "INVALID_CONTRACT", "该实例不是已登记的战术（exit_rules / intraday_t）")
    ctx = conn.execute("SELECT * FROM research.evaluation_context WHERE evaluation_context_id=%s AND sealed_at IS NOT NULL", (body["evaluation_context_id"],)).fetchone()
    if ctx is None:
        raise StrategyError(404, "NOT_FOUND", "评价上下文不存在或未封存")
    hyp = conn.execute("SELECT 1 FROM research.hypothesis WHERE hypothesis_id=%s AND hypothesis_version=%s", (body["hypothesis_id"], body["hypothesis_version"])).fetchone()
    if hyp is None:
        raise StrategyError(404, "NOT_FOUND", "命题不存在")
    w0 = datetime.fromisoformat(rs["window"]["start"].replace("Z", "+00:00"))
    w1 = datetime.fromisoformat(rs["window"]["end"].replace("Z", "+00:00"))
    if w1 <= w0:
        raise StrategyError(400, "INVALID_CONTRACT", "window.end 必须晚于 window.start")
    if w0 < ctx["train_start"] or w1 > ctx["validation_end"]:
        raise StrategyError(409, "VERSION_CONFLICT", "回放区间必须在封存上下文的 train/validation 内（盲测时段不混入）")
    if rs["cost_policy_version"] != ctx["cost_policy_version"] and _profile_of(rs["cost_policy_version"]).profile_id.split("-x")[0] != "user-account":
        raise StrategyError(409, "VERSION_CONFLICT", "费用口径与封存上下文不一致")
    rs_hash = canonical_hash(rs)
    manifest = {"replay_spec": rs, "replay_spec_hash": rs_hash, "principal_id": str(pid), "idempotency_key": key, "kind": "tactic_replay"}
    digest = canonical_hash([str(inst["instance_id"]), inst["parameter_hash"], "tactics-v1", KERNEL_VERSION])
    rec = E.new_run(instance_id=inst["instance_id"], hypothesis_id=UUID(body["hypothesis_id"]), hypothesis_version=body["hypothesis_version"],
                    evaluation_context_id=UUID(body["evaluation_context_id"]), run_purpose="screening", implementation_digest=digest,
                    random_seed=rs["random_seed"], run_manifest=manifest, family_key=f"tactic:{inst['strategy_id']}")
    research_repo.register_experiment(conn, rec)
    ensure_jobs(conn)
    store = PgJobStore(conn)
    run_id = uuid.uuid5(TACTIC_JOB, str(rec.experiment_id))
    store.enqueue(job_run_id=run_id, job_id=TACTIC_JOB, job_version=1, idempotency_key=f"tactic:{rec.experiment_id}", parameter_hash=rs_hash,
                  scheduled_for=now, resource_type="experiment", resource_id=rec.experiment_id)
    return {"receipt": S.receipt_of(store, run_id, "getEvaluation"), "context_contract_version": contracts.CONTRACT_VERSION}


def run_tactic_replay(conn: psycopg.Connection, lake: LakeStore, *, clock=lambda: datetime.now(timezone.utc)) -> dict | None:
    """Claim and run one queued tactic replay; metrics + terminal state are written; a failure is recorded with its error code."""
    store = PgJobStore(conn)
    job = store.claim(job_id=TACTIC_JOB, now=clock())
    if job is None:
        return None
    epoch = job.lease_epoch
    row = conn.execute("SELECT * FROM research.experiment_run WHERE experiment_id=%s", (job.resource_id,)).fetchone()
    rec = E.ExperimentRecord(row["experiment_id"], row["instance_id"], row["hypothesis_id"], row["hypothesis_version"], row["evaluation_context_id"],
                             row["run_purpose"], row["run_state"], row["implementation_digest"], row["random_seed"], row["run_manifest"],
                             row["lease_epoch"], row["run_manifest"].get("family_key", ""))
    started = clock()
    try:
        ctx = conn.execute("SELECT * FROM research.evaluation_context WHERE evaluation_context_id=%s", (rec.evaluation_context_id,)).fetchone()
        irow = conn.execute("SELECT d.specification, i.parameters FROM strategy.instance i JOIN strategy.definition d USING (strategy_id, strategy_version)"
                            " WHERE i.instance_id=%s", (rec.instance_id,)).fetchone()
        tactic = {**irow["specification"]["tactic"], **{k: v for k, v in irow["parameters"].items() if k in ("up_pct", "rebuy_pct", "share")}}
        metrics = evaluate_tactic(conn, lake, ctx, rec.run_manifest["replay_spec"], tactic)
        digest = sha256_of(metrics)
        with conn.transaction():
            research_repo.insert_metrics(conn, rec.experiment_id, metrics, computed_at=clock())
            research_repo.finish_experiment(conn, E.complete(rec, started_at=started, completed_at=clock(), result_ref=f"evaluation:{digest}", result_hash=digest))
        store.succeed(job.job_run_id, epoch, output_ref=f"experiment:{rec.experiment_id}", output_hash=digest, now=clock())
        return {"experiment_id": str(rec.experiment_id), "state": "completed", "metrics": len(metrics)}
    except Exception as exc:                                              # failure is a recorded trial, not an empty success
        code = getattr(exc, "code", None) or f"{type(exc).__name__}:{str(exc)[:80]}"
        with conn.transaction():
            research_repo.finish_experiment(conn, E.fail(rec, started_at=started, completed_at=clock(), error_code=code))
        store.fail(job.job_run_id, epoch, error_code=code[:100], retryable=False, now=clock())
        return {"experiment_id": str(rec.experiment_id), "state": "failed", "error": code}


# --------------------------------------------------------------------------------------------------------
# analog study (D03) and interval statistics
# --------------------------------------------------------------------------------------------------------
ANALOG_DATASET = "pulse.analog_study"
ANALOG_CONTRACT = ("pulse.analog_study", contracts.CONTRACT_VERSION)   # manifest rows reference the platform contract version


def parse_policy(selection_policy_version: str) -> tuple[str, int]:
    base, _, h = selection_policy_version.partition(":h")
    if base not in AN.POLICIES:
        raise StrategyError(400, "INVALID_CONTRACT", f"未知选择法：{selection_policy_version}（可选 {', '.join(AN.POLICIES)}，可加 :h5..:h60）")
    return base, int(h) if h else 20


def release_row(conn: psycopg.Connection, release_id: UUID) -> dict:
    r = conn.execute("SELECT * FROM decision.release WHERE release_id=%s AND release_state IN ('READY','SUPERSEDED')", (release_id,)).fetchone()
    if r is None:
        raise StrategyError(404, "NOT_FOUND", "发布不存在")
    return r


def analog_query_key(conn: psycopg.Connection, instrument_id: UUID, rel: dict) -> tuple[str, dict]:
    man = B._ready_manifest(conn, B.RESEARCH_DATASET, rel["decision_as_of"])
    if man is None:
        raise StrategyError(409, "STALE_INPUT", "发布时点没有可见的研究快照")
    run = {"input_document": {"panel": {"research_snapshot_id": str(man["snapshot_id"]), "research_content_hash": man["content_hash"]},
                              "decision_as_of": contracts.utc_z(rel["decision_as_of"]), "universe_policy_version": "analog-universe-v1"}}
    return key_hash(S.feature_key("analog_features", str(instrument_id), run)), man


def study_identity(query_key: str, policy: str, decision_as_of: datetime) -> str:
    return sha256_of({"query_feature_key_hash": query_key, "selection_policy_version": policy, "decision_as_of": contracts.utc_z(decision_as_of)})


def ensure_analog_contract(conn: psycopg.Connection) -> None:
    doc = {"description": "Analog study: fixed neighbour set of one query (features-only selection).", "type": "object"}
    conn.execute("INSERT INTO core.contract_version(contract_id, schema_version, schema_document, schema_hash, compatibility_policy, created_at)"
                 " VALUES (%s,%s,%s,%s,'additive-optional-v1',now()) ON CONFLICT DO NOTHING", (*ANALOG_CONTRACT, Jsonb(doc), sha256_of(doc)))


def build_analog_study(svc, instrument_id: UUID, release_id: UUID, policy_version: str) -> dict:
    """Worker/CLI: compute and publish the study (D03: a manifest dataset; identity = query key, policy, decision_as_of)."""
    conn, lake = svc.conn, svc.lake
    base, horizon = parse_policy(policy_version)
    rel = release_row(conn, release_id)
    qkey, man = analog_query_key(conn, instrument_id, rel)
    ident = study_identity(qkey, policy_version, rel["decision_as_of"])
    hit = B.find_derived(conn, ANALOG_DATASET, "identity", ident)
    if hit is not None:
        return hit
    panel = S.load_panel(conn, lake, rel["decision_as_of"], with_caps=False)
    by = panel.by_id()
    q = by.get(str(instrument_id))
    if q is None:
        raise StrategyError(404, "NOT_FOUND", "标的在研究面板中没有日线")
    decision_date = panel.sessions[-1]
    nb = AN.find_neighbors(panel.views, q, q.n - 1, horizon=horizon, decision_date=decision_date, policy=base)
    if not nb:
        raise StrategyError(422, "UNSUPPORTED_CONTEXT_SHAPE", "历史不足或特征缺失，无法构建相似情景")
    frame = pl.DataFrame({
        "rank": list(range(1, len(nb) + 1)), "instrument_id": [n.instrument_id for n in nb], "case_date": [n.case_date for n in nb],
        "distance": [n.distance for n in nb], "outcome_end": [n.outcome_end for n in nb],
        "forward_return": [None if n.forward_return is None else repr(round(n.forward_return, 10)) for n in nb]},
        schema={"rank": pl.Int64, "instrument_id": pl.String, "case_date": pl.String, "distance": pl.Float64, "outcome_end": pl.String, "forward_return": pl.String})
    cov = {"dataset": ANALOG_DATASET, "identity": ident, "query_feature_key_hash": qkey, "selection_policy_version": policy_version,
           "decision_as_of": contracts.utc_z(rel["decision_as_of"]), "instrument_id": str(instrument_id), "release_id": str(release_id),
           "horizon_sessions": horizon, "neighbors": len(nb), "price_basis": PRICE_BASIS,
           "note": "邻居只按特征距离选取；结果不影响选取；展示的是经验分布，不是校准概率"}
    ensure_analog_contract(conn)
    deps = [(man["snapshot_id"], "bars")]
    return B.publish_derived(svc, ANALOG_DATASET, frame, cov, contract_id=ANALOG_CONTRACT[0], dependencies=deps, watermark=rel["decision_as_of"])


def query_analog(conn: psycopg.Connection, lake: LakeStore, instrument_id: UUID, release_id: UUID, feature_key_hash: str, policy_version: str,
                 limit: int, cursor: str | None) -> tuple[dict, dict]:
    parse_policy(policy_version)
    rel = release_row(conn, release_id)
    qkey, _ = analog_query_key(conn, instrument_id, rel)
    if qkey != feature_key_hash:
        raise StrategyError(409, "STALE_INPUT", "feature_key_hash 与该发布/标的的查询特征不一致")
    ident = study_identity(qkey, policy_version, rel["decision_as_of"])
    man = B.find_derived(conn, ANALOG_DATASET, "identity", ident)
    if man is None:
        raise StrategyError(409, "RESULT_NOT_READY", "相似情景研究尚未构建（查询不会触发新研究）；请运行 worker: analog-study")
    df = lake.read_snapshot_frames(man["content_hash"]).sort("rank")
    start = 0
    if cursor:
        try:
            import json as _j
            c = _j.loads(cursor)
            if c["h"] != man["content_hash"]:
                raise StrategyError(409, "CURSOR_EXPIRED", "游标属于另一版本的研究")
            start = int(c["o"])
        except (ValueError, KeyError, TypeError) as e:
            raise StrategyError(409, "CURSOR_EXPIRED", "游标无效") from e
    page = df.slice(start, limit)
    horizon = man["coverage_document"]["horizon_sessions"]
    neighbors = []
    for r in page.iter_rows(named=True):
        case_ts = datetime.fromisoformat(r["case_date"] + "T15:00:00+08:00")
        end_ts = datetime.fromisoformat(r["outcome_end"] + "T15:00:00+08:00")
        asof_ok = end_ts <= rel["decision_as_of"] or end_ts.date() <= rel["decision_as_of"].astimezone(B.CST).date()
        outcome = None
        if r["forward_return"] is not None and asof_ok:
            outcome = {"name": f"forward_return_{horizon}d", "definition_version": "analog-v1", "value": Decimal(r["forward_return"]), "unit": "ratio",
                       "sample_count": 1, "missing_reason": None}
        neighbors.append({"instrument_id": r["instrument_id"], "case_as_of": case_ts, "feature_key_hash": sha256_of([r["instrument_id"], r["case_date"], "analog-features-v1"]),
                          "distance": r["distance"], "outcome_end": end_ts, "outcome": outcome})
    import json as _j
    nxt = _j.dumps({"h": man["content_hash"], "o": start + limit}) if start + limit < df.height else None
    study = {"ref": f"analog-study:{man['snapshot_id']}", "content_hash": man["content_hash"], "contract_id": ANALOG_CONTRACT[0], "schema_version": ANALOG_CONTRACT[1]}
    return {"study": study, "neighbors": neighbors, "page": {"next_cursor": nxt, "snapshot_hash": man["content_hash"]}}, {"manifest": man, "release": rel}


def interval_statistics(conn: psycopg.Connection, lake: LakeStore, instrument_id: UUID, snapshot_id: UUID, window: dict, adjustment_mode: str,
                        metric_policy_version: str) -> tuple[list[dict], dict]:
    if metric_policy_version != IV.POLICY:
        raise StrategyError(400, "INVALID_CONTRACT", f"未知计算口径：{metric_policy_version}（可选 {IV.POLICY}）")
    man = market_repo.get_manifest(conn, snapshot_id)
    if man is None or man["manifest_state"] != "READY":
        raise StrategyError(404, "NOT_FOUND", "行情快照不存在或未就绪")
    cov = man["coverage_document"] or {}
    mode = cov.get("adjustment_mode") or ("forward_adjusted" if man["dataset_id"] == B.RESEARCH_DATASET else "raw")
    if mode != adjustment_mode:
        raise StrategyError(422, "UNSUPPORTED_CONTEXT_SHAPE", f"快照 {man['dataset_id']} 的复权口径是 {mode}，请求 {adjustment_mode}；不做隐式换算")
    df = lake.read_snapshot_frames(man["content_hash"])
    if df is None or df.height == 0:
        raise StrategyError(404, "NOT_FOUND", "快照没有数据")
    cols = ("open_price", "high_price", "low_price", "close_price") if "open_price" in df.columns else ("open_price_raw", "high_price_raw", "low_price_raw", "close_price_raw")
    df = df.filter(pl.col("instrument_id") == str(instrument_id))
    if "timeframe" in df.columns:
        df = df.filter(pl.col("timeframe") == "1d")
    if "revision" in df.columns:
        df = df.sort("revision").unique(["instrument_id", "trade_date"], keep="last")
    df = df.sort("trade_date")
    if df.height == 0:
        raise StrategyError(404, "NOT_FOUND", "该标的在快照中没有日线")
    w0 = _cst(datetime.fromisoformat(window["start"].replace("Z", "+00:00")))
    w1 = _cst(datetime.fromisoformat(window["end"].replace("Z", "+00:00")))
    bars = [IV.Bar(r["trade_date"], Decimal(str(r[cols[0]])), Decimal(str(r[cols[1]])), Decimal(str(r[cols[2]])), Decimal(str(r[cols[3]])),
                   None if r.get("volume_shares") is None else Decimal(str(r["volume_shares"]))) for r in df.iter_rows(named=True)
            if all(r[c] is not None for c in cols)]
    return IV.compute(bars, w0, w1), man
