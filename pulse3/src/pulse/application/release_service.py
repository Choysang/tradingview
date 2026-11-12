"""build_release: one AnalysisRelease with every required component produced in the same run (I1, M3 + M4-E02 + M5 inputs).

Why: FINAL_SYSTEM_SPEC §3/§6/§7/§12-13 and REFERENCE_RECONCILIATION §6/§13 require one release in slot "main" whose
rankings, sector views, candidate pool, trade plans, workspace projections and explanations are consistent and
published atomically through the W02 stage/CAS protocol with an outbox event. The pages never pick "latest" themselves.

Invariants:
  * point-in-time: every input is bounded by ``decision_as_of``; derived ``available_at`` = max(inputs, computed_at);
  * horizons are explicit and versioned (短线 5td, 波段 20td, 长期 60td); each ranking has one forecast target;
  * the baseline is the explainable fixed-weight scorer; the trial budget rules run with no approved candidate, so the
    baseline keeps the whole weight; nothing is calibrated and no probability is claimed;
  * a required component that is missing / failed / expired blocks READY (``IncompleteRelease`` -> release FAILED, the
    slot pointer is unchanged); optional ``heating.*`` components may be MISSING under ALLOW_MISSING and are reported;
  * the release id is a function of the input context, so re-running for the same inputs returns the published release;
  * plans are ``watching`` with validation ``pending``: no strategy instance is admitted, so none can be ``actionable``.
Not covered: plan validation by backtest (M5), actionability (``account_service``), supplier of adjusted history after the
legacy cut, news/community/fund surge components (stay MISSING), workspace for in-sector page entries beyond market page 1.
"""
from __future__ import annotations

import hashlib
import statistics
import time
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

import polars as pl
import psycopg
from psycopg.types.json import Jsonb

from pulse import contracts
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import decision_repo, market_repo
from pulse.adapters.postgres.ledger_repo import LedgerRepo
from pulse.adapters.postgres.release import PgAnalysisRelease
from pulse.application import bindings as B
from pulse.application.data_service import DataService
from pulse.application.ports.errors import IncompleteRelease
from pulse.application.ranking_service import build_and_store_ranking
from pulse.domain import features as F
from pulse.domain.features.definitions import Series
from pulse.domain.market_rules import MARKET_RULES_VERSION
from pulse.domain.market_rules.costs import user_account_2026
from pulse.domain.plans import bodies as plan_bodies
from pulse.domain.plans import conditions as C
from pulse.domain.plans.compiler import CompileError, StrategyOutput, compile_plan
from pulse.domain.plans.versions import check_append, latest as latest_plan, ref as plan_ref
from pulse.domain.ranking.baseline import BaselinePolicy, FeatureSpec, RankingBlocked, RankingTarget
from pulse.domain.ranking.run import ActionAssessment, build_ranking
from pulse.domain.ranking.universe import InstrumentFacts, UniversePolicy, UniverseRules, UserUniversePreference, apply_universe
from pulse.domain.sectors import pool as P
from pulse.domain.sectors.ranking import (SectorPolicy, SectorScore, assemble_sector_overview, score_sectors_by_opportunity)
from pulse.domain.strategy.canonical import canonical_hash, quantize12

PRODUCER = "analysis-release/1.0"
PLAN_TEMPLATE_VERSION = "baseline-plan-template-v1"
SECTOR_POLICY = SectorPolicy("sector-opportunity-top5-mean-v1")
POOL_POLICY = P.PoolPolicy(
    policy_id="sector-heating-v1", policy_version="sector-heating-v1-uncalibrated",
    weights={"relative_strength_change": Decimal("0.25"), "turnover_acceleration": Decimal("0.25"),
             "breadth": Decimal("0.25"), "leader_confirmation": Decimal("0.25")},
    enter_threshold=Decimal("0.75"), exit_threshold=Decimal("0.25"), min_dwell_evaluations=2, cooling_max_evaluations=3,
    required_components=frozenset({"relative_strength_change", "breadth"}))
MISSING_SURGE = {"news_novelty": "no news-novelty feature is computed from the ingested announcements (single-day window)",
                 "community_growth": "no community source is ingested", "fund_proxy": "no fund-flow source is ingested"}
PLAN_ROLE_CATALOG = {"entry": "S001", "exit": "S056", "holding": "S054", "allocation": "S060"}
WORKSPACE_FEATURES: tuple[tuple[str, dict | None], ...] = (
    ("ma", {"n": 20}), ("ma", {"n": 60}), ("ema", {"n": 12}), ("macd", None), ("rsi", None), ("atr", None))
CLOCK = {"kind": "bar_close", "timeframe": "1d", "session_policy_version": B.SESSION_POLICY}
TICK = Decimal("0.01")


@dataclass(frozen=True)
class Horizon:
    horizon_id: str
    label: str
    sessions: int
    target_id: str
    version: str = "v1"
    features: tuple[tuple[str, int, str, str], ...] = ()    # (recipe id, direction, weight, source_group)

    @property
    def policy_version(self) -> str:
        return f"baseline-explainable-{self.horizon_id}-{self.version}"


def _horizon(hid: str, label: str, n: int, feats) -> Horizon:
    return Horizon(hid, label, n, f"rel_return_{hid}_vs_universe_median_after_declared_costs_v1", "v1", tuple(feats))


HORIZONS: tuple[Horizon, ...] = (
    _horizon("5td", "短线", 5, [("trend_dev_5", 1, "0.25", "price_trend"), ("rel_strength_5", 1, "0.25", "relative_strength"),
                                ("volume_ratio_5", 1, "0.20", "volume"), ("atr_pct_14", -1, "0.15", "volatility"),
                                ("macd_hist_pct", 1, "0.15", "macd_momentum")]),
    _horizon("20td", "波段", 20, [("trend_dev_20", 1, "0.25", "price_trend"), ("rel_strength_20", 1, "0.25", "relative_strength"),
                                  ("volume_ratio_5", 1, "0.15", "volume"), ("atr_pct_14", -1, "0.20", "volatility"),
                                  ("macd_hist_pct", 1, "0.15", "macd_momentum")]),
    _horizon("60td", "长期", 60, [("trend_dev_60", 1, "0.30", "price_trend"), ("rel_strength_60", 1, "0.30", "relative_strength"),
                                  ("atr_pct_14", -1, "0.25", "volatility"), ("macd_hist_pct", 1, "0.15", "macd_momentum")]),
)
HORIZON_BY_ID = {h.horizon_id: h for h in HORIZONS}


def baseline_policy(h: Horizon, costs_version: str) -> BaselinePolicy:
    return BaselinePolicy(
        policy_version=h.policy_version,
        target=RankingTarget(h.target_id, h.horizon_id, costs_version, "universe_median"),
        features=tuple(FeatureSpec(fid, "v1", direction, Decimal(w), grp) for fid, direction, w, grp in h.features))


@dataclass(frozen=True)
class BuildConfig:
    slot_key: str = B.SLOT_KEY
    top_n_plans: int = 10
    workspace_page: int = 50
    sector_min_members: int = 5
    legacy_warehouse: str | None = None          # path of the legacy warehouse (caps are read through the legacy adapter)
    availability: str = B.AVAILABILITY_POLICY     # observed (live) | B.RECONSTRUCTED_POLICY (history rebuild, explicit as-of)
    pool_policy: P.PoolPolicy = POOL_POLICY
    skip_components: frozenset[str] = frozenset()  # test seam: component is never produced (stays unrecorded)
    fail_components: frozenset[str] = frozenset()  # test seam: component raises while being built


class BuildFailed(RuntimeError):
    def __init__(self, release_id: uuid.UUID, problems: Sequence[str]):
        self.release_id, self.problems = release_id, tuple(problems)
        super().__init__(f"release {release_id} not published: " + "; ".join(problems))


class BuildInProgress(RuntimeError):
    pass


@dataclass
class BuildResult:
    release_id: uuid.UUID
    replayed: bool
    decision_as_of: datetime
    session_date: date | None
    pointer_generation: int | None
    counts: dict[str, Any]
    wall_seconds: float
    degraded: tuple[str, ...] = ()


# --------------------------------------------------------------------------------------------------------
# input context
# --------------------------------------------------------------------------------------------------------
@dataclass
class BuildInputs:
    research: B.ResearchInputs
    raw_manifests: list[dict]
    membership: B.Membership | None
    caps: dict[uuid.UUID, Decimal]
    cap_manifest: dict | None
    metas: dict[uuid.UUID, B.InstrumentMeta]
    listed: dict[uuid.UUID, date | None]
    raw_close: dict[uuid.UUID, Decimal]
    facts: list[InstrumentFacts]
    universe_policy: UniversePolicy
    decision_as_of: datetime
    context_hash: str
    context_doc: dict


def universe_policy_for(caps_known: bool) -> UniversePolicy:
    pref = UserUniversePreference(
        preference_version="default-pref-v1" if caps_known else "default-pref-v1+circ-cap-unavailable",
        price_cap_raw=Decimal("100"),
        circulating_market_cap_cap_cny=Decimal("10000000000") if caps_known else None)
    return UniversePolicy(UniverseRules(), pref)


def _latest_source_time(conn: psycopg.Connection, lake: LakeStore, cap_manifest: dict | None) -> datetime:
    times = [m["ready_at"] for m in B.ready_manifests(conn, [B.RESEARCH_DATASET, B.INDEX_DATASET, *B.RAW_DATASETS], None)]
    if cap_manifest is not None:
        times.append(cap_manifest["ready_at"])
    if not times:
        raise LookupError("no source data has been published")
    return max(times)


#: A session's universe needs execution-grade (raw) closes; fewer than this share of the research instruments means
#: the raw increment for that session has not been ingested yet, and no release is built on it.
MIN_RAW_COVERAGE = 0.5


class RawCoverageMissing(LookupError):
    """The research session is ahead of the raw daily bars (run ``ingest live-daily --universe`` first)."""


def require_raw_coverage(research, raw_close) -> None:
    on_day = research.bars.filter(pl.col("trade_date") == research.session_date)["instrument_id"].n_unique()
    if on_day and len(raw_close) < MIN_RAW_COVERAGE * on_day:
        raise RawCoverageMissing(
            f"raw daily bars missing for session {research.session_date.isoformat()}: {len(raw_close)} raw closes"
            f" for {on_day} research instruments (need >= {MIN_RAW_COVERAGE:.0%}); run `pulse.workers.ingest live-daily --universe`")


def require_research_window(research, min_sessions: int) -> None:
    """Listing age is counted in research sessions: a window shorter than the new-listing rule makes every stock 'new'."""
    if len(research.sessions) < min_sessions:
        raise RawCoverageMissing(
            f"research snapshot holds {len(research.sessions)} sessions, fewer than the {min_sessions}-session new-listing rule;"
            " import a longer legacy window (`ingest legacy-import --since <400 days ago>`)")


def resolve_inputs(svc: DataService, lake: LakeStore, cfg: BuildConfig, as_of: datetime | None) -> BuildInputs:
    conn = svc.conn
    cap_manifest = None
    if cfg.legacy_warehouse and as_of is None:
        cap_manifest = B.publish_cap_snapshot(svc, cfg.legacy_warehouse)       # source-side fact, published before the cutoff is fixed
    pol = cfg.availability
    if B.reconstructed(pol) and as_of is None:
        raise ValueError("history rebuild with a reconstructed availability policy needs an explicit --as-of")
    decision_as_of = as_of or _latest_source_time(conn, lake, cap_manifest)
    research = B.load_research_inputs(conn, lake, decision_as_of, pol)
    raw = B.raw_snapshot_refs(conn, decision_as_of, pol)
    caps, cap_manifest = B.load_caps(conn, lake, decision_as_of, pol)
    membership = B.ensure_membership(svc, lake, decision_as_of)
    metas, listed = B.load_instrument_meta(conn, decision_as_of, pol)
    raw_close = B.load_raw_close(conn, research.session_date, decision_as_of, pol)
    require_raw_coverage(research, raw_close)
    facts = B.build_facts(metas, listed, research.sessions, raw_close, caps)
    policy = universe_policy_for(bool(caps))
    require_research_window(research, policy.rules.new_listing_min_sessions)
    doc = {
        "availability_policy": pol, "slot": cfg.slot_key, "session_date": research.session_date.isoformat(), "decision_as_of": decision_as_of,
        "research": [str(research.manifest["snapshot_id"]), research.manifest["content_hash"]],
        "benchmark": None if research.benchmark_manifest is None else research.benchmark_manifest["content_hash"],
        "raw": sorted(m["content_hash"] for m in raw), "caps": None if cap_manifest is None else cap_manifest["content_hash"],
        "membership": None if membership is None else membership.manifest["content_hash"],
        "universe_policy": policy.policy_version, "market_rules": MARKET_RULES_VERSION,
        "horizons": [h.policy_version for h in HORIZONS], "plan_template": PLAN_TEMPLATE_VERSION,
        "pool_policy": cfg.pool_policy.policy_version, "top_n": cfg.top_n_plans, "workspace_page": cfg.workspace_page}
    return BuildInputs(research, raw, membership, caps, cap_manifest, metas, listed, raw_close, facts, policy, decision_as_of,
                       canonical_hash(doc), doc)


# --------------------------------------------------------------------------------------------------------
# plans
# --------------------------------------------------------------------------------------------------------
FLOAT_KERNEL_PLACES = 8     # declared step: float64 kernel outputs (ATR, MA) are cleaned to 1e-8 before the price tick is applied


def q(value: float | Decimal) -> Decimal:
    """Price tick quantization. A Decimal is rounded directly (half-even); a float64 kernel output first goes through the
    declared 1e-8 step so representation noise (10.565000000000001) cannot decide a half-tick tie."""
    exact = value if isinstance(value, Decimal) else B.dec(value, FLOAT_KERNEL_PLACES)
    return exact.quantize(TICK, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class RawIndicators:
    close: Decimal
    atr: Decimal
    ma: dict[int, Decimal]
    sessions: int


def raw_indicators(series: Mapping[str, list] | None) -> RawIndicators | None:
    """Close / ATR(14) / MA(5,20,60) from the RAW series with the shared kernels; any missing input -> None."""
    if not series or len(series["c"]) < 61:
        return None
    s = Series(tuple(d.isoformat() for d in series["d"]), *(tuple(series[k]) for k in ("o", "h", "l", "c", "v")))
    atr = F.compute("atr", s, {"n": 14})["atr"][-1]
    mas = {n: F.compute("ma", s, {"n": n})["ma"][-1] for n in (5, 20, 60)}
    close = series["dc"][-1] if "dc" in series else series["c"][-1]     # exact Decimal raw close when the loader provides it
    if close is None or atr is None or atr <= 0 or any(v is None for v in mas.values()):
        return None
    return RawIndicators(q(close), q(atr), {n: q(v) for n, v in mas.items()}, len(series["c"]))


def _operand(fid: str, iid: uuid.UUID, field_path: str, version: str = "1", policy: str = "latest-completed-bar-v1") -> dict:
    return C.feature_operand(feature_id=fid, instrument_id=iid, timeframe="1d", field_path=field_path, unit="CNY",
                             definition_version=version, resolution_policy_version=policy)


def plan_conditions(iid: uuid.UUID, h: Horizon, ind: RawIndicators) -> dict[str, Any]:
    """Template rules (not validated): pullback entry in an uptrend, ATR invalidation, ATR target."""
    close = _operand("px.close", iid, "close")
    ma_n = {5: 5, 20: 20, 60: 60}[h.sessions]
    ma = _operand("ma", iid, "ma", "v1", f"raw-latest-completed-bar-v1;n={ma_n}")
    lower, upper = q(ind.close - ind.atr / 2), ind.close
    stop, target = q(ind.close - ind.atr * Decimal("1.5")), q(ind.close + ind.atr * 2)
    if lower <= 0 or stop <= 0:
        raise CompileError("price range collapses below zero for this ATR")
    return {
        "zone": {"lower_price_raw": lower, "upper_price_raw": upper, "currency": "CNY", "adjustment_mode": "raw"},
        "entry": C.all_of(CLOCK, C.compare("gt", close, ma, CLOCK), C.compare("gte", close, C.constant(lower, "CNY"), CLOCK),
                          C.compare("lte", close, C.constant(upper, "CNY"), CLOCK)),
        "exit": C.compare("gte", close, C.constant(target, "CNY"), CLOCK),
        "invalidation": C.compare("lt", close, C.constant(stop, "CNY"), CLOCK),
        "levels": {"entry_lower": lower, "entry_upper": upper, "stop": stop, "target": target, "ma_n": ma_n,
                   "ma_value": ind.ma[ma_n], "atr14": ind.atr, "reference_close": ind.close},
    }


TRIGGER = {"trigger_id": "plan-entry", "fire_mode": "on_transition_to_true", "unknown_policy": "do_not_fire",
           "rearm": {"mode": "never", "cooldown_ms": 0}, "max_fires": 1, "min_interval_ms": 0, "require_final_bar": True,
           "pending_order_policy": "block_retrigger"}


def strategy_instance_refs(conn: psycopg.Connection) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT d.specification->>'catalog_id' cid, i.instance_id, i.strategy_id, i.strategy_version, i.lifecycle"
        " FROM strategy.definition d JOIN strategy.instance i USING (strategy_id, strategy_version)"
        " WHERE d.specification->>'catalog_id' = ANY(%s)", (list(PLAN_ROLE_CATALOG.values()),)).fetchall()
    return {r["cid"]: r for r in rows}


def compile_template_plan(*, plan_id: uuid.UUID, iid: uuid.UUID, release_id: uuid.UUID, h: Horizon, ind: RawIndicators,
                          refs: Mapping[str, dict], decision_as_of: datetime, available_at: datetime) -> tuple[dict, dict]:
    cond = plan_conditions(iid, h, ind)

    def out(role: str, payload: dict) -> StrategyOutput:
        r = refs[PLAN_ROLE_CATALOG[role]]
        return StrategyOutput(role, {"instance_id": r["instance_id"], "strategy_id": r["strategy_id"],
                                     "strategy_version": r["strategy_version"]}, r["lifecycle"], payload)

    outputs = [
        out("entry", {"entry_conditions": cond["entry"], "entry_price_zone": cond["zone"], "trigger_policy": TRIGGER}),
        out("exit", {"exit_conditions": cond["exit"], "invalidation": cond["invalidation"]}),
        out("allocation", {"sizing": {"kind": "portfolio_fraction", "fraction": "0.05", "maximum_quantity_shares": None}}),
        out("holding", {"holding_horizon": {"horizon_id": h.horizon_id, "unit": "trading_days", "value": h.sessions}})]
    res = compile_plan(plan_id=plan_id, instrument_id=iid, release_id=release_id, outputs=outputs, valid_from=decision_as_of,
                       expires_at=decision_as_of + timedelta(days=max(7, int(h.sessions * 1.5))), available_at=available_at,
                       validation_policy_version="plan-validation-v1-not-run")
    return res.plan, {"levels": cond["levels"], "not_admitted_strategy_instances": list(res.not_admitted)}


def append_plan_version(conn: psycopg.Connection, plan: dict) -> dict:
    """Append ``plan`` as version 1, or as the next version of an existing history (supersedes chain)."""
    history, dead = decision_repo.load_visible_plan_history(conn, uuid.UUID(plan["plan_id"]),
                                                            building_release_id=uuid.UUID(plan["based_on_release_id"]))
    last = latest_plan(history)
    new = dict(plan)
    new["plan_version"] = max(last["plan_version"] if last else 0, max(dead, default=0)) + 1
    new["supersedes"] = None if last is None else plan_ref(last)
    if last is not None and new["available_at"] < last["available_at"]:
        new["available_at"] = last["available_at"]
    check_append(history, new, dead_versions=dead)
    contracts.check("TradePlan", new)
    decision_repo.append_plan_version(conn, new)      # checked above against the visible history (plan_service.append_plan sees the dead rows)
    return new


def stable_plan_id(conn: psycopg.Connection, kind: str, iid: uuid.UUID, scope: str, release_id: uuid.UUID | None = None) -> uuid.UUID:
    """One id per (instrument, scope); a terminal history (superseded/expired) starts a new epoch.
    Only the visible history counts (public releases and ``release_id`` being built): a failed attempt decides nothing."""
    for epoch in range(100):
        pid = B.aid("plan", kind, iid, scope, PLAN_TEMPLATE_VERSION, epoch)
        last = latest_plan(decision_repo.load_visible_plan_history(conn, pid, building_release_id=release_id)[0])
        if last is None or last["state"] not in ("superseded", "expired"):
            return pid
    raise RuntimeError("plan epoch overflow")


# --------------------------------------------------------------------------------------------------------
# T / grid sub-plans (only where the account has sellable inventory)
# --------------------------------------------------------------------------------------------------------
def build_inventory_subplans(conn: psycopg.Connection, *, release_id: uuid.UUID, account_snapshot, series_by_instrument,
                             refs: Mapping[str, dict], decision_as_of: datetime, available_at: datetime,
                             lot: int = 100) -> list[dict]:
    """T and grid plans for held instruments with sellable inventory; none when the account holds nothing."""
    out: list[dict] = []
    for iid, pos in sorted(account_snapshot.positions.items(), key=lambda kv: str(kv[0])):
        sellable = pos.sellable_shares - pos.reserved_sell_shares
        ind = raw_indicators(series_by_instrument.get(iid))
        if sellable < lot or ind is None:
            continue
        close_op = _operand("px.close", iid, "close")
        qty = max(lot, (min(sellable, max(pos.quantity_shares // 3, lot)) // lot) * lot)
        sell_lo, sell_hi = q(ind.close + ind.atr / 2), q(ind.close + ind.atr)
        buy_lo, buy_hi = q(ind.close - ind.atr / 2), ind.close
        stop = q(ind.close - ind.atr * Decimal("1.5"))
        expires = decision_as_of + timedelta(days=1)
        t_body = plan_bodies.TPlanBody(
            1, "sell_first", sellable, lot, plan_bodies.TLeg("sell", qty, sell_lo, sell_hi, canonical_hash(["t-sell", str(iid)])),
            plan_bodies.TLeg("buy", qty, buy_lo, buy_hi, canonical_hash(["t-buy", str(iid)])), "carry", expires)
        grids = None
        try:
            grids = plan_bodies.GridBody(
                1, "last_close", ind.close, "atr", Decimal("0.5"), ind.atr, 2, 2, lot, lot, lot * 2, ind.close * lot * 3,
                min(sellable, lot * 2), Decimal("0.0016"), Decimal("0.002"), "skip_gapped_levels", "one_level_per_bar",
                "open_low_high_close", "price_outside_band")
        except plan_bodies.BodyError:
            grids = None
        for kind, body, entry, exit_ in (
                ("t_plan", t_body, C.compare("gte", close_op, C.constant(sell_lo, "CNY"), CLOCK), C.compare("lte", close_op, C.constant(buy_hi, "CNY"), CLOCK)),
                ("grid_plan", grids, None if grids is None else C.compare("lte", close_op, C.constant(grids.buy_prices()[0], "CNY"), CLOCK),
                 None if grids is None else C.compare("gte", close_op, C.constant(grids.sell_prices()[0], "CNY"), CLOCK))):
            if body is None:
                continue
            body_hash = plan_bodies.body_hash(body)
            conn.execute(
                "INSERT INTO decision.plan_body(body_hash, body_kind, body_version, supersedes_hash, body_payload, created_at)"
                " VALUES (%s,%s,1,NULL,%s,%s) ON CONFLICT DO NOTHING",
                (body_hash, kind, Jsonb(contracts.to_wire(asdict(body))), available_at))
            def o(role, payload):
                r = refs[PLAN_ROLE_CATALOG[role]]
                return StrategyOutput(role, {"instance_id": r["instance_id"], "strategy_id": r["strategy_id"],
                                             "strategy_version": r["strategy_version"]}, r["lifecycle"], payload)
            zone = {"lower_price_raw": sell_lo if kind == "t_plan" else q(grids.buy_prices()[-1]), "upper_price_raw": sell_hi if kind == "t_plan" else ind.close,
                    "currency": "CNY", "adjustment_mode": "raw"}
            res = compile_plan(
                plan_id=stable_plan_id(conn, kind, iid, str(account_snapshot.account_id), release_id), instrument_id=iid, release_id=release_id,
                outputs=[o("entry", {"entry_conditions": entry, "entry_price_zone": zone, "trigger_policy": {**TRIGGER, "trigger_id": kind}}),
                         o("exit", {"exit_conditions": exit_, "invalidation": C.compare("lt", close_op, C.constant(stop, "CNY"), CLOCK)}),
                         o("allocation", {"sizing": {"kind": "fixed_shares", "quantity_shares": qty if kind == "t_plan" else lot}}),
                         o("holding", {"holding_horizon": {"horizon_id": "1td", "unit": "trading_days", "value": 1}})],
                valid_from=decision_as_of, expires_at=decision_as_of + timedelta(days=2), available_at=available_at,
                account_id=account_snapshot.account_id, expected_account_version=account_snapshot.account_version,
                validation_policy_version="plan-validation-v1-not-run")
            plan = dict(res.plan)
            plan["validation"] = {**plan["validation"], "result_ref": f"planbody:{kind}:{body_hash}"}
            out.append(append_plan_version(conn, plan))
    return out


# --------------------------------------------------------------------------------------------------------
# sector heating
# --------------------------------------------------------------------------------------------------------
def _zscores(values: Mapping[str, float]) -> dict[str, float]:
    if len(values) < 3:
        return {}
    mean = statistics.fmean(values.values())
    sd = statistics.pstdev(values.values())
    return {k: 0.0 if sd == 0 else (v - mean) / sd for k, v in values.items()}


def sector_surge(membership: B.Membership, matrix: B.PitFeatureMatrix, included: set[uuid.UUID], alpha_rank: Mapping[uuid.UUID, int]
                 ) -> tuple[dict[str, P.SurgeComponents], dict[str, dict]]:
    """Per-sector heating components (standardised across sectors) from the shared feature matrix."""
    ids = sorted(included, key=str)
    cols = matrix.matrix(ids, ["rel_strength_5", "rel_strength_20", "volume_ratio_5"], matrix.shared.inputs.decision_as_of)
    raw: dict[str, dict[str, float]] = {"relative_strength_change": {}, "turnover_acceleration": {}, "breadth": {}, "leader_confirmation": {}}
    detail: dict[str, dict] = {}
    for node, members in membership.members.items():
        obs = [m for m in members if m in included and all(cols[m][c] is not None for c in ("rel_strength_5", "rel_strength_20", "volume_ratio_5"))]
        if len(obs) < 3:
            detail[node] = {"observed": len(obs), "reason": "fewer than 3 observed members"}
            continue
        rs5 = [float(cols[m]["rel_strength_5"]) for m in obs]
        rs20 = [float(cols[m]["rel_strength_20"]) for m in obs]
        vr = [float(cols[m]["volume_ratio_5"]) for m in obs]
        leaders = sorted(obs, key=lambda m: (alpha_rank.get(m, 10**9), str(m)))[:3]
        raw["relative_strength_change"][node] = statistics.fmean(rs5) - statistics.fmean(rs20)
        raw["turnover_acceleration"][node] = statistics.fmean(vr)
        raw["breadth"][node] = sum(1 for v in rs5 if v > 0) / len(rs5)
        raw["leader_confirmation"][node] = statistics.fmean(float(cols[m]["rel_strength_5"]) for m in leaders)
        detail[node] = {"observed": len(obs), "members": len(members), "raw": {k: raw[k][node] for k in raw}}
    z = {k: _zscores(v) for k, v in raw.items()}
    comps = {n: P.SurgeComponents(**{k: (None if n not in z[k] else quantize12(Decimal(str(z[k][n]))) ) for k in raw})
             for n in detail if all(n in z[k] for k in raw)}
    return comps, detail


# --------------------------------------------------------------------------------------------------------
# the build
# --------------------------------------------------------------------------------------------------------
def _existing_published(conn: psycopg.Connection, slot: str, ctx_hash: str) -> uuid.UUID | None:
    for attempt in range(1, 50):
        rid = B.aid("release", slot, ctx_hash, attempt)
        row = conn.execute("SELECT release_state FROM decision.release WHERE release_id=%s", (rid,)).fetchone()
        if row is None:
            return None
        if row["release_state"] in ("READY", "SUPERSEDED"):
            return rid
    return None


def _next_attempt_id(conn: psycopg.Connection, slot: str, ctx_hash: str) -> uuid.UUID:
    for attempt in range(1, 50):
        rid = B.aid("release", slot, ctx_hash, attempt)
        row = conn.execute("SELECT release_state FROM decision.release WHERE release_id=%s", (rid,)).fetchone()
        if row is None:
            return rid
        if row["release_state"] == "BUILDING":   # a crashed earlier attempt: close it, never resume half-written rankings
            conn.execute("UPDATE decision.release SET release_state='FAILED' WHERE release_id=%s AND release_state='BUILDING'", (rid,))
    raise RuntimeError("too many failed attempts for one input context")


def summarize_release(conn: psycopg.Connection, release_id: uuid.UUID) -> dict[str, Any]:
    one = lambda sql: conn.execute(sql, (release_id,)).fetchone()["n"]  # noqa: E731
    market = {r["horizon_id"]: r["n"] for r in conn.execute(
        "SELECT r.horizon_id, count(e.instrument_id) n FROM decision.ranking_run r LEFT JOIN decision.ranking_entry e USING (ranking_id)"
        " WHERE r.release_id=%s AND r.ranking_scope='market' GROUP BY 1 ORDER BY 1", (release_id,))}
    return {
        "ranked_instruments": market,
        "sector_overview_runs": one("SELECT count(*) n FROM decision.ranking_run WHERE release_id=%s AND ranking_scope='sector_overview'"),
        "sectors_ranked": conn.execute(
            "SELECT count(DISTINCT s.node_id) n FROM decision.sector_rank_entry s JOIN decision.ranking_run r USING (ranking_id) WHERE r.release_id=%s",
            (release_id,)).fetchone()["n"],
        "in_sector_ranking_runs": one("SELECT count(*) n FROM decision.ranking_run WHERE release_id=%s AND ranking_scope='sector'"),
        "pool_items": conn.execute(
            "SELECT count(*) n FROM (SELECT DISTINCT ON (pool_id, instrument_id) current_state FROM decision.candidate_pool_event"
            " WHERE release_id=%s ORDER BY pool_id, instrument_id, event_sequence DESC) x", (release_id,)).fetchone()["n"],
        "pool_events": one("SELECT count(*) n FROM decision.candidate_pool_event WHERE release_id=%s"),
        "plans": one("SELECT count(*) n FROM decision.trade_plan WHERE based_on_release_id=%s"),
        "workspaces": one("SELECT count(*) n FROM decision.workspace_projection WHERE release_id=%s"),
    }


def build_release(conn: psycopg.Connection, lake: LakeStore, *, as_of: datetime | None = None, cfg: BuildConfig = BuildConfig(),
                  account_id: uuid.UUID | None = None, clock=lambda: datetime.now(timezone.utc)) -> BuildResult:
    """Build and publish one release. ``conn`` must be autocommit. Idempotent per input context."""
    t0 = time.monotonic()
    svc = DataService(conn, lake, clock=clock)
    svc.bootstrap()
    inputs = resolve_inputs(svc, lake, cfg, as_of)
    existing = _existing_published(conn, cfg.slot_key, inputs.context_hash)
    if existing is not None:
        view = PgAnalysisRelease(conn).get_release(existing)
        return BuildResult(existing, True, view.decision_as_of, inputs.research.session_date, None, summarize_release(conn, existing),
                           time.monotonic() - t0)
    lock_key = int.from_bytes(hashlib.sha256(f"build:{cfg.slot_key}:{inputs.context_hash}".encode()).digest()[:7], "big")
    if not conn.execute("SELECT pg_try_advisory_lock(%s) ok", (lock_key,)).fetchone()["ok"]:
        raise BuildInProgress("another builder holds this input context")
    try:
        return _build(conn, lake, svc, inputs, cfg, account_id, clock, t0)
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))


def _build(conn, lake, svc: DataService, inp: BuildInputs, cfg: BuildConfig, account_id, clock, t0) -> BuildResult:
    raw_clock = clock
    clock = lambda: max(raw_clock(), inp.decision_as_of)       # noqa: E731  derived available_at = max(inputs, computed_at)
    pool_policy = cfg.pool_policy
    rel = PgAnalysisRelease(conn)
    rel.ensure_slot(cfg.slot_key)
    release_id = _next_attempt_id(conn, cfg.slot_key, inp.context_hash)
    h_ids = [h.horizon_id for h in HORIZONS]
    tax = B.TAXONOMY_ID
    required = (["bars_research", "bars_raw", "universe", "sector_membership", "explanations", "workspace_projection"]
                + [f"ranking:market:{h}" for h in h_ids] + [f"sector_overview:{tax}:{h}" for h in h_ids]
                + [f"sector_rising:{tax}"] + [f"ranking:sector:{tax}:{h}" for h in h_ids] + [f"plans:{h}" for h in h_ids])
    pool_id = B.aid("pool", tax, pool_policy.policy_version)
    required.append(f"candidate_pool:{pool_id}")
    optional = {f"heating.{c}": "ALLOW_MISSING" for c in P.COMPONENTS}
    optional.update(ext_a_optional_components())                                   # EXT-A: optional market_state component
    rel.create_release(release_id=release_id, decision_as_of=inp.decision_as_of, availability_policy_version=cfg.availability,
                       universe_policy_version=inp.universe_policy.policy_version, market_rules_version=MARKET_RULES_VERSION,
                       required_components=required, optional_components=optional, created_at=clock())
    stage = rel.begin_stage(stage_id=B.aid("stage", release_id), slot_key=cfg.slot_key, release_id=release_id,
                            input_context_hash=inp.context_hash)
    ctx = inp.context_hash
    bodies: dict[str, Any] = {}                    # ref -> JSON body (explanations and component summaries)
    problems: dict[str, str] = {}
    counts: dict[str, Any] = {}

    def done(key: str, ref: str, digest: str, *, snapshot_id=None, valid_until=None) -> None:
        if key in cfg.skip_components:
            return
        rel.mark_ready(release_id, key, artifact_ref=ref, artifact_hash=digest, input_version_hash=ctx, snapshot_id=snapshot_id,
                       computed_at=clock(), valid_until=valid_until)

    def summary_component(key: str, body: dict, snapshot_id=None) -> None:
        digest = canonical_hash(body)
        ref = f"summary:{key}:{digest[:16]}"
        bodies[ref] = contracts.to_wire(body)
        done(key, ref, digest, snapshot_id=snapshot_id)

    def guarded(key: str, fn) -> Any:
        try:
            if key in cfg.fail_components:
                raise RuntimeError(f"forced failure of {key}")
            return fn()
        except Exception as exc:  # noqa: BLE001 - recorded as a FAILED component; publication then refuses
            problems[key] = f"{type(exc).__name__}: {exc}"
            if key not in cfg.skip_components:
                rel.put_component(release_id, key, component_state="FAILED", artifact_ref=f"failed:{type(exc).__name__}",
                                  input_version_hash=ctx, computed_at=clock())
            return None

    res = inp.research
    man = res.manifest
    # ---- data input components
    def bars_research():
        done("bars_research", f"snapshot:{man['dataset_id']}:{man['manifest_version']}", man["content_hash"], snapshot_id=man["snapshot_id"])

    def bars_raw():
        if not inp.raw_manifests:
            raise LookupError("no raw daily bars visible")
        digest = canonical_hash(sorted((str(m["snapshot_id"]), m["content_hash"]) for m in inp.raw_manifests))
        done("bars_raw", f"snapshots:{len(inp.raw_manifests)}:{digest[:16]}", digest, snapshot_id=inp.raw_manifests[-1]["snapshot_id"])

    guarded("bars_research", bars_research)
    guarded("bars_raw", bars_raw)
    if inp.membership is None:
        problems["sector_membership"] = "no legacy industry boards evidence visible"
        rel.put_component(release_id, "sector_membership", component_state="MISSING", artifact_ref="missing:legacy_boards",
                          input_version_hash=ctx, computed_at=clock())
    else:
        m = inp.membership.manifest
        guarded("sector_membership", lambda: done("sector_membership", f"snapshot:{m['dataset_id']}:{m['manifest_version']}", m["content_hash"],
                                                 snapshot_id=m["snapshot_id"]))
    computed_at = clock()

    # ---- universe + feature matrix
    policy = inp.universe_policy
    uni = apply_universe(policy, inp.facts)
    reasons = Counter(r for e in uni.exclusions for r in e.reason_codes)
    universe_summary = {
        "policy_version": policy.policy_version, "session_date": res.session_date, "instruments_seen": len(inp.facts),
        "included": len(uni.included), "excluded": len(uni.exclusions), "exclusion_reasons": dict(sorted(reasons.items())),
        "price_basis": "raw close of the session (Sina/Tencent unadjusted daily bars)",
        "circulating_cap": ("known for %d instruments (legacy securities float_cap, unit assumed 100M CNY)" % len(inp.caps)
                            if inp.caps else "MISSING for every instrument: the cap bound is not applied and is marked missing"),
        "notes": res.notes}
    guarded("universe", lambda: summary_component("universe", universe_summary))
    ids_all = [uuid.UUID(i) for i in sorted(res.bars["instrument_id"].unique().to_list())]
    shared = B.SharedFeatures(conn, lake, res, universe_policy_version=policy.policy_version, market_rules_version=MARKET_RULES_VERSION,
                              clock=clock)
    matrix = B.PitFeatureMatrix(shared, ids_all)
    costs_version = user_account_2026().version

    # ---- first pass (pure): top N per horizon -> plans
    series_ids: set[uuid.UUID] = set()
    first: dict[str, Any] = {}
    for h in HORIZONS:
        pol = baseline_policy(h, costs_version)
        try:
            first[h.horizon_id] = build_ranking(
                ranking_id=B.aid("ranking0", release_id, h.horizon_id), release_id=release_id, ranking_scope="market",
                scope_key=f"market/{h.horizon_id}", policy=pol, universe=uni,
                matrix=matrix.matrix([f.instrument_id for f in uni.included], [f.feature_id for f in pol.features], inp.decision_as_of))
            series_ids |= {e.instrument_id for e in first[h.horizon_id].entries[:cfg.top_n_plans]}
        except RankingBlocked as exc:
            problems[f"ranking:market:{h.horizon_id}"] = f"RankingBlocked: {exc}"
    raw_series = B.load_raw_series(conn, sorted(series_ids, key=str), res.session_date, inp.decision_as_of, availability_policy=cfg.availability)
    acct = None
    if account_id is not None:
        from pulse.application.bindings import LedgerAccountSnapshotPort
        acct = LedgerAccountSnapshotPort(conn).snapshot(account_id)
        held = [i for i, p in acct.positions.items() if p.sellable_shares > 0]
        raw_series.update(B.load_raw_series(conn, held, res.session_date, inp.decision_as_of, availability_policy=cfg.availability))

    from pulse.application.research_service import seed_registry
    if not strategy_instance_refs(conn):
        seed_registry(conn, Path(__file__).resolve().parents[3] / "spec_package" / "catalogs", now=clock())
    refs = strategy_instance_refs(conn)

    plans_by_h: dict[str, dict[uuid.UUID, dict]] = {h: {} for h in h_ids}
    missing_plan: dict[str, list[uuid.UUID]] = {h: [] for h in h_ids}
    for h in HORIZONS:
        run0 = first.get(h.horizon_id)
        if run0 is None:
            continue

        def make_plans(h=h, run0=run0):
            for e in run0.entries[:cfg.top_n_plans]:
                ind = raw_indicators(raw_series.get(e.instrument_id))
                if ind is None:
                    missing_plan[h.horizon_id].append(e.instrument_id)
                    continue
                pid = stable_plan_id(conn, "market", e.instrument_id, h.horizon_id, release_id)
                plan, meta = compile_template_plan(plan_id=pid, iid=e.instrument_id, release_id=release_id, h=h, ind=ind, refs=refs,
                                                   decision_as_of=inp.decision_as_of, available_at=clock())
                with conn.transaction():
                    plans_by_h[h.horizon_id][e.instrument_id] = append_plan_version(conn, plan)
                bodies.setdefault("plan_levels:" + str(pid), contracts.to_wire(meta))
            body = {"horizon": h.horizon_id, "template": PLAN_TEMPLATE_VERSION, "state": "watching", "validation": "pending (not run)",
                    "plans": len(plans_by_h[h.horizon_id]), "missing_inputs": [str(i) for i in missing_plan[h.horizon_id]],
                    "strategy_refs": {role: cid for role, cid in PLAN_ROLE_CATALOG.items()},
                    "strategy_note": "template rules resemble the catalog candidates; none is implemented or admitted",
                    "plan_ids": sorted(str(p["plan_id"]) + ":" + str(p["plan_version"]) for p in plans_by_h[h.horizon_id].values())}
            summary_component(f"plans:{h.horizon_id}", body)
        guarded(f"plans:{h.horizon_id}", make_plans)
    sub_plans: list[dict] = []
    if acct is not None:
        with conn.transaction():
            sub_plans = build_inventory_subplans(
                conn, release_id=release_id, account_snapshot=acct, series_by_instrument=raw_series, refs=refs,
                decision_as_of=inp.decision_as_of, available_at=clock())

    # ---- persisted market rankings
    runs: dict[str, Any] = {}
    for h in HORIZONS:
        key = f"ranking:market:{h.horizon_id}"
        if h.horizon_id not in first:
            if key not in cfg.skip_components:
                rel.put_component(release_id, key, component_state="FAILED", artifact_ref="failed:RankingBlocked",
                                  input_version_hash=ctx, computed_at=clock())
            continue
        actions = {}
        for e in first[h.horizon_id].entries[:cfg.top_n_plans]:
            p = plans_by_h[h.horizon_id].get(e.instrument_id)
            actions[e.instrument_id] = (ActionAssessment(e.instrument_id, "watching", ("TEMPLATE_PLAN_NOT_VALIDATED",), (uuid.UUID(p["plan_id"]), p["plan_version"]))
                                        if p else ActionAssessment(e.instrument_id, "missing_data", ("PLAN_INPUT_MISSING_RAW_BARS",)))

        def persist(h=h, actions=actions, key=key):
            writer = B.PgReleaseComponentWriter(rel, input_version_hash=ctx, computed_at=clock())
            if key in cfg.skip_components:
                writer = type("NoWriter", (), {"add_component": lambda *a, **k: None})()
            with conn.transaction():
                built = build_and_store_ranking(
                    conn, ranking_id=B.aid("ranking", release_id, "market", h.horizon_id), release_id=release_id, ranking_scope="market",
                    scope_key=f"market/{h.horizon_id}", universe_policy=policy, facts=inp.facts, policy=baseline_policy(h, costs_version),
                    features=matrix, decision_as_of=inp.decision_as_of, created_at=clock(), writer=writer, component_key=key, actions=actions)
            runs[h.horizon_id] = built
            bodies.update({k: v for k, v in built.explanations.items()})
        guarded(key, persist)

    # ---- sector views
    snaps = B.sector_snapshots(inp.membership) if inp.membership else []
    included = {f.instrument_id for f in uni.included}
    sector_exclusions: dict[str, list] = {}
    sector_scores_by_h: dict[str, list[SectorScore]] = {}
    for h in HORIZONS:
        run = runs.get(h.horizon_id)
        if run is None or not snaps:
            continue
        member_scores = {e.instrument_id: e.relative_rank_score for e in run.run.entries}
        scored, excluded = score_sectors_by_opportunity(SECTOR_POLICY, snaps, member_scores, inp.decision_as_of)
        sector_scores_by_h[h.horizon_id] = scored
        sector_exclusions[h.horizon_id] = [(n.node_id, why) for n, why in excluded]

        def overview(h=h, scored=scored):
            payload, expl = assemble_sector_overview(
                ranking_id=B.aid("sector_overview", release_id, h.horizon_id), release_id=release_id, taxonomy_id=tax,
                taxonomy_version=B.TAXONOMY_VERSION, horizon_id=h.horizon_id, forecast_target=h.target_id,
                ranking_policy_version=f"sector-opportunity-top5-mean-{h.horizon_id}-v1", scores=scored)
            with conn.transaction():
                decision_repo.insert_sector_overview(conn, payload, created_at=clock())
            bodies.update(expl)
            done(f"sector_overview:{tax}:{h.horizon_id}", f"ranking:{payload['ranking_id']}", canonical_hash(payload),
                 snapshot_id=inp.membership.manifest["snapshot_id"])
        guarded(f"sector_overview:{tax}:{h.horizon_id}", overview)

        def in_sector(h=h):
            n_runs = 0
            skipped: dict[str, str] = {}
            for s in snaps:
                node = s.node.node_id
                members = set(s.member_ids)
                facts_n = [f for f in inp.facts if f.instrument_id in members]
                if len([f for f in facts_n if f.instrument_id in included]) < cfg.sector_min_members:
                    skipped[node] = "fewer included members than the minimum"
                    continue
                try:
                    with conn.transaction():
                        built = build_and_store_ranking(
                            conn, ranking_id=B.aid("ranking", release_id, "sector", h.horizon_id, node), release_id=release_id,
                            ranking_scope="sector", scope_key=f"{tax}/{node}/{h.horizon_id}", universe_policy=policy, facts=facts_n,
                            policy=baseline_policy(h, costs_version), features=matrix, decision_as_of=inp.decision_as_of,
                            created_at=clock(), writer=None,
                            taxonomy_node={"taxonomy_id": tax, "taxonomy_version": B.TAXONOMY_VERSION, "node_id": node},
                            membership_snapshot_id=s.membership_snapshot_id)
                except RankingBlocked as exc:
                    skipped[node] = f"RankingBlocked: {exc}"
                    continue
                bodies.update(built.explanations)
                n_runs += 1
            body = {"horizon": h.horizon_id, "taxonomy": tax, "version": B.TAXONOMY_VERSION, "runs": n_runs, "skipped": skipped}
            summary_component(f"ranking:sector:{tax}:{h.horizon_id}", body, inp.membership.manifest["snapshot_id"])
        guarded(f"ranking:sector:{tax}:{h.horizon_id}", in_sector)

    # ---- fast-heating: surge components, rising overview, pool
    surge_detail: dict[str, dict] = {}
    comps: dict[str, P.SurgeComponents] = {}
    assess: dict[str, P.SurgeAssessment] = {}
    if inp.membership is not None and "20td" in runs:
        alpha20 = {e.instrument_id: e.alpha_rank for e in runs["20td"].run.entries}
        comps, surge_detail = sector_surge(inp.membership, matrix, included, alpha20)
        assess = {n: P.assess_surge(pool_policy, c) for n, c in comps.items()}

    def rising():
        scored = []
        by_node = {s.node.node_id: s for s in snaps}
        for node, a in sorted(assess.items()):
            if a.score is None:
                continue
            s = by_node[node]
            obs = surge_detail[node]["observed"]
            scored.append(SectorScore(s, "sector_rising_rank", a.score, len(set(s.member_ids)), obs,
                                      "valid" if obs / max(len(set(s.member_ids)), 1) >= Decimal("0.8") else "partial",
                                      {"method": "mean of 4 standardised surge components", "present": list(a.present), "missing": list(a.missing),
                                       "contributions": a.contributions, "policy_version": pool_policy.policy_version,
                                       "unavailable_components": MISSING_SURGE, "taxonomy": s.node.wire()}))
        payload, expl = assemble_sector_overview(
            ranking_id=B.aid("sector_rising", release_id), release_id=release_id, taxonomy_id=tax, taxonomy_version=B.TAXONOMY_VERSION,
            horizon_id="5td", forecast_target="sector_heating_surge_score_v1", ranking_policy_version=pool_policy.policy_version, scores=scored)
        with conn.transaction():
            decision_repo.insert_sector_overview(conn, payload, created_at=clock())
        bodies.update(expl)
        done(f"sector_rising:{tax}", f"ranking:{payload['ranking_id']}", canonical_hash(payload), snapshot_id=inp.membership.manifest["snapshot_id"])
    guarded(f"sector_rising:{tax}", rising)

    pool_items: list[P.PoolEvent] = []

    def pool():
        prev_rows = {r["instrument_id"]: r for r in conn.execute(
            "SELECT DISTINCT ON (instrument_id) instrument_id, event_sequence, current_state, node_id, decision_as_of FROM"
            " decision.candidate_pool_event WHERE pool_id=%s ORDER BY instrument_id, event_sequence DESC", (pool_id,))}
        alpha = {e.instrument_id: e.alpha_rank for e in runs["20td"].run.entries}
        heating = sorted((n for n, a in assess.items() if a.score is not None and a.score >= pool_policy.enter_threshold),
                         key=lambda n: (-assess[n].score, n))
        candidates: dict[uuid.UUID, str] = {i: r["node_id"] for i, r in prev_rows.items() if r["current_state"] != "removed"}
        for node in heating:
            top = sorted((m for m in inp.membership.members[node] if m in alpha), key=lambda m: (alpha[m], str(m)))[:3]
            for m in top:
                candidates.setdefault(m, node)
        member_snap = inp.membership.manifest["snapshot_id"]
        for iid, node in sorted(candidates.items(), key=lambda kv: str(kv[0])):
            a = assess.get(node)
            if a is None:
                continue
            pr = prev_rows.get(iid)
            prev = None
            if pr is not None:
                dwell = 1 + conn.execute("SELECT count(*) n FROM decision.release WHERE release_state IN ('READY','SUPERSEDED')"
                                         " AND decision_as_of > %s", (pr["decision_as_of"],)).fetchone()["n"]
                prev = P.PoolState(pr["current_state"], pr["event_sequence"], dwell)
            invalid = None if iid in included else "left_universe"
            _, change = P.decide_next_state(pool_policy, prev, a, hard_invalidation=invalid)
            if change is None:
                continue
            ev = P.make_event(pool_id=pool_id, instrument_id=iid, policy=pool_policy, prev=prev, new=change,
                              taxonomy=(tax, B.TAXONOMY_VERSION, node), membership_snapshot_id=member_snap, release_id=release_id,
                              plan_ref=None, decision_as_of=inp.decision_as_of, available_at=max(clock(), inp.decision_as_of))
            with conn.transaction():
                decision_repo.append_pool_event(conn, ev, recorded_at=clock())
            pool_items.append(ev)
        wire = decision_repo.load_pool_wire(conn, pool_id, release_id) if pool_items or prev_rows else None
        body = {"pool_id": pool_id, "policy": pool_policy.policy_version, "heating_sectors": heating, "new_events": len(pool_items),
                "events_by_state": dict(Counter(e.current_state for e in pool_items)),
                "dwell_note": "dwell counted as releases published since the previous event (state table stores no counter)",
                "pool_wire_hash": None if wire is None else canonical_hash(wire)}
        summary_component(f"candidate_pool:{pool_id}", body, inp.membership.manifest["snapshot_id"])
    guarded(f"candidate_pool:{pool_id}", pool)

    for c in P.COMPONENTS:
        key = f"heating.{c}"
        if c in pool_policy.weights and assess:
            table = {n: str(getattr(comps[n], c)) for n in sorted(comps)}
            digest = canonical_hash(table)
            ref = f"summary:{key}:{digest[:16]}"
            bodies[ref] = table
            rel.mark_ready(release_id, key, artifact_ref=ref, artifact_hash=digest, input_version_hash=ctx, computed_at=clock(),
                           snapshot_id=inp.membership.manifest["snapshot_id"])
        else:
            rel.put_component(release_id, key, component_state="MISSING",
                              artifact_ref="missing:" + MISSING_SURGE.get(c, "inputs unavailable").replace(" ", "_")[:60],
                              input_version_hash=ctx, computed_at=clock())

    # ---- workspace projections
    def workspaces():
        want: dict[uuid.UUID, str] = {}
        for h in HORIZONS:
            r = runs.get(h.horizon_id)
            if r:
                for e in r.run.entries[:cfg.workspace_page]:
                    want.setdefault(e.instrument_id, "market page 1")
        for h in h_ids:
            for iid in plans_by_h[h]:
                want.setdefault(iid, "plan")
        for e in pool_items:
            want.setdefault(e.instrument_id, "pool")
        n = _write_workspaces(conn, lake, shared, inp, release_id, want, plans_by_h, sub_plans, clock)
        counts["workspace_sources"] = dict(Counter(want.values()))
        summary_component("workspace_projection", {"workspaces": n, "view_kind": "public_research", "scope_key": "public",
                                                   "sources": dict(Counter(want.values())), "projection_version": 1})
    guarded("workspace_projection", workspaces)

    # ---- explanations dataset (all bodies, content-addressed) -> component
    def explanations():
        frame = pl.DataFrame({"ref": list(bodies), "body": [B.canonical_json(v) for v in bodies.values()]}, schema={"ref": pl.String, "body": pl.String})
        digest = hashlib.sha256(frame.sort("ref").write_csv().encode()).hexdigest()
        cov = {"dataset": "analysis_explanations", "release_id": str(release_id), "rows": frame.height, "frame_sha256": digest,
               "refs": "explain:<sha256 of body> for ranking/sector explanations; summary:<component>:<digest> for component summaries"}
        deps = [(man["snapshot_id"], "bars")] + ([(inp.membership.manifest["snapshot_id"], "sector_membership")] if inp.membership else [])
        snap = B.publish_derived(svc, "analysis_explanations", frame, cov, contract_id="pulse.analysis_artifact", dependencies=deps)
        done("explanations", f"snapshot:analysis_explanations:{snap['manifest_version']}", snap["content_hash"], snapshot_id=snap["snapshot_id"])
        counts["explanation_bodies"] = frame.height
    guarded("explanations", explanations)

    ext_a_market_state(conn, lake, rel, inp, cfg, release_id, ctx, clock, counts)        # EXT-A: optional, never blocks READY

    # ---- publish
    if problems:
        rel.fail_release(release_id)
        raise BuildFailed(release_id, [f"{k}: {v}" for k, v in sorted(problems.items())])
    try:
        pub = rel.publish(stage.stage_id, producer_version=PRODUCER)
    except IncompleteRelease as exc:
        rel.fail_release(release_id)
        raise BuildFailed(release_id, exc.missing or (str(exc),)) from exc
    counts.update(summarize_release(conn, release_id))
    counts["universe"] = {"included": len(uni.included), "excluded": len(uni.exclusions)}
    counts["features"] = {"computed": shared.computed, "reused": shared.reused}
    counts["sub_plans"] = len(sub_plans)
    return BuildResult(release_id, pub.replayed, inp.decision_as_of, res.session_date, pub.pointer_generation, counts,
                       time.monotonic() - t0, pub.degraded_components)


# --------------------------------------------------------------------------------------------------------
# workspace projections
# --------------------------------------------------------------------------------------------------------
def _write_workspaces(conn, lake, shared: B.SharedFeatures, inp: BuildInputs, release_id, want: Mapping[uuid.UUID, str],
                      plans_by_h, sub_plans, clock) -> int:
    res = inp.research
    man = res.manifest
    generated = clock()
    plan_refs: dict[uuid.UUID, list[dict]] = {}
    for h, plans in plans_by_h.items():
        for iid, p in plans.items():
            plan_refs.setdefault(iid, []).append({"plan_id": p["plan_id"], "plan_version": p["plan_version"]})
    for p in sub_plans:
        plan_refs.setdefault(uuid.UUID(p["instrument_id"]), []).append({"plan_id": p["plan_id"], "plan_version": p["plan_version"]})
    raw_by_inst = publish_chart_window(conn, want, res.session_date, inp.decision_as_of)
    n = 0
    rows = []
    for iid in sorted(want, key=str):
        meta = inp.metas.get(iid)
        if meta is None:
            continue
        feats = []
        for fid, params in WORKSPACE_FEATURES:
            _, key, row = shared.result(fid, params, [str(iid)])
            feats.append({"feature_key_hash": row["feature_key_hash"], "key": key.to_wire(), "result_state": row["result_state"],
                          "artifact": {"ref": f"feature:{row['feature_key_hash']}", "content_hash": row["result_hash"],
                                       "contract_id": "FeatureResult", "schema_version": "3.0.0"},
                          "computed_at": row["computed_at"], "quality_status": "partial"})
        charts = [{"ref": f"bars:{man['snapshot_id']}:{iid}:1d:forward_adjusted", "content_hash": man["content_hash"],
                   "contract_id": "AdjustedBar", "schema_version": "3.0.0"}]
        if iid in raw_by_inst:
            charts.append({"ref": f"bars:{raw_by_inst[iid]['snapshot_id']}:{iid}:1d:raw", "content_hash": raw_by_inst[iid]["content_hash"],
                           "contract_id": "CanonicalBar", "schema_version": "3.0.0"})
        payload = contracts.to_wire({
            "instrument": {"instrument_id": iid, "venue_code": meta.venue_code, "instrument_type": meta.instrument_type,
                           "currency": meta.currency, "exchange_timezone": meta.exchange_timezone, "display_name": meta.display_name,
                           "state_version": meta.state_version,
                           "source_symbols": [{"source_id": s, "source_symbol": c} for s, c in meta.source_symbols],
                           "trading_status": meta.trading_status, "is_risk_warning": meta.is_risk_warning, "board_code": meta.board_code},
            "release_id": release_id, "view_kind": "public_research", "scope_key": "public", "projection_version": 1,
            "projection_hash": "0" * 64, "chart_window_refs": charts, "features": feats,
            "plan_refs": sorted(plan_refs.get(iid, []), key=lambda r: (r["plan_id"], r["plan_version"])),
            "evidence_revision_ids": [], "strategy_contributions": [], "actionability_id": None, "generated_at": generated,
            "quote_overlay": None})
        digest = canonical_hash({k: v for k, v in payload.items() if k != "projection_hash"})
        payload["projection_hash"] = digest
        contracts.check("Workspace", payload)
        rows.append((iid, release_id, "public_research", "public", 1, Jsonb(payload), digest, generated))
    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO decision.workspace_projection(instrument_id, release_id, view_kind, scope_key, projection_version,"
                " projection_payload, projection_hash, generated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", rows)
    return len(rows)


CHART_WINDOW_DATASET = "bars_raw_1d_release_window"


def publish_chart_window(conn: psycopg.Connection, want, session_date, decision_as_of) -> dict:
    """One immutable raw daily window per release for the workspace instruments.

    Why: raw daily bars arrive as many per-batch snapshots (history backfill + daily increments); a chart pinned
    to one batch shows only that batch. This derived snapshot merges, per (instrument, trade_date), the latest
    revision visible at decision_as_of across every READY raw daily snapshot, records each input as a 'bars'
    dependency, and is itself READY and immutable. Rows are copied (only workspace instruments), never rewritten.
    Returns {instrument_id: {"snapshot_id", "content_hash"}}.
    """
    from pulse.adapters.postgres import market_repo
    from pulse.domain.strategy.canonical import canonical_hash

    rows = conn.execute(
        "SELECT DISTINCT ON (b.instrument_id, b.trade_date) b.*, m.snapshot_id AS src FROM market.bar_projection b"
        " JOIN market.dataset_manifest m ON m.snapshot_id=b.snapshot_id"
        " WHERE m.manifest_state='READY' AND m.dataset_id LIKE 'bars_raw_1d%%' AND m.dataset_id<>%s"
        " AND b.instrument_id = ANY(%s) AND b.timeframe='1d' AND b.trade_date <= %s AND b.available_at <= %s"
        " ORDER BY b.instrument_id, b.trade_date, b.available_at DESC",
        (CHART_WINDOW_DATASET, list(want), session_date, decision_as_of)).fetchall()
    if not rows:
        return {}
    body = [(str(r["instrument_id"]), str(r["trade_date"]), str(r["close_price_raw"]), str(r["observation_id"])) for r in rows]
    digest = canonical_hash({"rows": body, "as_of": decision_as_of.isoformat(), "layout": "release-window-v2"})
    snap = uuid.uuid5(uuid.UUID("0b8e6c52-7d0a-4f1e-9a3c-5e2d1f4b6a70"), digest)
    existing = conn.execute("SELECT manifest_state FROM market.dataset_manifest WHERE snapshot_id=%s", (snap,)).fetchone()
    if existing is None:
        now = datetime.now(timezone.utc)
        srcs = sorted({r["src"] for r in rows}, key=str)
        # Derived copy gets its own batch (dataset_id+batch_id is unique) on the newest input batch's endpoint,
        # so ingest lineage stays a real source endpoint; partition_key names the derivation.
        src_batch = conn.execute(
            "SELECT b.* FROM market.dataset_manifest m JOIN ingest.batch b ON b.batch_id=m.batch_id"
            " WHERE m.snapshot_id = ANY(%s) ORDER BY m.ready_at DESC NULLS LAST LIMIT 1", (srcs,)).fetchone()
        batch = snap
        conn.execute(
            "INSERT INTO ingest.batch(batch_id, source_id, endpoint_id, source_schema_version, mapping_version, partition_key,"
            " batch_state, received_count, started_at, raw_committed_at) VALUES (%s,%s,%s,%s,%s,%s,'NORMALIZED',0,%s,%s)"
            " ON CONFLICT (batch_id) DO NOTHING",
            (batch, src_batch["source_id"], src_batch["endpoint_id"], src_batch["source_schema_version"],
             src_batch["mapping_version"], f"derived:release_window:{snap}", now, now))
        market_repo.stage_manifest(
            conn, snapshot_id=snap, dataset_id=CHART_WINDOW_DATASET, batch_id=batch, lake_manifest_sha=digest,
            storage_uri="pg:market.bar_projection", contract_id="CanonicalBar", schema_version="3.0.0",
            row_count=len(rows), available_watermark=max(r["available_at"] for r in rows),
            coverage_document=_window_coverage(conn, rows, srcs),
            now=now, dependencies=[(s, "bars") for s in srcs])
        copy = []
        for r in rows:
            d = {k: r[k] for k in ("trade_date", "instrument_id", "timeframe", "session_scope", "bar_open_time",
                                   "bar_close_time", "is_final", "available_at", "quality_status", "observation_id")}
            d.update(snapshot_id=snap, open=r["open_price_raw"], high=r["high_price_raw"], low=r["low_price_raw"],
                     close=r["close_price_raw"], volume_shares=r["volume_shares"], turnover_cny=r["turnover_amount_cny"],
                     exchange_reference_price=r["exchange_reference_price_raw"])
            copy.append(d)
        market_repo.insert_bars(conn, copy)
        market_repo.publish_manifest(conn, snap, now, required_dependency_roles=("bars",), expected_row_count=len(rows))
    return {r["instrument_id"]: {"snapshot_id": snap, "content_hash": digest} for r in rows}


def _window_coverage(conn: psycopg.Connection, rows, srcs) -> dict:
    """Coverage of the derived window: capability is the intersection of its inputs' declared capabilities."""
    caps = [r["coverage_document"].get("capability") or {} for r in conn.execute(
        "SELECT coverage_document FROM market.dataset_manifest WHERE snapshot_id = ANY(%s)", (srcs,)).fetchall()]
    feats = set(caps[0].get("features", [])) if caps else set()
    for c in caps[1:]:
        feats &= set(c.get("features", []))
    notes = sorted({n for c in caps for n in c.get("notes", [])}) + ["derived merge of READY raw daily snapshots"]
    dates = [r["trade_date"] for r in rows]
    return {"rows": len(rows), "dataset": CHART_WINDOW_DATASET, "instruments": len({r["instrument_id"] for r in rows}),
            "first_date": str(min(dates)), "last_date": str(max(dates)), "price_basis": "raw", "adjustment_mode": "raw",
            "units": "volume shares (interval), prices CNY", "derived_from": [str(s) for s in srcs],
            "rule": "latest visible revision per instrument and trade_date",
            "non_final_rows": sum(1 for r in rows if not r["is_final"]),
            "capability": {"features": sorted(feats), "estimated": False, "source_id": "derived",
                           "dataset_id": CHART_WINDOW_DATASET, "endpoint_id": "release_window", "notes": notes,
                           "history_depth": "as inputs", "sampled_interval_seconds": None}}


# ==== EXT-A BEGIN: optional ``market_state`` component (extension group A, PEX01/PEX09) ====
MARKET_STATE_COMPONENT = "market_state"


def ext_a_optional_components() -> dict[str, str]:
    """Declared optional with ALLOW_MISSING: a failure here degrades the release, it never blocks a required component."""
    return {MARKET_STATE_COMPONENT: "ALLOW_MISSING"}


def ext_a_market_state(conn, lake, rel: PgAnalysisRelease, inp: BuildInputs, cfg: BuildConfig, release_id: uuid.UUID, ctx: str, clock,
                       counts: dict[str, Any]) -> None:
    """Compute and publish the market state of this release; on any failure record the component MISSING with the reason."""
    from pulse.application import market_today_service as MT

    if MARKET_STATE_COMPONENT in cfg.skip_components:                                  # test seam, as for the other components
        return
    try:
        if MARKET_STATE_COMPONENT in cfg.fail_components:
            raise RuntimeError(f"forced failure of {MARKET_STATE_COMPONENT}")
        svc = DataService(conn, lake, clock=clock)
        pack, pub = MT.build_component(svc, release_id=release_id, decision_as_of=inp.decision_as_of, availability=cfg.availability,
                                       membership=inp.membership)
        rel.mark_ready(release_id, MARKET_STATE_COMPONENT, artifact_ref=f"snapshot:{MT.TODAY_DATASET}:{pub.today['manifest_version']}",
                       artifact_hash=pub.today["content_hash"], input_version_hash=ctx, snapshot_id=pub.today["snapshot_id"], computed_at=clock())
        counts["market_state"] = {"session": pack.session_date.isoformat(), "panels": len(MT.STATE_PANELS), "sentiment_features": len(pub.feature_hashes) - len(MT.STATE_PANELS),
                                  "scopes": len({(k, key) for k, key, _, _ in pack.scopes})}
    except Exception as exc:  # noqa: BLE001 - optional component: recorded as MISSING with the reason, never a build problem
        rel.put_component(release_id, MARKET_STATE_COMPONENT, component_state="MISSING",
                          artifact_ref=("missing:market_state:" + type(exc).__name__ + ":" + str(exc).replace(" ", "_"))[:200],
                          input_version_hash=ctx, computed_at=clock())
        counts["market_state"] = {"missing": f"{type(exc).__name__}: {exc}"}
# ==== EXT-A END ====
