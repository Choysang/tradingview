"""Comprehensive unit + live PostgreSQL 18 integration tests for Pulse 3.5 Nexus (v2.0.0)."""
from __future__ import annotations

import json
import math
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
import polars as pl
import psycopg
import pytest
from fastapi.testclient import TestClient

from pulse import contracts
from pulse.adapters.lake import LakeStore
from pulse.adapters.sources import xinhua_news
from pulse.api.app import create_app
from pulse.api.deps import conn_dep
from pulse.application import bootstrap, nexus_service
from pulse.domain.arena import (
    DailyStockBarSlice,
    GlickoRating,
    build_full_taxonomy_catalog,
    compute_amihud_illiquidity,
    compute_ram_portfolio_weights,
    compute_raptor_bl_tilt,
    discover_alphaprobe_candidates,
    glicko2_update,
    run_swiss_glicko_season,
    score_aaai2024_sector_hypergraph,
    score_icml2025_alphaqcm_tail,
    score_ijcai2026_prism_vq_moe,
    score_jf2026_margin_lottery_shield,
    score_jfqa2025_micro_tug_of_war,
    score_kdd2026_ebm_interpretable,
    score_ms2025_lot_momentum,
    synthesize_orthogonal_ensemble,
)
from pulse.domain.benchmark import (
    PanelBar,
    UnifiedBacktestEngine,
    audit_entrypoints,
    compute_order_cost_cny,
    compute_perturbation_fidelity,
    compute_relative_rank_entropy,
    metatrader_worst_case_cvar,
    moving_block_bootstrap_t,
    price_limit_ratio,
    verify_strategy_multi_gate,
)
from pulse.domain.execution.grid_and_t import build_adaptive_grid_plan, build_intraday_t_plan
from pulse.domain.intelligence.guba_four_session import analyze_four_session_resonance
from pulse.domain.intelligence.macro_cross_verify import (
    classify_headline_asymmetric,
    cross_verify_documents,
)
from pulse.domain.intelligence.realtime_adjuster import build_realtime_news_strategy_adjustment
from pulse.domain.intelligence.seat_personas import analyze_lhb_battle, match_seat_persona
from pulse.domain.intelligence.source_normalizer import CANONICAL_MATRIX, normalize_record
from pulse.domain.market_state import outcomes
from pulse.domain.market_state.daily import Bar as OutcomeBar
from pulse.domain.technical.tv_pro import (
    SimpleBar,
    compute_all_pivot_points,
    compute_pro_signals_and_markers,
    compute_tv_technical_rating,
    compute_vpvr_profile,
    resample_bars,
)
from pulse.workers import nexus as nexus_worker


def _sample_bars(
    n: int = 240,
    start_price: float = 20.0,
    volume: float = 1_500_000.0,
    start_date: date | None = None,
) -> list[SimpleBar]:
    out: list[SimpleBar] = []
    d0 = start_date or date(2025, 6, 2)
    px = start_price
    for i in range(n):
        cur_d = d0 + timedelta(days=i)
        if cur_d.weekday() >= 5:
            continue
        wave = 0.012 * math.sin(i / 6.0) + 0.0015
        o = px * (1.0 - 0.003)
        c = max(2.5, px * (1.0 + wave))
        h = max(o, c) * 1.015
        l = min(o, c) * 0.985
        v = volume * (2.1 if i == n - 1 else 1.0)
        out.append(SimpleBar(d=cur_d, o=o, h=h, l=l, c=c, v=v))
        px = c
    return out


def _multi_stock_panel(n_stocks: int = 8, n_days: int = 90) -> tuple[dict[str, list[PanelBar]], dict[date, list[DailyStockBarSlice]]]:
    base_bars = _sample_bars(n_days, start_price=15.0, volume=3_500_000.0)
    panel: dict[str, list[PanelBar]] = {}
    by_day: dict[date, list[DailyStockBarSlice]] = {}

    for k in range(1, n_stocks + 1):
        iid = f"inst-{k:02d}"
        sec = "半导体" if k <= 4 else "银行"
        bars_k: list[PanelBar] = []
        os_h, hs_h, ls_h, cs_h, vs_h = [], [], [], [], []
        for idx, b in enumerate(base_bars):
            drift = (1.0 + 0.0012 * ((k % 5) - 1) * math.cos(idx / 7.0))
            o = round(b.o * (1 + 0.08 * k) * drift, 2)
            c = round(b.c * (1 + 0.08 * k) * drift * (1 + 0.0008 * k), 2)
            h = round(max(o, c) * 1.018, 2)
            l = round(min(o, c) * 0.982, 2)
            v = int(b.v * (1.0 + 0.1 * k))
            pb = PanelBar(
                date=b.d,
                open=o,
                high=h,
                low=l,
                close=c,
                volume=v,
                board_code="main",
                sector_node=sec,
            )
            bars_k.append(pb)
            os_h.append(o)
            hs_h.append(h)
            ls_h.append(l)
            cs_h.append(c)
            vs_h.append(float(v))
            by_day.setdefault(b.d, []).append(
                DailyStockBarSlice(
                    instrument_id=iid,
                    symbol=f"60010{k}",
                    sector_node=sec,
                    opens=tuple(os_h),
                    highs=tuple(hs_h),
                    lows=tuple(ls_h),
                    closes=tuple(cs_h),
                    volumes=tuple(vs_h),
                )
            )
        panel[iid] = bars_k
    return panel, by_day


# ---------------------------------------------------------------------------------------------------------
# 1. UnifiedBacktestEngine, Limit-Queue 0/30/100%, Penny Filter, 5-Gate & Live Entrypoint Audit
# ---------------------------------------------------------------------------------------------------------
def test_unified_backtest_engine_limits_and_entrypoint_parity():
    b_fee, b_slip = compute_order_cost_cny("buy", Decimal("8000"), order_shares=400, bar_volume_shares=1_000_000)
    s_fee, s_slip = compute_order_cost_cny("sell", Decimal("8000"), order_shares=400, bar_volume_shares=1_000_000)
    assert b_fee >= Decimal("5.00") and s_fee > b_fee and b_slip > 0 and s_slip > 0

    # 2026-07-06 ST limit reform check
    assert price_limit_ratio("main", date(2026, 6, 30), is_risk_warning=True) == Decimal("0.05")
    assert price_limit_ratio("main", date(2026, 7, 6), is_risk_warning=True) == Decimal("0.10")
    assert price_limit_ratio("chinext", date(2026, 8, 1)) == Decimal("0.20")
    assert price_limit_ratio("bse", date(2026, 8, 1)) == Decimal("0.30")

    # Board-aware Outcomes
    bars_18pct = [
        OutcomeBar(date(2026, 10, 12), Decimal("10"), Decimal("10.5"), Decimal("9.9"), Decimal("10"), 10000),
        OutcomeBar(date(2026, 10, 13), Decimal("10.2"), Decimal("11.9"), Decimal("10.1"), Decimal("11.8"), 20000),
    ]
    assert outcomes.path_return("i1", bars_18pct, date(2026, 10, 12), date(2026, 10, 13), board_code="main").reason == "UNVERIFIABLE_PRICE_PATH"
    res_cyb = outcomes.path_return("i1", bars_18pct, date(2026, 10, 12), date(2026, 10, 13), board_code="chinext")
    assert res_cyb.reason is None and res_cyb.gross == Decimal("0.18")

    # One-word limit-up = 0% fill & <¥2 penny filter
    d0, d1, d2 = date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14)
    panel_limit = {
        "one_word": [
            PanelBar(d0, 10.0, 10.2, 9.8, 10.0, 1_000_000),
            PanelBar(d1, 11.0, 11.0, 11.0, 11.0, 500_000),  # one-word limit-up -> 0% fill
            PanelBar(d2, 11.2, 11.5, 11.0, 11.3, 1_000_000),
        ],
        "penny": [
            PanelBar(d0, 1.80, 1.85, 1.75, 1.80, 1_000_000),
            PanelBar(d1, 1.82, 1.90, 1.80, 1.88, 1_000_000),
            PanelBar(d2, 1.90, 1.95, 1.88, 1.92, 1_000_000),
        ],
    }
    rep_lim = UnifiedBacktestEngine().run(panel_limit, {d0: {"one_word": 1.0, "penny": 0.9}}, hold_sessions=2, top_k=2)
    assert rep_lim.trade_count == 0
    assert rep_lim.diagnostics["skipped_one_word_limit_up"] >= 1
    assert rep_lim.diagnostics["skipped_penny_stock"] >= 1

    # Full panel UnifiedBacktestEngine: 3-tier queue sensitivity, 3-leg attribution, 2x stress, 5-Gate
    panel, by_day = _multi_stock_panel(8, 85)
    sigs = {d: sc for d in sorted(by_day) if (sc := score_jfqa2025_micro_tug_of_war(by_day[d]))}
    rep = UnifiedBacktestEngine().run(panel, sigs, hold_sessions=5, top_k=4)
    assert rep.sessions_evaluated > 30 and rep.trade_count > 0
    assert set(rep.queue_sensitivity.keys()) == {"p0", "p30", "p100"}
    assert set(rep.three_leg_attribution.keys()) == {"d0_intraday", "overnight_gap", "d1_to_exit"}
    assert isinstance(rep.stress_2x_net_return_pct, float)

    ver = verify_strategy_multi_gate(panel, signal_fn=score_kdd2026_ebm_interpretable, existing_signals={"tow": sigs}, hold_sessions=5, top_k=4)
    assert ver.gate1_alphaeval["pfs"] is not None
    assert ver.gate1_alphaeval["rre"] is not None

    # Dynamic live entrypoint parity audit
    entries = audit_entrypoints()
    assert len(entries) == 7
    assert entries[0]["uses_unified_standard"] is True
    assert entries[1]["uses_unified_standard"] is True
    assert entries[3]["uses_unified_standard"] is False  # Frozen Fusion E012/E036 preserved


# ---------------------------------------------------------------------------------------------------------
# 2. Historical Panel Swiss-Glicko2, AlphaSAGE, RAM, AlphaPROBE & All-Voter Redline
# ---------------------------------------------------------------------------------------------------------
def test_historical_swiss_arena_ram_alphaprobe_and_voter_redline():
    bars = _sample_bars(80)
    slices = [
        DailyStockBarSlice(
            instrument_id=f"inst-{k}",
            symbol=f"60000{k}",
            sector_node="半导体" if k < 4 else "银行",
            opens=tuple(b.o * (1 + 0.05 * k) * (1 + 0.001 * k * idx) for idx, b in enumerate(bars)),
            highs=tuple(b.h * (1 + 0.05 * k) * (1 + 0.001 * k * idx) for idx, b in enumerate(bars)),
            lows=tuple(b.l * (1 + 0.05 * k) * (1 + 0.001 * k * idx) for idx, b in enumerate(bars)),
            closes=tuple(b.c * (1 + 0.05 * k) * (1 + 0.001 * k * idx) for idx, b in enumerate(bars)),
            volumes=tuple(b.v for b in bars),
            abnormal_guba_z=3.1 if k == 1 else 0.2,
            lhasa_flag=(k == 1),
        )
        for k in range(1, 8)
    ]
    assert len(score_ms2025_lot_momentum(slices)) == 7
    tow = score_jfqa2025_micro_tug_of_war(slices)
    assert tow["inst-1"] < tow["inst-2"]
    assert len(score_aaai2024_sector_hypergraph(slices)) == 7
    assert len(score_jf2026_margin_lottery_shield(slices)) == 7
    assert len(score_icml2025_alphaqcm_tail(slices)) == 7
    assert len(score_kdd2026_ebm_interpretable(slices)) == 7
    assert len(score_ijcai2026_prism_vq_moe(slices, market_regime="bull_mainline")) == 7
    assert compute_amihud_illiquidity(slices[0]) is not None

    # All-voter normalization redline + bounded RAPTOR tilt
    signals = {"strong": {"strong_pick": 1.0}, "weak": {"only_weak": 1.0}, "unvalidated_aux": {"aux_only": 1.0}}
    weights = {"strong": 0.80, "weak": 0.20, "unvalidated_aux": 0.10}
    ens, _, n_v = synthesize_orthogonal_ensemble(signals, weights, validated_voters={"strong", "weak"})
    assert n_v == 3
    assert math.isclose(ens["only_weak"], 0.20 / 1.10, rel_tol=1e-6)
    assert "aux_only" not in ens

    tilted = compute_raptor_bl_tilt({"a": 0.50, "b": 0.50}, {"a": 1.0, "b": -1.0}, max_tilt=0.15)
    assert abs(tilted["a"] - 0.50) <= 0.15 and abs(tilted["b"] - 0.50) <= 0.15

    # RAM floor guarantees >= eps/N floor even on degenerate learned weights
    ram_w = compute_ram_portfolio_weights({"a": 1.0, "b": 0.0, "c": 0.0}, min_uniform_floor=0.15)
    assert math.isclose(ram_w["b"], 0.05, rel_tol=1e-6)

    # Empirical Swiss-Glicko2 over historical panel + AlphaPROBE candidate discovery
    panel, by_day = _multi_stock_panel(8, 80)
    sig_hist = {
        "paper_ms2025_lot_momentum": {d: sc for d in sorted(by_day) if (sc := score_ms2025_lot_momentum(by_day[d]))},
        "paper_jfqa2025_micro_tug_of_war": {d: sc for d in sorted(by_day) if (sc := score_jfqa2025_micro_tug_of_war(by_day[d]))},
        "paper_kdd2026_alphaeval_rre": {d: sc for d in sorted(by_day) if (sc := score_kdd2026_ebm_interpretable(by_day[d]))},
    }
    cards = build_full_taxonomy_catalog()
    assert len(cards) == 62
    season = run_swiss_glicko_season(cards, rounds=4, panel=panel, signal_history=sig_hist)
    assert season["evaluation_basis"] == "empirical_backtest_gips_v1"
    assert season["sessions_evaluated"] >= 50
    by_code = {s["strategy_code"]: s for s in season["strategies"]}
    assert by_code["paper_ms2025_lot_momentum"]["evaluation_source"] == "unified_backtest_engine"
    assert by_code["pb:first_board_relay"]["evaluation_source"] == "ledger_calibrated_prior"
    assert sum(float(s["ensemble_weight"]) for s in season["strategies"]) == pytest.approx(1.0, abs=0.02)

    disc = discover_alphaprobe_candidates(by_day, sig_hist, max_candidates=3)
    assert len(disc) == 3 and all("gate_verdict" in c for c in disc)


# ---------------------------------------------------------------------------------------------------------
# 3. TV Pro Wilder ADX, 24-Bin VPVR, Zero-Lookahead Intraday-T & High-Price Grid Overdraft Guard
# ---------------------------------------------------------------------------------------------------------
def test_tv_pro_vpvr_intraday_t_and_high_price_grid_guard():
    daily = _sample_bars(320, start_price=25.0)
    for tf in ("1d", "1w", "1mo", "1y"):
        agg = resample_bars(daily, tf)
        rt = compute_tv_technical_rating(agg, tf)
        assert rt["timeframe"] == tf and rt["bar_count"] > 0

    pivots = compute_all_pivot_points(daily)
    assert {p["method"] for p in pivots} == {"traditional", "classic", "fibonacci", "camarilla", "woodie", "demark"}

    vpvr = compute_vpvr_profile(daily)
    assert vpvr["available"] is True and len(vpvr["bins"]) == 24
    assert float(vpvr["val"]) <= float(vpvr["poc"]) <= float(vpvr["vah"])

    sig, _ = compute_pro_signals_and_markers(daily)
    assert sig["supertrend_dir"] in ("bullish", "bearish")
    assert sig["adx_14"] is not None and 0.0 <= float(sig["adx_14"]) <= 100.0

    t_zero = build_intraday_t_plan(daily, base_sellable_shares=0, available_cash_cny=50000.0)
    assert t_zero["recommended_lots"] == 0 and t_zero["t1_lock_verified"] is True

    # High-price stock (e.g. ¥1500/share -> 1 lot = ¥150,000) with ¥100,000 budget MUST NOT overdraft!
    high_px = _sample_bars(100, start_price=1500.0)
    grid_hi = build_adaptive_grid_plan(high_px, mode="atr_adaptive", grid_count=6, total_budget_cny=100000.0)
    assert grid_hi["mode"] == "budget_insufficient"
    assert float(grid_hi["backtest"]["max_capital_usage_pct"]) <= 100.0

    grid_ok = build_adaptive_grid_plan(daily, mode="atr_adaptive", grid_count=6, total_budget_cny=100000.0)
    assert grid_ok["mode"] == "atr_adaptive" and float(grid_ok["backtest"]["max_capital_usage_pct"]) <= 100.0


# ---------------------------------------------------------------------------------------------------------
# 4. 11-Source Normalizer, 17 Seat Personas, 4-Session Guba & Controlled gpt-6-luna Guard
# ---------------------------------------------------------------------------------------------------------
def test_11_sources_normalizer_seats_guba_and_realtime_llm_guard():
    assert len(CANONICAL_MATRIX) == 11
    # Sentinel "0.000" -> None (never coerced to 0), Tencent lots*100, Eastmoney LHB CNY_10000 unit convert
    n_sina = normalize_record("sina", {"day": "2026-11-26", "open": "0.000", "high": "10.5", "low": "10.0", "close": "10.2", "volume": "100000", "amount": "--"})
    assert n_sina["quality_status"] == "partial" and n_sina["canonical"]["open_price_raw"] is None
    n_tx = normalize_record("tencent", {"date": "2026-11-26", "open": "10.0", "high": "10.5", "low": "9.9", "close": "10.3", "volume_lots": "500"})
    assert n_tx["canonical"]["volume_shares"] == 50000
    n_lhb = normalize_record("eastmoney_lhb", {"SECURITY_CODE": "600888", "TRADE_DATE": "2026-11-26", "OPERATEDEPT_NAME": "国泰海通证券江苏路", "BUY": "100", "SELL": "20", "raw_unit": "CNY_10000"})
    assert n_lhb["source_id"] == "eastmoney_lhb_disclosure" and n_lhb["canonical"]["net_amount_cny"] == "800000.00"

    # Seat personas + Guotai-Haitong + Liuyi retired seat + 4D fake-institution
    zmz = match_seat_persona("国泰海通证券股份有限公司上海长宁区江苏路证券营业部", date(2026, 5, 1))
    assert zmz is not None and zmz.persona_code == "hotmoney_zhang_mengzhu"
    liuyi = match_seat_persona("招商证券股份有限公司福州六一中路证券营业部", date(2026, 5, 1))
    assert liuyi is not None and liuyi.category == "legacy_hijacked_seat"

    battle = analyze_lhb_battle(
        [
            {"seat_name": "东方财富证券股份有限公司拉萨团结路第一证券营业部", "buy_cny": 3e7, "sell_cny": 1e6},
            {"seat_name": "东方财富证券股份有限公司拉萨东环路第一证券营业部", "buy_cny": 2.5e7, "sell_cny": 1e6},
            {"seat_name": "东方财富证券股份有限公司拉萨金融城南环路证券营业部", "buy_cny": 2.2e7, "sell_cny": 1e6},
            {"seat_name": "机构专用", "buy_cny": 4e7, "sell_cny": 2e7},
            {"seat_name": "华鑫证券有限责任公司上海分公司", "buy_cny": 3.5e7, "sell_cny": 0},
        ],
        report_date=date(2026, 11, 25),
        float_cap_cny=2.8e9,
        turnover_cny=3.5e8,
    )
    assert battle["lhasa_buyer_count"] == 3
    assert battle["fake_institution_check"]["verdict"] == "quant_fake_institution_risk"

    # Controlled gpt-6-luna guard: non-allowed model is blocked offline BEFORE any I/O!
    now = datetime(2026, 11, 26, 8, 30, tzinfo=timezone.utc)
    clusters = cross_verify_documents([
        {"source_id": "xinhua_finance", "title": "600888 获得重大科技突破", "body": "新华社电", "published_at": now, "instrument_ids": ["600888"]},
    ], as_of=now)
    adj_blocked = build_realtime_news_strategy_adjustment(
        symbol="600888", verified_clusters=clusters, as_of=now, model="gpt-4o", avg_daily_turnover_cny=2e8
    )
    assert adj_blocked["llm_model_used"] is None and adj_blocked["llm_status"] == "blocked_model_guard"

    adj_off = build_realtime_news_strategy_adjustment(
        symbol="600888", verified_clusters=clusters, as_of=now, env={}, avg_daily_turnover_cny=2e8
    )
    assert adj_off["llm_model_used"] is None and adj_off["llm_status"] == "disabled_switch_missing"
    assert -150.0 <= adj_off["tilt_bps"] <= 150.0


# ---------------------------------------------------------------------------------------------------------
# 5. Live PostgreSQL 18 End-to-End: Unseeded + pulse_api RBAC + 6 Workers + v2.0.0 Schema + Forward Adj
# ---------------------------------------------------------------------------------------------------------
@pytest.fixture()
def nexus_private_dsn(pg_dsn):
    """Private migrated DB so publishing to slot 'main' never pollutes the shared session pg_dsn."""
    import os
    from pulse.adapters.postgres import migrate
    from pulse.api import deps

    admin = os.environ.get("PULSE_TEST_ADMIN_DSN", "postgresql://postgres@127.0.0.1:55432/postgres")
    name = f"pulse_nexus_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(admin, autocommit=True) as a:
        a.execute(f'CREATE DATABASE "{name}"')
    dsn = psycopg.conninfo.make_conninfo(admin, dbname=name)
    migrate.apply(dsn)
    deps.close_pool()
    try:
        yield dsn
    finally:
        deps.close_pool()
        with psycopg.connect(admin, autocommit=True) as a:
            a.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.mark.pg
def test_pg_nexus_rbac_pulse_api_workers_and_contracts_end_to_end(nexus_private_dsn, monkeypatch, tmp_path):
    monkeypatch.setenv("PULSE_DSN", nexus_private_dsn)
    monkeypatch.setenv("PULSE_LAKE", str(tmp_path / "lake"))
    now = datetime(2026, 11, 26, 8, 0, tzinfo=timezone.utc)
    iid = uuid.uuid4()
    rid = uuid.uuid4()
    snap_id = uuid.uuid4()

    with psycopg.connect(nexus_private_dsn, row_factory=psycopg.rows.dict_row) as conn:
        bootstrap.seed_registry(conn)
        bootstrap.seed_jobs(conn)
        with conn.transaction():
            conn.execute(
                "INSERT INTO core.source(source_id, display_name, provider_uri, default_origin_group, created_at)"
                " VALUES ('sina', 'Sina', 'https://sina.cn', NULL, %s) ON CONFLICT DO NOTHING",
                (now,),
            )
            conn.execute(
                "INSERT INTO core.source_endpoint(source_id, endpoint_id, source_schema_version, mapping_version,"
                " contract_id, schema_version, mapping_hash, enabled, transport_kind, stale_after_seconds, policy_document, created_at)"
                " VALUES ('sina', 'kline_daily', 'v1', '1.0.0', 'urn:pulse:contracts:v3', '3.0.0', repeat('a',64), true, 'poll', 86400, '{}'::jsonb, %s)"
                " ON CONFLICT DO NOTHING",
                (now,),
            )
            conn.execute(
                "INSERT INTO core.instrument(instrument_id, venue_code, instrument_type, currency, exchange_timezone, created_at)"
                " VALUES (%s, 'XSHG', 'equity', 'CNY', 'Asia/Shanghai', %s)",
                (iid, now),
            )
            conn.execute(
                "INSERT INTO core.instrument_alias(alias_id, instrument_id, source_id, source_symbol, effective_from, available_at, revision, source_anchor)"
                " VALUES (%s, %s, 'sina', '600888', %s, %s, 1, 'test:alias')",
                (uuid.uuid4(), iid, now - timedelta(days=300), now - timedelta(days=300)),
            )
            conn.execute(
                "INSERT INTO core.instrument_state(instrument_id, state_version, display_name, trading_status,"
                " is_risk_warning, board_code, effective_from, available_at, evidence_ref)"
                " VALUES (%s, 1, '测试旗舰股', 'active', false, 'main', %s, %s, 'test:state')",
                (iid, now - timedelta(days=300), now - timedelta(days=300)),
            )
            conn.execute(
                "INSERT INTO core.taxonomy(taxonomy_id, taxonomy_version, taxonomy_kind, owner_source_id, definition, available_at)"
                " VALUES ('legacy_boards_industry', 1, 'industry', NULL, '{}'::jsonb, %s) ON CONFLICT DO NOTHING",
                (now - timedelta(days=300),),
            )
            conn.execute(
                "INSERT INTO core.taxonomy_node(taxonomy_id, taxonomy_version, node_id, parent_node_id, display_name, definition)"
                " VALUES ('legacy_boards_industry', 1, '半导体', NULL, '半导体', '{}'::jsonb) ON CONFLICT DO NOTHING"
            )
            conn.execute(
                "INSERT INTO core.instrument_tag(assignment_id, instrument_id, taxonomy_id, taxonomy_version, node_id,"
                " effective_from, available_at, assertion_kind, evidence_ref)"
                " VALUES (%s, %s, 'legacy_boards_industry', 1, '半导体', %s, %s, 'observed', 'test:tag')",
                (uuid.uuid4(), iid, now - timedelta(days=300), now - timedelta(days=300)),
            )
            batch_id = uuid.uuid4()
            raw_id = uuid.uuid4()
            conn.execute(
                "INSERT INTO ingest.batch(batch_id, source_id, endpoint_id, source_schema_version, mapping_version,"
                " partition_key, batch_state, started_at, raw_committed_at)"
                " VALUES (%s, 'sina', 'kline_daily', 'v1', '1.0.0', 'all', 'NORMALIZED', %s, %s)",
                (batch_id, now, now),
            )
            conn.execute(
                "INSERT INTO ingest.raw_object(raw_object_id, source_id, endpoint_id, source_schema_version, source_record_key,"
                " content_hash, storage_uri, mime_type, source_time_precision, first_seen_at, ingested_at)"
                " VALUES (%s, 'sina', 'kline_daily', 'v1', 'k1', repeat('b',64), 'mem://k1', 'application/json', 'date', %s, %s)",
                (raw_id, now, now),
            )
            conn.execute(
                "INSERT INTO market.dataset_manifest(snapshot_id, dataset_id, manifest_version, batch_id, manifest_state, contract_id,"
                " schema_version, storage_uri, content_hash, row_count, coverage_document, available_watermark, created_at, ready_at)"
                " VALUES (%s, 'bars_raw_1d', 1, %s, 'READY', 'urn:pulse:contracts:v3', '3.0.0', 'mem://s1', repeat('c',64), 60, '{}'::jsonb, %s, %s, %s)",
                (snap_id, batch_id, now, now, now),
            )
            # Insert one UNRELATED market-wide news document + one stock-specific CNINFO doc to prove ZERO leakage!
            conn.execute(
                "INSERT INTO core.source(source_id, display_name, provider_uri, default_origin_group, created_at)"
                " VALUES ('cls_telegraph', 'CLS', 'https://cls.cn', NULL, %s),"
                "        ('cninfo_announcements', 'CNINFO', 'http://cninfo.com.cn', NULL, %s)"
                " ON CONFLICT DO NOTHING",
                (now, now),
            )
            raw_cls_id = uuid.uuid4()
            raw_cninfo_id = uuid.uuid4()
            conn.execute(
                "INSERT INTO ingest.raw_object(raw_object_id, source_id, endpoint_id, source_schema_version, source_record_key,"
                " content_hash, storage_uri, mime_type, source_time_precision, first_seen_at, ingested_at)"
                " VALUES (%s, 'cls_telegraph', 'roll_list_web', 'v1', 'news-cls', repeat('d',64), 'mem://n1', 'application/json', 'second', %s, %s),"
                "        (%s, 'cninfo_announcements', 'announcement', 'v1', 'news-cninfo', repeat('e',64), 'mem://n2', 'application/json', 'second', %s, %s)",
                (raw_cls_id, now, now, raw_cninfo_id, now, now),
            )
            conn.execute(
                "INSERT INTO intelligence.document_revision(document_revision_id, document_id, revision, source_id,"
                " source_record_id, raw_object_id, document_status, origin_group, origin_status, title, published_at,"
                " published_time_precision, first_seen_at, available_at, recorded_at, quality_status, field_quality)"
                " VALUES (%s, %s, 1, 'cls_telegraph', 'unrelated-01', %s, 'published', 'cls', 'confirmed_independent_report',"
                " '毫不相干的泛市场快讯：某宏观统计发布', %s, 'second', %s, %s, %s, 'valid', '{}'::jsonb)",
                (uuid.uuid4(), uuid.uuid4(), raw_cls_id, now - timedelta(hours=1), now - timedelta(hours=1), now - timedelta(hours=1), now),
            )
            conn.execute(
                "INSERT INTO intelligence.document_revision(document_revision_id, document_id, revision, source_id,"
                " source_record_id, raw_object_id, document_status, origin_group, origin_status, title, published_at,"
                " published_time_precision, first_seen_at, available_at, recorded_at, quality_status, field_quality)"
                " VALUES (%s, %s, 1, 'cninfo_announcements', 'rel-600888', %s, 'published', 'cninfo', 'confirmed_original',"
                " '600888 测试旗舰股拟回购股份并获重大合同', %s, 'second', %s, %s, %s, 'valid', %s::jsonb)",
                (
                    uuid.uuid4(), uuid.uuid4(), raw_cninfo_id,
                    now - timedelta(hours=2), now - timedelta(hours=2), now - timedelta(hours=2), now,
                    json.dumps({"stock_refs": {"raw_value": "1.600888"}}),
                ),
            )
            for idx, b in enumerate(_sample_bars(95, start_price=18.5, volume=4_000_000.0, start_date=date(2026, 7, 1))):
                obs_id = uuid.uuid4()
                conn.execute(
                    "INSERT INTO ingest.observation(observation_id, raw_object_id, contract_id, schema_version, mapping_version,"
                    " entity_type, entity_key, natural_key, observation_revision, event_time, available_at, recorded_at,"
                    " quality_status, canonical_payload, field_quality, content_hash)"
                    " VALUES (%s, %s, 'urn:pulse:contracts:v3', '3.0.0', '1.0.0', 'instrument', %s, %s, 1, %s, %s, %s, 'valid', '{}'::jsonb, '{}'::jsonb, %s)",
                    (obs_id, raw_id, str(iid), f"bar:{idx}", now - timedelta(days=1), now - timedelta(days=1), now, f"{idx:064x}"),
                )
                conn.execute(
                    "INSERT INTO market.bar_projection(snapshot_id, instrument_id, timeframe, session_scope, trade_date,"
                    " bar_open_time, bar_close_time, currency, open_price_raw, high_price_raw, low_price_raw, close_price_raw,"
                    " volume_shares, turnover_amount_cny, is_final, quality_status, available_at, observation_id)"
                    " VALUES (%s, %s, '1d', 'regular_continuous_v1', %s, %s, %s, 'CNY', %s, %s, %s, %s, %s, %s, true, 'valid', %s, %s)",
                    (
                        snap_id, iid, b.d,
                        datetime(b.d.year, b.d.month, b.d.day, 1, 30, tzinfo=timezone.utc),
                        datetime(b.d.year, b.d.month, b.d.day, 7, 0, tzinfo=timezone.utc),
                        Decimal(f"{b.o:.2f}"), Decimal(f"{b.h:.2f}"), Decimal(f"{b.l:.2f}"), Decimal(f"{b.c:.2f}"),
                        int(b.v), Decimal(f"{b.c * b.v:.2f}"), now - timedelta(days=1), obs_id,
                    ),
                )
        from pulse.adapters.postgres.release import PgAnalysisRelease
        rel_pub = PgAnalysisRelease(conn)
        rel_pub.ensure_slot("main")
        rel_pub.create_release(
            release_id=rid,
            decision_as_of=now,
            availability_policy_version="observed-first-seen-v1",
            universe_policy_version="u-v1",
            market_rules_version="m-v1",
            required_components=["bars"],
            created_at=now,
        )
        rel_pub.mark_ready(
            rid, "bars", artifact_ref="mem://bars", artifact_hash="b" * 64,
            input_version_hash="c" * 64, snapshot_id=snap_id, computed_at=now,
        )
        stg = rel_pub.begin_stage(stage_id=uuid.uuid4(), slot_key="main", release_id=rid, input_context_hash="a" * 64)
        rel_pub.publish(stg.stage_id, producer_version="test-nexus")
        conn.commit()

        # 1) BEFORE seeding: test pulse_api on an UNSEEDED DB (must NEVER throw InsufficientPrivilege!)
        with conn.transaction():
            conn.execute("SET LOCAL ROLE pulse_api")
            hub_unseeded = nexus_service.get_hub_overview(conn)
            assert hub_unseeded["arena_weight_source"] == "in_memory_prior_unseeded"
            contracts.check("NexusHubOverview", hub_unseeded, contracts.backfill_id("nexus"))
            arena_unseeded = nexus_service.get_arena_overview(conn)
            assert arena_unseeded["evaluation_basis"] == "in_memory_prior_unseeded"
            contracts.check("NexusArenaOverview", arena_unseeded, contracts.backfill_id("nexus"))
        conn.commit()

        # 2) Run all 6 Worker CLI operations directly against real PG18 (as privileged worker)
        w_seed = nexus_worker.cmd_seed(conn)
        assert w_seed["ok"] is True

        fake_xinhua = httpx.MockTransport(lambda req: httpx.Response(200, json={
            "datasource": [{
                "contentId": "xh-100",
                "title": "新华社：加快发展新质生产力，半导体产业链迎政策支持 (600888)",
                "summary": "权威政策定调",
                "publishTime": "2026-11-26 09:00:00",
            }],
            "errno": "0", "data": {"roll_data": []},
        }))
        w_poll = nexus_worker.cmd_poll_intel(conn, offline=False, transport=fake_xinhua)
        assert w_poll["ok"] is True and w_poll["clusters_verified_and_stored"] >= 1
        assert "eastmoney_news_724" in w_poll["errors"]  # Structured error recorded, never swallowed!

        w_rt = nexus_worker.cmd_realtime_adjust(conn)
        assert w_rt["ok"] is True and w_rt["adjustments_written"] >= 1

        w_arena = nexus_worker.cmd_run_arena(conn, rounds=4, sessions=100, max_stocks=50)
        assert w_arena["ok"] is True and w_arena["total_strategies"] == 62
        assert w_arena["evaluation_basis"] == "empirical_backtest_gips_v1"

        w_disc = nexus_worker.cmd_discover_strategies(conn, sessions=100, max_stocks=50)
        assert w_disc["ok"] is True and w_disc["candidates_evaluated"] >= 1
        w_audit = nexus_worker.cmd_audit_integrity(conn)
        assert w_audit["audit_version"] == "nexus-integrity-2.0.0"
        assert w_audit["database_health"]["realtime_adjustments_rows"] >= 1
        conn.commit()

    # 3) FastAPI endpoints STRICTLY under SET LOCAL ROLE pulse_api!
    app = create_app()

    def _pulse_api_conn_override():
        with psycopg.connect(nexus_private_dsn, row_factory=psycopg.rows.dict_row, autocommit=True) as c:
            c.execute("SET TIME ZONE 'UTC'")
            with c.transaction():
                c.execute("SET LOCAL ROLE pulse_api")
                yield c

    app.dependency_overrides[conn_dep] = _pulse_api_conn_override
    try:
        with TestClient(app) as client:
            r_hub = client.get("/v1/x/bf/nexus/hub", params={"track": "composite"})
            assert r_hub.status_code == 200, r_hub.text
            d_hub = r_hub.json()["data"]
            contracts.check("NexusHubOverview", d_hub, contracts.backfill_id("nexus"))
            assert d_hub["arena_weight_source"].startswith("pg:")
            assert len(d_hub["active_realtime_adjustments"]) >= 1
            assert len(d_hub["picks"]) >= 1

            # Cold compute -> cached_in_postgres MUST be False!
            r_pro1 = client.get(f"/v1/x/bf/nexus/stocks/{iid}/pro", params={"timeframe": "1m", "cash_cny": 60000})
            assert r_pro1.status_code == 200, r_pro1.text
            d_pro1 = r_pro1.json()["data"]
            contracts.check("NexusStockProAnalysis", d_pro1, contracts.backfill_id("nexus"))
            assert d_pro1["timeframe"] == "1d"  # 1m normalized cleanly!
            assert d_pro1["cached_in_postgres"] is False
            assert len(d_pro1["kline_bars"]) >= 60
            # Unrelated market doc MUST NEVER leak into stock verified_news!
            headlines = [n["headline"] for n in d_pro1["verified_news"]]
            assert all("毫不相干的泛市场快讯" not in h for h in headlines)
            assert any("600888" in h for h in headlines)

            # Default params cold -> warm cache verification under pulse_api
            r_pro_c1 = client.get(f"/v1/x/bf/nexus/stocks/{iid}/pro", params={"timeframe": "1d"})
            assert r_pro_c1.status_code == 200 and r_pro_c1.json()["data"]["cached_in_postgres"] is False
            r_pro_c2 = client.get(f"/v1/x/bf/nexus/stocks/{iid}/pro", params={"timeframe": "1d"})
            assert r_pro_c2.status_code == 200 and r_pro_c2.json()["data"]["cached_in_postgres"] is True

            # Honest fallback when no forward_adjusted / backward_adjusted LakeStore snapshot exists!
            r_fwd = client.get(f"/v1/x/bf/nexus/stocks/{iid}/pro", params={"adjustment_mode": "forward_adjusted"})
            assert r_fwd.status_code == 200
            d_fwd = r_fwd.json()["data"]
            contracts.check("NexusStockProAnalysis", d_fwd, contracts.backfill_id("nexus"))
            assert d_fwd["adjustment_mode"] == "raw"
            assert "已回退原始价" in d_fwd["price_basis_note"]

            r_bwd = client.get(f"/v1/x/bf/nexus/stocks/{iid}/pro", params={"adjustment": "backward_adjusted"})
            assert r_bwd.status_code == 200
            d_bwd = r_bwd.json()["data"]
            contracts.check("NexusStockProAnalysis", d_bwd, contracts.backfill_id("nexus"))
            assert d_bwd["adjustment_mode"] == "raw"
            assert "当前库无后复权湖快照" in d_bwd["price_basis_note"]

            # Sector deep-dive auto-resolves taxonomy_id=None -> legacy_boards_industry
            r_sec = client.get("/v1/x/bf/nexus/sectors/半导体/deep-dive")
            assert r_sec.status_code == 200, r_sec.text
            d_sec = r_sec.json()["data"]
            contracts.check("NexusSectorDeepDive", d_sec, contracts.backfill_id("nexus"))
            assert d_sec["taxonomy_id"] == "legacy_boards_industry"
            assert d_sec["top_picks"][0]["symbol"] == "600888"

            # Arena supports horizon alias & validates v2.0.0 fields
            r_arena = client.get("/v1/x/bf/nexus/arena", params={"paradigm": "top_paper_2026"})
            assert r_arena.status_code == 200, r_arena.text
            d_arena = r_arena.json()["data"]
            contracts.check("NexusArenaOverview", d_arena, contracts.backfill_id("nexus"))
            assert d_arena["evaluation_basis"] == "empirical_backtest_gips_v1"
            assert len(d_arena["discovered_candidates"]) >= 1

            # Integrity audit validates v2.0.0 schema & real counts
            r_int = client.get("/v1/x/bf/nexus/integrity")
            assert r_int.status_code == 200, r_int.text
            d_int = r_int.json()["data"]
            contracts.check("NexusIntegrityAudit", d_int, contracts.backfill_id("nexus"))
            assert d_int["database_health"]["workbench_cache_rows"] >= 1
            assert d_int["database_health"]["realtime_adjustments_rows"] >= 1
            assert d_int["database_health"]["discovered_candidates_rows"] >= 1
            assert len(d_int["source_normalization_matrix"]) == 11

            # Real forward_adjusted LakeStore read test
            lake = LakeStore(tmp_path / "lake")
            fwd_source_bars = _sample_bars(45, start_price=18.5, volume=4_000_000.0, start_date=date(2026, 7, 1))
            fwd_rows = [
                {
                    "instrument_id": str(iid),
                    "trade_date": b.d,
                    "open_price": round(b.o * 0.85, 4),
                    "high_price": round(b.h * 0.85, 4),
                    "low_price": round(b.l * 0.85, 4),
                    "close_price": round(b.c * 0.85, 4),
                    "volume_shares": int(b.v),
                    "available_at": now - timedelta(hours=1),
                    "revision": 1,
                }
                for b in fwd_source_bars
            ]
            fwd_df = pl.DataFrame(fwd_rows)
            obj_ref = lake.put_parquet(fwd_df, owner="test-fwd")
            man_ref = lake.commit_manifest("bars_legacy_fwd_adjusted_1d", [obj_ref], owner="test-fwd")
            with psycopg.connect(nexus_private_dsn, autocommit=True) as admin_c:
                with admin_c.transaction():
                    admin_c.execute(
                        "INSERT INTO market.dataset_manifest(snapshot_id, dataset_id, manifest_version, batch_id,"
                        " manifest_state, contract_id, schema_version, storage_uri, content_hash, row_count,"
                        " coverage_document, available_watermark, created_at, ready_at)"
                        " VALUES (%s, 'bars_legacy_fwd_adjusted_1d', 1, %s, 'READY', 'urn:pulse:contracts:v3',"
                        " '3.0.0', %s, %s, %s, '{}'::jsonb, %s, %s, %s)",
                        (uuid.uuid4(), batch_id, man_ref.rel_path, man_ref.sha256, len(fwd_rows), now, now, now),
                    )
            r_fwd_ok = client.get(f"/v1/x/bf/nexus/stocks/{iid}/pro", params={"adjustment": "forward_adjusted"})
            assert r_fwd_ok.status_code == 200
            d_fwd_ok = r_fwd_ok.json()["data"]
            contracts.check("NexusStockProAnalysis", d_fwd_ok, contracts.backfill_id("nexus"))
            assert d_fwd_ok["adjustment_mode"] == "forward_adjusted"
            assert d_fwd_ok["cached_in_postgres"] is False
            assert "前复权研究价" in d_fwd_ok["price_basis_note"]
            assert len(d_fwd_ok["kline_bars"]) == len(fwd_source_bars)
            assert float(d_fwd_ok["kline_bars"][-1]["close"]) < float(r_pro_c1.json()["data"]["kline_bars"][-1]["close"])
            # P1 invariant: forward-adjusted K-line NEVER contaminates real CNY tradable action / T / grid plans!
            raw_pro = r_pro_c1.json()["data"]
            assert d_fwd_ok["action_plan"]["entry_low"] == raw_pro["action_plan"]["entry_low"]
            assert d_fwd_ok["intraday_t_plan"]["reference_price"] == raw_pro["intraday_t_plan"]["reference_price"]
            assert d_fwd_ok["grid_plan"]["anchor_price"] == raw_pro["grid_plan"]["anchor_price"]
            r_fwd_warm = client.get(f"/v1/x/bf/nexus/stocks/{iid}/pro", params={"adjustment": "forward_adjusted"})
            assert r_fwd_warm.status_code == 200 and r_fwd_warm.json()["data"]["cached_in_postgres"] is True

            # P1 invariant: _merge_session_pulse_fallback preserves None for missing metrics (never fake 0)
            from pulse.application.nexus_service import _merge_session_pulse_fallback
            merged = _merge_session_pulse_fallback(
                {"total_posts": 0},
                [{
                    "session_bucket": "intraday", "post_count": 5, "unique_authors": 4,
                    "bullish_ratio": 0.6, "divergence_index": 0.4, "abnormal_attn_z": None,
                    "sir_contagion_slope": None, "crowd_verdict": "healthy_divergence", "claim_checks_json": [],
                }],
            )
            assert merged["template_spam_ratio"] is None
            assert merged["abnormal_attention_z"] is None
            assert merged["sir_contagion_slope"] is None

            # Verify /arena horizon alias too
            r_arena_hz = client.get("/v1/x/bf/nexus/arena", params={"horizon": "short_5d"})
            assert r_arena_hz.status_code == 200
            assert all(s["horizon_bucket"] == "short_5d" for s in r_arena_hz.json()["data"]["strategies"])

            llms = client.get("/llms.txt").text
            assert "getNexusHubOverview" in llms and "getNexusStockProAnalysis" in llms
    finally:
        app.dependency_overrides.clear()
