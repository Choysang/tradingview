-- 0053_nexus_platform_upgrade.sql
-- Pulse 3.5 Nexus upgrade: high-volume query indexes, multi-source field normalization registry,
-- cross-verified intelligence clusters, real-time RAPTOR Black-Litterman & gpt-6-luna signal adjustments,
-- 4-session community resonance, LHB famous seat personas, strategy taxonomy & Swiss-Glicko2 real-backtest
-- arena tables, AlphaPROBE strategy discovery pool, and PostgreSQL-backed stock workbench cache.
-- Strictly adheres to Pulse3 numeric(38,12), timestamptz, FK-to-unique, and least-privilege runtime role policies.

-- 1. High-throughput performance indexes on existing large tables
CREATE INDEX IF NOT EXISTS community_obs_avail_idx
    ON intelligence.community_observation (available_at DESC);

CREATE INDEX IF NOT EXISTS community_obs_payload_gin
    ON intelligence.community_observation USING gin (analysis_payload jsonb_path_ops);

CREATE INDEX IF NOT EXISTS ext_artifact_contract_idx
    ON ops.ext_artifact (contract_id);

CREATE INDEX IF NOT EXISTS ext_artifact_body_gin
    ON ops.ext_artifact USING gin (body jsonb_path_ops);

CREATE INDEX IF NOT EXISTS ranking_run_release_scope_idx
    ON decision.ranking_run (release_id, ranking_scope, horizon_id);

CREATE INDEX IF NOT EXISTS bar_proj_snapshot_inst_idx
    ON market.bar_projection (snapshot_id, instrument_id, trade_date);

-- 2. Unified multi-source field mapping & normalization specification registry
CREATE TABLE IF NOT EXISTS ingest.source_field_mapping (
    mapping_id           text PRIMARY KEY,
    source_id            text NOT NULL REFERENCES core.source (source_id),
    domain_kind          text NOT NULL CHECK (domain_kind IN ('bars', 'quote', 'news', 'announcement', 'fundflow', 'lhb_seat', 'community', 'macro_calendar')),
    raw_field_spec       jsonb NOT NULL,
    canonical_field_spec jsonb NOT NULL,
    missing_policy       text NOT NULL,
    unit_guard_policy    text NOT NULL,
    schema_version       text NOT NULL,
    updated_at           timestamptz NOT NULL DEFAULT now()
);

-- 3. Multi-source cross-verified intelligence & macro factor clusters
CREATE TABLE IF NOT EXISTS intelligence.cross_source_cluster (
    cluster_id          text PRIMARY KEY,
    fingerprint_hash    core.sha256_hex NOT NULL,
    headline            text NOT NULL,
    highest_tier        text NOT NULL CHECK (highest_tier IN ('tier1_official', 'tier2_financial_press', 'tier3_portal_social')),
    verification_state  text NOT NULL CHECK (verification_state IN ('official_confirmed', 'multi_source_verified', 'single_source_flash', 'contradicted')),
    corroboration_count integer NOT NULL CHECK (corroboration_count >= 1),
    confidence_score    numeric(38, 12) NOT NULL,
    polarity            text NOT NULL CHECK (polarity IN ('positive', 'negative', 'neutral')),
    decay_half_life_hrs numeric(38, 12) NOT NULL,
    sources_json        jsonb NOT NULL,
    instrument_ids_json jsonb NOT NULL,
    sector_nodes_json   jsonb NOT NULL,
    bull_bear_debate    jsonb NOT NULL,
    first_published_at  timestamptz NOT NULL,
    available_at        timestamptz NOT NULL,
    recorded_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS cross_source_cluster_avail_idx
    ON intelligence.cross_source_cluster (available_at DESC);

CREATE INDEX IF NOT EXISTS cross_source_cluster_inst_gin
    ON intelligence.cross_source_cluster USING gin (instrument_ids_json jsonb_path_ops);

-- 3b. Real-time news -> RAPTOR Black-Litterman tilt & gated gpt-6-luna commentary ledger
CREATE TABLE IF NOT EXISTS intelligence.realtime_signal_adjustment (
    adjustment_id       text PRIMARY KEY,
    cluster_id          text NOT NULL REFERENCES intelligence.cross_source_cluster (cluster_id),
    target_scope        text NOT NULL CHECK (target_scope IN ('market', 'sector', 'instrument')),
    target_key          text NOT NULL,
    tilt_bps            numeric(38, 12) NOT NULL,
    action_directive    text NOT NULL CHECK (action_directive IN ('catalyst_boost', 'tighten_stop_grid', 'risk_veto', 'neutral_monitor')),
    liquidity_passed    boolean NOT NULL DEFAULT true,
    amihud_ratio        numeric(38, 12),
    llm_model_used      text,
    llm_summary         text,
    evidence_json       jsonb NOT NULL,
    effective_until     timestamptz NOT NULL,
    available_at        timestamptz NOT NULL,
    recorded_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS realtime_sig_adj_target_idx
    ON intelligence.realtime_signal_adjustment (target_scope, target_key, available_at DESC);

-- 4. Four-session retail & Guba discussion resonance (pre_market / intraday / midday_break / post_market)
CREATE TABLE IF NOT EXISTS intelligence.session_sentiment_pulse (
    pulse_id            uuid PRIMARY KEY,
    instrument_id       uuid NOT NULL REFERENCES core.instrument (instrument_id),
    trade_date          date NOT NULL,
    session_bucket      text NOT NULL CHECK (session_bucket IN ('pre_market', 'intraday', 'midday_break', 'post_market')),
    post_count          integer NOT NULL CHECK (post_count >= 0),
    unique_authors      integer NOT NULL CHECK (unique_authors >= 0),
    bullish_ratio       numeric(38, 12),
    divergence_index    numeric(38, 12),
    abnormal_attn_z     numeric(38, 12),
    sir_contagion_slope numeric(38, 12),
    crowd_verdict       text NOT NULL,
    claim_checks_json   jsonb NOT NULL,
    available_at        timestamptz NOT NULL,
    recorded_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (instrument_id, trade_date, session_bucket)
);

CREATE INDEX IF NOT EXISTS session_sentiment_inst_date_idx
    ON intelligence.session_sentiment_pulse (instrument_id, trade_date DESC);

-- 5. Famous LHB brokerage seat battle personas (compatible with 2025-04 Guotai-Haitong rename & fake-inst detection)
CREATE TABLE IF NOT EXISTS intelligence.seat_battle_profile (
    persona_id          text PRIMARY KEY,
    persona_code        text NOT NULL,
    persona_label       text NOT NULL,
    category            text NOT NULL CHECK (category IN ('lhasa_retail_corps', 'one_day_dump_raider', 'pattern_leader_synergy', 'quant_high_freq', 'institution_channel', 'legacy_hijacked_seat')),
    match_patterns_json jsonb NOT NULL,
    empirical_d1_gap    numeric(38, 12),
    empirical_d5_excess numeric(38, 12),
    style_summary       text NOT NULL,
    counter_tactic      text NOT NULL,
    effective_note      text NOT NULL,
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- 6. Multi-dimensional strategy taxonomy router & Swiss-Glicko2 real-backtest tournament ratings
CREATE TABLE IF NOT EXISTS strategy.taxonomy_catalog (
    strategy_code       text PRIMARY KEY,
    display_name        text NOT NULL,
    layer               text NOT NULL CHECK (layer IN ('stock_selection', 'sector_rotation', 'regime_timing', 'intraday_execution')),
    horizon_bucket      text NOT NULL CHECK (horizon_bucket IN ('intraday_t0', 'ultra_short_1_3d', 'short_5d', 'swing_10_20d', 'position_60d')),
    paradigm            text NOT NULL CHECK (paradigm IN ('top_paper_2026', 'multi_factor_fusion', 'classic_factor', 'technical_pattern', 'sentiment_microstructure', 'hot_money_seat', 'grid_intraday_t', 'rl_distributional', 'llm_hybrid')),
    portfolio_role      text NOT NULL CHECK (portfolio_role IN ('core_alpha', 'defensive_shield', 'risk_veto', 'cost_reducer', 'exploratory_shadow')),
    cluster_id          text NOT NULL,
    regime_fit_json     jsonb NOT NULL,
    paper_citation      text,
    summary             text NOT NULL,
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS research.swiss_arena_season (
    season_id           text PRIMARY KEY,
    benchmark_standard  text NOT NULL,
    rounds_completed    integer NOT NULL CHECK (rounds_completed >= 0),
    evaluation_basis    text NOT NULL DEFAULT 'empirical_backtest_gips_v1',
    sessions_evaluated  integer NOT NULL DEFAULT 0 CHECK (sessions_evaluated >= 0),
    pairings_json       jsonb NOT NULL,
    correlation_json    jsonb NOT NULL DEFAULT '{}'::jsonb,
    evaluated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS research.swiss_arena_rating (
    season_id           text NOT NULL REFERENCES research.swiss_arena_season (season_id),
    strategy_code       text NOT NULL REFERENCES strategy.taxonomy_catalog (strategy_code),
    glicko_mu           numeric(38, 12) NOT NULL,
    glicko_phi          numeric(38, 12) NOT NULL,
    glicko_sigma        numeric(38, 12) NOT NULL,
    conservative_score  numeric(38, 12) NOT NULL,
    swiss_wins          integer NOT NULL DEFAULT 0 CHECK (swiss_wins >= 0),
    swiss_draws         integer NOT NULL DEFAULT 0 CHECK (swiss_draws >= 0),
    swiss_losses        integer NOT NULL DEFAULT 0 CHECK (swiss_losses >= 0),
    net_return_pct      numeric(38, 12),
    excess_return_pct   numeric(38, 12),
    sharpe_ratio        numeric(38, 12),
    max_drawdown_pct    numeric(38, 12),
    nw_t_stat           numeric(38, 12),
    worst_cvar_pct      numeric(38, 12),
    rre_stability       numeric(38, 12),
    pfs_robustness      numeric(38, 12),
    evaluation_source   text NOT NULL DEFAULT 'unified_backtest_engine',
    is_cluster_champion boolean NOT NULL DEFAULT false,
    lifecycle_tier      text NOT NULL CHECK (lifecycle_tier IN ('champion_active', 'promoted_active', 'candidate_shadow', 'observing_trial', 'demoted_paused', 'retired_falsified')),
    ensemble_weight     numeric(38, 12) NOT NULL,
    ram_floor_weight    numeric(38, 12) NOT NULL DEFAULT 0,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (season_id, strategy_code)
);

-- 6b. Automated Strategy Discovery Pool (AlphaPROBE DAG evolution + 5-Gate screening)
CREATE TABLE IF NOT EXISTS research.strategy_discovery_candidate (
    candidate_id        text PRIMARY KEY,
    parent_code         text,
    dag_depth           integer NOT NULL DEFAULT 1 CHECK (dag_depth >= 1),
    display_name        text NOT NULL,
    formula_spec        jsonb NOT NULL,
    max_corr_existing   numeric(38, 12),
    rre_stability       numeric(38, 12),
    pfs_robustness      numeric(38, 12),
    net_sharpe          numeric(38, 12),
    nw_t_stat           numeric(38, 12),
    gate_verdict        text NOT NULL CHECK (gate_verdict IN ('admitted_shadow', 'rejected_redundant', 'rejected_turnover_rre', 'rejected_fragile_pfs', 'rejected_negative_net', 'pending_eval')),
    evidence_json       jsonb NOT NULL,
    discovered_at       timestamptz NOT NULL DEFAULT now()
);

-- 7. PostgreSQL-backed Stock Pro Workbench cache (unifies Frontend <-> Backend <-> Database)
CREATE TABLE IF NOT EXISTS decision.stock_workbench_cache (
    cache_key_hash      core.sha256_hex PRIMARY KEY,
    instrument_id       uuid NOT NULL REFERENCES core.instrument (instrument_id),
    release_id          uuid NOT NULL REFERENCES decision.release (release_id),
    timeframe           text NOT NULL,
    adjustment_mode     text NOT NULL,
    decision_as_of      timestamptz NOT NULL,
    payload_json        jsonb NOT NULL,
    computed_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (instrument_id, release_id, timeframe, adjustment_mode)
);

CREATE INDEX IF NOT EXISTS stock_workbench_cache_rel_idx
    ON decision.stock_workbench_cache (release_id, instrument_id);

-- 8. Least-privilege runtime role permissions (strictly honors 0007_runtime_roles.sql & test_roles.py)
GRANT SELECT ON
    ingest.source_field_mapping,
    intelligence.cross_source_cluster,
    intelligence.realtime_signal_adjustment,
    intelligence.session_sentiment_pulse,
    intelligence.seat_battle_profile,
    strategy.taxonomy_catalog,
    research.swiss_arena_season,
    research.swiss_arena_rating,
    research.strategy_discovery_candidate,
    decision.stock_workbench_cache
TO pulse_api, pulse_worker, pulse_coordinator;

GRANT INSERT, UPDATE ON
    ingest.source_field_mapping,
    intelligence.cross_source_cluster,
    intelligence.realtime_signal_adjustment,
    intelligence.session_sentiment_pulse,
    intelligence.seat_battle_profile,
    strategy.taxonomy_catalog,
    research.swiss_arena_season,
    research.swiss_arena_rating,
    research.strategy_discovery_candidate,
    decision.stock_workbench_cache
TO pulse_worker, pulse_coordinator;

-- pulse_api stays strictly read-mostly: ONLY read-through workbench cache writes permitted.
GRANT INSERT, UPDATE ON
    decision.stock_workbench_cache
TO pulse_api;
