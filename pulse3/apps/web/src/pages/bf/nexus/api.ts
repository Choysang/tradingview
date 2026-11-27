/**
 * Typed client and wire contracts for the Nexus 2026/3.5 unified stock selection,
 * multi-timeframe TradingView Pro analysis, Intraday-T / Adaptive Grid execution,
 * 4-session Guba & LHB seat personas, Real-Time RAPTOR adjustments, and Swiss Arena (/v1/x/bf/nexus/*).
 *
 * Mirrors src/pulse/contracts/data/backfill_nexus.schema.json (urn:pulse:contracts:backfill:nexus:v1, v2.0.0).
 * Invariants: Decimal strings stay strings; null means missing/unavailable and is NEVER coerced to 0.
 */
import { extGet } from '../../ext/a/api';

export type DecStr = string | null;
export type HorizonTrack = 'composite' | 'short' | 'swing' | 'long';
export type RatingLabel = 'strong_sell' | 'sell' | 'neutral' | 'buy' | 'strong_buy' | 'unavailable';
export type TvLabel = RatingLabel;
export type SectorRole = 'leader_1' | 'leader_2' | 'core_capacity' | 'pioneer' | 'catchup' | 'member';
export type GridMode = 'atr_adaptive' | 'geometric' | 'arithmetic';
export type CrumbleRiskLevel = 'low' | 'moderate' | 'high' | 'unknown';

export interface GaugeItem {
  name: string;
  value: DecStr;
  signal: 'buy' | 'neutral' | 'sell' | 'unavailable';
}

export interface GaugeGroup {
  label: RatingLabel;
  score: DecStr;
  buy_count: number;
  neutral_count: number;
  sell_count: number;
  total_count: number;
  items: GaugeItem[];
}

export interface TimeframeTechnicalRating {
  timeframe: '1d' | '1w' | '1mo' | '1y';
  bar_count: number;
  last_bar_date: string | null;
  summary: GaugeGroup;
  moving_averages: GaugeGroup;
  oscillators: GaugeGroup;
}

export interface PivotLevelSet {
  method: 'traditional' | 'classic' | 'fibonacci' | 'camarilla' | 'woodie' | 'demark';
  p: DecStr;
  r1: DecStr;
  r2: DecStr;
  r3: DecStr;
  r4: DecStr;
  s1: DecStr;
  s2: DecStr;
  s3: DecStr;
  s4: DecStr;
}

export interface VpvrBin {
  low: string;
  high: string;
  mid: string;
  volume_share: string;
  up_share: string;
  is_value_area: boolean;
  is_poc: boolean;
  node_kind: 'poc' | 'hvn' | 'lvn' | 'normal';
}

export interface VpvrProfile {
  available: boolean;
  lookback_bars: number;
  poc: DecStr;
  vah: DecStr;
  val: DecStr;
  winner_ratio: DecStr;
  avg_chip_cost: DecStr;
  concentration_90: DecStr;
  hvn_zones: string[];
  lvn_zones: string[];
  bins: VpvrBin[];
}

export interface NeurosymbolicGuardCheck {
  guard_id: string;
  passed: boolean;
  detail: string;
}

export interface NeurosymbolicGuards {
  all_passed: boolean;
  crumble_risk_level: CrumbleRiskLevel;
  spread_to_cost_multiple: DecStr;
  violations: string[];
  checks: NeurosymbolicGuardCheck[];
}

export interface IntradayTPlan {
  archetype_id: string;
  archetype_name: string;
  paper_ref: string;
  recommended_direction: 'forward_t' | 'reverse_t' | 'dual_vwap_t' | 'lock_hold' | 'exit_only' | 'unavailable';
  reference_price: DecStr;
  reservation_price: DecStr;
  forward_buy_price: DecStr;
  forward_sell_price: DecStr;
  reverse_sell_price: DecStr;
  reverse_buyback_price: DecStr;
  abort_stop_price: DecStr;
  base_sellable_shares: number;
  available_cash_cny: string;
  recommended_lots: number;
  recommended_shares: number;
  round_trip_fee_bps: string;
  min_profitable_spread_pct: string;
  expected_cost_reduction_pct: DecStr;
  cash_non_overdraft_verified: boolean;
  t1_lock_verified: boolean;
  crumble_risk_level: CrumbleRiskLevel;
  neurosymbolic_guards: NeurosymbolicGuards;
  execution_notes: string[];
  backtest_summary: {
    sessions_tested: number;
    triggered_days: number;
    win_rate: DecStr;
    cum_extra_return_pct: DecStr;
    vs_hold_delta_pct: DecStr;
  };
}

export interface GridRung {
  level_index: number;
  trigger_price: string;
  action: 'buy_dip' | 'anchor' | 'sell_rally';
  shares: number;
  target_pair_price: DecStr;
  est_net_profit_cny: DecStr;
}

export interface GridPlan {
  mode: GridMode | 'budget_insufficient' | 'unavailable';
  anchor_price: DecStr;
  lower_bound: DecStr;
  upper_bound: DecStr;
  upper_circuit_breaker: DecStr;
  lower_stop_breaker: DecStr;
  grid_count: number;
  step_pct: DecStr;
  per_grid_lots: number;
  per_grid_shares: number;
  base_locked_shares: number;
  rungs: GridRung[];
  crumble_risk_level: CrumbleRiskLevel;
  neurosymbolic_guards: NeurosymbolicGuards;
  budget_status_note: string | null;
  backtest: {
    sessions_tested: number;
    completed_round_trips: number;
    grid_net_profit_cny: DecStr;
    cost_reduction_per_share_cny: DecStr;
    cost_reduction_pct: DecStr;
    vs_buy_hold_excess_pct: DecStr;
    max_capital_usage_pct: DecStr;
  };
}

export interface SessionPulse {
  session_bucket: 'pre_market' | 'intraday' | 'midday_break' | 'post_market';
  label: string;
  post_count: number;
  unique_authors: number;
  bullish_ratio: DecStr;
  divergence_index: DecStr;
  dominant_topic: string;
  heat_state: 'cold' | 'normal' | 'warming' | 'overheated_warning' | 'no_data';
}

export interface RetailClaimCheck {
  retail_claim: string;
  verdict: 'supported_by_facts' | 'contradicted_by_facts' | 'unverified_noise';
  objective_evidence: string;
}

export interface RetailResonance {
  total_posts: number;
  template_spam_ratio: DecStr;
  abnormal_attention_z: DecStr;
  sir_contagion_slope: DecStr;
  crowd_state: 'overheated_reversal_risk' | 'healthy_divergence' | 'early_discovery' | 'quiet_ignored' | 'unavailable';
  sessions: SessionPulse[];
  claim_checks: RetailClaimCheck[];
  synthesized_judgment: string;
}

export interface SeatDetail {
  seat_name: string;
  side: 'buy' | 'sell' | 'both';
  category: string;
  net_amount_cny: DecStr;
  persona_code: string;
  persona_label: string;
  style_summary: string;
  empirical_d1_gap_pct: DecStr;
  empirical_d5_excess_pct: DecStr;
  counter_tactic: string;
}

export interface SeatBattleAnalysis {
  has_recent_lhb: boolean;
  report_date: string | null;
  lhasa_buyer_count: number;
  one_day_raider_count: number;
  synergy_leader_count: number;
  quant_seat_count: number;
  net_buy_amount_cny: DecStr;
  lhasa_ratio: DecStr;
  battle_verdict: 'synergy_bullish' | 'neutral_turnover' | 'retail_trap_bearish' | 'raider_dump_warning' | 'no_lhb_data';
  fake_institution_check: {
    institution_present: boolean;
    verdict: 'real_institution_likely' | 'quant_fake_institution_risk' | 'mixed_uncertain' | 'none';
    risk_score: DecStr;
    reasons: string[];
  };
  seats: SeatDetail[];
  opponent_strategy: string;
}

export interface NexusChartSignalMarker {
  time: string;
  position: 'aboveBar' | 'belowBar';
  shape: 'arrowUp' | 'arrowDown' | 'circle' | 'square';
  tone: 'buy' | 'sell' | 'warn' | 'info';
  text: string;
}

export interface ChartAnchorLine {
  id: string;
  price: string;
  title: string;
  tone: 'plan' | 'exit' | 'risk' | 'draw';
  dashed: boolean;
}

export interface CompactBar {
  trade_date: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
}

export interface RealtimeAdjustmentBrief {
  tilt_bps: string;
  action_directive: 'catalyst_boost' | 'tighten_stop_grid' | 'risk_veto' | 'neutral_monitor';
  liquidity_filter_triggered: boolean;
  risk_veto_reasons: string[];
  llm_model_used: string | null;
  llm_status: string;
  guard_message: string;
  bull_points: string[];
  bear_points: string[];
  holding_plan: string;
}

export interface VerifiedNewsItem {
  cluster_id: string;
  headline: string;
  tier: 'tier1_official' | 'tier2_financial_press' | 'tier3_portal_social';
  verification_state: 'official_confirmed' | 'multi_source_verified' | 'single_source_flash' | 'contradicted';
  sources: string[];
  polarity: 'positive' | 'negative' | 'neutral';
  published_at: string | null;
  bull_bear_note: string;
}

export interface NexusStockProAnalysis {
  instrument_id: string;
  symbol: string;
  display_name: string;
  board_code: string | null;
  release_id: string;
  decision_as_of: string;
  cached_in_postgres: boolean;
  cache_key_hash: string;
  timeframe: string;
  adjustment_mode: string;
  price_basis_note: string;
  kline_bars: CompactBar[];
  multi_timeframe_ratings: TimeframeTechnicalRating[];
  mtf_resonance_label: RatingLabel;
  mtf_resonance_score: DecStr;
  pivot_points: PivotLevelSet[];
  vpvr: VpvrProfile;
  pro_signals: {
    supertrend_dir: 'bullish' | 'bearish' | 'unavailable';
    supertrend_level: DecStr;
    volatility_squeeze: 'squeeze_on' | 'squeeze_fired_up' | 'squeeze_fired_down' | 'normal' | 'unavailable';
    bb_kc_squeeze_on: boolean | null;
    squeeze_momentum: DecStr;
    adx_14: DecStr;
    anchored_vwap_20d: DecStr;
    rvol_20d: DecStr;
    macd_divergence: 'bullish_bottom_div' | 'bearish_top_div' | 'none';
    rsi_divergence: 'bullish_bottom_div' | 'bearish_top_div' | 'none';
    volume_patterns: string[];
    atr_14: DecStr;
  };
  chart_lines: ChartAnchorLine[];
  chart_markers: NexusChartSignalMarker[];
  action_plan: {
    verdict: 'strong_priority' | 'accumulate_on_dip' | 'hold_and_grid_t' | 'watch_wait' | 'risk_avoid';
    composite_score: DecStr;
    entry_low: DecStr;
    entry_high: DecStr;
    stop_loss: DecStr;
    target_1: DecStr;
    target_2: DecStr;
    holding_sessions: string;
    max_position_pct: string;
    risk_vetoes: string[];
    pillar_scores: {
      technical: DecStr;
      capital_flow: DecStr;
      news_catalyst: DecStr;
      sentiment_micro: DecStr;
    };
    rationale: string[];
  };
  realtime_adjustment: RealtimeAdjustmentBrief;
  intraday_t_plan: IntradayTPlan;
  grid_plan: GridPlan;
  retail_resonance: RetailResonance;
  seat_battle: SeatBattleAnalysis;
  verified_news: VerifiedNewsItem[];
}

export interface EnsemblePickRow {
  rank: number;
  instrument_id: string;
  symbol: string;
  display_name: string;
  sector_node: string | null;
  sector_role: SectorRole;
  ensemble_score: string;
  voter_support_count: number;
  total_active_voters: number;
  entry_low: DecStr;
  entry_high: DecStr;
  stop_loss: DecStr;
  target_price: DecStr;
  holding_horizon: string;
  tags: string[];
  supporting_strategies: string[];
  risk_flags: string[];
  catalyst_summary: string;
}

export interface WatchCandidateBrief {
  instrument_id: string;
  symbol: string;
  display_name: string;
  role: string;
  pool_state: string;
  score: DecStr;
}

export interface HeatingSectorCard {
  rank: number;
  taxonomy_id: string;
  node_id: string;
  surge_score: string;
  velocity_label: 'rapid_promotion' | 'steady_warming' | 'observing';
  member_count: number;
  present_components: string[];
  missing_components: string[];
  watch_candidates: WatchCandidateBrief[];
}

export interface MacroFactorItem {
  dimension: 'domestic_policy_macro' | 'global_linkage' | 'capital_proxy_2026' | 'retail_sentiment' | 'microstructure_seats';
  factor_name: string;
  status: 'observed' | 'partial' | 'awaiting_feed';
  signal: 'supportive' | 'neutral' | 'cautious' | 'unknown';
  detail: string;
  source_authority: string;
}

export interface SourcePollingItem {
  source_id: string;
  tier: string;
  runtime_state: string;
  freshness_state: string;
  last_success_at: string | null;
  fallback_group: string;
}

export interface SourcePollingStatus {
  total_sources: number;
  healthy_count: number;
  degraded_count: number;
  silent_warning_count: number;
  host_rate_limit_rps: string;
  sources: SourcePollingItem[];
}

export type FlashVerificationState = 'official_confirmed' | 'multi_source_verified' | 'single_source_flash' | 'contradicted';

export interface CrossVerifiedFlash {
  cluster_id: string;
  headline: string;
  verification_state: FlashVerificationState;
  corroboration_count: number;
  confidence_score: string;
  sources: string[];
  polarity: string;
  affected_sectors: string[];
  published_at: string | null;
}

export interface ActiveRealtimeAdjustment {
  adjustment_id: string;
  target_scope: 'market' | 'sector' | 'instrument';
  target_key: string;
  tilt_bps: string;
  action_directive: string;
  llm_model_used: string | null;
  summary: string;
  available_at: string | null;
}

export interface NexusHubOverview {
  release_id: string | null;
  decision_as_of: string | null;
  horizon_track: HorizonTrack;
  normalization_rule: string;
  arena_weight_source: string;
  market_regime: {
    code: string;
    label: string;
    curve_gate_active: boolean;
    recommended_exposure_pct: string;
    summary: string;
  };
  macro_factor_matrix: MacroFactorItem[];
  source_polling_status: SourcePollingStatus;
  cross_verified_flashes: CrossVerifiedFlash[];
  active_realtime_adjustments: ActiveRealtimeAdjustment[];
  picks: EnsemblePickRow[];
  rapid_rising_sectors: HeatingSectorCard[];
  candidate_watch_pool: Array<{
    instrument_id: string;
    symbol: string;
    display_name: string;
    sector_node: string;
    pool_state: string;
    trigger_code: string;
    entry_hint: string;
  }>;
}

export interface SurgeComponentItem {
  key: string;
  label: string;
  value: DecStr;
  status: 'ready' | 'missing';
}

export interface SectorWatchPoolMember {
  instrument_id: string;
  symbol: string;
  display_name: string;
  pool_state: string;
  role: string;
  trigger_code: string;
  plan_ready: boolean;
}

export interface NexusSectorDeepDive {
  release_id: string;
  taxonomy_id: string;
  node_id: string;
  horizon_id: string;
  is_rapidly_rising: boolean;
  surge_score: DecStr;
  surge_components: SurgeComponentItem[];
  top_picks: EnsemblePickRow[];
  watch_pool_members: SectorWatchPoolMember[];
  sector_news: string[];
}

export interface BenchmarkStandardSpec {
  standard_id: string;
  execution_timing: string;
  stamp_duty_bps: string;
  commission_bps: string;
  min_commission_cny: string;
  transfer_fee_bps: string;
  slippage_model: string;
  limit_queue_rule: string;
  validation_protocol: string;
}

export interface StrategyTaxonomyEntry {
  strategy_code: string;
  display_name: string;
  layer: 'stock_selection' | 'sector_rotation' | 'regime_timing' | 'intraday_execution';
  horizon_bucket: 'intraday_t0' | 'ultra_short_1_3d' | 'short_5d' | 'swing_10_20d' | 'position_60d';
  paradigm:
    | 'top_paper_2026'
    | 'multi_factor_fusion'
    | 'classic_factor'
    | 'technical_pattern'
    | 'sentiment_microstructure'
    | 'hot_money_seat'
    | 'grid_intraday_t'
    | 'rl_distributional'
    | 'llm_hybrid';
  regime_fit: string[];
  portfolio_role: 'core_alpha' | 'defensive_shield' | 'risk_veto' | 'cost_reducer' | 'exploratory_shadow';
  cluster_id: string;
  is_cluster_champion: boolean;
  glicko_mu: string;
  glicko_phi: string;
  glicko_sigma: string;
  conservative_score: string;
  swiss_wins: number;
  swiss_draws: number;
  swiss_losses: number;
  net_return_pct: DecStr;
  excess_return_pct: DecStr;
  sharpe_ratio: DecStr;
  max_drawdown_pct: DecStr;
  nw_t_stat: DecStr;
  worst_cvar_pct: DecStr;
  rre_stability: DecStr;
  pfs_robustness: DecStr;
  evaluation_source: string;
  lifecycle_tier:
    | 'champion_active'
    | 'promoted_active'
    | 'candidate_shadow'
    | 'observing_trial'
    | 'demoted_paused'
    | 'retired_falsified';
  ensemble_weight: string;
  ram_floor_weight: string;
  paper_citation: string | null;
  summary: string;
}

export interface DiscoveredCandidateItem {
  candidate_id: string;
  parent_code: string | null;
  dag_depth: number;
  display_name: string;
  max_corr_existing: DecStr;
  rre_stability: DecStr;
  pfs_robustness: DecStr;
  net_sharpe: DecStr;
  nw_t_stat: DecStr;
  gate_verdict:
    | 'admitted_shadow'
    | 'rejected_redundant'
    | 'rejected_turnover_rre'
    | 'rejected_fragile_pfs'
    | 'rejected_negative_net'
    | 'pending_eval';
}

export interface PaperSpecItem {
  paper_id: string;
  title: string;
  venue_year: string;
  authors: string;
  core_formula: string;
  ashare_operator: string;
  integrated_strategy_code: string;
}

export interface SwissPairingItem {
  round_no: number;
  left_code: string;
  right_code: string;
  winner: string;
  score_delta: string;
  reason: string;
}

export interface NexusArenaOverview {
  benchmark_standard: BenchmarkStandardSpec;
  season_id: string;
  evaluation_basis: string;
  sessions_evaluated: number;
  rounds_completed: number;
  total_strategies: number;
  orthogonal_clusters: number;
  taxonomy_facets: {
    layers: string[];
    horizons: string[];
    paradigms: string[];
    roles: string[];
  };
  strategies: StrategyTaxonomyEntry[];
  recent_pairings: SwissPairingItem[];
  top_papers_2026: PaperSpecItem[];
  discovered_candidates: DiscoveredCandidateItem[];
}

export interface EntrypointParityItem {
  entrypoint: string;
  module_path: string;
  uses_unified_standard: boolean;
  fee_basis: string;
  limit_rule: string;
  t1_enforced: boolean;
  note: string;
}

export interface SourceNormalizationItem {
  source_id: string;
  domain: string;
  raw_fields: string;
  canonical_fields: string;
  missing_value_policy: string;
  unit_guard: string;
}

export interface OutboxChannelItem {
  event_type: string;
  producer_wired: boolean;
  total_emitted: number;
  last_emitted_at: string | null;
}

export interface NexusIntegrityAudit {
  audit_version: string;
  overall_status: 'healthy' | 'warnings_present' | 'degraded';
  benchmark_standard: BenchmarkStandardSpec;
  entrypoint_accounting_parity: EntrypointParityItem[];
  source_normalization_matrix: SourceNormalizationItem[];
  outbox_event_channels: OutboxChannelItem[];
  database_health: {
    nexus_tables_ready: boolean;
    workbench_cache_rows: number;
    cross_verified_clusters: number;
    realtime_adjustments_rows: number;
    arena_ratings_rows: number;
    discovered_candidates_rows: number;
    performance_indexes_ready: boolean;
  };
}

const BASE = '/v1/x/bf/nexus';

export const nexusOps = {
  hub: (q: { track?: HorizonTrack; limit?: number; release_id?: string | null } = {}, signal?: AbortSignal) =>
    extGet<NexusHubOverview>(`${BASE}/hub`, q, signal),
  sectorDeepDive: (nodeId: string, q: { taxonomy_id?: string; horizon_id?: string; release_id?: string | null } = {}, signal?: AbortSignal) =>
    extGet<NexusSectorDeepDive>(`${BASE}/sectors/${encodeURIComponent(nodeId)}/deep-dive`, q, signal),
  stockPro: (
    instrumentId: string,
    q: {
      timeframe?: string;
      adjustment?: string;
      release_id?: string | null;
      base_shares?: number;
      available_cash?: number;
      grid_mode?: GridMode;
    } = {},
    signal?: AbortSignal,
  ) => extGet<NexusStockProAnalysis>(`${BASE}/stocks/${encodeURIComponent(instrumentId)}/pro`, q, signal),
  arena: (q: { layer?: string; horizon_bucket?: string; paradigm?: string; role?: string } = {}, signal?: AbortSignal) =>
    extGet<NexusArenaOverview>(`${BASE}/arena`, q, signal),
  integrity: (signal?: AbortSignal) => extGet<NexusIntegrityAudit>(`${BASE}/integrity`, {}, signal),
};

export const TV_LABEL_TEXT: Record<RatingLabel, string> = {
  strong_buy: '强力买入',
  buy: '买入',
  neutral: '中性',
  sell: '卖出',
  strong_sell: '强力卖出',
  unavailable: '预热/缺失',
};

export const TV_LABEL_TONE: Record<RatingLabel, 'good' | 'warn' | 'bad' | 'neutral'> = {
  strong_buy: 'good',
  buy: 'good',
  neutral: 'neutral',
  sell: 'bad',
  strong_sell: 'bad',
  unavailable: 'warn',
};

export const SECTOR_ROLE_TEXT: Record<SectorRole, string> = {
  leader_1: '板块龙一',
  leader_2: '板块龙二',
  core_capacity: '容量中军',
  pioneer: '异动先锋',
  catchup: '低位补涨',
  member: '板块成员',
};

export const VERDICT_TEXT: Record<NexusStockProAnalysis['action_plan']['verdict'], { label: string; tone: 'good' | 'warn' | 'bad' | 'neutral' }> = {
  strong_priority: { label: '重点优选 · 右侧共振', tone: 'good' },
  accumulate_on_dip: { label: '回踩低吸 · 分批建仓', tone: 'good' },
  hold_and_grid_t: { label: '持仓网格 / 底仓做T降本', tone: 'neutral' },
  watch_wait: { label: '观察等待 · 暂不出手', tone: 'warn' },
  risk_avoid: { label: '风控一票否决 · 规避', tone: 'bad' },
};

export const LAYER_TEXT: Record<StrategyTaxonomyEntry['layer'], string> = {
  regime_timing: '大盘择时与风控闸门',
  sector_rotation: '板块轮动与主线捕捉',
  stock_selection: '截面选股 Alpha',
  intraday_execution: '持仓日内做T与网格执行',
};

export const PARADIGM_TEXT: Record<StrategyTaxonomyEntry['paradigm'], string> = {
  top_paper_2026: '2025-2026顶会论文算子',
  multi_factor_fusion: '多因子ICIR融合',
  classic_factor: '经典因子与红利价值',
  technical_pattern: '经典量价技术战法',
  sentiment_microstructure: '情绪与微观结构',
  hot_money_seat: '游资接力与席位博弈',
  grid_intraday_t: '底仓做T与自适应网格',
  rl_distributional: '强化学习与分布分位',
  llm_hybrid: '受控大模型新闻混合',
};

export const HORIZON_BUCKET_TEXT: Record<StrategyTaxonomyEntry['horizon_bucket'], string> = {
  intraday_t0: '日内 T+0 降本',
  ultra_short_1_3d: '超短 1~3日',
  short_5d: '短线 5日',
  swing_10_20d: '波段 10~20日',
  position_60d: '中长线 20~60日',
};

export const LIFECYCLE_TIER_TEXT: Record<StrategyTaxonomyEntry['lifecycle_tier'], { label: string; tone: 'good' | 'warn' | 'bad' | 'neutral' }> = {
  champion_active: { label: '簇冠军 · 实盘投票', tone: 'good' },
  promoted_active: { label: '瑞士轮晋级 · 激活', tone: 'good' },
  candidate_shadow: { label: '影子观察池', tone: 'neutral' },
  observing_trial: { label: '试运行观察', tone: 'neutral' },
  demoted_paused: { label: '回撤降级暂停', tone: 'warn' },
  retired_falsified: { label: '实证证伪淘汰', tone: 'bad' },
};

export const DISCOVERED_GATE_TEXT: Record<DiscoveredCandidateItem['gate_verdict'], { label: string; tone: 'good' | 'warn' | 'bad' | 'neutral' }> = {
  admitted_shadow: { label: '准入影子池', tone: 'good' },
  rejected_redundant: { label: '相关性过高淘汰', tone: 'warn' },
  rejected_turnover_rre: { label: '换手过快(RRE)淘汰', tone: 'bad' },
  rejected_fragile_pfs: { label: '扰动脆弱(PFS)淘汰', tone: 'bad' },
  rejected_negative_net: { label: '扣费净收益为负淘汰', tone: 'bad' },
  pending_eval: { label: '待评估', tone: 'neutral' },
};

export function fmtVal(v: string | number | null | undefined, suffix = ''): string {
  if (v === null || v === undefined || v === '') return '缺失';
  return `${v}${suffix}`;
}
