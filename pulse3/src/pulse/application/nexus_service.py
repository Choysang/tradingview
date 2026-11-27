"""Nexus Unified Application Service: connects PostgreSQL <-> Pure Functional Domain <-> Contracts.

Why: Bridges Frontend, Backend, and PostgreSQL into one zero-404, least-privilege-safe architecture:
* Zero RBAC leaks under ``pulse_api``: online GET paths NEVER DDL/DML ``core.*``, ``strategy.*``,
  ``research.*``, ``intelligence.*``, or ``ingest.*``; only read-through ``decision.stock_workbench_cache``
  writes run inside a SAVEPOINT with graceful ``InsufficientPrivilege`` fallback.
* Supports real ``adjustment_mode`` (``raw`` PIT bars, ``forward_adjusted`` / ``backward_adjusted`` LakeStore
  snapshots with honest fallback reporting when snapshots are absent).
* Eliminates all mocked regime scores, fake linear sector ranks, and unrelated market-news leaks:
  computes live market breadth/MA20/limit-up/Fusion-NAV-gate regimes, FOMC & ETF capital proxies,
  circulating-cap + turnover 4D fake-institution LHB diagnostics, funding flows, 4-session Guba resonance,
  RAPTOR real-time tilts, and 7-voter orthogonal ensemble synthesis weighted by Swiss Arena ratings.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import polars as pl
import psycopg
from psycopg.types.json import Jsonb

from pulse import config, contracts
from pulse.adapters.lake import LakeStore
from pulse.application import bindings as B
from pulse.domain.arena import (
    DailyStockBarSlice,
    build_full_taxonomy_catalog,
    compute_amihud_illiquidity,
    paper_specs_wire,
    run_swiss_glicko_season,
    score_aaai2024_sector_hypergraph,
    score_ijcai2026_prism_vq_moe,
    score_jf2026_margin_lottery_shield,
    score_jfqa2025_micro_tug_of_war,
    score_kdd2026_ebm_interpretable,
    score_ms2025_lot_momentum,
    synthesize_orthogonal_ensemble,
)
from pulse.domain.benchmark import DEFAULT_STANDARD, audit_entrypoints, price_limit_ratio
from pulse.domain.execution.grid_and_t import build_adaptive_grid_plan, build_intraday_t_plan
from pulse.domain.intelligence import feeds
from pulse.domain.intelligence.guba_four_session import analyze_four_session_resonance
from pulse.domain.intelligence.macro_cross_verify import (
    build_ashare_macro_factor_matrix,
    cross_verify_documents,
)
from pulse.domain.intelligence.realtime_adjuster import build_realtime_news_strategy_adjustment
from pulse.domain.intelligence.seat_personas import SEAT_PERSONA_REGISTRY, analyze_lhb_battle, match_seat_persona
from pulse.domain.intelligence.source_normalizer import CANONICAL_MATRIX, evaluate_polling_health
from pulse.domain.technical.tv_pro import (
    SimpleBar,
    compute_all_pivot_points,
    compute_pro_signals_and_markers,
    compute_tv_technical_rating,
    compute_vpvr_profile,
    resample_bars,
)

DEFAULT_SEASON_ID = "2026-S4-NEXUS"

# Strictly aligned with source_normalizer.CANONICAL_MATRIX (all 11 canonical core.source IDs)
_ENSURE_SOURCES: dict[str, tuple[str, str]] = {
    "xinhua_finance": ("Xinhuanet Finance official public roll (Tier-1)", "https://www.news.cn/fortune/"),
    "cninfo_announcements": ("CNINFO statutory announcements (Tier-1)", "http://www.cninfo.com.cn"),
    "cls_telegraph": ("CLS 7x24 telegraph (Tier-2)", "https://www.cls.cn/telegraph"),
    "eastmoney_news_724": ("Eastmoney 7x24 fast news (Tier-3)", "https://kuaixun.eastmoney.com"),
    "ths_news_724": ("Tonghuashun 7x24 stock push (Tier-3)", "https://news.10jqka.com.cn"),
    "sina": ("Sina Finance quotes and daily bars", "https://finance.sina.com.cn"),
    "tencent": ("Tencent Finance quotes and bars", "https://gu.qq.com"),
    "eastmoney_lhb_disclosure": ("Eastmoney Dragon-Tiger List seat disclosure", "https://data.eastmoney.com/stock/tradedetail.html"),
    "eastmoney_guba_comments": ("Eastmoney Guba community attention proxy", "https://guba.eastmoney.com"),
    "eastmoney_fund_flow": ("Eastmoney fund flow order-size proxy", "https://data.eastmoney.com/zjlx/"),
    "eastmoney_calendar": ("Eastmoney financial macro calendar", "https://data.eastmoney.com/cjrl/default.html"),
}

_CRUMBLE_SCHEMA_MAP = {
    "low": "low",
    "moderate": "moderate",
    "elevated": "moderate",
    "high": "high",
    "critical": "high",
    "unknown": "unknown",
}

_OUTBOX_PRODUCER_SPECS: tuple[tuple[str, str, str, str | None], ...] = (
    ("decision.published", "pulse.adapters.postgres.release", "PgAnalysisRelease", "publish"),
    ("account.updated", "pulse.application.account_service", "open_paper_account", None),
    ("source.health.changed", "pulse.adapters.postgres.ingest_repo", "save_health", None),
    ("actionability.ready", "pulse.application.account_service", "build_actionability", None),
)

_SURGE_COMPONENT_LABELS: tuple[tuple[str, str], ...] = (
    ("relative_strength_change", "相对强度跃升"),
    ("turnover_acceleration", "换手率加速度"),
    ("breadth", "成分扩散广度"),
    ("leader_confirmation", "前排龙头确认"),
    ("news_novelty", "多源新催化密度"),
    ("community_growth", "四时段讨论增速"),
    ("fund_proxy", "主力资金净流代理"),
)


def _dec_or_none(val: Any) -> Decimal | None:
    if val is None or val == "":
        return None
    try:
        d = Decimal(str(val))
        return d if d.is_finite() else None
    except Exception:
        return None


def _fmt_dec(val: Any, decimals: int = 4) -> str | None:
    d = _dec_or_none(val)
    if d is None:
        return None
    return f"{d:.{decimals}f}"


def _sanitize_execution_plan_enums(plan: dict[str, Any]) -> dict[str, Any]:
    lvl = _CRUMBLE_SCHEMA_MAP.get(str(plan.get("crumble_risk_level") or "unknown"), "unknown")
    plan["crumble_risk_level"] = lvl
    guards = plan.get("neurosymbolic_guards")
    if isinstance(guards, dict):
        guards["crumble_risk_level"] = _CRUMBLE_SCHEMA_MAP.get(
            str(guards.get("crumble_risk_level") or lvl), "unknown"
        )
    return plan


# ---------------------------------------------------------------------------------------------------------
# Seed / Bootstrap Helper (Worker & Privileged Bootstrap ONLY — NEVER called on GET requests)
# ---------------------------------------------------------------------------------------------------------
def seed_nexus_tables(conn: psycopg.Connection) -> None:
    """Idempotently populate sources, normalization specs, seat personas, taxonomy, and prior Swiss Arena.

    Note: NEVER call this inside read/GET API handlers (``pulse_api`` does not own DML rights on these tables).
    Preserves existing empirical backtest rows in ``research.swiss_arena_season`` / ``rating``.
    """
    src_cnt = conn.execute(
        "SELECT count(*) AS n FROM core.source WHERE source_id = ANY(%s)", (list(_ENSURE_SOURCES.keys()),)
    ).fetchone()["n"]
    map_cnt = conn.execute("SELECT count(*) AS n FROM ingest.source_field_mapping").fetchone()["n"]
    stale_map_cnt = conn.execute(
        "SELECT count(*) AS n FROM ingest.source_field_mapping WHERE source_id IN ('eastmoney_lhb', 'eastmoney_guba')"
    ).fetchone()["n"]
    seat_cnt = conn.execute("SELECT count(*) AS n FROM intelligence.seat_battle_profile").fetchone()["n"]
    tax_cnt = conn.execute("SELECT count(*) AS n FROM strategy.taxonomy_catalog").fetchone()["n"]
    rat_cnt = conn.execute(
        "SELECT count(*) AS n FROM research.swiss_arena_rating WHERE season_id=%s", (DEFAULT_SEASON_ID,)
    ).fetchone()["n"]
    season_row = conn.execute(
        "SELECT season_id, evaluation_basis FROM research.swiss_arena_season WHERE season_id=%s", (DEFAULT_SEASON_ID,)
    ).fetchone()

    cards = build_full_taxonomy_catalog()
    if (
        src_cnt >= len(_ENSURE_SOURCES)
        and map_cnt >= len(CANONICAL_MATRIX)
        and stale_map_cnt == 0
        and seat_cnt >= len(SEAT_PERSONA_REGISTRY)
        and tax_cnt >= len(cards)
        and season_row is not None
        and rat_cnt >= len(cards)
    ):
        return

    now = datetime.now(timezone.utc)
    with conn.transaction():
        for sid, (title, url) in _ENSURE_SOURCES.items():
            conn.execute(
                "INSERT INTO core.source(source_id, display_name, provider_uri, default_origin_group, created_at)"
                " VALUES (%s, %s, %s, NULL, %s) ON CONFLICT (source_id) DO NOTHING",
                (sid, title, url, now),
            )
        if stale_map_cnt > 0:
            conn.execute("DELETE FROM ingest.source_field_mapping WHERE source_id IN ('eastmoney_lhb', 'eastmoney_guba')")
        domain_kind_map = {"capital_flow": "fundflow"}
        for m in CANONICAL_MATRIX:
            db_domain_kind = domain_kind_map.get(m.domain_kind, m.domain_kind)
            conn.execute(
                "INSERT INTO ingest.source_field_mapping(mapping_id, source_id, domain_kind, raw_field_spec,"
                " canonical_field_spec, missing_policy, unit_guard_policy, schema_version, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (mapping_id) DO UPDATE SET source_id=EXCLUDED.source_id,"
                " domain_kind=EXCLUDED.domain_kind,"
                " raw_field_spec=EXCLUDED.raw_field_spec, canonical_field_spec=EXCLUDED.canonical_field_spec,"
                " missing_policy=EXCLUDED.missing_policy, unit_guard_policy=EXCLUDED.unit_guard_policy,"
                " schema_version=EXCLUDED.schema_version, updated_at=EXCLUDED.updated_at",
                (
                    m.mapping_id, m.source_id, db_domain_kind,
                    Jsonb({"fields": list(m.raw_fields), "sentinels": list(m.missing_sentinels)}),
                    Jsonb({"fields": list(m.canonical_fields), "tier": m.authority_tier, "fallback_group": m.fallback_group}),
                    m.missing_policy, m.unit_guard_policy, m.schema_version, now,
                ),
            )
        for sp in SEAT_PERSONA_REGISTRY:
            conn.execute(
                "INSERT INTO intelligence.seat_battle_profile(persona_id, persona_code, persona_label, category,"
                " match_patterns_json, empirical_d1_gap, empirical_d5_excess, style_summary, counter_tactic, effective_note, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (persona_id) DO UPDATE SET persona_label=EXCLUDED.persona_label,"
                " match_patterns_json=EXCLUDED.match_patterns_json, updated_at=EXCLUDED.updated_at",
                (
                    sp.persona_id, sp.persona_code, sp.persona_label, sp.category,
                    Jsonb([sp.regex_pattern]),
                    Decimal(sp.empirical_d1_gap_pct) if sp.empirical_d1_gap_pct else None,
                    Decimal(sp.empirical_d5_excess_pct) if sp.empirical_d5_excess_pct else None,
                    sp.style_summary, sp.counter_tactic, sp.effective_note, now,
                ),
            )
        for c in cards:
            conn.execute(
                "INSERT INTO strategy.taxonomy_catalog(strategy_code, display_name, layer, horizon_bucket, paradigm,"
                " portfolio_role, cluster_id, regime_fit_json, paper_citation, summary, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (strategy_code) DO UPDATE SET display_name=EXCLUDED.display_name,"
                " layer=EXCLUDED.layer, horizon_bucket=EXCLUDED.horizon_bucket, paradigm=EXCLUDED.paradigm,"
                " portfolio_role=EXCLUDED.portfolio_role, cluster_id=EXCLUDED.cluster_id,"
                " regime_fit_json=EXCLUDED.regime_fit_json, paper_citation=EXCLUDED.paper_citation,"
                " summary=EXCLUDED.summary, updated_at=EXCLUDED.updated_at",
                (
                    c.strategy_code, c.display_name, c.layer, c.horizon_bucket, c.paradigm,
                    c.portfolio_role, c.cluster_id, Jsonb(list(c.regime_fit)), c.paper_citation, c.summary, now,
                ),
            )

        is_empirical = bool(season_row and season_row["evaluation_basis"] == "empirical_backtest_gips_v1")
        if season_row is None or rat_cnt < len(cards):
            season = run_swiss_glicko_season(cards, rounds=5, season_id=DEFAULT_SEASON_ID)
            if not is_empirical:
                conn.execute(
                    "INSERT INTO research.swiss_arena_season(season_id, benchmark_standard, rounds_completed,"
                    " evaluation_basis, sessions_evaluated, pairings_json, correlation_json, evaluated_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (season_id) DO UPDATE SET rounds_completed=EXCLUDED.rounds_completed,"
                    " evaluation_basis=EXCLUDED.evaluation_basis, sessions_evaluated=EXCLUDED.sessions_evaluated,"
                    " pairings_json=EXCLUDED.pairings_json, correlation_json=EXCLUDED.correlation_json,"
                    " evaluated_at=EXCLUDED.evaluated_at",
                    (
                        DEFAULT_SEASON_ID, DEFAULT_STANDARD.standard_id, season["rounds_completed"],
                        season["evaluation_basis"], int(season["sessions_evaluated"]),
                        Jsonb(season["recent_pairings"]), Jsonb(season.get("pairwise_corr") or {}), now,
                    ),
                )
            for st in season["strategies"]:
                conn.execute(
                    "INSERT INTO research.swiss_arena_rating(season_id, strategy_code, glicko_mu, glicko_phi, glicko_sigma,"
                    " conservative_score, swiss_wins, swiss_draws, swiss_losses, net_return_pct, excess_return_pct,"
                    " sharpe_ratio, max_drawdown_pct, nw_t_stat, worst_cvar_pct, rre_stability, pfs_robustness,"
                    " evaluation_source, is_cluster_champion, lifecycle_tier, ensemble_weight, ram_floor_weight, updated_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (season_id, strategy_code) DO NOTHING",
                    (
                        DEFAULT_SEASON_ID, st["strategy_code"],
                        Decimal(st["glicko_mu"]), Decimal(st["glicko_phi"]), Decimal(st["glicko_sigma"]),
                        Decimal(st["conservative_score"]),
                        int(st["swiss_wins"]), int(st["swiss_draws"]), int(st["swiss_losses"]),
                        _dec_or_none(st["net_return_pct"]), _dec_or_none(st["excess_return_pct"]),
                        _dec_or_none(st["sharpe_ratio"]), _dec_or_none(st["max_drawdown_pct"]),
                        _dec_or_none(st["nw_t_stat"]), _dec_or_none(st["worst_cvar_pct"]),
                        _dec_or_none(st["rre_stability"]), _dec_or_none(st["pfs_robustness"]),
                        st["evaluation_source"], bool(st["is_cluster_champion"]), st["lifecycle_tier"],
                        Decimal(st["ensemble_weight"]), Decimal(st["ram_floor_weight"]), now,
                    ),
                )


# Compatibility alias for worker & privileged scripts
ensure_nexus_seeded = seed_nexus_tables


# ---------------------------------------------------------------------------------------------------------
# Read-Only Shared Lookups (PIT-Strict)
# ---------------------------------------------------------------------------------------------------------
def _resolve_release(conn: psycopg.Connection, release_id: UUID | None) -> dict[str, Any] | None:
    if release_id is not None:
        return conn.execute(
            "SELECT release_id, decision_as_of, availability_policy_version FROM decision.release"
            " WHERE release_id=%s AND release_state IN ('READY','SUPERSEDED')",
            (release_id,),
        ).fetchone()
    slot = conn.execute(
        "SELECT r.release_id, r.decision_as_of, r.availability_policy_version"
        " FROM decision.release_slot s JOIN decision.release r ON r.release_id = s.current_release_id"
        " WHERE s.slot_key=%s AND r.release_state IN ('READY','SUPERSEDED')",
        (B.SLOT_KEY,),
    ).fetchone()
    if slot:
        return slot
    return conn.execute(
        "SELECT release_id, decision_as_of, availability_policy_version FROM decision.release"
        " WHERE release_state IN ('READY','SUPERSEDED') ORDER BY decision_as_of DESC LIMIT 1"
    ).fetchone()


def _load_instrument_info(conn: psycopg.Connection, instrument_id: UUID) -> dict[str, Any] | None:
    inst = conn.execute(
        "SELECT i.instrument_id, i.venue_code, s.display_name, s.board_code, s.is_risk_warning,"
        " (SELECT a.source_symbol FROM core.instrument_alias a WHERE a.instrument_id=i.instrument_id"
        "  ORDER BY a.effective_from DESC LIMIT 1) AS sym,"
        " (SELECT t.node_id FROM core.instrument_tag t WHERE t.instrument_id=i.instrument_id"
        "  ORDER BY t.available_at DESC, t.effective_from DESC LIMIT 1) AS sec"
        " FROM core.instrument i"
        " LEFT JOIN LATERAL (SELECT display_name, board_code, is_risk_warning FROM core.instrument_state st"
        "                    WHERE st.instrument_id=i.instrument_id ORDER BY st.effective_from DESC, st.state_version DESC LIMIT 1) s ON true"
        " WHERE i.instrument_id=%s",
        (instrument_id,),
    ).fetchone()
    if not inst:
        return None
    sym = inst["sym"] or str(instrument_id)[:8]
    return {
        "instrument_id": inst["instrument_id"],
        "symbol": sym,
        "display_name": inst["display_name"] or sym,
        "board_code": inst["board_code"],
        "sector_node": inst["sec"],
        "is_risk_warning": bool(inst["is_risk_warning"]),
    }


def _load_daily_bars_pit(conn: psycopg.Connection, instrument_id: UUID, as_of: datetime, limit: int = 360) -> list[SimpleBar]:
    rows = conn.execute(
        "SELECT DISTINCT ON (trade_date) trade_date, open_price_raw o, high_price_raw h, low_price_raw l,"
        " close_price_raw c, volume_shares v"
        " FROM market.bar_projection"
        " WHERE instrument_id=%s AND timeframe='1d' AND available_at <= %s"
        "   AND open_price_raw IS NOT NULL AND high_price_raw IS NOT NULL"
        "   AND low_price_raw IS NOT NULL AND close_price_raw IS NOT NULL"
        " ORDER BY trade_date DESC, available_at DESC LIMIT %s",
        (instrument_id, as_of, limit),
    ).fetchall()
    rows.reverse()
    return [
        SimpleBar(
            d=r["trade_date"],
            o=float(r["o"]),
            h=float(r["h"]),
            l=float(r["l"]),
            c=float(r["c"]),
            v=float(r["v"] or 0),
        )
        for r in rows
    ]


def _lake_parquet_paths(manifest_hash: str) -> list[Path]:
    root = config.load().lake_root
    mpath = root / "manifests" / f"{manifest_hash}.json"
    data = mpath.read_bytes()
    if hashlib.sha256(data).hexdigest() != manifest_hash:
        raise ValueError(f"manifest hash mismatch: {manifest_hash}")
    doc = json.loads(data)
    return [root / m["path"] for m in doc.get("members", []) if str(m.get("path", "")).endswith(".parquet")]


def _load_bars_with_adjustment(
    conn: psycopg.Connection,
    instrument_id: UUID,
    as_of: datetime,
    requested_mode: str,
    limit: int = 360,
) -> tuple[list[SimpleBar], list[SimpleBar], str, str, float | None]:
    """Load PIT daily bars honoring ``raw``, ``forward_adjusted``, and ``backward_adjusted``.

    Returns ``(display_bars, raw_bars, effective_adjustment_mode, price_basis_note, latest_turnover_cny)``.
    Never fabricates adjusted prices: falls back to ``raw`` with explicit note when lake snapshot missing.
    """
    raw_rows = conn.execute(
        "SELECT DISTINCT ON (trade_date) trade_date, open_price_raw o, high_price_raw h, low_price_raw l,"
        " close_price_raw c, volume_shares v, turnover_amount_cny amt"
        " FROM market.bar_projection"
        " WHERE instrument_id=%s AND timeframe='1d' AND available_at <= %s"
        "   AND open_price_raw IS NOT NULL AND high_price_raw IS NOT NULL"
        "   AND low_price_raw IS NOT NULL AND close_price_raw IS NOT NULL"
        " ORDER BY trade_date DESC, available_at DESC LIMIT %s",
        (instrument_id, as_of, limit),
    ).fetchall()
    raw_rows.reverse()
    raw_bars = [
        SimpleBar(
            d=r["trade_date"],
            o=float(r["o"]),
            h=float(r["h"]),
            l=float(r["l"]),
            c=float(r["c"]),
            v=float(r["v"] or 0),
        )
        for r in raw_rows
    ]
    latest_turnover: float | None = None
    if raw_rows:
        last_r = raw_rows[-1]
        if last_r["amt"] is not None:
            latest_turnover = float(last_r["amt"])
        elif last_r["c"] is not None and last_r["v"] is not None:
            latest_turnover = float(last_r["c"]) * float(last_r["v"])

    if requested_mode == "raw":
        return raw_bars, raw_bars, "raw", "不复权原始可交易价（market.bar_projection PIT）", latest_turnover

    try:
        prefer_native = B.refdata_source() == "native"
    except ValueError:
        prefer_native = False

    if requested_mode == "forward_adjusted":
        ds_candidates = (
            ("bars_native_fwd_adjusted_1d", "bars_legacy_fwd_adjusted_1d")
            if prefer_native
            else ("bars_legacy_fwd_adjusted_1d", "bars_native_fwd_adjusted_1d")
        )
        fallback_note = "当前库无前复权湖快照，已回退原始价"
        ok_label = "前复权研究价"
    else:
        ds_candidates = ("bars_legacy_bwd_adjusted_1d", "bars_native_bwd_adjusted_1d", "bars_backward_adjusted_1d")
        fallback_note = "当前库无后复权湖快照，已回退原始价"
        ok_label = "后复权研究价"

    mans = conn.execute(
        "SELECT dataset_id, content_hash, ready_at, manifest_version FROM market.dataset_manifest"
        " WHERE dataset_id = ANY(%s) AND manifest_state='READY' AND (ready_at IS NULL OR ready_at <= %s)"
        " ORDER BY ready_at DESC NULLS LAST, manifest_version DESC",
        (list(ds_candidates), as_of),
    ).fetchall()
    by_ds = {m["dataset_id"]: m for m in mans}
    ordered_mans = [by_ds[ds] for ds in ds_candidates if ds in by_ds]

    for man in ordered_mans:
        try:
            paths = _lake_parquet_paths(man["content_hash"])
            if not paths:
                continue
            lf = pl.scan_parquet(paths).filter(pl.col("instrument_id") == str(instrument_id))
            frame = lf.collect()
            if frame.height == 0:
                continue
            if "revision" in frame.columns and "available_at" in frame.columns:
                frame = B.latest_visible_revision(frame, as_of)
            elif "available_at" in frame.columns:
                frame = frame.filter(pl.col("available_at") <= as_of)
            frame = frame.sort("trade_date").tail(limit)
            if frame.height == 0:
                continue

            o_col = "open_price" if "open_price" in frame.columns else ("open" if "open" in frame.columns else "open_price_raw")
            h_col = "high_price" if "high_price" in frame.columns else ("high" if "high" in frame.columns else "high_price_raw")
            l_col = "low_price" if "low_price" in frame.columns else ("low" if "low" in frame.columns else "low_price_raw")
            c_col = "close_price" if "close_price" in frame.columns else ("close" if "close" in frame.columns else "close_price_raw")
            v_col = "volume_shares" if "volume_shares" in frame.columns else ("volume" if "volume" in frame.columns else None)

            adj_bars: list[SimpleBar] = []
            for row in frame.iter_rows(named=True):
                o_v, h_v, l_v, c_v = row.get(o_col), row.get(h_col), row.get(l_col), row.get(c_col)
                if o_v is None or h_v is None or l_v is None or c_v is None:
                    continue
                if float(c_v) <= 0:
                    continue
                v_v = float(row.get(v_col) or 0.0) if v_col else 0.0
                td = row["trade_date"]
                if isinstance(td, datetime):
                    td = td.date()
                elif isinstance(td, str):
                    td = date.fromisoformat(td[:10])
                adj_bars.append(SimpleBar(d=td, o=float(o_v), h=float(h_v), l=float(l_v), c=float(c_v), v=v_v))

            if adj_bars:
                return (
                    adj_bars,
                    raw_bars,
                    requested_mode,
                    f"{ok_label}（{man['dataset_id']} · LakeStore PIT；买卖区间、做T与网格挂单仍按原始可交易价计算）",
                    latest_turnover,
                )
        except Exception:
            continue

    return raw_bars, raw_bars, "raw", fallback_note, latest_turnover


def _load_float_cap_cny(conn: psycopg.Connection, instrument_id: UUID, as_of: datetime) -> float | None:
    """Read real PIT circulating market cap (CNY) from ``instrument_circulating_cap_*`` LakeStore snapshot."""
    try:
        prefer_native = B.refdata_source() == "native"
    except ValueError:
        prefer_native = False
    cap_datasets = (
        ["instrument_circulating_cap_native", "instrument_circulating_cap_legacy"]
        if prefer_native
        else ["instrument_circulating_cap_legacy", "instrument_circulating_cap_native"]
    )
    mans = conn.execute(
        "SELECT dataset_id, content_hash FROM market.dataset_manifest"
        " WHERE dataset_id = ANY(%s) AND manifest_state='READY' AND (ready_at IS NULL OR ready_at <= %s)"
        " ORDER BY ready_at DESC NULLS LAST, manifest_version DESC",
        (cap_datasets, as_of),
    ).fetchall()
    by_ds = {m["dataset_id"]: m for m in mans}
    for ds in cap_datasets:
        man = by_ds.get(ds)
        if not man:
            continue
        try:
            paths = _lake_parquet_paths(man["content_hash"])
            if not paths:
                continue
            lf = pl.scan_parquet(paths).filter(pl.col("instrument_id") == str(instrument_id))
            df = lf.collect()
            if df.height == 0:
                continue
            if "available_at" in df.columns:
                df = df.filter(pl.col("available_at") <= as_of)
            if df.height == 0:
                continue
            sort_cols = [c for c in ("as_of", "available_at") if c in df.columns]
            if sort_cols:
                df = df.sort(sort_cols)
            val = df["float_cap_cny"][-1]
            if val is not None:
                f_val = float(val)
                if math.isfinite(f_val) and f_val > 0:
                    return f_val
        except Exception:
            continue
    return None


def _load_recent_fund_flow(conn: psycopg.Connection, instrument_id: UUID, as_of: datetime) -> dict[str, Any]:
    """Load recent Eastmoney main/large order flow observations for an instrument."""
    rows = conn.execute(
        "SELECT observation_window_end, metric_name, canonical_value"
        " FROM ("
        "   SELECT DISTINCT ON (source_id, observation_window_end, metric_namespace, metric_name, methodology_version)"
        "          observation_window_end, metric_name, canonical_value"
        "   FROM intelligence.funding_observation"
        "   WHERE instrument_id=%s AND available_at <= %s"
        "     AND metric_name IN ('main_net_inflow', 'main_net_inflow_ratio', 'large_net_inflow', 'super_large_net_inflow')"
        "   ORDER BY source_id, observation_window_end, metric_namespace, metric_name, methodology_version, revision DESC"
        " ) latest"
        " ORDER BY observation_window_end DESC LIMIT 40",
        (instrument_id, as_of),
    ).fetchall()
    if not rows:
        return {"has_data": False, "latest_main_net_cny": None, "main_net_5d_cny": None, "latest_ratio_pct": None, "net_large_order_positive": None}

    by_win_main: dict[datetime, float] = {}
    latest_ratio: float | None = None
    for r in rows:
        val = r["canonical_value"]
        if val is None:
            continue
        fval = float(val)
        mname = r["metric_name"]
        w_end = r["observation_window_end"]
        if mname == "main_net_inflow":
            by_win_main.setdefault(w_end, fval)
        elif mname == "main_net_inflow_ratio" and latest_ratio is None:
            latest_ratio = fval

    if not by_win_main:
        # Try deriving sum of large + super_large for latest window if main_net_inflow absent
        win_latest = rows[0]["observation_window_end"]
        lg_vals = [
            float(r["canonical_value"])
            for r in rows
            if r["observation_window_end"] == win_latest
            and r["metric_name"] in ("large_net_inflow", "super_large_net_inflow")
            and r["canonical_value"] is not None
        ]
        if lg_vals:
            latest_main = sum(lg_vals)
            return {
                "has_data": True,
                "latest_main_net_cny": latest_main,
                "main_net_5d_cny": latest_main,
                "latest_ratio_pct": latest_ratio,
                "net_large_order_positive": latest_main > 0,
            }
        return {"has_data": False, "latest_main_net_cny": None, "main_net_5d_cny": None, "latest_ratio_pct": latest_ratio, "net_large_order_positive": None}

    ordered_wins = sorted(by_win_main.keys(), reverse=True)
    latest_main = by_win_main[ordered_wins[0]]
    sum_5d = sum(by_win_main[w] for w in ordered_wins[:5])
    return {
        "has_data": True,
        "latest_main_net_cny": latest_main,
        "main_net_5d_cny": sum_5d,
        "latest_ratio_pct": latest_ratio,
        "net_large_order_positive": (latest_main > 0) if abs(latest_main) > 1e-6 else None,
    }


def _load_recent_lhb_for_stock(conn: psycopg.Connection, instrument_id: UUID, as_of: datetime) -> tuple[date | None, list[dict[str, Any]]]:
    rep = conn.execute(
        "SELECT report_revision_id, window_end FROM intelligence.seat_report"
        " WHERE instrument_id=%s AND available_at <= %s ORDER BY window_end DESC, available_at DESC LIMIT 1",
        (instrument_id, as_of),
    ).fetchone()
    if not rep:
        return None, []
    rows = conn.execute(
        "SELECT raw_seat_name, side, canonical_amount_cny FROM intelligence.seat_report_row"
        " WHERE report_revision_id=%s ORDER BY side, rank",
        (rep["report_revision_id"],),
    ).fetchall()
    return rep["window_end"], [
        {
            "seat_name": r["raw_seat_name"],
            "side": r["side"],
            "buy_cny": float(r["canonical_amount_cny"] or 0) if r["side"] == "buy" else 0.0,
            "sell_cny": float(r["canonical_amount_cny"] or 0) if r["side"] == "sell" else 0.0,
        }
        for r in rows
    ]


def _load_recent_guba_for_stock(conn: psycopg.Connection, instrument_id: UUID, as_of: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load Guba posts (resolving titles from ``analysis_payload`` or LakeStore raw HTML) plus stored session pulses."""
    iid_s = str(instrument_id)
    since = as_of - timedelta(days=14)
    rows = conn.execute(
        "SELECT c.post_id, c.author_pseudonym, c.post_created_at, c.available_at, c.analysis_payload,"
        " r.content_hash AS raw_hash, r.source_id AS raw_source_id"
        " FROM intelligence.community_observation c"
        " LEFT JOIN ingest.raw_object r ON r.raw_object_id = c.raw_object_id"
        " WHERE c.available_at <= %s AND c.available_at >= %s"
        "   AND (c.analysis_payload->'instrument_ids' ? %s OR c.analysis_payload->>'instrument_id' = %s)"
        " ORDER BY c.available_at DESC LIMIT 120",
        (as_of, since, iid_s, iid_s),
    ).fetchall()

    raw_title_cache: dict[str, dict[str, str]] = {}
    lake: LakeStore | None = None
    out: list[dict[str, Any]] = []

    for r in rows:
        p = r["analysis_payload"] or {}
        title = str(p.get("title") or p.get("headline") or "").strip()
        if not title and r.get("raw_hash") and r.get("raw_source_id") == "eastmoney_guba_comments":
            rh = str(r["raw_hash"])
            if rh not in raw_title_cache:
                title_map: dict[str, str] = {}
                try:
                    if lake is None:
                        lake = LakeStore(config.load().lake_root)
                    raw_bytes = lake.read_object(rh, verify=False)
                    parsed = feeds.parse_guba_list(raw_bytes)
                    for gp in parsed.posts:
                        title_map[str(gp.post_id)] = gp.title
                except Exception:
                    title_map = {}
                raw_title_cache[rh] = title_map
            title = raw_title_cache[rh].get(str(r["post_id"]), "")

        out.append({
            "post_id": r["post_id"],
            "title": title,
            "author_hash": r["author_pseudonym"] or p.get("author_hash") or "",
            "published_at": r["post_created_at"] or r["available_at"],
        })

    pulse_rows = conn.execute(
        "SELECT DISTINCT ON (session_bucket) session_bucket, post_count, unique_authors, bullish_ratio,"
        " divergence_index, abnormal_attn_z, sir_contagion_slope, crowd_verdict, claim_checks_json, trade_date"
        " FROM intelligence.session_sentiment_pulse"
        " WHERE instrument_id=%s AND available_at <= %s"
        " ORDER BY session_bucket, trade_date DESC, available_at DESC",
        (instrument_id, as_of),
    ).fetchall()

    return out, pulse_rows


def _merge_session_pulse_fallback(
    retail_res: dict[str, Any],
    pulse_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """When ``community_observation`` lacks raw rows/titles but ``session_sentiment_pulse`` is present, surface stored pulse."""
    if retail_res.get("total_posts", 0) > 0 or not pulse_rows:
        return retail_res
    from pulse.domain.intelligence.guba_four_session import SESSION_DEFS

    by_b = {r["session_bucket"]: r for r in pulse_rows}
    sessions_wire = []
    tot_posts = 0
    abn_z = None
    sir_slope = None
    claims: list[dict[str, Any]] = []
    verdict = "quiet_ignored"
    for key, label in SESSION_DEFS:
        pr = by_b.get(key)
        if not pr:
            sessions_wire.append({
                "session_bucket": key, "label": label, "post_count": 0, "unique_authors": 0,
                "bullish_ratio": None, "divergence_index": None, "dominant_topic": "暂无该时段样本",
                "heat_state": "no_data",
            })
            continue
        pc = int(pr["post_count"] or 0)
        tot_posts += pc
        br = float(pr["bullish_ratio"]) if pr["bullish_ratio"] is not None else 0.5
        if pr.get("abnormal_attn_z") is not None:
            abn_z = f"{float(pr['abnormal_attn_z']):.2f}"
        if pr.get("sir_contagion_slope") is not None:
            sir_slope = f"{float(pr['sir_contagion_slope']):+.2f}"
        if pr.get("crowd_verdict") in ("overheated_reversal_risk", "healthy_divergence", "early_discovery", "quiet_ignored"):
            verdict = pr["crowd_verdict"]
        for ck in (pr.get("claim_checks_json") or []):
            if isinstance(ck, dict) and ck.get("retail_claim"):
                claims.append({
                    "retail_claim": str(ck["retail_claim"]),
                    "verdict": ck.get("verdict") if ck.get("verdict") in ("supported_by_facts", "contradicted_by_facts", "unverified_noise") else "unverified_noise",
                    "objective_evidence": str(ck.get("objective_evidence") or "来自session_sentiment_pulse聚合观测"),
                })
        heat = "overheated_warning" if pc >= 25 and br >= 0.78 else ("warming" if pc >= 8 else ("normal" if pc >= 3 else "cold"))
        sessions_wire.append({
            "session_bucket": key,
            "label": label,
            "post_count": pc,
            "unique_authors": int(pr["unique_authors"] or 0),
            "bullish_ratio": _fmt_dec(pr["bullish_ratio"], 2),
            "divergence_index": _fmt_dec(pr["divergence_index"], 2),
            "dominant_topic": "基于四时段情绪脉冲落库聚合统计",
            "heat_state": heat,
        })

    if tot_posts == 0:
        return retail_res
    return {
        "total_posts": tot_posts,
        "template_spam_ratio": None,
        "abnormal_attention_z": abn_z,
        "sir_contagion_slope": sir_slope,
        "crowd_state": verdict,
        "sessions": sessions_wire,
        "claim_checks": claims[:5],
        "synthesized_judgment": f"已加载库内四时段情绪脉冲（合计样本 {tot_posts} 帖，状态={verdict}）。",
    }


def _stock_ref_tokens(meta: dict[str, Any]) -> set[str]:
    tokens: set[str] = {str(x) for x in (meta.get("instrument_ids") or [])}
    tokens.update(str(x) for x in (meta.get("instrument_codes") or []))
    srefs = meta.get("stock_refs")
    raw_val = srefs.get("raw_value") if isinstance(srefs, dict) else (srefs if isinstance(srefs, str) else "")
    if raw_val:
        tokens.update(re.findall(r"(?<!\d)(\d{6})(?!\d)", str(raw_val)))
    return tokens


def _doc_matches_stock(
    title: str,
    meta: dict[str, Any],
    *,
    iid_s: str,
    symbol: str | None,
    display_name: str | None,
) -> bool:
    tokens = _stock_ref_tokens(meta)
    if iid_s in tokens:
        return True
    if symbol and symbol in tokens:
        return True
    if symbol and symbol in title:
        return True
    if display_name and len(display_name.strip()) >= 2:
        clean_name = display_name.replace("*", "").replace("ST", "").strip()
        if len(clean_name) >= 2 and clean_name in title:
            return True
    return False


def _load_recent_docs(
    conn: psycopg.Connection,
    as_of: datetime,
    instrument_id: UUID | None = None,
    limit: int = 80,
    *,
    symbol: str | None = None,
    display_name: str | None = None,
) -> list[dict[str, Any]]:
    if instrument_id is not None:
        iid_s = str(instrument_id)
        if symbol is None and display_name is None:
            inf = _load_instrument_info(conn, instrument_id)
            if inf:
                symbol = inf["symbol"]
                display_name = inf["display_name"]
        sym_pat = f"%{symbol}%" if symbol else "%__NONE__%"
        clean_name = (display_name or "").replace("*", "").replace("ST", "").strip()
        name_pat = f"%{clean_name}%" if len(clean_name) >= 2 else "%__NONE__%"
        rows = conn.execute(
            "SELECT document_revision_id, source_id, title, published_at, available_at, field_quality"
            " FROM intelligence.document_revision"
            " WHERE available_at <= %s"
            "   AND ("
            "       (field_quality->'instrument_ids') ? %s"
            "       OR (field_quality->'instrument_codes') ? %s"
            "       OR COALESCE(field_quality->'stock_refs'->>'raw_value', '') LIKE %s"
            "       OR title LIKE %s"
            "       OR (%s <> '%%__NONE__%%' AND title LIKE %s)"
            "   )"
            " ORDER BY available_at DESC LIMIT %s",
            (as_of, iid_s, symbol or "__NONE__", sym_pat, sym_pat, name_pat, name_pat, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT document_revision_id, source_id, title, published_at, available_at, field_quality"
            " FROM intelligence.document_revision"
            " WHERE available_at <= %s ORDER BY available_at DESC LIMIT %s",
            (as_of, limit),
        ).fetchall()
        iid_s = None

    out = []
    for r in rows:
        meta = r["field_quality"] or {}
        title = r["title"] or ""
        if iid_s and not _doc_matches_stock(title, meta, iid_s=iid_s, symbol=symbol, display_name=display_name):
            continue
        insts = list(_stock_ref_tokens(meta))
        out.append({
            "doc_id": str(r["document_revision_id"]),
            "source_id": r["source_id"],
            "title": title,
            "body": "",
            "published_at": r["published_at"] or r["available_at"],
            "instrument_ids": insts,
            "sector_nodes": list(meta.get("sector_nodes") or []),
        })
    return out


def _load_stock_news_clusters(
    conn: psycopg.Connection,
    as_of: datetime,
    instrument_id: UUID,
    symbol: str,
    display_name: str,
    limit: int = 40,
) -> list[dict[str, Any]]:
    """Combine stock-matched ``cross_source_cluster`` rows and stock-matched ``document_revision`` clusters."""
    iid_s = str(instrument_id)
    sym_pat = f"%{symbol}%"
    clean_name = display_name.replace("*", "").replace("ST", "").strip()
    name_pat = f"%{clean_name}%" if len(clean_name) >= 2 else "%__NONE__%"

    db_clusters = conn.execute(
        "SELECT cluster_id, fingerprint_hash, headline, highest_tier, verification_state,"
        " corroboration_count, confidence_score, polarity, sources_json, instrument_ids_json,"
        " sector_nodes_json, bull_bear_debate, first_published_at, available_at"
        " FROM intelligence.cross_source_cluster"
        " WHERE available_at <= %s"
        "   AND ("
        "       instrument_ids_json ? %s"
        "       OR instrument_ids_json ? %s"
        "       OR headline LIKE %s"
        "       OR (%s <> '%%__NONE__%%' AND headline LIKE %s)"
        "   )"
        " ORDER BY available_at DESC LIMIT %s",
        (as_of, iid_s, symbol, sym_pat, name_pat, name_pat, limit),
    ).fetchall()

    merged: list[dict[str, Any]] = []
    seen_fp: set[str] = set()
    for c in db_clusters:
        fp = str(c["fingerprint_hash"])
        seen_fp.add(fp)
        debate = c["bull_bear_debate"] or {}
        pub = c["first_published_at"] or c["available_at"]
        merged.append({
            "cluster_id": c["cluster_id"],
            "fingerprint_hash": fp,
            "headline": c["headline"],
            "tier": c["highest_tier"],
            "verification_state": c["verification_state"],
            "corroboration_count": int(c["corroboration_count"]),
            "confidence_score": f"{float(c['confidence_score']):.2f}",
            "sources": list(c["sources_json"] or []),
            "polarity": c["polarity"],
            "affected_sectors": list(c["sector_nodes_json"] or []),
            "instrument_ids": list(c["instrument_ids_json"] or []),
            "published_at": contracts.utc_z(pub),
            "bull_bear_note": str(debate.get("note") or ""),
        })

    docs = _load_recent_docs(conn, as_of, instrument_id=instrument_id, limit=limit, symbol=symbol, display_name=display_name)
    for cl in cross_verify_documents(docs, as_of=as_of):
        if cl["fingerprint_hash"] not in seen_fp:
            seen_fp.add(cl["fingerprint_hash"])
            merged.append(cl)
    return merged


# ---------------------------------------------------------------------------------------------------------
# Real Macro / Market Regime / FOMC / ETF Signals (Zero Hardcoding)
# ---------------------------------------------------------------------------------------------------------
def _read_fusion_nav_gate(conn: psycopg.Connection, as_of: datetime) -> tuple[bool | None, float | None]:
    """Inspect latest READY ``fusion_forward_log`` lake snapshot for the 20d NAV curve gate state."""
    man = conn.execute(
        "SELECT content_hash FROM market.dataset_manifest"
        " WHERE dataset_id='fusion_forward_log' AND manifest_state='READY' AND (ready_at IS NULL OR ready_at <= %s)"
        " ORDER BY ready_at DESC NULLS LAST, manifest_version DESC LIMIT 1",
        (as_of,),
    ).fetchone()
    if not man:
        return None, None
    try:
        lake = LakeStore(config.load().lake_root)
        df = lake.read_snapshot_frames(man["content_hash"])
        if df is None or df.height == 0 or "log" not in df.columns:
            return None, None
        main_df = df.filter(pl.col("log") == "main")
        if main_df.height == 0:
            return None, None
        navs: list[tuple[date, float, float | None, bool | None]] = []
        for r in main_df.iter_rows(named=True):
            try:
                p = json.loads(r["payload"])
                d_val = r.get("date") or p.get("date")
                if isinstance(d_val, str):
                    d_val = date.fromisoformat(d_val[:10])
                if d_val is None or d_val > as_of.date():
                    continue
                if p.get("nav") is not None:
                    gw = float(p["gate_w"]) if p.get("gate_w") is not None else None
                    nba = p.get("new_buys_allowed")
                    navs.append((d_val, float(p["nav"]), gw, bool(nba) if nba is not None else None))
            except Exception:
                continue
        if not navs:
            return None, None
        navs.sort(key=lambda x: x[0])
        latest_d, latest_nav, latest_gw, latest_nba = navs[-1]
        ma20 = sum(x[1] for x in navs[-20:]) / min(20, len(navs))
        gate_triggered = (
            (latest_nba is False)
            or (latest_gw is not None and latest_gw < 1.0)
            or (len(navs) >= 20 and latest_nav < ma20)
        )
        return gate_triggered, latest_nav
    except Exception:
        return None, None


def _detect_real_market_regime(conn: psycopg.Connection, as_of: datetime) -> dict[str, Any]:
    """Compute live market regime from ``market.bar_projection`` MA20 breadth, limit-ups, amount ratio & Fusion gate."""
    max_td_row = conn.execute(
        "SELECT MAX(trade_date) AS td FROM market.bar_projection"
        " WHERE timeframe='1d' AND trade_date <= %s AND available_at <= %s",
        (as_of.date(), as_of),
    ).fetchone()
    until_d = max_td_row["td"] if (max_td_row and max_td_row["td"]) else as_of.date()
    since_d = until_d - timedelta(days=60)

    bars = conn.execute(
        "WITH inst_st AS ("
        "   SELECT DISTINCT ON (instrument_id) instrument_id, board_code, is_risk_warning"
        "   FROM core.instrument_state WHERE available_at <= %s"
        "   ORDER BY instrument_id, effective_from DESC, state_version DESC"
        "),"
        " b_dedup AS ("
        "   SELECT DISTINCT ON (instrument_id, trade_date)"
        "          instrument_id, trade_date, close_price_raw::float8 AS c,"
        "          COALESCE(turnover_amount_cny::float8, close_price_raw::float8 * COALESCE(volume_shares, 0)::float8) AS amt"
        "   FROM market.bar_projection"
        "   WHERE timeframe='1d' AND trade_date BETWEEN %s AND %s AND available_at <= %s"
        "     AND close_price_raw IS NOT NULL AND close_price_raw > 0"
        "   ORDER BY instrument_id, trade_date, available_at DESC"
        "),"
        " win AS ("
        "   SELECT b.instrument_id, b.trade_date, b.c, b.amt,"
        "          LAG(b.c) OVER (PARTITION BY b.instrument_id ORDER BY b.trade_date) AS prev_c,"
        "          AVG(b.c) OVER (PARTITION BY b.instrument_id ORDER BY b.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS ma20,"
        "          COUNT(*) OVER (PARTITION BY b.instrument_id ORDER BY b.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS cnt"
        "   FROM b_dedup b"
        "),"
        " max_d AS (SELECT MAX(trade_date) AS d FROM b_dedup)"
        " SELECT w.instrument_id, w.trade_date, w.c, w.prev_c, w.ma20, w.cnt,"
        "        COALESCE(s.board_code, 'main') AS board_code, COALESCE(s.is_risk_warning, false) AS is_rw"
        " FROM win w JOIN max_d m ON w.trade_date = m.d"
        " LEFT JOIN inst_st s ON s.instrument_id = w.instrument_id",
        (as_of, since_d, until_d, as_of),
    ).fetchall()

    amt_rows = conn.execute(
        "WITH b_dedup AS ("
        "   SELECT DISTINCT ON (instrument_id, trade_date)"
        "          instrument_id, trade_date,"
        "          COALESCE(turnover_amount_cny::float8, close_price_raw::float8 * COALESCE(volume_shares, 0)::float8) AS amt"
        "   FROM market.bar_projection"
        "   WHERE timeframe='1d' AND trade_date BETWEEN %s AND %s AND available_at <= %s"
        "     AND close_price_raw IS NOT NULL AND close_price_raw > 0"
        "   ORDER BY instrument_id, trade_date, available_at DESC"
        ")"
        " SELECT trade_date, SUM(amt) AS tot_amt FROM b_dedup GROUP BY trade_date ORDER BY trade_date DESC LIMIT 22",
        (since_d, until_d, as_of),
    ).fetchall()

    fusion_gate, fusion_nav = _read_fusion_nav_gate(conn, as_of)

    if not bars:
        return {
            "code": "range_chop",
            "label": "等待市场日线投影完善",
            "curve_gate_active": bool(fusion_gate),
            "recommended_exposure_pct": "50%",
            "summary": "当前时点尚无足够日线截面样本，维持中性半仓观察与严格止损。",
            "breadth_ma20_pct": None,
            "limit_up_count": None,
            "amount_ratio": None,
            "etf_surge_active": False,
        }

    latest_d = bars[0]["trade_date"]
    ma_eligible = [r for r in bars if int(r["cnt"]) >= 5] or bars
    above_ma20 = sum(1 for r in ma_eligible if float(r["c"]) >= float(r["ma20"]))
    breadth_ma20_pct = round(100.0 * above_ma20 / max(1, len(ma_eligible)), 1)

    limit_up_count = 0
    for r in bars:
        pc = r["prev_c"]
        if pc is None or pc <= 0:
            continue
        lim_r = float(price_limit_ratio(r["board_code"], latest_d, is_risk_warning=bool(r["is_rw"])))
        up_px = round(float(pc) * (1.0 + lim_r), 2)
        if float(r["c"]) >= up_px - 0.01:
            limit_up_count += 1

    amount_ratio: float | None = None
    if len(amt_rows) >= 3:
        latest_amt = float(amt_rows[0]["tot_amt"] or 0.0)
        hist_amts = sorted(float(r["tot_amt"] or 0.0) for r in amt_rows[1:] if (r["tot_amt"] or 0) > 0)
        if hist_amts and latest_amt > 0:
            med_amt = hist_amts[len(hist_amts) // 2]
            if med_amt > 0:
                amount_ratio = round(latest_amt / med_amt, 2)

    etf_surge_active = bool(amount_ratio is not None and amount_ratio >= 1.80)
    curve_gate_active = bool(fusion_gate) or (breadth_ma20_pct < 28.0)

    if curve_gate_active or breadth_ma20_pct < 32.0:
        code = "defensive_bear"
        label = "防御防守体制 · 净值闸门收紧" if fusion_gate else "弱势防御体制 · 宽度跌破警戒线"
        exposure = "25%" if fusion_gate else "35%"
        summary = (
            f"实测站上MA20宽度 {breadth_ma20_pct:.1f}%、涨停 {limit_up_count} 家"
            f"{f'、20日净值均线闸门触发(NAV={fusion_nav:.3f})' if fusion_gate and fusion_nav else ''}，"
            "收紧敞口并优先启用下尾防线与抗跌策略。"
        )
    elif breadth_ma20_pct >= 62.0 and (amount_ratio is None or amount_ratio >= 1.15):
        code = "bull_mainline"
        label = "主升进攻体制 · 宽基放量共振"
        exposure = "85%"
        summary = (
            f"实测站上MA20宽度 {breadth_ma20_pct:.1f}%、涨停 {limit_up_count} 家"
            f"{f'、全市场量比 {amount_ratio:.2f}x' if amount_ratio is not None else ''}，"
            "趋势与成交量共振，多策略加权偏向动量龙头与板块超边溢出。"
        )
    elif breadth_ma20_pct >= 45.0:
        code = "structural_rotation"
        label = "结构性主线轮动 · 20日净值均线护航"
        exposure = "75%"
        summary = (
            f"实测站上MA20宽度 {breadth_ma20_pct:.1f}%、涨停 {limit_up_count} 家"
            f"{f'、量比 {amount_ratio:.2f}x' if amount_ratio is not None else ''}，"
            "基于GIPS-AShare-2026统一基准与瑞士轮冠军簇正交合成，优选高胜率轮动主线。"
        )
    else:
        code = "range_chop"
        label = "震荡分化轮动 · 控仓网格降本"
        exposure = "55%"
        summary = (
            f"实测站上MA20宽度 {breadth_ma20_pct:.1f}%、涨停 {limit_up_count} 家，"
            "结构分化期控制追高仓位，强化日内正倒T与网格低吸降本。"
        )

    return {
        "code": code,
        "label": label,
        "curve_gate_active": curve_gate_active,
        "recommended_exposure_pct": exposure,
        "summary": summary,
        "breadth_ma20_pct": breadth_ma20_pct,
        "limit_up_count": limit_up_count,
        "amount_ratio": amount_ratio,
        "etf_surge_active": etf_surge_active,
    }


def _check_fomc_window(conn: psycopg.Connection, as_of: datetime) -> bool:
    """Detect whether ±2 days around ``as_of`` falls inside an FOMC / Fed rate decision window."""
    d_lo = as_of.date() - timedelta(days=2)
    d_hi = as_of.date() + timedelta(days=2)
    cal_hit = conn.execute(
        "SELECT 1 FROM calendar.event_revision"
        " WHERE available_at <= %s"
        "   AND COALESCE((scheduled_at AT TIME ZONE 'Asia/Shanghai')::date, scheduled_date) BETWEEN %s AND %s"
        "   AND status <> 'cancelled'"
        "   AND (name ILIKE '%%FOMC%%' OR name ILIKE '%%美联储%%' OR name ILIKE '%%议息%%' OR name ILIKE '%%Federal Reserve%%')"
        " LIMIT 1",
        (as_of, d_lo, d_hi),
    ).fetchone()
    if cal_hit:
        return True
    news_hit = conn.execute(
        "SELECT 1 FROM intelligence.cross_source_cluster"
        " WHERE available_at BETWEEN %s AND %s"
        "   AND (headline ILIKE '%%FOMC%%' OR headline ILIKE '%%美联储议息%%' OR headline ILIKE '%%美联储利率决议%%')"
        " LIMIT 1",
        (as_of - timedelta(days=2), as_of),
    ).fetchone()
    return bool(news_hit)


# ---------------------------------------------------------------------------------------------------------
# Arena Weight Loading (Read-Only with In-Memory Prior Fallback)
# ---------------------------------------------------------------------------------------------------------
_TRACK_AFFINITY: dict[str, dict[str, float]] = {
    "composite": {
        "fusion_static40_safe": 1.00,
        "paper_jf2026_margin_lottery_shield": 0.90,
        "paper_kdd2026_ebm_interpretable": 0.85,
        "paper_jfqa2025_micro_tug_of_war": 0.85,
        "paper_ms2025_lot_momentum": 0.80,
        "paper_aaai2024_sector_hypergraph": 0.78,
        "paper_ijcai2026_prism_vq_moe": 0.80,
    },
    "short": {
        "fusion_static40_safe": 0.95,
        "paper_jfqa2025_micro_tug_of_war": 1.15,
        "paper_aaai2024_sector_hypergraph": 1.10,
        "paper_kdd2026_ebm_interpretable": 0.75,
        "paper_ms2025_lot_momentum": 0.60,
        "paper_jf2026_margin_lottery_shield": 0.55,
        "paper_ijcai2026_prism_vq_moe": 0.45,
    },
    "swing": {
        "fusion_static40_safe": 0.90,
        "paper_ms2025_lot_momentum": 1.15,
        "paper_jf2026_margin_lottery_shield": 1.05,
        "paper_kdd2026_ebm_interpretable": 0.95,
        "paper_aaai2024_sector_hypergraph": 0.90,
        "paper_jfqa2025_micro_tug_of_war": 0.60,
        "paper_ijcai2026_prism_vq_moe": 0.70,
    },
    "long": {
        "paper_ijcai2026_prism_vq_moe": 1.25,
        "paper_jf2026_margin_lottery_shield": 1.20,
        "paper_kdd2026_ebm_interpretable": 1.00,
        "fusion_static40_safe": 0.90,
        "paper_ms2025_lot_momentum": 0.65,
        "paper_aaai2024_sector_hypergraph": 0.40,
        "paper_jfqa2025_micro_tug_of_war": 0.35,
    },
}

_VOTER_TO_CATALOG_CODE = {
    "fusion_static40_safe": "fusion_static40_safe",
    "paper_jf2026_margin_lottery_shield": "paper_icml2025_alphaqcm_tail",
    "paper_kdd2026_ebm_interpretable": "paper_kdd2026_alphaeval_rre",
    "paper_ms2025_lot_momentum": "paper_ms2025_lot_momentum",
    "paper_jfqa2025_micro_tug_of_war": "paper_jfqa2025_micro_tug_of_war",
    "paper_aaai2024_sector_hypergraph": "paper_aaai2024_sector_hypergraph",
    "paper_ijcai2026_prism_vq_moe": "paper_ijcai2026_prism_vq_moe",
}


def _load_arena_voter_weights(conn: psycopg.Connection, horizon_track: str) -> tuple[dict[str, float], str]:
    """Read active champion ``ensemble_weight + ram_floor_weight`` from PG, or compute in-memory prior if unseeded."""
    season_row = conn.execute(
        "SELECT season_id, evaluation_basis FROM research.swiss_arena_season"
        " ORDER BY CASE WHEN season_id=%s THEN 0 ELSE 1 END, evaluated_at DESC LIMIT 1",
        (DEFAULT_SEASON_ID,),
    ).fetchone()

    rating_map: dict[str, tuple[float, float]] = {}  # strategy_code -> (raw_weight, conservative_score)
    source_tag = "in_memory_prior_unseeded"

    if season_row:
        r_rows = conn.execute(
            "SELECT strategy_code, ensemble_weight, ram_floor_weight, conservative_score"
            " FROM research.swiss_arena_rating WHERE season_id=%s",
            (season_row["season_id"],),
        ).fetchall()
        if r_rows:
            for r in r_rows:
                w = float(r["ensemble_weight"] or 0.0) + float(r["ram_floor_weight"] or 0.0)
                cs = float(r["conservative_score"] or 1500.0)
                rating_map[r["strategy_code"]] = (w, cs)
            source_tag = f"pg:{season_row['season_id']}:{season_row.get('evaluation_basis') or 'unknown'}"

    if not rating_map:
        season = run_swiss_glicko_season(build_full_taxonomy_catalog(), rounds=5, season_id=DEFAULT_SEASON_ID)
        for st in season["strategies"]:
            w = float(st["ensemble_weight"] or 0.0) + float(st["ram_floor_weight"] or 0.0)
            cs = float(st["conservative_score"] or 1500.0)
            rating_map[st["strategy_code"]] = (w, cs)

    avg_pos_w = sum(w for w, _ in rating_map.values() if w > 0) / max(1, sum(1 for w, _ in rating_map.values() if w > 0))
    aff_map = _TRACK_AFFINITY.get(horizon_track, _TRACK_AFFINITY["composite"])
    out_weights: dict[str, float] = {}
    for voter_id, cat_code in _VOTER_TO_CATALOG_CODE.items():
        w_arena, cs = rating_map.get(cat_code, (0.0, 1500.0))
        aff = aff_map.get(voter_id, 0.5)
        if w_arena > 0:
            base_w = w_arena
        elif cat_code == "paper_kdd2026_alphaeval_rre" and cs >= 1460.0:
            # regime_timing champion participates as EBM 4-pillar voter weighted near mean champion floor
            base_w = max(0.08, avg_pos_w * min(1.25, cs / 1550.0))
        else:
            # Non-champion stock-selection voter retains small positive floor only if unretired
            base_w = max(0.04, avg_pos_w * 0.45) if cs >= 1460.0 else 0.0
        if base_w > 0:
            out_weights[voter_id] = round(base_w * aff, 6)

    return out_weights, source_tag


def _compute_7_voter_signals(
    slices: list[DailyStockBarSlice],
    baseline_pct: dict[str, float],
    *,
    market_regime_code: str,
) -> dict[str, dict[str, float]]:
    return {
        "fusion_static40_safe": baseline_pct,
        "paper_jf2026_margin_lottery_shield": score_jf2026_margin_lottery_shield(slices),
        "paper_kdd2026_ebm_interpretable": score_kdd2026_ebm_interpretable(slices),
        "paper_ms2025_lot_momentum": score_ms2025_lot_momentum(slices),
        "paper_jfqa2025_micro_tug_of_war": score_jfqa2025_micro_tug_of_war(slices),
        "paper_aaai2024_sector_hypergraph": score_aaai2024_sector_hypergraph(slices),
        "paper_ijcai2026_prism_vq_moe": score_ijcai2026_prism_vq_moe(slices, market_regime=market_regime_code),
    }


def _slice_atr14(sl: DailyStockBarSlice | None) -> float | None:
    if sl is None or len(sl.closes) < 5:
        return None
    n = len(sl.closes)
    start = max(1, n - 14)
    trs = [
        max(sl.highs[i] - sl.lows[i], abs(sl.highs[i] - sl.closes[i - 1]), abs(sl.lows[i] - sl.closes[i - 1]))
        for i in range(start, n)
    ]
    if not trs:
        return sl.closes[-1] * 0.025
    atr = sum(trs) / len(trs)
    return atr if atr > 0 else sl.closes[-1] * 0.025


# ---------------------------------------------------------------------------------------------------------
# 1. Stock Pro Workbench (100% Market Coverage + PostgreSQL Cache + RAPTOR Real-Time Adjustment)
# ---------------------------------------------------------------------------------------------------------
def get_stock_pro_analysis(
    conn: psycopg.Connection,
    *,
    instrument_id: UUID,
    timeframe: str = "1d",
    adjustment_mode: str = "raw",
    release_id: UUID | None = None,
    base_sellable_shares: int = 1000,
    available_cash_cny: float = 50000.0,
    grid_mode: str = "atr_adaptive",
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return ``(NexusStockProAnalysis, release_row)``; None if instrument or release missing."""
    eff_tf = "1d" if timeframe in ("1m", "5m", "15m", "1h") else timeframe
    rel = _resolve_release(conn, release_id)
    if rel is None:
        return None, None
    info = _load_instrument_info(conn, instrument_id)
    if info is None:
        return None, rel

    rid: UUID = rel["release_id"]
    as_of: datetime = rel["decision_as_of"]
    is_default_params = (
        base_sellable_shares == 1000
        and abs(available_cash_cny - 50000.0) < 0.01
        and grid_mode == "atr_adaptive"
    )
    cache_key = hashlib.sha256(f"{instrument_id}|{rid}|{eff_tf}|{adjustment_mode}".encode("utf-8")).hexdigest()

    if is_default_params:
        cached = conn.execute(
            "SELECT payload_json, computed_at FROM decision.stock_workbench_cache WHERE cache_key_hash=%s",
            (cache_key,),
        ).fetchone()
        cache_fresh = True
        if cached and release_id is None:
            lt_row = conn.execute(
                "SELECT GREATEST("
                " COALESCE((SELECT MAX(recorded_at) FROM intelligence.realtime_signal_adjustment), '1970-01-01'::timestamptz),"
                " COALESCE((SELECT MAX(recorded_at) FROM intelligence.cross_source_cluster), '1970-01-01'::timestamptz)"
                ") AS ts"
            ).fetchone()
            latest_intel_ts = lt_row["ts"] if lt_row else None
            if latest_intel_ts is not None and (cached["computed_at"] is None or cached["computed_at"] < latest_intel_ts):
                cache_fresh = False
        if (
            cached
            and cache_fresh
            and isinstance(cached["payload_json"], dict)
            and "realtime_adjustment" in cached["payload_json"]
            and "kline_bars" in cached["payload_json"]
            and "price_basis_note" in cached["payload_json"]
        ):
            payload = dict(cached["payload_json"])
            payload["cached_in_postgres"] = True
            return payload, rel

    daily_bars, raw_daily_bars, eff_adj_mode, price_basis_note, latest_turnover_cny = _load_bars_with_adjustment(
        conn, instrument_id, as_of, adjustment_mode, limit=360
    )
    exec_bars = raw_daily_bars if raw_daily_bars else daily_bars

    # Multi-timeframe TradingView 26-Indicator Ratings (1d / 1w / 1mo / 1y)
    mtf_ratings = []
    tf_weights = {"1d": 0.40, "1w": 0.30, "1mo": 0.20, "1y": 0.10}
    w_num, w_den = 0.0, 0.0
    for tf_k in ("1d", "1w", "1mo", "1y"):
        tf_bars = resample_bars(daily_bars, tf_k)
        rt = compute_tv_technical_rating(tf_bars, tf_k)
        mtf_ratings.append(rt)
        sc_str = rt["summary"]["score"]
        if sc_str is not None:
            sc_f = float(sc_str)
            w_num += tf_weights[tf_k] * sc_f
            w_den += tf_weights[tf_k]

    mtf_res_score = (w_num / w_den) if w_den > 0 else None
    if mtf_res_score is None:
        mtf_label = "unavailable"
    elif mtf_res_score < -0.5:
        mtf_label = "strong_sell"
    elif mtf_res_score < -0.1:
        mtf_label = "sell"
    elif mtf_res_score <= 0.1:
        mtf_label = "neutral"
    elif mtf_res_score <= 0.5:
        mtf_label = "buy"
    else:
        mtf_label = "strong_buy"

    active_bars = resample_bars(daily_bars, eff_tf if eff_tf in ("1d", "1w", "1mo", "1y") else "1d")
    pivots = compute_all_pivot_points(active_bars, eff_tf if eff_tf in ("1d", "1w", "1mo", "1y") else "1d")
    vpvr = compute_vpvr_profile(daily_bars)
    pro_signals, chart_markers = compute_pro_signals_and_markers(active_bars)

    kline_bars = [
        {
            "trade_date": b.d.isoformat(),
            "open": f"{b.o:.2f}",
            "high": f"{b.h:.2f}",
            "low": f"{b.l:.2f}",
            "close": f"{b.c:.2f}",
            "volume": f"{b.v:.0f}",
        }
        for b in active_bars[-120:]
    ]

    # Real Float Cap & Turnover -> 4D Fake-Institution LHB Check
    float_cap_cny = _load_float_cap_cny(conn, instrument_id, as_of)
    lhb_date, lhb_rows = _load_recent_lhb_for_stock(conn, instrument_id, as_of)
    seat_battle = analyze_lhb_battle(
        lhb_rows,
        report_date=lhb_date,
        float_cap_cny=float_cap_cny,
        turnover_cny=latest_turnover_cny,
    )

    # Real Capital / Fund Flow Observations (eastmoney_fund_flow)
    fund_flow = _load_recent_fund_flow(conn, instrument_id, as_of)

    # Strict Stock-Filtered Cross-Verified News (live view sees real-time breaking flashes; pinned release stays PIT at as_of)
    intel_cutoff = max(as_of, datetime.now(timezone.utc)) if release_id is None else as_of
    verified_clusters = _load_stock_news_clusters(
        conn, intel_cutoff, instrument_id, info["symbol"], info["display_name"], limit=40
    )
    has_neg_notice = any(c["polarity"] == "negative" and c["tier"] == "tier1_official" for c in verified_clusters)

    # 4-Session Guba Resonance & Fact-Check (with real fund_flow net_large_order_positive!)
    ret5_pct = ((daily_bars[-1].c / daily_bars[-6].c - 1.0) * 100.0) if len(daily_bars) >= 6 and daily_bars[-6].c > 0 else None
    rvol_f = float(pro_signals["rvol_20d"]) if pro_signals["rvol_20d"] is not None else None
    guba_posts, pulse_rows = _load_recent_guba_for_stock(conn, instrument_id, as_of)
    retail_res = analyze_four_session_resonance(
        guba_posts,
        price_change_5d_pct=ret5_pct,
        rvol_20d=rvol_f,
        lhasa_buyer_count=seat_battle["lhasa_buyer_count"],
        one_day_raider_count=seat_battle["one_day_raider_count"],
        has_negative_official_notice=has_neg_notice,
        net_large_order_positive=fund_flow["net_large_order_positive"],
    )
    retail_res = _merge_session_pulse_fallback(retail_res, pulse_rows)

    # All tradable CNY execution prices (intraday-T, grid, RAPTOR base, action plan, raw chart lines) stay strictly on raw tradable prices
    if eff_adj_mode == "raw":
        exec_vpvr = vpvr
        exec_pro_signals = pro_signals
    else:
        exec_vpvr = compute_vpvr_profile(exec_bars)
        exec_pro_signals, _ = compute_pro_signals_and_markers(exec_bars)

    poc_f = float(exec_vpvr["poc"]) if exec_vpvr["poc"] is not None else None
    val_f = float(exec_vpvr["val"]) if exec_vpvr["val"] is not None else None
    vah_f = float(exec_vpvr["vah"]) if exec_vpvr["vah"] is not None else None
    board_code_eff = info["board_code"] or "main"
    is_rw = bool(info["is_risk_warning"])

    t_plan = _sanitize_execution_plan_enums(
        build_intraday_t_plan(
            exec_bars,
            base_sellable_shares=base_sellable_shares,
            available_cash_cny=available_cash_cny,
            supertrend_dir=pro_signals["supertrend_dir"],
            lhasa_buyer_count=seat_battle["lhasa_buyer_count"],
            vpvr_poc=poc_f,
            board_code=board_code_eff,
            is_risk_warning=is_rw,
        )
    )
    grid_plan = _sanitize_execution_plan_enums(
        build_adaptive_grid_plan(
            exec_bars,
            mode=grid_mode,
            total_budget_cny=max(available_cash_cny, 50000.0) if base_sellable_shares > 0 else max(available_cash_cny, 10000.0),
            vpvr_val=val_f,
            vpvr_vah=vah_f,
            board_code=board_code_eff,
            is_risk_warning=is_rw,
        )
    )

    # Real-Time News RAPTOR Black-Litterman + Amihud Illiquidity + Controlled gpt-6-luna Guard
    stock_slice = DailyStockBarSlice(
        instrument_id=str(instrument_id),
        symbol=info["symbol"],
        sector_node=info.get("sector_node"),
        opens=tuple(b.o for b in exec_bars),
        highs=tuple(b.h for b in exec_bars),
        lows=tuple(b.l for b in exec_bars),
        closes=tuple(b.c for b in exec_bars),
        volumes=tuple(b.v for b in exec_bars),
    )
    amihud_val = compute_amihud_illiquidity(stock_slice)
    avg_turnover_20d: float | None = None
    if len(exec_bars) >= 5:
        turns = [b.c * b.v for b in exec_bars[-20:] if b.c > 0 and b.v > 0]
        if turns:
            avg_turnover_20d = sum(turns) / len(turns)
    elif latest_turnover_cny is not None:
        avg_turnover_20d = latest_turnover_cny

    spam_ratio_f = float(retail_res["template_spam_ratio"]) if retail_res.get("template_spam_ratio") is not None else None
    atr_f = float(exec_pro_signals["atr_14"]) if exec_pro_signals["atr_14"] is not None else None

    rt_adj = build_realtime_news_strategy_adjustment(
        symbol=info["symbol"],
        display_name=info["display_name"],
        verified_clusters=verified_clusters,
        as_of=as_of,
        instrument_id=str(instrument_id),
        sector_node=info.get("sector_node"),
        base_entry_price=exec_bars[-1].c if exec_bars else None,
        atr_14=atr_f,
        supertrend_dir=pro_signals["supertrend_dir"] if pro_signals["supertrend_dir"] != "unavailable" else "neutral",
        rvol_20d=rvol_f,
        lhasa_buyer_count=seat_battle["lhasa_buyer_count"],
        one_day_raider_count=seat_battle["one_day_raider_count"],
        template_spam_ratio=spam_ratio_f,
        amihud_illiquidity=amihud_val,
        avg_daily_turnover_cny=avg_turnover_20d,
        has_negative_official_notice=has_neg_notice,
    )
    llm_v = rt_adj["llm_view"]
    llm_sum = llm_v["summary"]
    rt_exec = rt_adj["execution_plan"]
    realtime_adjustment = {
        "tilt_bps": f"{float(rt_adj['tilt_bps']):.1f}",
        "action_directive": rt_adj["action_directive"],
        "liquidity_filter_triggered": bool(rt_adj["liquidity_filter_triggered"]),
        "risk_veto_reasons": list(rt_adj["risk_veto_reasons"]),
        "llm_model_used": rt_adj["llm_model_used"],
        "llm_status": str(rt_adj["llm_status"]),
        "guard_message": str(llm_v["guard_message"]),
        "bull_points": list(llm_sum["bull_points"]),
        "bear_points": list(llm_sum["bear_points"]),
        "holding_plan": str(rt_exec["holding_plan"]),
    }

    # Synthesize Unified Action Plan & Chart Lines reflecting RAPTOR + Fund Flow + Microstructure
    risk_vetoes: list[str] = []
    if info["is_risk_warning"]:
        risk_vetoes.append("ST/风险警示标的：执行严格仓位隔离")
    if seat_battle["lhasa_buyer_count"] >= 3:
        risk_vetoes.append(f"龙虎榜拉萨天团{seat_battle['lhasa_buyer_count']}席位扎堆(E048负超额预警)")
    if seat_battle["one_day_raider_count"] >= 1:
        risk_vetoes.append("上塘路/联储等一日游砸盘席位现身买榜")
    if seat_battle["fake_institution_check"]["verdict"] == "quant_fake_institution_risk":
        risk_vetoes.append("疑似量化假机构席位诱多(E094/E098风险)")
    if retail_res["crowd_state"] == "overheated_reversal_risk":
        risk_vetoes.append("股吧四时段SIR情绪过热反转预警(PBFJ 2024)")
    if has_neg_notice or rt_adj["action_directive"] == "risk_veto":
        risk_vetoes.append("实时风控/法定披露存在重大负面否决信号(E070/E088)")

    tech_sc = ((mtf_res_score or 0.0) + 1.0) * 50.0
    micro_sc = max(
        10.0,
        min(
            95.0,
            62.0
            - 14.0 * seat_battle["lhasa_buyer_count"]
            - 18.0 * seat_battle["one_day_raider_count"]
            + 10.0 * seat_battle["synergy_leader_count"]
            - (12.0 if seat_battle["fake_institution_check"]["verdict"] == "quant_fake_institution_risk" else 0.0),
        ),
    )
    news_sc = max(
        5.0,
        min(
            95.0,
            50.0 + float(rt_adj["tilt_bps"]) * 0.25,
        ),
    )

    cap_sc = 50.0
    if rvol_f is not None:
        if 0.9 <= rvol_f <= 2.2 and pro_signals["supertrend_dir"] == "bullish":
            cap_sc += 10.0
        elif rvol_f > 3.0:
            cap_sc -= 5.0
    if fund_flow["has_data"]:
        if fund_flow["latest_ratio_pct"] is not None:
            cap_sc += max(-24.0, min(28.0, float(fund_flow["latest_ratio_pct"]) * 2.5))
        elif fund_flow["latest_main_net_cny"] is not None:
            cap_sc += 14.0 if fund_flow["latest_main_net_cny"] > 0 else -12.0
    cap_sc = max(5.0, min(95.0, cap_sc))

    comp_sc = max(5.0, min(95.0, 0.35 * tech_sc + 0.25 * cap_sc + 0.20 * news_sc + 0.20 * micro_sc))

    chart_lines: list[dict[str, Any]] = []
    if exec_bars:
        last_c = exec_bars[-1].c
        atr_eff = atr_f if atr_f and atr_f > 0 else last_c * 0.025
        e_lo = max(0.01, min(last_c * 0.992, poc_f or last_c * 0.985))
        e_hi = max(e_lo, last_c * 1.005)
        stop_p = max(0.01, e_lo - 1.8 * atr_eff)
        tp1 = last_c + 2.0 * atr_eff
        tp2 = max(tp1 * 1.03, vah_f or (last_c + 3.5 * atr_eff))

        # Apply RAPTOR real-time execution directives onto entry / stop / targets
        if rt_adj["action_directive"] == "catalyst_boost":
            if rt_exec.get("entry_low"):
                e_lo = min(e_lo, float(rt_exec["entry_low"]))
            if rt_exec.get("take_profit_price"):
                tp1 = max(tp1, float(rt_exec["take_profit_price"]))
        elif rt_adj["action_directive"] == "tighten_stop_grid":
            if rt_exec.get("stop_loss_price"):
                stop_p = max(stop_p, float(rt_exec["stop_loss_price"]))
            if rt_exec.get("entry_high"):
                e_hi = min(e_hi, float(rt_exec["entry_high"]))
                e_lo = min(e_lo, e_hi)

        entry_low, entry_high = f"{e_lo:.2f}", f"{e_hi:.2f}"
        stop_loss, target_1, target_2 = f"{stop_p:.2f}", f"{tp1:.2f}", f"{tp2:.2f}"

        chart_lines.extend([
            {"id": "nx-entry-lo", "price": entry_low, "title": "Nexus低吸下沿", "tone": "plan", "dashed": True},
            {"id": "nx-stop", "price": stop_loss, "title": "风控止损线(ATR)", "tone": "risk", "dashed": False},
            {"id": "nx-tp1", "price": target_1, "title": "第一目标止盈(TP1)", "tone": "exit", "dashed": True},
        ])
        if exec_vpvr["poc"]:
            chart_lines.append({"id": "nx-poc", "price": exec_vpvr["poc"], "title": "VPVR筹码控制点(POC)", "tone": "draw", "dashed": True})
    else:
        entry_low = entry_high = stop_loss = target_1 = target_2 = None

    if risk_vetoes:
        verdict = "risk_avoid"
        max_pos = "0%"
        hold_h = "观望规避（等待风险信号消化）"
    elif rt_adj["action_directive"] == "tighten_stop_grid":
        verdict = "hold_and_grid_t" if comp_sc < 66.0 else "accumulate_on_dip"
        max_pos = rt_exec.get("max_position_pct") or "3%"
        hold_h = "短线防守<=3个交易日（收紧止损与网格步长）"
    elif comp_sc >= 66.0 and pro_signals["supertrend_dir"] == "bullish":
        verdict = "strong_priority"
        max_pos = rt_exec.get("max_position_pct") if rt_adj["action_directive"] == "catalyst_boost" else "10%"
        hold_h = "5~10个交易日（配合正T降本）"
    elif comp_sc >= 54.0:
        verdict = "accumulate_on_dip"
        max_pos = "5%~8%"
        hold_h = "5~20个交易日（回踩POC/S1分批低吸）"
    elif len(daily_bars) >= 20:
        verdict = "hold_and_grid_t"
        max_pos = "5%"
        hold_h = "箱体网格/底仓做T降本周期"
    else:
        verdict = "watch_wait"
        max_pos = "0%"
        hold_h = "观察等待K线与量能完备"

    rationale = [
        f"多周期TradingView评级共振：{mtf_label}（综合分 {mtf_res_score:.2f}）" if mtf_res_score is not None else "多周期指标处于预热阶段",
        f"VPVR筹码结构：控制点POC={vpvr['poc'] or '—'}，价值区[{vpvr['val'] or '—'}, {vpvr['vah'] or '—'}]，获利盘比例={vpvr['winner_ratio'] or '—'}",
        f"实时RAPTOR调制：directive={rt_adj['action_directive']}，tilt={rt_adj['tilt_bps']:+.1f}bps，Amihud低流动性过滤={'已触发' if rt_adj['liquidity_filter_triggered'] else '通过'}",
        f"量价与形态：{'、'.join(pro_signals['volume_patterns'])}；MACD背离={pro_signals['macd_divergence']}",
        seat_battle["opponent_strategy"],
    ]

    payload = contracts.to_wire({
        "instrument_id": instrument_id,
        "symbol": info["symbol"],
        "display_name": info["display_name"],
        "board_code": info["board_code"],
        "release_id": rid,
        "decision_as_of": contracts.utc_z(as_of),
        "cached_in_postgres": False,
        "cache_key_hash": cache_key,
        "timeframe": eff_tf,
        "adjustment_mode": eff_adj_mode,
        "price_basis_note": price_basis_note,
        "kline_bars": kline_bars,
        "multi_timeframe_ratings": mtf_ratings,
        "mtf_resonance_label": mtf_label,
        "mtf_resonance_score": f"{mtf_res_score:.3f}" if mtf_res_score is not None else None,
        "pivot_points": pivots,
        "vpvr": vpvr,
        "pro_signals": pro_signals,
        "chart_lines": chart_lines,
        "chart_markers": chart_markers,
        "action_plan": {
            "verdict": verdict,
            "composite_score": f"{comp_sc:.1f}" if daily_bars else None,
            "entry_low": entry_low,
            "entry_high": entry_high,
            "stop_loss": stop_loss,
            "target_1": target_1,
            "target_2": target_2,
            "holding_sessions": hold_h,
            "max_position_pct": max_pos,
            "risk_vetoes": risk_vetoes,
            "pillar_scores": {
                "technical": f"{tech_sc:.1f}" if daily_bars else None,
                "capital_flow": f"{cap_sc:.1f}" if daily_bars else None,
                "news_catalyst": f"{news_sc:.1f}",
                "sentiment_micro": f"{micro_sc:.1f}",
            },
            "rationale": rationale,
        },
        "realtime_adjustment": realtime_adjustment,
        "intraday_t_plan": t_plan,
        "grid_plan": grid_plan,
        "retail_resonance": retail_res,
        "seat_battle": seat_battle,
        "verified_news": [
            {
                "cluster_id": c["cluster_id"],
                "headline": c["headline"],
                "tier": c["tier"],
                "verification_state": c["verification_state"],
                "sources": c["sources"],
                "polarity": c["polarity"],
                "published_at": c["published_at"],
                "bull_bear_note": c["bull_bear_note"],
            }
            for c in verified_clusters[:8]
        ],
    })

    if is_default_params and eff_adj_mode == adjustment_mode:
        try:
            with conn.transaction():
                conn.execute(
                    "INSERT INTO decision.stock_workbench_cache(cache_key_hash, instrument_id, release_id, timeframe,"
                    " adjustment_mode, decision_as_of, payload_json, computed_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (instrument_id, release_id, timeframe, adjustment_mode)"
                    " DO UPDATE SET cache_key_hash=EXCLUDED.cache_key_hash, decision_as_of=EXCLUDED.decision_as_of,"
                    " payload_json=EXCLUDED.payload_json, computed_at=EXCLUDED.computed_at",
                    (cache_key, instrument_id, rid, eff_tf, adjustment_mode, as_of, Jsonb(payload), datetime.now(timezone.utc)),
                )
        except psycopg.errors.InsufficientPrivilege:
            pass

    return payload, rel


# ---------------------------------------------------------------------------------------------------------
# 2. Universe Slice Loader (Microstructure-Enriched PIT) & Sector Role Classifier
# ---------------------------------------------------------------------------------------------------------
def _classify_sector_role(rank_in_sector: int, ret5: float, avg_vol: float, sec_median_vol: float) -> str:
    if rank_in_sector == 1 and ret5 > 0.04:
        return "leader_1"
    if rank_in_sector == 2 and ret5 > 0.02:
        return "leader_2"
    if avg_vol >= max(1.0, sec_median_vol * 2.0):
        return "core_capacity"
    if ret5 >= 0.07:
        return "pioneer"
    if ret5 <= 0.01 and rank_in_sector <= 5:
        return "catchup"
    return "member"


def _load_microstructure_flags(
    conn: psycopg.Connection,
    uids: list[UUID],
    as_of: datetime,
) -> tuple[dict[str, tuple[bool, bool]], dict[str, float]]:
    """Batch-load LHB (lhasa_flag, one_day_raider_flag) and Guba abnormal_z per instrument."""
    if not uids:
        return {}, {}
    lhb_flags: dict[str, tuple[bool, bool]] = {}
    lhb_since = as_of.date() - timedelta(days=10)
    seat_rows = conn.execute(
        "WITH latest_rep AS ("
        "   SELECT DISTINCT ON (instrument_id) instrument_id, report_revision_id, window_end"
        "   FROM intelligence.seat_report"
        "   WHERE instrument_id = ANY(%s) AND window_end >= %s AND available_at <= %s"
        "   ORDER BY instrument_id, window_end DESC, available_at DESC"
        ")"
        " SELECT lr.instrument_id, lr.window_end, sr.raw_seat_name, sr.side"
        " FROM latest_rep lr JOIN intelligence.seat_report_row sr ON sr.report_revision_id = lr.report_revision_id",
        (uids, lhb_since, as_of),
    ).fetchall()
    by_inst_seats: dict[str, list[dict[str, Any]]] = {}
    for r in seat_rows:
        by_inst_seats.setdefault(str(r["instrument_id"]), []).append(r)
    for iid_s, srows in by_inst_seats.items():
        lhasa_b = 0
        raiders = 0
        for sr in srows:
            if sr["side"] != "buy":
                continue
            sp = match_seat_persona(sr["raw_seat_name"] or "", sr["window_end"])
            if sp is None:
                continue
            if sp.category == "lhasa_retail_corps":
                lhasa_b += 1
            elif sp.category == "one_day_dump_raider":
                raiders += 1
        lhb_flags[iid_s] = (lhasa_b >= 2, raiders >= 1)

    guba_z: dict[str, float] = {}
    pulse_rows = conn.execute(
        "SELECT DISTINCT ON (instrument_id) instrument_id, abnormal_attn_z"
        " FROM intelligence.session_sentiment_pulse"
        " WHERE instrument_id = ANY(%s) AND available_at <= %s AND abnormal_attn_z IS NOT NULL"
        " ORDER BY instrument_id, trade_date DESC, available_at DESC",
        (uids, as_of),
    ).fetchall()
    for pr in pulse_rows:
        guba_z[str(pr["instrument_id"])] = float(pr["abnormal_attn_z"])

    guba_since = as_of - timedelta(days=5)
    obs_rows = conn.execute(
        "SELECT c.analysis_payload->>'instrument_id' AS iid, count(*) AS cnt"
        " FROM intelligence.community_observation c"
        " WHERE c.available_at BETWEEN %s AND %s AND c.analysis_payload->>'instrument_id' = ANY(%s)"
        " GROUP BY 1",
        (guba_since, as_of, [str(u) for u in uids]),
    ).fetchall()
    for orow in obs_rows:
        iid_s = orow["iid"]
        if iid_s and iid_s not in guba_z:
            cnt = int(orow["cnt"] or 0)
            guba_z[iid_s] = round((math.log(1.0 + cnt) - math.log(1.0 + 8.0)) / 0.45, 2)

    return lhb_flags, guba_z


def _build_slices_for_instruments(
    conn: psycopg.Connection,
    uids: list[UUID],
    as_of: datetime,
    *,
    taxonomy_id: str | None = None,
    lookback_days: int = 95,
) -> tuple[list[DailyStockBarSlice], dict[str, dict[str, Any]]]:
    if not uids:
        return [], {}

    tag_cond = " AND t.taxonomy_id = %s" if taxonomy_id else ""

    meta_rows = conn.execute(
        "SELECT i.instrument_id, s.display_name, s.board_code, s.is_risk_warning,"
        " (SELECT a.source_symbol FROM core.instrument_alias a WHERE a.instrument_id=i.instrument_id"
        "  ORDER BY a.effective_from DESC LIMIT 1) AS sym,"
        f" (SELECT t.node_id FROM core.instrument_tag t WHERE t.instrument_id=i.instrument_id AND t.available_at <= %s"
        f"  AND (t.effective_to IS NULL OR t.effective_to > %s){tag_cond}"
        "  ORDER BY t.effective_from DESC, t.available_at DESC LIMIT 1) AS sec"
        " FROM core.instrument i"
        " LEFT JOIN LATERAL (SELECT display_name, board_code, is_risk_warning FROM core.instrument_state st"
        "                    WHERE st.instrument_id=i.instrument_id ORDER BY st.effective_from DESC, st.state_version DESC LIMIT 1) s ON true"
        " WHERE i.instrument_id = ANY(%s)",
        (as_of, as_of, *([taxonomy_id] if taxonomy_id else []), uids),
    ).fetchall()
    info_map = {
        str(r["instrument_id"]): {
            "symbol": r["sym"] or str(r["instrument_id"])[:8],
            "display_name": r["display_name"] or r["sym"] or str(r["instrument_id"])[:8],
            "sector_node": r["sec"],
            "board_code": r["board_code"],
            "is_risk_warning": bool(r["is_risk_warning"]),
        }
        for r in meta_rows
    }

    max_td_row = conn.execute(
        "SELECT MAX(trade_date) AS td FROM market.bar_projection"
        " WHERE instrument_id = ANY(%s) AND timeframe='1d' AND trade_date <= %s AND available_at <= %s",
        (uids, as_of.date(), as_of),
    ).fetchone()
    anchor_d = max_td_row["td"] if (max_td_row and max_td_row["td"]) else as_of.date()
    since = anchor_d - timedelta(days=max(lookback_days, 160))
    bar_rows = conn.execute(
        "SELECT DISTINCT ON (instrument_id, trade_date) instrument_id, trade_date,"
        " open_price_raw o, high_price_raw h, low_price_raw l, close_price_raw c, volume_shares v"
        " FROM market.bar_projection"
        " WHERE instrument_id = ANY(%s) AND timeframe='1d' AND trade_date BETWEEN %s AND %s AND available_at <= %s"
        "   AND open_price_raw IS NOT NULL AND close_price_raw IS NOT NULL"
        " ORDER BY instrument_id, trade_date ASC, available_at DESC",
        (uids, since, anchor_d, as_of),
    ).fetchall()

    lhb_flags, guba_z = _load_microstructure_flags(conn, uids, as_of)

    grouped: dict[str, list[Any]] = {}
    for b in bar_rows:
        grouped.setdefault(str(b["instrument_id"]), []).append(b)

    slices: list[DailyStockBarSlice] = []
    for iid_s, brows in grouped.items():
        inf = info_map.get(iid_s, {})
        lh_flag, rd_flag = lhb_flags.get(iid_s, (False, False))
        slices.append(DailyStockBarSlice(
            instrument_id=iid_s,
            symbol=inf.get("symbol", iid_s[:8]),
            sector_node=inf.get("sector_node"),
            opens=tuple(float(x["o"]) for x in brows),
            highs=tuple(float(x["h"] or x["c"]) for x in brows),
            lows=tuple(float(x["l"] or x["c"]) for x in brows),
            closes=tuple(float(x["c"]) for x in brows),
            volumes=tuple(float(x["v"] or 0) for x in brows),
            abnormal_guba_z=guba_z.get(iid_s),
            lhasa_flag=lh_flag,
            one_day_raider_flag=rd_flag,
        ))
    return slices, info_map


def _load_universe_slices(
    conn: psycopg.Connection, rel_id: UUID, as_of: datetime, max_stocks: int = 260
) -> tuple[list[DailyStockBarSlice], dict[str, dict[str, Any]], dict[str, float]]:
    ranked_rows = conn.execute(
        "SELECT DISTINCT ON (e.instrument_id) e.instrument_id, e.alpha_rank, e.relative_rank_score"
        " FROM decision.ranking_entry e JOIN decision.ranking_run r USING (ranking_id)"
        " WHERE r.release_id=%s AND r.ranking_scope='market'"
        " ORDER BY e.instrument_id, e.alpha_rank ASC LIMIT %s",
        (rel_id, max_stocks),
    ).fetchall()
    if not ranked_rows:
        ranked_rows = conn.execute(
            "SELECT i.instrument_id, 999 AS alpha_rank, 0.5 AS relative_rank_score"
            " FROM core.instrument i WHERE i.instrument_type='equity' LIMIT %s",
            (min(max_stocks, 120),),
        ).fetchall()

    uids = [r["instrument_id"] for r in ranked_rows]
    baseline_pct: dict[str, float] = {}
    n_rk = max(1, len(ranked_rows))
    sorted_rk = sorted(ranked_rows, key=lambda x: (int(x["alpha_rank"]), str(x["instrument_id"])))
    for idx, r in enumerate(sorted_rk):
        baseline_pct[str(r["instrument_id"])] = (n_rk - idx) / n_rk

    slices, info_map = _build_slices_for_instruments(conn, uids, as_of, lookback_days=95)
    return slices, info_map, baseline_pct


# ---------------------------------------------------------------------------------------------------------
# 3. Hub Overview: Orthogonal Recommendations + Rapidly Rising Sectors + Macro & Realtime RAPTOR Views
# ---------------------------------------------------------------------------------------------------------
def get_hub_overview(
    conn: psycopg.Connection,
    *,
    horizon_track: str = "composite",
    limit: int = 20,
    release_id: UUID | None = None,
) -> dict[str, Any]:
    if horizon_track not in ("composite", "short", "swing", "long"):
        horizon_track = "composite"
    now = datetime.now(timezone.utc)
    rel = _resolve_release(conn, release_id)

    h_rows = conn.execute(
        "SELECT source_id, runtime_state, freshness_state,"
        " COALESCE(last_new_record_at, last_transport_ok_at) AS last_success_at"
        " FROM ingest.source_health"
    ).fetchall()
    poll_eval = evaluate_polling_health(h_rows, now=now)
    poll_wire = {
        "total_sources": poll_eval["total_sources"],
        "healthy_count": poll_eval["healthy_count"],
        "degraded_count": poll_eval["degraded_count"],
        "silent_warning_count": poll_eval["silent_warning_count"],
        "host_rate_limit_rps": poll_eval["host_rate_limit_rps"],
        "sources": [
            {
                "source_id": d.source_id,
                "tier": d.authority_tier,
                "runtime_state": d.runtime_state,
                "freshness_state": d.freshness_state,
                "last_success_at": contracts.utc_z(d.last_success_at),
                "fallback_group": d.fallback_group,
            }
            for d in poll_eval["diagnostics"][:20]
        ],
    }

    voter_weights, arena_weight_source = _load_arena_voter_weights(conn, horizon_track)

    if rel is None:
        return contracts.to_wire({
            "release_id": None,
            "decision_as_of": None,
            "horizon_track": horizon_track,
            "normalization_rule": "All-Voter Weight Normalization (unpicked=0, validated-voter gate active)",
            "arena_weight_source": arena_weight_source,
            "market_regime": {
                "code": "awaiting_release",
                "label": "等待首次分析发布",
                "curve_gate_active": False,
                "recommended_exposure_pct": "0%",
                "summary": "当前数据库尚无已发布的分析快照；运行 analysis daily 后自动生成全量推荐榜与升温备选池。",
            },
            "macro_factor_matrix": build_ashare_macro_factor_matrix(),
            "source_polling_status": poll_wire,
            "cross_verified_flashes": [],
            "active_realtime_adjustments": [],
            "picks": [],
            "rapid_rising_sectors": [],
            "candidate_watch_pool": [],
        })

    rid: UUID = rel["release_id"]
    as_of: datetime = rel["decision_as_of"]

    # Real Market Regime + FOMC + ETF + Funding + Guba counts
    regime_info = _detect_real_market_regime(conn, as_of)
    intel_cutoff = max(as_of, now) if release_id is None else as_of
    fomc_active = _check_fomc_window(conn, intel_cutoff)

    # Cross-verify recent multi-source documents (merge DB clusters + doc_revisions)
    db_flash_rows = conn.execute(
        "SELECT cluster_id, fingerprint_hash, headline, highest_tier, verification_state,"
        " corroboration_count, confidence_score, polarity, sources_json, instrument_ids_json,"
        " sector_nodes_json, first_published_at, available_at"
        " FROM intelligence.cross_source_cluster"
        " WHERE available_at <= %s ORDER BY available_at DESC LIMIT 40",
        (intel_cutoff,),
    ).fetchall()
    docs = _load_recent_docs(conn, intel_cutoff, limit=80)
    doc_flashes = cross_verify_documents(docs, as_of=intel_cutoff)

    flashes: list[dict[str, Any]] = []
    seen_fp: set[str] = set()
    for c in db_flash_rows:
        seen_fp.add(str(c["fingerprint_hash"]))
        flashes.append({
            "cluster_id": c["cluster_id"],
            "headline": c["headline"],
            "verification_state": c["verification_state"],
            "corroboration_count": int(c["corroboration_count"]),
            "confidence_score": f"{float(c['confidence_score']):.2f}",
            "sources": list(c["sources_json"] or []),
            "polarity": c["polarity"],
            "affected_sectors": list(c["sector_nodes_json"] or []),
            "instrument_ids": list(c["instrument_ids_json"] or []),
            "published_at": contracts.utc_z(c["first_published_at"] or c["available_at"]),
        })
    for cl in doc_flashes:
        if cl["fingerprint_hash"] not in seen_fp:
            seen_fp.add(cl["fingerprint_hash"])
            flashes.append({
                "cluster_id": cl["cluster_id"],
                "headline": cl["headline"],
                "verification_state": cl["verification_state"],
                "corroboration_count": cl["corroboration_count"],
                "confidence_score": cl["confidence_score"],
                "sources": cl["sources"],
                "polarity": cl["polarity"],
                "affected_sectors": cl["affected_sectors"],
                "instrument_ids": cl["instrument_ids"],
                "published_at": cl["published_at"],
            })

    # Load active realtime RAPTOR adjustments (last 10 visible up to intel_cutoff)
    rt_rows = conn.execute(
        "SELECT adjustment_id, target_scope, target_key, tilt_bps, action_directive, llm_model_used, llm_summary, available_at"
        " FROM intelligence.realtime_signal_adjustment"
        " WHERE available_at <= %s ORDER BY available_at DESC LIMIT 10",
        (intel_cutoff,),
    ).fetchall()
    active_realtime_adjustments = [
        {
            "adjustment_id": r["adjustment_id"],
            "target_scope": r["target_scope"],
            "target_key": r["target_key"],
            "tilt_bps": f"{float(r['tilt_bps']):.1f}",
            "action_directive": r["action_directive"],
            "llm_model_used": r["llm_model_used"],
            "summary": str(r["llm_summary"] or f"{r['action_directive']} ({float(r['tilt_bps']):+.1f}bps)"),
            "available_at": contracts.utc_z(r["available_at"]),
        }
        for r in rt_rows
    ]

    # Load universe slices & run ALL 7 orthogonal voters
    slices, info_map, baseline_pct = _load_universe_slices(conn, rid, as_of, max_stocks=260)
    slice_by_id = {s.instrument_id: s for s in slices}
    voter_signals = _compute_7_voter_signals(slices, baseline_pct, market_regime_code=regime_info["code"])

    # Map active_realtime_adjustments -> bounded RAPTOR BL views per instrument_id
    raptor_views: dict[str, float] = {}
    sym_to_iid = {inf["symbol"]: iid_s for iid_s, inf in info_map.items()}
    for r in rt_rows:
        view_val = max(-1.0, min(1.0, float(r["tilt_bps"]) / 150.0))
        scope = r["target_scope"]
        key = str(r["target_key"])
        if scope == "instrument":
            tgt_iid = key if key in slice_by_id else sym_to_iid.get(key)
            if tgt_iid:
                raptor_views[tgt_iid] = max(-1.0, min(1.0, raptor_views.get(tgt_iid, 0.0) + view_val))
        elif scope == "sector":
            for iid_s, inf in info_map.items():
                if inf.get("sector_node") == key:
                    raptor_views[iid_s] = max(-1.0, min(1.0, raptor_views.get(iid_s, 0.0) + 0.6 * view_val))

    validated = set(voter_signals.keys())
    ens_scores, supporters, n_voters = synthesize_orthogonal_ensemble(
        voter_signals,
        voter_weights,
        validated_voters=validated,
        raptor_views=raptor_views or None,
    )

    horizon_labels = {
        "composite": "5~15交易日（多策略正交共振）",
        "short": "3~5交易日（短线微观博弈）",
        "swing": "10~20交易日（波段趋势中军）",
        "long": "20~60交易日（中长线低波底仓）",
    }

    sorted_picks = sorted(ens_scores.items(), key=lambda kv: (-kv[1], kv[0]))[: max(1, min(limit, 50))]
    picks_wire: list[dict[str, Any]] = []
    for rk, (iid_s, sc) in enumerate(sorted_picks, start=1):
        inf = info_map.get(iid_s, {"symbol": iid_s[:8], "display_name": iid_s[:8], "sector_node": None, "is_risk_warning": False})
        sl = slice_by_id.get(iid_s)
        if sl and len(sl.closes) >= 2:
            c_last = sl.closes[-1]
            ret5 = (sl.closes[-1] / sl.closes[-6] - 1.0) if len(sl.closes) >= 6 and sl.closes[-6] > 0 else 0.0
            atr_p = _slice_atr14(sl) or (c_last * 0.025)
            e_lo = f"{max(0.01, c_last - 0.5 * atr_p):.2f}"
            e_hi = f"{c_last + 0.15 * atr_p:.2f}"
            stop = f"{max(0.01, c_last - 1.8 * atr_p):.2f}"
            tp = f"{c_last + 2.5 * atr_p:.2f}"
            avg_v = sum(sl.volumes[-5:]) / max(1, len(sl.volumes[-5:]))
            role = _classify_sector_role(rk, ret5, avg_v, avg_v)
        else:
            e_lo = e_hi = stop = tp = None
            role = "member"

        sup = supporters.get(iid_s, [])
        tags = [horizon_track.upper(), f"共振{len(sup)}/{n_voters}"]
        if inf.get("sector_node"):
            tags.append(str(inf["sector_node"]))
        if sl and sl.lhasa_flag:
            tags.append("拉萨拥挤警示")
        r_flags = ["ST风险警示"] if inf.get("is_risk_warning") else []
        if sl and sl.one_day_raider_flag:
            r_flags.append("一日游席位警示")
        picks_wire.append({
            "rank": rk,
            "instrument_id": iid_s,
            "symbol": inf["symbol"],
            "display_name": inf["display_name"],
            "sector_node": inf.get("sector_node"),
            "sector_role": role,
            "ensemble_score": f"{sc * 100.0:.2f}",
            "voter_support_count": len(sup),
            "total_active_voters": n_voters,
            "entry_low": e_lo,
            "entry_high": e_hi,
            "stop_loss": stop,
            "target_price": tp,
            "holding_horizon": horizon_labels[horizon_track],
            "tags": tags,
            "supporting_strategies": sup[:4],
            "risk_flags": r_flags,
            "catalyst_summary": f"由 {', '.join(sup[:2])} 等 {len(sup)}/{n_voters} 算子按瑞士轮冠军权重归一合成",
        })

    # Rapidly Rising Sectors & Candidate Watch Pool
    rising_rows = conn.execute(
        "SELECT e.taxonomy_id, e.node_id, e.rank, e.score, e.member_count, e.explanation_ref"
        " FROM decision.sector_rank_entry e JOIN decision.ranking_run r USING (ranking_id)"
        " WHERE r.release_id=%s AND e.rank_kind='sector_rising_rank' ORDER BY e.rank ASC LIMIT 8",
        (rid,),
    ).fetchall()
    pool_rows = conn.execute(
        "SELECT DISTINCT ON (instrument_id) instrument_id, taxonomy_id, node_id, current_state, trigger_code, trigger_payload"
        " FROM decision.candidate_pool_event WHERE release_id=%s ORDER BY instrument_id, event_sequence DESC",
        (rid,),
    ).fetchall()

    pool_by_node: dict[str, list[dict[str, Any]]] = {}
    watch_pool_wire: list[dict[str, Any]] = []
    for pr in pool_rows:
        p_iid = str(pr["instrument_id"])
        p_inf = info_map.get(p_iid) or _load_instrument_info(conn, pr["instrument_id"]) or {"symbol": p_iid[:8], "display_name": p_iid[:8]}
        t_pay = pr["trigger_payload"] or {}
        sc_val = str(t_pay.get("score")) if t_pay.get("score") is not None else None
        cand_obj = {
            "instrument_id": p_iid,
            "symbol": p_inf["symbol"],
            "display_name": p_inf["display_name"],
            "role": "pioneer" if pr["current_state"] == "actionable" else "catchup",
            "pool_state": pr["current_state"],
            "score": sc_val,
        }
        pool_by_node.setdefault(pr["node_id"], []).append(cand_obj)
        watch_pool_wire.append({
            "instrument_id": p_iid,
            "symbol": p_inf["symbol"],
            "display_name": p_inf["display_name"],
            "sector_node": pr["node_id"],
            "pool_state": pr["current_state"],
            "trigger_code": pr["trigger_code"],
            "entry_hint": "已通过升温驻留与交易计划校验，可进入工作台执行低吸" if pr["current_state"] == "actionable" else "板块火速晋升早期锁定，纳入重点观察备选池",
        })

    heating_cards: list[dict[str, Any]] = []
    for rr in rising_rows:
        nid = rr["node_id"]
        sc_f = float(rr["score"])
        vel = "rapid_promotion" if rr["rank"] <= 3 else ("steady_warming" if sc_f > 0 else "observing")
        heating_cards.append({
            "rank": int(rr["rank"]),
            "taxonomy_id": rr["taxonomy_id"],
            "node_id": nid,
            "surge_score": f"{sc_f:.3f}",
            "velocity_label": vel,
            "member_count": int(rr["member_count"]),
            "present_components": ["relative_strength_change", "turnover_acceleration", "breadth", "leader_confirmation"],
            "missing_components": ["news_novelty", "community_growth", "fund_proxy"] if not flashes else [],
            "watch_candidates": pool_by_node.get(nid, [])[:4],
        })

    lhb_cnt = conn.execute("SELECT count(*) AS n FROM intelligence.seat_report WHERE available_at <= %s", (as_of,)).fetchone()["n"]
    guba_cnt = conn.execute(
        "SELECT count(*) AS n FROM intelligence.community_observation WHERE available_at BETWEEN %s AND %s",
        (as_of - timedelta(days=7), as_of),
    ).fetchone()["n"]
    ff_sum_row = conn.execute(
        "WITH latest AS ("
        "   SELECT DISTINCT ON (instrument_id) instrument_id, canonical_value, observation_window_end::date AS w_d"
        "   FROM intelligence.funding_observation"
        "   WHERE metric_name='main_net_inflow' AND available_at <= %s AND observation_window_end >= %s"
        "   ORDER BY instrument_id, observation_window_end DESC, revision DESC"
        ")"
        " SELECT SUM(canonical_value)::float8 AS net_cny, COUNT(*) AS cnt FROM latest",
        (as_of, as_of - timedelta(days=7)),
    ).fetchone()
    ff_net_cny = float(ff_sum_row["net_cny"]) if (ff_sum_row and ff_sum_row["cnt"] and ff_sum_row["net_cny"] is not None) else None

    return contracts.to_wire({
        "release_id": str(rid),
        "decision_as_of": contracts.utc_z(as_of),
        "horizon_track": horizon_track,
        "normalization_rule": "All-Voter Weight Normalization: sum(w_v * pct_v) / sum_{all active voters}(w_v); unpicked=0",
        "arena_weight_source": arena_weight_source,
        "market_regime": {
            "code": regime_info["code"],
            "label": regime_info["label"],
            "curve_gate_active": regime_info["curve_gate_active"],
            "recommended_exposure_pct": regime_info["recommended_exposure_pct"],
            "summary": regime_info["summary"],
        },
        "macro_factor_matrix": build_ashare_macro_factor_matrix(
            market_regime_code=regime_info["code"],
            regime_label=regime_info["label"],
            breadth_ma20_pct=regime_info["breadth_ma20_pct"],
            limit_up_count=regime_info["limit_up_count"],
            amount_ratio=regime_info["amount_ratio"],
            etf_surge_active=regime_info["etf_surge_active"],
            fomc_window_active=fomc_active,
            verified_cluster_count=len(flashes),
            lhb_seat_rows_count=int(lhb_cnt),
            guba_post_count=int(guba_cnt) if guba_cnt and int(guba_cnt) > 0 else None,
            fund_flow_net_cny=ff_net_cny,
        ),
        "source_polling_status": poll_wire,
        "cross_verified_flashes": [
            {
                "cluster_id": c["cluster_id"],
                "headline": c["headline"],
                "verification_state": c["verification_state"],
                "corroboration_count": c["corroboration_count"],
                "confidence_score": c["confidence_score"],
                "sources": c["sources"],
                "polarity": c["polarity"],
                "affected_sectors": c["affected_sectors"],
                "published_at": c["published_at"],
            }
            for c in flashes[:10]
        ],
        "active_realtime_adjustments": active_realtime_adjustments,
        "picks": picks_wire,
        "rapid_rising_sectors": heating_cards,
        "candidate_watch_pool": watch_pool_wire[:20],
    })


# ---------------------------------------------------------------------------------------------------------
# 4. Sector Deep Dive: Real Auto-Resolved Taxonomy, Real Ensemble Ranking & Real Surge Breakdown
# ---------------------------------------------------------------------------------------------------------
def _resolve_sector_taxonomy(
    conn: psycopg.Connection,
    *,
    release_id: UUID,
    node_id: str,
    requested_taxonomy: str | None,
    as_of: datetime,
) -> tuple[str, str, str]:
    """Return ``(resolved_taxonomy_id, resolved_node_id, node_display_name)``."""
    tax_candidates: list[str] = []
    if requested_taxonomy:
        tax_candidates.append(requested_taxonomy)

    # Check if requested taxonomy matches node_id directly
    if requested_taxonomy:
        has_run = conn.execute(
            "SELECT 1 FROM decision.ranking_run WHERE release_id=%s AND taxonomy_id=%s AND node_id=%s LIMIT 1",
            (release_id, requested_taxonomy, node_id),
        ).fetchone()
        has_tag = conn.execute(
            "SELECT 1 FROM core.instrument_tag WHERE taxonomy_id=%s AND node_id=%s AND available_at <= %s LIMIT 1",
            (requested_taxonomy, node_id, as_of),
        ).fetchone()
        if has_run or has_tag:
            tn = conn.execute(
                "SELECT display_name FROM core.taxonomy_node WHERE taxonomy_id=%s AND node_id=%s ORDER BY taxonomy_version DESC LIMIT 1",
                (requested_taxonomy, node_id),
            ).fetchone()
            return requested_taxonomy, node_id, (tn["display_name"] if tn else node_id)

    # Auto-detect real taxonomy in DB for this release / node_id
    run_tax = conn.execute(
        "SELECT taxonomy_id FROM decision.ranking_run"
        " WHERE release_id=%s AND node_id=%s AND taxonomy_id IS NOT NULL LIMIT 1",
        (release_id, node_id),
    ).fetchone()
    if run_tax:
        tax = run_tax["taxonomy_id"]
        tn = conn.execute(
            "SELECT display_name FROM core.taxonomy_node WHERE taxonomy_id=%s AND node_id=%s ORDER BY taxonomy_version DESC LIMIT 1",
            (tax, node_id),
        ).fetchone()
        return tax, node_id, (tn["display_name"] if tn else node_id)

    sec_tax = conn.execute(
        "SELECT e.taxonomy_id FROM decision.sector_rank_entry e JOIN decision.ranking_run r USING (ranking_id)"
        " WHERE r.release_id=%s AND e.node_id=%s LIMIT 1",
        (release_id, node_id),
    ).fetchone()
    if sec_tax:
        tax = sec_tax["taxonomy_id"]
        tn = conn.execute(
            "SELECT display_name FROM core.taxonomy_node WHERE taxonomy_id=%s AND node_id=%s ORDER BY taxonomy_version DESC LIMIT 1",
            (tax, node_id),
        ).fetchone()
        return tax, node_id, (tn["display_name"] if tn else node_id)

    tag_tax = conn.execute(
        "SELECT taxonomy_id FROM core.instrument_tag WHERE node_id=%s AND available_at <= %s"
        " ORDER BY available_at DESC LIMIT 1",
        (node_id, as_of),
    ).fetchone()
    if tag_tax:
        tax = tag_tax["taxonomy_id"]
        tn = conn.execute(
            "SELECT display_name FROM core.taxonomy_node WHERE taxonomy_id=%s AND node_id=%s ORDER BY taxonomy_version DESC LIMIT 1",
            (tax, node_id),
        ).fetchone()
        return tax, node_id, (tn["display_name"] if tn else node_id)

    # Try matching node_id by display_name inside core.taxonomy_node (e.g. "半导体" -> real node_id)
    by_name = conn.execute(
        "SELECT taxonomy_id, node_id, display_name FROM core.taxonomy_node"
        " WHERE display_name=%s OR node_id=%s ORDER BY CASE WHEN taxonomy_id=%s THEN 0 ELSE 1 END, taxonomy_version DESC LIMIT 1",
        (node_id, node_id, B.TAXONOMY_ID),
    ).fetchone()
    if by_name:
        return by_name["taxonomy_id"], by_name["node_id"], by_name["display_name"]

    fallback_tax = requested_taxonomy or B.TAXONOMY_ID
    any_run = conn.execute(
        "SELECT taxonomy_id FROM decision.ranking_run WHERE release_id=%s AND taxonomy_id IS NOT NULL LIMIT 1",
        (release_id,),
    ).fetchone()
    return (any_run["taxonomy_id"] if any_run else fallback_tax), node_id, node_id


def _load_explanation_body(conn: psycopg.Connection, release_id: UUID, explanation_ref: str | None) -> dict[str, Any] | None:
    if not explanation_ref:
        return None
    comp = conn.execute(
        "SELECT snapshot_id FROM decision.release_component"
        " WHERE release_id=%s AND component_key='explanations' AND component_state='READY' LIMIT 1",
        (release_id,),
    ).fetchone()
    if not comp or not comp["snapshot_id"]:
        return None
    man = conn.execute(
        "SELECT content_hash FROM market.dataset_manifest WHERE snapshot_id=%s AND manifest_state='READY' LIMIT 1",
        (comp["snapshot_id"],),
    ).fetchone()
    if not man:
        return None
    try:
        paths = _lake_parquet_paths(man["content_hash"])
        if not paths:
            return None
        df = pl.scan_parquet(paths).filter(pl.col("ref") == explanation_ref).collect()
        if df.height == 0:
            return None
        raw_body = df["body"][0]
        return json.loads(raw_body) if isinstance(raw_body, str) else raw_body
    except Exception:
        return None


def get_sector_deep_dive(
    conn: psycopg.Connection,
    *,
    node_id: str,
    taxonomy_id: str | None = None,
    horizon_id: str = "5td",
    release_id: UUID | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    rel = _resolve_release(conn, release_id)
    if rel is None:
        return None, None
    rid: UUID = rel["release_id"]
    as_of: datetime = rel["decision_as_of"]

    eff_tax, eff_node_id, node_display = _resolve_sector_taxonomy(
        conn, release_id=rid, node_id=node_id, requested_taxonomy=taxonomy_id, as_of=as_of
    )

    rising = conn.execute(
        "SELECT e.rank, e.score, e.explanation_ref FROM decision.sector_rank_entry e JOIN decision.ranking_run r USING (ranking_id)"
        " WHERE r.release_id=%s AND e.node_id=%s AND e.rank_kind='sector_rising_rank'"
        " ORDER BY CASE WHEN e.taxonomy_id=%s THEN 0 ELSE 1 END, e.rank ASC LIMIT 1",
        (rid, eff_node_id, eff_tax),
    ).fetchone()
    is_rising = bool(rising and int(rising["rank"]) <= 5)
    surge_sc = f"{float(rising['score']):.3f}" if rising else None

    # Fetch in-sector ranked entries, then supplement with PIT tagged instruments so synthesis runs on full sector
    sec_entries = conn.execute(
        "SELECT e.instrument_id, e.alpha_rank, e.relative_rank_score, p.plan_payload"
        " FROM decision.ranking_run r"
        " JOIN decision.ranking_entry e USING (ranking_id)"
        " LEFT JOIN decision.trade_plan p ON p.plan_id=e.plan_id AND p.plan_version=e.plan_version"
        " WHERE r.release_id=%s AND r.ranking_scope='sector' AND r.node_id=%s AND r.horizon_id=%s"
        " ORDER BY e.alpha_rank ASC LIMIT 40",
        (rid, eff_node_id, horizon_id),
    ).fetchall()
    if not sec_entries:
        sec_entries = conn.execute(
            "SELECT DISTINCT ON (e.instrument_id) e.instrument_id, e.alpha_rank, e.relative_rank_score, p.plan_payload"
            " FROM decision.ranking_run r"
            " JOIN decision.ranking_entry e USING (ranking_id)"
            " JOIN core.instrument_tag t ON t.instrument_id=e.instrument_id AND t.node_id=%s AND t.taxonomy_id=%s"
            " LEFT JOIN decision.trade_plan p ON p.plan_id=e.plan_id AND p.plan_version=e.plan_version"
            " WHERE r.release_id=%s AND r.ranking_scope='market'"
            " ORDER BY e.instrument_id, e.alpha_rank ASC LIMIT 40",
            (eff_node_id, eff_tax, rid),
        ).fetchall()
        sec_entries.sort(key=lambda x: int(x["alpha_rank"]))
    if not sec_entries:
        tagged_rows = conn.execute(
            "SELECT DISTINCT instrument_id FROM core.instrument_tag"
            " WHERE taxonomy_id=%s AND node_id=%s AND available_at <= %s AND (effective_to IS NULL OR effective_to > %s)"
            " LIMIT 40",
            (eff_tax, eff_node_id, as_of, as_of),
        ).fetchall()
        sec_entries = [
            {"instrument_id": tr["instrument_id"], "alpha_rank": idx, "relative_rank_score": None, "plan_payload": None}
            for idx, tr in enumerate(tagged_rows, start=1)
        ]

    uids = [r["instrument_id"] for r in sec_entries]
    slices, info_map = _build_slices_for_instruments(conn, uids, as_of, taxonomy_id=eff_tax, lookback_days=95)
    slice_by_id = {s.instrument_id: s for s in slices}
    plan_by_id = {str(r["instrument_id"]): (r["plan_payload"] or {}) for r in sec_entries}

    # Baseline rank within sector
    n_sec = max(1, len(sec_entries))
    baseline_pct = {
        str(r["instrument_id"]): (n_sec - idx) / n_sec
        for idx, r in enumerate(sorted(sec_entries, key=lambda x: (int(x["alpha_rank"]), str(x["instrument_id"]))))
    }

    regime_info = _detect_real_market_regime(conn, as_of)
    voter_weights, _ = _load_arena_voter_weights(conn, "composite")
    voter_signals = _compute_7_voter_signals(slices, baseline_pct, market_regime_code=regime_info["code"])
    ens_scores, supporters, n_voters = synthesize_orthogonal_ensemble(
        voter_signals, voter_weights, validated_voters=set(voter_signals.keys())
    )

    # Real 5d return & volume statistics across sector members
    bar_stats: dict[str, tuple[float, float, float, float | None]] = {}
    for iid_s, sl in slice_by_id.items():
        if not sl.closes:
            continue
        c_now = sl.closes[-1]
        r5 = (c_now / sl.closes[-6] - 1.0) if len(sl.closes) >= 6 and sl.closes[-6] > 0 else 0.0
        av = sum(sl.volumes[-5:]) / max(1, len(sl.volumes[-5:]))
        atr_p = _slice_atr14(sl)
        bar_stats[iid_s] = (c_now, r5, av, atr_p)

    vols_sorted = sorted(v[2] for v in bar_stats.values() if v[2] > 0)
    med_vol = vols_sorted[len(vols_sorted) // 2] if vols_sorted else 1.0

    # Rank sector picks by real orthogonal ensemble score DESC (no fake linear decay!)
    ranked_iids = sorted(
        [str(r["instrument_id"]) for r in sec_entries],
        key=lambda iid_s: (-(ens_scores.get(iid_s, 0.0) if ens_scores else baseline_pct.get(iid_s, 0.0)), iid_s),
    )

    top_picks: list[dict[str, Any]] = []
    for idx, iid_s in enumerate(ranked_iids[:12], start=1):
        inf = info_map.get(iid_s) or _load_instrument_info(conn, UUID(iid_s)) or {"symbol": iid_s[:8], "display_name": iid_s[:8], "is_risk_warning": False}
        st = bar_stats.get(iid_s)
        plan_p = plan_by_id.get(iid_s, {})
        zone = plan_p.get("entry_price_zone") or {}
        if st:
            c_now, r5, av, atr_p = st
            atr_eff = atr_p or (c_now * 0.025)
            role = _classify_sector_role(idx, r5, av, med_vol)
            e_lo = zone.get("lower_price_raw") or f"{max(0.01, c_now - 0.5 * atr_eff):.2f}"
            e_hi = zone.get("upper_price_raw") or f"{c_now + 0.15 * atr_eff:.2f}"
            stop = f"{max(0.01, c_now - 1.8 * atr_eff):.2f}"
            tp = f"{c_now + 2.5 * atr_eff:.2f}"
        else:
            role = "member"
            e_lo = zone.get("lower_price_raw")
            e_hi = zone.get("upper_price_raw")
            stop = tp = None

        sc_pct = ens_scores.get(iid_s, baseline_pct.get(iid_s, 0.0)) * 100.0
        sup = supporters.get(iid_s, ["fusion_static40_safe"])
        eff_n_voters = max(n_voters, len(sup), 1)
        role_cn = {
            "leader_1": "板块龙一",
            "leader_2": "板块龙二",
            "core_capacity": "容量中军",
            "pioneer": "异动先锋",
            "catchup": "低位补涨",
            "member": "核心成分",
        }[role]
        sl = slice_by_id.get(iid_s)
        r_flags = ["ST风险警示"] if inf.get("is_risk_warning") else []
        if sl and sl.lhasa_flag:
            r_flags.append("拉萨散户接盘预警")
        if sl and sl.one_day_raider_flag:
            r_flags.append("一日游砸盘席位警示")

        top_picks.append({
            "rank": idx,
            "instrument_id": iid_s,
            "symbol": inf["symbol"],
            "display_name": inf["display_name"],
            "sector_node": eff_node_id,
            "sector_role": role,
            "ensemble_score": f"{sc_pct:.2f}",
            "voter_support_count": len(sup),
            "total_active_voters": eff_n_voters,
            "entry_low": str(e_lo) if e_lo is not None else None,
            "entry_high": str(e_hi) if e_hi is not None else None,
            "stop_loss": stop,
            "target_price": tp,
            "holding_horizon": f"{horizon_id}（板块{role_cn}策略）",
            "tags": [role_cn, node_display],
            "supporting_strategies": sup[:4],
            "risk_flags": r_flags,
            "catalyst_summary": f"{node_display}板块第{idx}顺位（身位：{role_cn}），由 {', '.join(sup[:2])} 等 {len(sup)}/{eff_n_voters} 算子合成",
        })

    # Watch pool members in this sector
    pool_rows = conn.execute(
        "SELECT DISTINCT ON (instrument_id) instrument_id, current_state, trigger_code, plan_id"
        " FROM decision.candidate_pool_event WHERE release_id=%s AND node_id=%s"
        " ORDER BY instrument_id, event_sequence DESC",
        (rid, eff_node_id),
    ).fetchall()
    watch_members = []
    for pr in pool_rows:
        inf = _load_instrument_info(conn, pr["instrument_id"]) or {"symbol": str(pr["instrument_id"])[:8], "display_name": str(pr["instrument_id"])[:8]}
        watch_members.append({
            "instrument_id": str(pr["instrument_id"]),
            "symbol": inf["symbol"],
            "display_name": inf["display_name"],
            "pool_state": pr["current_state"],
            "role": "actionable_leader" if pr["current_state"] == "actionable" else "early_watch",
            "trigger_code": pr["trigger_code"],
            "plan_ready": pr["plan_id"] is not None,
        })

    # Real sector news from cross_source_cluster + document_revision matching node_id or node_display
    intel_cutoff = max(as_of, datetime.now(timezone.utc)) if release_id is None else as_of
    cl_sec = conn.execute(
        "SELECT headline FROM intelligence.cross_source_cluster"
        " WHERE available_at <= %s"
        "   AND (sector_nodes_json ? %s OR sector_nodes_json ? %s OR headline LIKE %s OR headline LIKE %s)"
        " ORDER BY available_at DESC LIMIT 8",
        (intel_cutoff, eff_node_id, node_display, f"%{eff_node_id}%", f"%{node_display}%"),
    ).fetchall()
    sec_news: list[str] = [r["headline"] for r in cl_sec if r["headline"]]
    if len(sec_news) < 6:
        docs = _load_recent_docs(conn, intel_cutoff, limit=60)
        for d in docs:
            t = d["title"]
            if t and t not in sec_news and (
                eff_node_id in t
                or node_display in t
                or eff_node_id in d.get("sector_nodes", [])
                or node_display in d.get("sector_nodes", [])
            ):
                sec_news.append(t)
                if len(sec_news) >= 6:
                    break

    # Real surge_components from release explanations artifact + OHLCV + DB intel/guba/fundflow
    expl_body = _load_explanation_body(conn, rid, rising["explanation_ref"] if rising else None)
    expl_raw: dict[str, Any] = {}
    if isinstance(expl_body, dict):
        expl_raw = expl_body.get("contributions") or expl_body.get("raw") or expl_body.get("components") or {}

    comp_vals: dict[str, float | None] = {k: None for k, _ in _SURGE_COMPONENT_LABELS}
    for k, _ in _SURGE_COMPONENT_LABELS:
        if k in expl_raw and expl_raw[k] is not None:
            try:
                comp_vals[k] = float(expl_raw[k])
            except (TypeError, ValueError):
                pass

    # Compute genuine PIT OHLCV sector sub-metrics if explanation body didn't populate them
    if bar_stats:
        r5_list = [v[1] for v in bar_stats.values()]
        if comp_vals["relative_strength_change"] is None:
            comp_vals["relative_strength_change"] = round(sum(r5_list) / len(r5_list) * 100.0, 2)
        if comp_vals["breadth"] is None:
            comp_vals["breadth"] = round(sum(1 for r in r5_list if r > 0) / len(r5_list) * 100.0, 1)
        if comp_vals["leader_confirmation"] is None:
            comp_vals["leader_confirmation"] = round(max(r5_list) * 100.0, 2)
        if comp_vals["turnover_acceleration"] is None:
            acc_vals: list[float] = []
            for sl in slice_by_id.values():
                if len(sl.volumes) >= 10:
                    v5 = sum(sl.volumes[-5:]) / 5.0
                    v20 = sum(sl.volumes[-20:]) / min(20, len(sl.volumes))
                    if v20 > 0:
                        acc_vals.append(v5 / v20)
            if acc_vals:
                acc_vals.sort()
                comp_vals["turnover_acceleration"] = round(acc_vals[len(acc_vals) // 2], 3)

    if comp_vals["news_novelty"] is None and sec_news:
        comp_vals["news_novelty"] = float(len(sec_news))

    if uids:
        sec_guba_cnt = conn.execute(
            "SELECT count(*) AS n FROM intelligence.community_observation"
            " WHERE available_at BETWEEN %s AND %s AND analysis_payload->>'instrument_id' = ANY(%s)",
            (as_of - timedelta(days=7), as_of, [str(u) for u in uids]),
        ).fetchone()["n"]
        if comp_vals["community_growth"] is None and sec_guba_cnt and int(sec_guba_cnt) > 0:
            comp_vals["community_growth"] = float(sec_guba_cnt)

        sec_ff = conn.execute(
            "WITH latest AS ("
            "   SELECT DISTINCT ON (instrument_id) instrument_id, canonical_value"
            "   FROM intelligence.funding_observation"
            "   WHERE instrument_id = ANY(%s) AND metric_name='main_net_inflow'"
            "     AND available_at <= %s AND observation_window_end >= %s"
            "   ORDER BY instrument_id, observation_window_end DESC, revision DESC"
            ")"
            " SELECT SUM(canonical_value)::float8 AS net_cny, COUNT(*) AS cnt FROM latest",
            (uids, as_of, as_of - timedelta(days=7)),
        ).fetchone()
        if comp_vals["fund_proxy"] is None and sec_ff and sec_ff["cnt"] and sec_ff["net_cny"] is not None:
            comp_vals["fund_proxy"] = round(float(sec_ff["net_cny"]) / 1e8, 3)

    surge_comps = [
        {
            "key": k,
            "label": lbl,
            "value": f"{comp_vals[k]:.3f}" if comp_vals[k] is not None else None,
            "status": "ready" if comp_vals[k] is not None else "missing",
        }
        for k, lbl in _SURGE_COMPONENT_LABELS
    ]

    return contracts.to_wire({
        "release_id": str(rid),
        "taxonomy_id": eff_tax,
        "node_id": eff_node_id,
        "horizon_id": horizon_id,
        "is_rapidly_rising": is_rising,
        "surge_score": surge_sc,
        "surge_components": surge_comps,
        "top_picks": top_picks,
        "watch_pool_members": watch_members,
        "sector_news": sec_news,
    }), rel


# ---------------------------------------------------------------------------------------------------------
# 5. Strategy Taxonomy Router & Swiss-Glicko2 Arena Overview (Read-Only + In-Memory Fallback)
# ---------------------------------------------------------------------------------------------------------
def get_arena_overview(
    conn: psycopg.Connection,
    *,
    layer: str | None = None,
    horizon_bucket: str | None = None,
    paradigm: str | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    season_row = conn.execute(
        "SELECT season_id, rounds_completed, evaluation_basis, sessions_evaluated, pairings_json"
        " FROM research.swiss_arena_season"
        " ORDER BY CASE WHEN season_id=%s THEN 0 ELSE 1 END, evaluated_at DESC LIMIT 1",
        (DEFAULT_SEASON_ID,),
    ).fetchone()

    rows = []
    if season_row:
        rows = conn.execute(
            "SELECT c.*, r.glicko_mu, r.glicko_phi, r.glicko_sigma, r.conservative_score,"
            " r.swiss_wins, r.swiss_draws, r.swiss_losses, r.net_return_pct, r.excess_return_pct,"
            " r.sharpe_ratio, r.max_drawdown_pct, r.nw_t_stat, r.worst_cvar_pct,"
            " r.rre_stability, r.pfs_robustness, r.evaluation_source, r.is_cluster_champion,"
            " r.lifecycle_tier, r.ensemble_weight, r.ram_floor_weight"
            " FROM strategy.taxonomy_catalog c"
            " JOIN research.swiss_arena_rating r ON r.strategy_code=c.strategy_code AND r.season_id=%s"
            " ORDER BY r.conservative_score DESC, c.strategy_code ASC",
            (season_row["season_id"],),
        ).fetchall()

    if not rows:
        # Pure read-only fallback when DB is unseeded (zero writes, safe for pulse_api!)
        cards = build_full_taxonomy_catalog()
        season = run_swiss_glicko_season(cards, rounds=5, season_id=DEFAULT_SEASON_ID)
        all_entries = [
            {
                **st,
                "evaluation_source": "in_memory_prior_unseeded",
            }
            for st in season["strategies"]
        ]
        eff_season_id = season["season_id"]
        eff_basis = "in_memory_prior_unseeded"
        eff_sessions = int(season["sessions_evaluated"])
        eff_rounds = int(season["rounds_completed"])
        recent_pairings = season["recent_pairings"]
    else:
        all_entries = [
            {
                "strategy_code": r["strategy_code"],
                "display_name": r["display_name"],
                "layer": r["layer"],
                "horizon_bucket": r["horizon_bucket"],
                "paradigm": r["paradigm"],
                "regime_fit": list(r["regime_fit_json"] or []),
                "portfolio_role": r["portfolio_role"],
                "cluster_id": r["cluster_id"],
                "is_cluster_champion": bool(r["is_cluster_champion"]),
                "glicko_mu": _fmt_dec(r["glicko_mu"], 2) or "1500.00",
                "glicko_phi": _fmt_dec(r["glicko_phi"], 2) or "80.00",
                "glicko_sigma": _fmt_dec(r.get("glicko_sigma"), 5) or "0.06000",
                "conservative_score": _fmt_dec(r["conservative_score"], 2) or "1340.00",
                "swiss_wins": int(r.get("swiss_wins") or 0),
                "swiss_draws": int(r.get("swiss_draws") or 0),
                "swiss_losses": int(r.get("swiss_losses") or 0),
                "net_return_pct": _fmt_dec(r.get("net_return_pct"), 4),
                "excess_return_pct": _fmt_dec(r.get("excess_return_pct"), 4),
                "sharpe_ratio": _fmt_dec(r.get("sharpe_ratio"), 4),
                "max_drawdown_pct": _fmt_dec(r.get("max_drawdown_pct"), 4),
                "nw_t_stat": _fmt_dec(r.get("nw_t_stat"), 4),
                "worst_cvar_pct": _fmt_dec(r.get("worst_cvar_pct"), 4),
                "rre_stability": _fmt_dec(r.get("rre_stability"), 3),
                "pfs_robustness": _fmt_dec(r.get("pfs_robustness"), 3),
                "evaluation_source": str(r.get("evaluation_source") or "ledger_calibrated_prior"),
                "lifecycle_tier": r["lifecycle_tier"],
                "ensemble_weight": _fmt_dec(r["ensemble_weight"], 4) or "0.0000",
                "ram_floor_weight": _fmt_dec(r.get("ram_floor_weight"), 4) or "0.0000",
                "paper_citation": r["paper_citation"],
                "summary": r["summary"],
            }
            for r in rows
        ]
        eff_season_id = season_row["season_id"]
        eff_basis = str(season_row.get("evaluation_basis") or "empirical_backtest_gips_v1")
        eff_sessions = int(season_row.get("sessions_evaluated") or 0)
        eff_rounds = int(season_row["rounds_completed"])
        recent_pairings = list(season_row["pairings_json"] or [])

    all_clusters = {r["cluster_id"] for r in all_entries}
    filtered = [
        r
        for r in all_entries
        if (not layer or r["layer"] == layer)
        and (not horizon_bucket or r["horizon_bucket"] == horizon_bucket)
        and (not paradigm or r["paradigm"] == paradigm)
        and (not role or r["portfolio_role"] == role)
    ]

    disc_rows = conn.execute(
        "SELECT candidate_id, parent_code, dag_depth, display_name, max_corr_existing,"
        " rre_stability, pfs_robustness, net_sharpe, nw_t_stat, gate_verdict"
        " FROM research.strategy_discovery_candidate"
        " ORDER BY CASE WHEN gate_verdict='admitted_shadow' THEN 0 ELSE 1 END, net_sharpe DESC NULLS LAST, candidate_id ASC LIMIT 30"
    ).fetchall()
    discovered_candidates = [
        {
            "candidate_id": d["candidate_id"],
            "parent_code": d["parent_code"],
            "dag_depth": int(d["dag_depth"]),
            "display_name": d["display_name"],
            "max_corr_existing": _fmt_dec(d["max_corr_existing"], 4),
            "rre_stability": _fmt_dec(d["rre_stability"], 4),
            "pfs_robustness": _fmt_dec(d["pfs_robustness"], 4),
            "net_sharpe": _fmt_dec(d["net_sharpe"], 4),
            "nw_t_stat": _fmt_dec(d["nw_t_stat"], 4),
            "gate_verdict": d["gate_verdict"],
        }
        for d in disc_rows
    ]

    return contracts.to_wire({
        "benchmark_standard": DEFAULT_STANDARD.wire_spec(),
        "season_id": eff_season_id,
        "evaluation_basis": eff_basis,
        "sessions_evaluated": eff_sessions,
        "rounds_completed": eff_rounds,
        "total_strategies": len(filtered),
        "orthogonal_clusters": len(all_clusters),
        "taxonomy_facets": {
            "layers": ["stock_selection", "sector_rotation", "regime_timing", "intraday_execution"],
            "horizons": ["intraday_t0", "ultra_short_1_3d", "short_5d", "swing_10_20d", "position_60d"],
            "paradigms": [
                "top_paper_2026", "multi_factor_fusion", "classic_factor",
                "technical_pattern", "sentiment_microstructure", "hot_money_seat",
                "grid_intraday_t", "rl_distributional", "llm_hybrid",
            ],
            "roles": ["core_alpha", "defensive_shield", "risk_veto", "cost_reducer", "exploratory_shadow"],
        },
        "strategies": filtered,
        "recent_pairings": recent_pairings,
        "top_papers_2026": paper_specs_wire(),
        "discovered_candidates": discovered_candidates,
    })


# ---------------------------------------------------------------------------------------------------------
# 6. Cross-Module Integrity, Dynamic Producer Verification & Database Health Audit
# ---------------------------------------------------------------------------------------------------------
def _verify_outbox_producer_wired(mod_path: str, symbol: str, method: str | None) -> bool:
    try:
        mod = importlib.import_module(mod_path)
        obj = getattr(mod, symbol, None)
        if obj is None:
            return False
        return hasattr(obj, method) if method else callable(obj)
    except Exception:
        return False


def get_integrity_audit(conn: psycopg.Connection) -> dict[str, Any]:
    outbox_stats = {
        r["event_type"]: (int(r["n"]), r["last_at"])
        for r in conn.execute(
            "SELECT event_type, count(*) AS n, max(recorded_at) AS last_at FROM ops.outbox GROUP BY event_type"
        ).fetchall()
    }
    channels = []
    for ev_type, mod_path, sym, method in _OUTBOX_PRODUCER_SPECS:
        cnt, last_at = outbox_stats.get(ev_type, (0, None))
        channels.append({
            "event_type": ev_type,
            "producer_wired": _verify_outbox_producer_wired(mod_path, sym, method),
            "total_emitted": cnt,
            "last_emitted_at": contracts.utc_z(last_at),
        })

    req_tables = (
        "ingest.source_field_mapping",
        "intelligence.cross_source_cluster",
        "intelligence.realtime_signal_adjustment",
        "intelligence.session_sentiment_pulse",
        "intelligence.seat_battle_profile",
        "strategy.taxonomy_catalog",
        "research.swiss_arena_season",
        "research.swiss_arena_rating",
        "research.strategy_discovery_candidate",
        "decision.stock_workbench_cache",
    )
    tbl_chk = conn.execute(
        "SELECT COUNT(*) AS n FROM unnest(%s::text[]) AS t(name) WHERE to_regclass(t.name) IS NOT NULL",
        (list(req_tables),),
    ).fetchone()["n"]
    tables_ready = int(tbl_chk) == len(req_tables)

    wb_cnt = conn.execute("SELECT count(*) AS n FROM decision.stock_workbench_cache").fetchone()["n"]
    cl_cnt = conn.execute("SELECT count(*) AS n FROM intelligence.cross_source_cluster").fetchone()["n"]
    rt_cnt = conn.execute("SELECT count(*) AS n FROM intelligence.realtime_signal_adjustment").fetchone()["n"]
    ar_cnt = conn.execute("SELECT count(*) AS n FROM research.swiss_arena_rating").fetchone()["n"]
    dc_cnt = conn.execute("SELECT count(*) AS n FROM research.strategy_discovery_candidate").fetchone()["n"]
    idx_cnt = conn.execute(
        "SELECT count(*) AS n FROM pg_indexes WHERE indexname IN"
        " ('community_obs_avail_idx','community_obs_payload_gin','ext_artifact_contract_idx',"
        "  'ranking_run_release_scope_idx','bar_proj_snapshot_inst_idx')"
    ).fetchone()["n"]

    norm_rows = [
        {
            "source_id": m.source_id,
            "domain": m.domain_kind,
            "raw_fields": ", ".join(m.raw_fields),
            "canonical_fields": ", ".join(m.canonical_fields),
            "missing_value_policy": m.missing_policy,
            "unit_guard": m.unit_guard_policy,
        }
        for m in CANONICAL_MATRIX
    ]

    ep_audit = audit_entrypoints()
    all_producers_ok = all(c["producer_wired"] for c in channels)
    overall = (
        "healthy"
        if tables_ready and int(idx_cnt) >= 5 and int(ar_cnt) >= 60 and all_producers_ok
        else ("degraded" if not tables_ready else "warnings_present")
    )

    return contracts.to_wire({
        "audit_version": "nexus-integrity-2.0.0",
        "overall_status": overall,
        "benchmark_standard": DEFAULT_STANDARD.wire_spec(),
        "entrypoint_accounting_parity": ep_audit,
        "source_normalization_matrix": norm_rows,
        "outbox_event_channels": channels,
        "database_health": {
            "nexus_tables_ready": tables_ready,
            "workbench_cache_rows": int(wb_cnt),
            "cross_verified_clusters": int(cl_cnt),
            "realtime_adjustments_rows": int(rt_cnt),
            "arena_ratings_rows": int(ar_cnt),
            "discovered_candidates_rows": int(dc_cnt),
            "performance_indexes_ready": int(idx_cnt) >= 5,
        },
    })
