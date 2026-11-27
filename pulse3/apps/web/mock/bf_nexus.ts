/**
 * Synthetic mock handler for /v1/x/bf/nexus/* so dev:mock and build:mock
 * render 100% contract-valid Nexus 3.5 payloads matching backfill_nexus.schema.json (v2.0.0).
 */
import type {
  BenchmarkStandardSpec,
  NexusArenaOverview,
  NexusHubOverview,
  NexusIntegrityAudit,
  NexusSectorDeepDive,
  NexusStockProAnalysis,
} from '../src/pages/bf/nexus/api.ts';
import { INSTRUMENTS, instrumentById, type ReleaseRec } from './market.ts';
import { buildMeta, errorResponse, iso, type MockResponse } from './util.ts';

const MOCK_BENCHMARK_STANDARD: BenchmarkStandardSpec = {
  standard_id: 'GIPS-AShare-2026-v1',
  execution_timing: 'T日收盘锁定PIT信号 -> T+1日开盘成交（100股整手，严格T+1锁仓，2026-07-06 主板 ST 调为 ±10%；双创 20%；北交所 30%）',
  stamp_duty_bps: '5.0',
  commission_bps: '2.5',
  min_commission_cny: '5.0',
  transfer_fee_bps: '0.1',
  slippage_model: 'Half-spread (2.5 bps) + Almgren-Chriss eta * sigma_d * sqrt(order_shares / bar_volume)',
  limit_queue_rule: '一字涨停 0% 成交；封板 0/30%/100% 三档排队敏感性；开板 100% 成交；跌停顺延 <= 8 日',
  validation_protocol: 'AlphaEval RRE/PFS -> Unified Engine 1x/2x/Queue -> E062 Excess & E082 NW-t -> MetaTrader CVaR & FINSABER -> AlphaSAGE |corr|<0.70',
};

export function buildMockNexusHub(
  r: ReleaseRec | null,
  track: 'composite' | 'short' | 'swing' | 'long' = 'composite',
): NexusHubOverview {
  const inst0 = INSTRUMENTS[0]!;
  const inst1 = INSTRUMENTS[1]!;
  const asOfIso = r ? iso(r.decisionAsOf) : null;

  const macroMatrix: NexusHubOverview['macro_factor_matrix'] = [
    {
      dimension: 'domestic_policy_macro',
      factor_name: '国内政策定调与2025新口径M1剪刀差 (权重25%)',
      status: 'observed',
      signal: 'supportive',
      detail: '纳入个人活期与非银备付金后活化资金跟踪更灵敏；2026-07-06 主板 ST 调为 ±10%；双创 20%；北交所 30%。[实证锚点: E070/E088/20日净值均线]',
      source_authority: '新华社 / 人民银行 / 证监会 / 巨潮资讯 (Tier-1)',
    },
    {
      dimension: 'global_linkage',
      factor_name: '美联储FOMC / 美债10Y / 金龙指数 / 离岸人民币 (权重15%)',
      status: 'observed',
      signal: 'neutral',
      detail: '隔夜美股09:25集合竞价一次定价完成，无日内额外隔夜溢出偏差。[实证锚点: E072/E083/E084/E101]',
      source_authority: '中金所 / 腾讯国际行情 / 东方财富宏观日历 (eastmoney_calendar)',
    },
    {
      dimension: 'capital_proxy_2026',
      factor_name: '北向日度停更后五大高频资金替代锚 (权重25%)',
      status: 'observed',
      signal: 'supportive',
      detail: '沪深300/A500宽基ETF申赎脉冲与大单净流差联合观测。[实证锚点: E074/E089/JFQA 2025大小单差]',
      source_authority: '交易所ETF快照 / 东方财富资金流 (eastmoney_fund_flow)',
    },
    {
      dimension: 'retail_sentiment',
      factor_name: '股吧四时段SIR情绪传染与热度校验 (权重15%)',
      status: 'partial',
      signal: 'neutral',
      detail: '自动去重模板水军并对Top5%过热拥挤降权。[实证锚点: PBFJ 2024 SIR传染模型]',
      source_authority: '东方财富股吧 (eastmoney_guba_comments) / 同花顺7x24 / 财联社电报',
    },
    {
      dimension: 'microstructure_seats',
      factor_name: '龙虎榜17大游资谱系与四维真伪机构鉴别 (权重20%)',
      status: 'observed',
      signal: 'neutral',
      detail: '兼容国泰海通2025-04更名与六一弃用警示，拦截拉萨8席踩踏与量化假机构。[实证锚点: E048/E094/E098]',
      source_authority: '东财龙虎榜 (eastmoney_lhb_disclosure) / Pulse席位博弈库',
    },
  ];

  const pollingStatus: NexusHubOverview['source_polling_status'] = {
    total_sources: 4,
    healthy_count: 3,
    degraded_count: 1,
    silent_warning_count: 0,
    host_rate_limit_rps: '1.0',
    sources: [
      {
        source_id: 'xinhua_finance',
        tier: 'tier1_official',
        runtime_state: 'healthy',
        freshness_state: 'current',
        last_success_at: asOfIso,
        fallback_group: 'news_authority',
      },
      {
        source_id: 'cninfo_announcements',
        tier: 'tier1_official',
        runtime_state: 'healthy',
        freshness_state: 'current',
        last_success_at: asOfIso,
        fallback_group: 'statutory_disclosure',
      },
      {
        source_id: 'cls_telegraph',
        tier: 'tier2_financial_press',
        runtime_state: 'healthy',
        freshness_state: 'current',
        last_success_at: asOfIso,
        fallback_group: 'news_fast_724',
      },
      {
        source_id: 'eastmoney_guba_comments',
        tier: 'tier3_portal_social',
        runtime_state: 'awaiting_first_poll',
        freshness_state: 'unknown',
        last_success_at: null,
        fallback_group: 'retail_community',
      },
    ],
  };

  if (!r) {
    return {
      release_id: null,
      decision_as_of: null,
      horizon_track: track,
      normalization_rule: 'All-Voter Weight Normalization: sum(w_v * pct_v) / sum_{all active voters}(w_v); unpicked=0',
      arena_weight_source: 'swiss_glicko_empirical_ram_v1',
      market_regime: {
        code: 'awaiting_release',
        label: '等待首次分析发布',
        curve_gate_active: false,
        recommended_exposure_pct: '0%',
        summary: '当前尚无已发布分析快照；运行 analysis daily 后自动生成全量正交推荐榜与升温备选池。',
      },
      macro_factor_matrix: macroMatrix,
      source_polling_status: pollingStatus,
      cross_verified_flashes: [],
      active_realtime_adjustments: [],
      picks: [],
      rapid_rising_sectors: [],
      candidate_watch_pool: [],
    };
  }

  return {
    release_id: r.id,
    decision_as_of: asOfIso,
    horizon_track: track,
    normalization_rule: 'All-Voter Weight Normalization: sum(w_v * pct_v) / sum_{all active voters}(w_v); unpicked=0',
    arena_weight_source: 'swiss_glicko_empirical_ram_v1',
    market_regime: {
      code: 'structural_rotation',
      label: '结构性主线轮动 · 20日净值均线风控护航',
      curve_gate_active: false,
      recommended_exposure_pct: '75%',
      summary: '合成样本：基于GIPS-AShare-2026-v1统一基准与瑞士轮冠军簇正交合成。',
    },
    macro_factor_matrix: macroMatrix,
    source_polling_status: pollingStatus,
    cross_verified_flashes: [
      {
        cluster_id: 'cl-mock-1',
        headline: '新华社与巨潮交叉核验：科技自主创新与并购重组提质增效',
        verification_state: 'official_confirmed',
        corroboration_count: 2,
        confidence_score: '0.96',
        sources: ['xinhua_finance', 'cninfo_announcements'],
        polarity: 'positive',
        affected_sectors: ['合成板块·半导体'],
        published_at: asOfIso,
      },
      {
        cluster_id: 'cl-mock-2',
        headline: '财联社与东财印证：新能源储能订单排期稳步提升',
        verification_state: 'multi_source_verified',
        corroboration_count: 2,
        confidence_score: '0.84',
        sources: ['cls_telegraph', 'eastmoney_news_724'],
        polarity: 'positive',
        affected_sectors: ['合成板块·新能源'],
        published_at: asOfIso,
      },
    ],
    active_realtime_adjustments: [
      {
        adjustment_id: 'raptor-adj-1',
        target_scope: 'instrument',
        target_key: inst0.symbol,
        tilt_bps: '85.0',
        action_directive: 'catalyst_boost',
        llm_model_used: 'gpt-6-luna',
        summary: '法定公告与Tier-1央媒交叉确认正面订单催化，在+-15%安全边界内上调权重85 bps。',
        available_at: asOfIso,
      },
    ],
    picks: [
      {
        rank: 1,
        instrument_id: inst0.id,
        symbol: inst0.symbol,
        display_name: inst0.name,
        sector_node: '合成板块·新能源',
        sector_role: 'leader_1',
        ensemble_score: '86.40',
        voter_support_count: 5,
        total_active_voters: 5,
        entry_low: '18.20',
        entry_high: '18.55',
        stop_loss: '17.30',
        target_price: '20.40',
        holding_horizon: '5~15交易日（多策略正交共振）',
        tags: [track.toUpperCase(), '共振5/5'],
        supporting_strategies: ['fusion_static40_safe', 'paper_icml2025_alphaqcm_tail'],
        risk_flags: [],
        catalyst_summary: '由 fusion_static40_safe, paper_icml2025_alphaqcm_tail 等 5 套低相关冠军策略按全体投票者权重归一共振入选',
      },
      {
        rank: 2,
        instrument_id: inst1.id,
        symbol: inst1.symbol,
        display_name: inst1.name,
        sector_node: '合成板块·半导体',
        sector_role: 'core_capacity',
        ensemble_score: '81.15',
        voter_support_count: 4,
        total_active_voters: 5,
        entry_low: '32.10',
        entry_high: '32.70',
        stop_loss: '30.50',
        target_price: '36.00',
        holding_horizon: '5~15交易日（多策略正交共振）',
        tags: [track.toUpperCase(), '共振4/5'],
        supporting_strategies: ['paper_ms2025_lot_momentum', 'paper_aaai2024_sector_hypergraph'],
        risk_flags: [],
        catalyst_summary: '由 paper_ms2025_lot_momentum 等 4 套冠军策略按全体投票者权重归一共振入选',
      },
    ],
    rapid_rising_sectors: [
      {
        rank: 1,
        taxonomy_id: 'industry',
        node_id: '合成板块·半导体',
        surge_score: '0.882',
        velocity_label: 'rapid_promotion',
        member_count: 18,
        present_components: ['relative_strength_change', 'turnover_acceleration', 'breadth', 'leader_confirmation'],
        missing_components: [],
        watch_candidates: [{ instrument_id: inst1.id, symbol: inst1.symbol, display_name: inst1.name, role: 'pioneer', pool_state: 'actionable', score: '0.88' }],
      },
    ],
    candidate_watch_pool: [
      {
        instrument_id: inst1.id,
        symbol: inst1.symbol,
        display_name: inst1.name,
        sector_node: '合成板块·半导体',
        pool_state: 'actionable',
        trigger_code: 'SECTOR_SURGE_EARLY_LOCK',
        entry_hint: '已通过升温驻留与交易计划校验，可进入工作台执行低吸',
      },
    ],
  };
}

export function buildMockSectorDeepDive(
  r: ReleaseRec,
  nodeId: string,
  taxonomyId = 'industry',
  horizonId = 'short_term',
): NexusSectorDeepDive {
  const inst0 = INSTRUMENTS[0]!;
  const inst1 = INSTRUMENTS[1]!;
  return {
    release_id: r.id,
    taxonomy_id: taxonomyId,
    node_id: nodeId,
    horizon_id: horizonId,
    is_rapidly_rising: true,
    surge_score: '0.845',
    surge_components: [
      { key: 'relative_strength_change', label: '相对强度跃升', value: '0.845', status: 'ready' },
      { key: 'turnover_acceleration', label: '换手率加速度', value: '0.792', status: 'ready' },
      { key: 'breadth', label: '成分扩散广度', value: '0.710', status: 'ready' },
      { key: 'leader_confirmation', label: '前排龙头确认', value: '0.880', status: 'ready' },
      { key: 'news_novelty', label: '多源新催化密度', value: '2.0', status: 'ready' },
      { key: 'community_growth', label: '四时段讨论增速', value: null, status: 'missing' },
      { key: 'fund_proxy', label: '主力资金净流代理', value: null, status: 'missing' },
    ],
    top_picks: [
      {
        rank: 1,
        instrument_id: inst0.id,
        symbol: inst0.symbol,
        display_name: inst0.name,
        sector_node: nodeId,
        sector_role: 'leader_1',
        ensemble_score: '88.20',
        voter_support_count: 5,
        total_active_voters: 5,
        entry_low: '18.20',
        entry_high: '18.55',
        stop_loss: '17.30',
        target_price: '20.40',
        holding_horizon: '5~10个交易日',
        tags: ['板块龙一'],
        supporting_strategies: ['fusion_static40_safe'],
        risk_flags: [],
        catalyst_summary: '板块内相对强度排名 #1（板块龙一）',
      },
      {
        rank: 2,
        instrument_id: inst1.id,
        symbol: inst1.symbol,
        display_name: inst1.name,
        sector_node: nodeId,
        sector_role: 'core_capacity',
        ensemble_score: '82.50',
        voter_support_count: 4,
        total_active_voters: 5,
        entry_low: '32.10',
        entry_high: '32.70',
        stop_loss: '30.50',
        target_price: '36.00',
        holding_horizon: '5~10个交易日',
        tags: ['容量中军'],
        supporting_strategies: ['paper_aaai2024_sector_hypergraph'],
        risk_flags: [],
        catalyst_summary: '板块内大容量中军趋势承接',
      },
    ],
    watch_pool_members: [
      {
        instrument_id: inst1.id,
        symbol: inst1.symbol,
        display_name: inst1.name,
        pool_state: 'actionable',
        role: 'actionable_leader',
        trigger_code: 'SECTOR_SURGE_EARLY_LOCK',
        plan_ready: true,
      },
    ],
    sector_news: [
      `${nodeId} 产业链高景气订单多源交叉确认（Tier-1/2）`,
    ],
  };
}

function buildMockCompactBars(basePrice: number, count = 20): NexusStockProAnalysis['kline_bars'] {
  const out: NexusStockProAnalysis['kline_bars'] = [];
  let px = basePrice * 0.93;
  // Build deterministic trading dates ending at 2026-11-26
  const dates: string[] = [];
  let cursor = Date.UTC(2026, 10, 26);
  while (dates.length < count) {
    const dt = new Date(cursor);
    const wd = dt.getUTCDay();
    if (wd !== 0 && wd !== 6) dates.push(dt.toISOString().slice(0, 10));
    cursor -= 86_400_000;
  }
  dates.reverse();

  for (let i = 0; i < dates.length; i += 1) {
    const o = px;
    const step = i % 3 === 0 ? 0.014 : i % 3 === 1 ? -0.005 : 0.008;
    const c = Math.max(1, o * (1 + step));
    const h = Math.max(o, c) * 1.009;
    const l = Math.min(o, c) * 0.992;
    px = c;
    out.push({
      trade_date: dates[i]!,
      open: o.toFixed(2),
      high: h.toFixed(2),
      low: l.toFixed(2),
      close: c.toFixed(2),
      volume: String(1200000 + i * 85000),
    });
  }
  return out;
}

export function buildMockStockPro(
  r: ReleaseRec,
  iid: string,
  tf = '1d',
  gridMode: 'atr_adaptive' | 'geometric' | 'arithmetic' = 'atr_adaptive',
  adjustment = 'raw',
): NexusStockProAnalysis {
  const inst = instrumentById(iid) ?? { id: iid, symbol: '600888', name: '全市场覆盖样本股', base: 18.4, risk: false };
  const bars = buildMockCompactBars(inst.base ?? 18.4, 20);
  const markerDate = bars[Math.max(0, bars.length - 4)]!.trade_date;

  return {
    instrument_id: iid,
    symbol: inst.symbol,
    display_name: inst.name,
    board_code: 'main',
    release_id: r.id,
    decision_as_of: iso(r.decisionAsOf),
    cached_in_postgres: true,
    cache_key_hash: 'a'.repeat(64),
    timeframe: tf,
    adjustment_mode: adjustment,
    price_basis_note: adjustment === 'raw'
      ? '交易参考区间与网格挂单严格基于原始可交易价（raw）。'
      : `当前K线采用 ${adjustment} 复权视图；买卖与做T挂单仍以原始可交易价为准。`,
    kline_bars: bars,
    multi_timeframe_ratings: [
      {
        timeframe: '1d',
        bar_count: 120,
        last_bar_date: bars[bars.length - 1]!.trade_date,
        summary: { label: 'buy', score: '0.425', buy_count: 16, neutral_count: 6, sell_count: 4, total_count: 26 },
        moving_averages: {
          label: 'strong_buy',
          score: '0.533',
          buy_count: 11,
          neutral_count: 1,
          sell_count: 3,
          total_count: 15,
          items: [
            { name: 'EMA(20)', value: '18.1200', signal: 'buy' },
            { name: 'SMA(20)', value: '18.0400', signal: 'buy' },
            { name: 'SMA(200)', value: null, signal: 'unavailable' },
          ],
        },
        oscillators: {
          label: 'buy',
          score: '0.318',
          buy_count: 5,
          neutral_count: 5,
          sell_count: 1,
          total_count: 11,
          items: [
            { name: 'RSI(14)', value: '58.40', signal: 'neutral' },
            { name: 'MACD(12,26,9)', value: '0.2450', signal: 'buy' },
            { name: 'ADX(14)', value: '28.50', signal: 'buy' },
          ],
        },
      },
      {
        timeframe: '1w',
        bar_count: 52,
        last_bar_date: bars[bars.length - 1]!.trade_date,
        summary: { label: 'buy', score: '0.310', buy_count: 14, neutral_count: 7, sell_count: 5, total_count: 26 },
        moving_averages: { label: 'buy', score: '0.400', buy_count: 9, neutral_count: 3, sell_count: 3, total_count: 15, items: [] },
        oscillators: { label: 'buy', score: '0.220', buy_count: 5, neutral_count: 4, sell_count: 2, total_count: 11, items: [] },
      },
      {
        timeframe: '1mo',
        bar_count: 24,
        last_bar_date: bars[bars.length - 1]!.trade_date,
        summary: { label: 'buy', score: '0.180', buy_count: 11, neutral_count: 8, sell_count: 4, total_count: 23 },
        moving_averages: { label: 'buy', score: '0.250', buy_count: 7, neutral_count: 3, sell_count: 2, total_count: 12, items: [] },
        oscillators: { label: 'buy', score: '0.110', buy_count: 4, neutral_count: 5, sell_count: 2, total_count: 11, items: [] },
      },
      {
        timeframe: '1y',
        bar_count: 4,
        last_bar_date: bars[bars.length - 1]!.trade_date,
        summary: { label: 'unavailable', score: null, buy_count: 0, neutral_count: 0, sell_count: 0, total_count: 0 },
        moving_averages: { label: 'unavailable', score: null, buy_count: 0, neutral_count: 0, sell_count: 0, total_count: 0, items: [] },
        oscillators: { label: 'unavailable', score: null, buy_count: 0, neutral_count: 0, sell_count: 0, total_count: 0, items: [] },
      },
    ].map((rt) => ({
      ...rt,
      summary: { ...rt.summary, items: [] },
    })) as NexusStockProAnalysis['multi_timeframe_ratings'],
    mtf_resonance_label: 'buy',
    mtf_resonance_score: '0.342',
    pivot_points: [
      { method: 'traditional', p: '18.40', r1: '18.85', r2: '19.20', r3: '19.65', r4: '20.10', s1: '18.05', s2: '17.60', s3: '17.15', s4: '16.70' },
      { method: 'classic', p: '18.40', r1: '18.85', r2: '19.20', r3: '19.65', r4: '20.10', s1: '18.05', s2: '17.60', s3: '17.15', s4: '16.70' },
      { method: 'fibonacci', p: '18.40', r1: '18.70', r2: '18.89', r3: '19.20', r4: null, s1: '18.10', s2: '17.91', s3: '17.60', s4: null },
      { method: 'camarilla', p: '18.40', r1: '18.52', r2: '18.60', r3: '18.68', r4: '18.92', s1: '18.38', s2: '18.30', s3: '18.22', s4: '17.98' },
      { method: 'woodie', p: '18.42', r1: '18.89', r2: '19.22', r3: '19.69', r4: '20.15', s1: '18.09', s2: '17.62', s3: '17.19', s4: '16.72' },
      { method: 'demark', p: '18.44', r1: '18.88', r2: null, r3: null, r4: null, s1: '18.08', s2: null, s3: null, s4: null },
    ],
    vpvr: {
      available: true,
      lookback_bars: 90,
      poc: '18.25',
      vah: '19.10',
      val: '17.75',
      winner_ratio: '0.742',
      avg_chip_cost: '18.18',
      concentration_90: '0.112',
      hvn_zones: ['18.00~18.50'],
      lvn_zones: ['19.10~19.40'],
      bins: [
        { low: '17.70', high: '18.00', mid: '17.85', volume_share: '0.1240', up_share: '0.550', is_value_area: true, is_poc: false, node_kind: 'normal' },
        { low: '18.00', high: '18.50', mid: '18.25', volume_share: '0.2480', up_share: '0.640', is_value_area: true, is_poc: true, node_kind: 'poc' },
        { low: '18.50', high: '19.10', mid: '18.80', volume_share: '0.1420', up_share: '0.580', is_value_area: true, is_poc: false, node_kind: 'hvn' },
        { low: '19.10', high: '19.40', mid: '19.25', volume_share: '0.0480', up_share: '0.450', is_value_area: false, is_poc: false, node_kind: 'lvn' },
      ],
    },
    pro_signals: {
      supertrend_dir: 'bullish',
      supertrend_level: '17.68',
      volatility_squeeze: 'squeeze_fired_up',
      bb_kc_squeeze_on: false,
      squeeze_momentum: '0.3820',
      adx_14: '28.50',
      anchored_vwap_20d: '18.19',
      rvol_20d: '1.42',
      macd_divergence: 'bullish_bottom_div',
      rsi_divergence: 'none',
      volume_patterns: ['倍量阳柱突破', '缩量回踩筹码峰'],
      atr_14: '0.48',
    },
    chart_lines: [
      { id: 'nx-entry-lo', price: '18.25', title: 'Nexus低吸下沿(POC)', tone: 'plan', dashed: true },
      { id: 'nx-stop', price: '17.39', title: '风控止损线(1.8ATR)', tone: 'risk', dashed: false },
      { id: 'nx-tp1', price: '19.46', title: '第一目标止盈(TP1)', tone: 'exit', dashed: true },
      { id: 'nx-poc', price: '18.25', title: 'VPVR筹码控制点(POC)', tone: 'draw', dashed: true },
    ],
    chart_markers: [
      { time: markerDate, position: 'belowBar', shape: 'arrowUp', tone: 'buy', text: 'SuperTrend转多' },
    ],
    action_plan: {
      verdict: 'strong_priority',
      composite_score: '74.8',
      entry_low: '18.25',
      entry_high: '18.59',
      stop_loss: '17.39',
      target_1: '19.46',
      target_2: '20.18',
      holding_sessions: '5~10个交易日（配合正T降本）',
      max_position_pct: '10%',
      risk_vetoes: [],
      pillar_scores: { technical: '71.2', capital_flow: '68.0', news_catalyst: '80.0', sentiment_micro: '78.0' },
      rationale: ['日/周/月三周期TradingView评级共振看多', 'VPVR筹码峰POC=18.25构成强支撑，获利盘74.2%'],
    },
    realtime_adjustment: {
      tilt_bps: '85.0',
      action_directive: 'catalyst_boost',
      liquidity_filter_triggered: false,
      risk_veto_reasons: [],
      llm_model_used: 'gpt-6-luna',
      llm_status: 'ok_bounded',
      guard_message: 'RAPTOR 实时核验通过（受控模型 gpt-6-luna），在 +-15% 边界内温和上调催化权重 +85 bps。',
      bull_points: ['法定公告确认长单签署', 'Tier-1 官媒政策主题同向共振'],
      bear_points: ['忌早盘高开逾 5% 追高'],
      holding_plan: '沿 5 日均线及 VPVR POC 支撑位持仓配合正T降本。',
    },
    intraday_t_plan: {
      archetype_id: 'arch_vwap_dual_t',
      archetype_name: '震荡箱体·A-S库存偏斜双边做T',
      paper_ref: 'ArchetypeTrader (AAAI 2026) + Avellaneda-Stoikov (2008) + ICAIF 2026 Guard',
      recommended_direction: 'forward_t',
      reference_price: '18.45',
      reservation_price: '18.42',
      forward_buy_price: '18.12',
      forward_sell_price: '18.78',
      reverse_sell_price: '18.74',
      reverse_buyback_price: '18.10',
      abort_stop_price: '17.65',
      base_sellable_shares: 1000,
      available_cash_cny: '50000.00',
      recommended_lots: 3,
      recommended_shares: 300,
      round_trip_fee_bps: '11.4',
      min_profitable_spread_pct: '0.45',
      expected_cost_reduction_pct: '1.68',
      cash_non_overdraft_verified: true,
      t1_lock_verified: true,
      crumble_risk_level: 'low',
      neurosymbolic_guards: {
        all_passed: true,
        crumble_risk_level: 'low',
        spread_to_cost_multiple: '3.20',
        violations: [],
        checks: [
          { guard_id: 't1_settlement_guard', passed: true, detail: 'A股T+1约束：日内卖腿仅使用T-1已结算底仓' },
          { guard_id: 'no_overdraft_guard', passed: true, detail: '现金不透支硬约束：仓位占用<=100%，禁用未到账卖出款' },
          { guard_id: 'fee_hurdle_guard', passed: true, detail: '费率护栏：目标价差/往返全成本 = 3.20x (须>2.20x)' },
          { guard_id: 'liquidity_crumble_guard', passed: true, detail: 'ICAIF 2026流动性溃散评估: low' },
        ],
      },
      execution_notes: ['严格锁定可卖老底仓300股，杜绝T+1加仓被套'],
      backtest_summary: {
        sessions_tested: 45,
        triggered_days: 7,
        win_rate: '71.4',
        cum_extra_return_pct: '+2.64',
        vs_hold_delta_pct: '+2.64',
      },
    },
    grid_plan: {
      mode: gridMode,
      anchor_price: '18.45',
      lower_bound: '17.50',
      upper_bound: '19.40',
      upper_circuit_breaker: '19.85',
      lower_stop_breaker: '17.10',
      grid_count: 6,
      step_pct: '2.10',
      per_grid_lots: 2,
      per_grid_shares: 200,
      base_locked_shares: 1000,
      rungs: [
        { level_index: -1, trigger_price: '18.06', action: 'buy_dip', shares: 200, target_pair_price: '18.45', est_net_profit_cny: '72.40' },
        { level_index: 0, trigger_price: '18.45', action: 'anchor', shares: 1000, target_pair_price: null, est_net_profit_cny: null },
        { level_index: 1, trigger_price: '18.84', action: 'sell_rally', shares: 200, target_pair_price: '18.45', est_net_profit_cny: '71.80' },
      ],
      crumble_risk_level: 'low',
      neurosymbolic_guards: {
        all_passed: true,
        crumble_risk_level: 'low',
        spread_to_cost_multiple: '3.10',
        violations: [],
        checks: [
          { guard_id: 'no_overdraft_guard', passed: true, detail: '现金不透支校验通过' },
          { guard_id: 'lot_affordability_guard', passed: true, detail: '100股整手门槛与预算安全上限(<=60%)校验通过' },
        ],
      },
      budget_status_note: '整手资金校验通过：底仓 1000 股，每格 200 股，总占用受100%硬顶约束。',
      backtest: {
        sessions_tested: 75,
        completed_round_trips: 6,
        grid_net_profit_cny: '438.60',
        cost_reduction_per_share_cny: '1.080',
        cost_reduction_pct: '5.85',
        vs_buy_hold_excess_pct: '+2.94',
        max_capital_usage_pct: '64.2',
      },
    },
    retail_resonance: {
      total_posts: 34,
      template_spam_ratio: '0.088',
      abnormal_attention_z: '1.15',
      sir_contagion_slope: '+0.04',
      crowd_state: 'healthy_divergence',
      sessions: [
        { session_bucket: 'pre_market', label: '盘前预期 (08:00-09:25)', post_count: 6, unique_authors: 5, bullish_ratio: '0.67', divergence_index: '0.66', dominant_topic: '讨论基本面业绩与回购催化', heat_state: 'normal' },
        { session_bucket: 'intraday', label: '盘中博弈 (09:30-11:30 / 13:00-15:00)', post_count: 14, unique_authors: 11, bullish_ratio: '0.57', divergence_index: '0.86', dominant_topic: '散户热议「主力洗盘/拿住锁仓」', heat_state: 'warming' },
        { session_bucket: 'midday_break', label: '午间休盘发酵 (11:30-13:00)', post_count: 5, unique_authors: 4, bullish_ratio: '0.60', divergence_index: '0.80', dominant_topic: '常规盘面强弱与消息面讨论', heat_state: 'normal' },
        { session_bucket: 'post_market', label: '盘后复盘定调 (15:00-次日08:00)', post_count: 9, unique_authors: 8, bullish_ratio: '0.66', divergence_index: '0.68', dominant_topic: '聚焦「游资锁仓与龙虎榜席位博弈」', heat_state: 'warming' },
      ],
      claim_checks: [
        { retail_claim: '评论区讨论「缩量回踩均线属良性换手洗盘」', verdict: 'supported_by_facts', objective_evidence: 'RVOL=1.42温和放量且买榜无拉萨散户席位扎堆，量价健康' },
      ],
      synthesized_judgment: '四时段讨论处于温和升温良性分歧区（Z=1.15），未达PBFJ过热反转阈值，散户洗盘说法获量价数据印证。',
    },
    seat_battle: {
      has_recent_lhb: true,
      report_date: '2026-11-25',
      lhasa_buyer_count: 0,
      one_day_raider_count: 0,
      synergy_leader_count: 1,
      quant_seat_count: 0,
      net_buy_amount_cny: '86500000.00',
      lhasa_ratio: '0.000',
      battle_verdict: 'synergy_bullish',
      fake_institution_check: {
        institution_present: true,
        verdict: 'real_institution_likely',
        risk_score: '0.10',
        reasons: ['中大市值容量标的且无高频一日游/量化同框，倾向真实机构配置通道'],
      },
      seats: [
        {
          seat_name: '中国银河证券股份有限公司大连黄河路证券营业部',
          side: 'buy',
          category: 'pattern_leader_synergy',
          net_amount_cny: '52000000.00',
          persona_code: 'hotmoney_chen_xiaoqun',
          persona_label: '陈小群常用席位（主线总龙头格局接力）',
          style_summary: '主线情绪核心容量标的格局锁仓型游资，次日溢价率高',
          empirical_d1_gap_pct: '+1.80',
          empirical_d5_excess_pct: '+0.65',
          counter_tactic: '处于主升初期且多路格局资金合力时可顺势持有看溢价',
        },
      ],
      opponent_strategy: '顶级格局游资与真机构同向锁仓，未见拉萨天团或一日游砸盘席位，可沿5日线与POC支撑顺势持有。',
    },
    verified_news: [
      {
        cluster_id: 'cl-stock-1',
        headline: '法定公告核验：核心产线良率提升与客户长单签署',
        tier: 'tier1_official',
        verification_state: 'official_confirmed',
        sources: ['cninfo_announcements'],
        polarity: 'positive',
        published_at: iso(r.decisionAsOf),
        bull_bear_note: '多头视角：法定公告实锤业绩增量；空头风控：忌高开逾5%追涨。',
      },
    ],
  };
}

export function buildMockArena(): NexusArenaOverview {
  const champ: NexusArenaOverview['strategies'][number] = {
    strategy_code: 'fusion_static40_safe',
    display_name: 'Level-Safe 392因子扩展窗口ICIR融合主书 (E012/E022/E036)',
    layer: 'stock_selection',
    horizon_bucket: 'short_5d',
    paradigm: 'multi_factor_fusion',
    regime_fit: ['bull_mainline', 'structural_rotation', 'range_chop', 'defensive_bear'],
    portfolio_role: 'core_alpha',
    cluster_id: 'cluster_multifactor_core',
    is_cluster_champion: true,
    glicko_mu: '1712.40',
    glicko_phi: '42.10',
    glicko_sigma: '0.06000',
    conservative_score: '1628.20',
    swiss_wins: 4,
    swiss_draws: 0,
    swiss_losses: 0,
    net_return_pct: '18.4200',
    excess_return_pct: '11.2500',
    sharpe_ratio: '1.8400',
    max_drawdown_pct: '8.6500',
    nw_t_stat: '3.1200',
    worst_cvar_pct: '-2.1400',
    rre_stability: '0.860',
    pfs_robustness: '0.940',
    evaluation_source: 'gips_ashare_2026_v1_empirical',
    lifecycle_tier: 'champion_active',
    ensemble_weight: '0.2800',
    ram_floor_weight: '0.0500',
    paper_citation: 'AlphaGen (KDD 2023) + Pulse Ledger E012/E022/E036',
    summary: '剔除36个价格水平前视因子后的392因子扩展窗ICIR加权主书，配20日净值均线0.3降仓闸门。',
  };
  const paperStrat: NexusArenaOverview['strategies'][number] = {
    strategy_code: 'paper_icml2025_alphaqcm_tail',
    display_name: '两融彩票防雷与低波稳健复合 (JF 2026 + ICML 2025 AlphaQCM 启发)',
    layer: 'stock_selection',
    horizon_bucket: 'swing_10_20d',
    paradigm: 'top_paper_2026',
    regime_fit: ['range_chop', 'defensive_bear', 'structural_rotation'],
    portfolio_role: 'defensive_shield',
    cluster_id: 'cluster_low_risk_tail',
    is_cluster_champion: true,
    glicko_mu: '1678.00',
    glicko_phi: '48.50',
    glicko_sigma: '0.06000',
    conservative_score: '1581.00',
    swiss_wins: 3,
    swiss_draws: 1,
    swiss_losses: 0,
    net_return_pct: '14.1800',
    excess_return_pct: '7.0100',
    sharpe_ratio: '1.6200',
    max_drawdown_pct: '6.1200',
    nw_t_stat: '2.7500',
    worst_cvar_pct: '-1.6800',
    rre_stability: '0.910',
    pfs_robustness: null,
    evaluation_source: 'gips_ashare_2026_v1_empirical',
    lifecycle_tier: 'promoted_active',
    ensemble_weight: '0.2400',
    ram_floor_weight: '0.0500',
    paper_citation: 'Journal of Finance (Aug 2026) + ICML 2025 AlphaQCM',
    summary: '融合两融拥挤度与MAX彩票脉冲避雷(JF 2026)及低波稳健约束，AlphaQCM分布式RL独立用于公式探索。',
  };
  return {
    benchmark_standard: MOCK_BENCHMARK_STANDARD,
    season_id: '2026-S4-NEXUS',
    evaluation_basis: 'empirical_backtest_gips_v1',
    sessions_evaluated: 120,
    rounds_completed: 5,
    total_strategies: 2,
    orthogonal_clusters: 2,
    taxonomy_facets: {
      layers: ['stock_selection', 'sector_rotation', 'regime_timing', 'intraday_execution'],
      horizons: ['intraday_t0', 'ultra_short_1_3d', 'short_5d', 'swing_10_20d', 'position_60d'],
      paradigms: ['top_paper_2026', 'multi_factor_fusion', 'classic_factor', 'technical_pattern', 'sentiment_microstructure', 'hot_money_seat', 'grid_intraday_t', 'rl_distributional', 'llm_hybrid'],
      roles: ['core_alpha', 'defensive_shield', 'risk_veto', 'cost_reducer', 'exploratory_shadow'],
    },
    strategies: [champ, paperStrat],
    recent_pairings: [
      {
        round_no: 5,
        left_code: 'fusion_static40_safe',
        right_code: 'paper_icml2025_alphaqcm_tail',
        winner: 'left',
        score_delta: '+1.420',
        reason: 'GIPS-AShare-2026-v1 empirical backtest + AlphaEval RRE/PFS duel',
      },
    ],
    top_papers_2026: [
      {
        paper_id: 'ICLR2026-AlphaSAGE',
        title: 'AlphaSAGE: Structure-Aware Alpha Mining via GFlowNets for Robust Exploration',
        venue_year: 'ICLR 2026',
        authors: 'Binqi Chen et al.',
        core_formula: 'R(alpha) = exp(w_IC * R_IC + w_NOV * (1 - max_j |Corr(alpha, f_j)|) + w_SA * R_SA)',
        ashare_operator: '低相关性正交残差因子筛选门（|corr| < 0.70）',
        integrated_strategy_code: 'paper_iclr2026_alphasage_ortho',
      },
      {
        paper_id: 'KDD2026-AlphaEval',
        title: 'AlphaEval: A Comprehensive and Efficient Evaluation Framework for Formula Alpha Mining',
        venue_year: 'KDD 2026',
        authors: 'Hongjun Ding et al.',
        core_formula: 'RRE = (1/(T-1)) * sum_t 1 / (1 + KL(S(F_t) || S(F_{t-1})))',
        ashare_operator: '1秒级无回测相对秩熵(RRE)低换手稳定度与微扰保真度(PFS)门控',
        integrated_strategy_code: 'paper_kdd2026_alphaeval_rre',
      },
    ],
    discovered_candidates: [
      {
        candidate_id: 'cand-dag-001',
        parent_code: 'paper_iclr2026_alphasage_ortho',
        dag_depth: 2,
        display_name: '缩量乖离正交残差因子 v2',
        max_corr_existing: '0.412',
        rre_stability: '0.885',
        pfs_robustness: '0.910',
        net_sharpe: '1.48',
        nw_t_stat: '2.64',
        gate_verdict: 'admitted_shadow',
      },
    ],
  };
}

export function buildMockIntegrity(): NexusIntegrityAudit {
  return {
    audit_version: 'nexus-integrity-2.0.0',
    overall_status: 'healthy',
    benchmark_standard: MOCK_BENCHMARK_STANDARD,
    entrypoint_accounting_parity: [
      {
        entrypoint: '1. Unified Benchmark & Swiss Arena (Nexus Standard)',
        module_path: 'pulse.domain.benchmark.accounting',
        uses_unified_standard: true,
        fee_basis: '5bp sell stamp + 2.5bp comm (min ¥5) + 0.1bp xfer + 2.5bp spread + Almgren-Chriss impact',
        limit_rule: 'Board-aware (2026-07-06 主板 ST 调为 ±10%；双创 20%；北交所 30%)',
        t1_enforced: true,
        note: 'Primary canonical benchmark & UnifiedBacktestEngine [LIVE_PARITY=PASS]',
      },
      {
        entrypoint: '2. Double-Entry Execution Kernel & Playbook Replay',
        module_path: 'pulse.domain.execution.kernel / pulse.domain.playbooks.replay',
        uses_unified_standard: true,
        fee_basis: 'user_account_2026 & gips_ashare_2026_cost_profile',
        limit_rule: 'PriceLimitRegistry + DailyBarMatcher NO_FILL_QUEUE',
        t1_enforced: true,
        note: 'Exact double-entry LedgerReducer verified live with golden test order',
      },
      {
        entrypoint: '3. Ranking Forward Outcomes (PEX02 Track Record)',
        module_path: 'pulse.domain.market_state.outcomes',
        uses_unified_standard: true,
        fee_basis: 'user_account_2026 round-trip ratio deducted against universe median',
        limit_rule: 'Board-aware path guard (11.1% main / 22.2% ChiNext-STAR / 33.3% BSE)',
        t1_enforced: true,
        note: 'Live-tested board-aware path guard verified',
      },
      {
        entrypoint: '4. Fusion Forward Books (Frozen E012/E036/E065/E085)',
        module_path: 'pulse.domain.fusion.books',
        uses_unified_standard: false,
        fee_basis: 'frozen_preregistered_shadow: 2.5bp comm + 5bp sell stamp + 5bp slip',
        limit_rule: 'frozen_preregistered_shadow: One-word limit-up skip; limit-down close deferred',
        t1_enforced: true,
        note: 'frozen_preregistered_shadow: preserved byte-exact per pre-registration rule',
      },
      {
        entrypoint: '5. Pre-registered Batch Panels (Frozen E093-E101)',
        module_path: 'pulse.domain.batch6.panel / batch7 / batch8 / e101',
        uses_unified_standard: false,
        fee_basis: 'frozen_preregistered_shadow: 25.2bp round-trip linear deduction',
        limit_rule: 'frozen_preregistered_shadow: ratio/guard/defer',
        t1_enforced: true,
        note: 'frozen_preregistered_shadow: locked by sha256 frozen_specs guard',
      },
      {
        entrypoint: '6. Intraday Paper Triggers Shadow Log (E053-E059)',
        module_path: 'pulse.domain.fwdlog.paper',
        uses_unified_standard: false,
        fee_basis: 'frozen_preregistered_shadow: raw spread telemetry (0bp)',
        limit_rule: 'frozen_preregistered_shadow: 3-tier 60s snapshot limit-queue depletion model',
        t1_enforced: true,
        note: 'frozen_preregistered_shadow: diagnostic intraday shadow telemetry',
      },
      {
        entrypoint: '7. Account Forward Book Tracker',
        module_path: 'pulse.application.portfolio_service',
        uses_unified_standard: true,
        fee_basis: 'ExecutionKernel + LedgerRepo double-entry cost engine',
        limit_rule: 'Limit-up buy rejection & limit-down sell deferral on raw open',
        t1_enforced: true,
        note: 'Shares the canonical ExecutionKernel verified above',
      },
    ],
    source_normalization_matrix: [
      {
        source_id: 'xinhua_finance',
        domain: 'news',
        raw_fields: 'contentId, title, summary, publishTime, publishUrl',
        canonical_fields: 'item_id, headline, body_text, published_at_utc, source_url',
        missing_value_policy: '缺失时间保留null绝不伪造秒级；游标skip不提前break',
        unit_guard: 'Asia/Shanghai -> UTC timestamptz; Tier-1官方主锚',
      },
      {
        source_id: 'tencent',
        domain: 'bars',
        raw_fields: 'date, open, close, high, low, volume_lots',
        canonical_fields: 'trade_date, open_price_raw, close_price_raw, high_price_raw, low_price_raw, volume_shares',
        missing_value_policy: '拒绝在raw通道使用qfqday/hfqday；缺失成交额保持null',
        unit_guard: 'lots * 100 -> canonical shares; WAF 501 立即停止不重试风暴',
      },
    ],
    outbox_event_channels: [
      { event_type: 'decision.published', producer_wired: true, total_emitted: 1, last_emitted_at: '2026-11-26T08:00:00Z' },
      { event_type: 'account.updated', producer_wired: true, total_emitted: 2, last_emitted_at: '2026-11-26T08:02:00Z' },
      { event_type: 'source.health.changed', producer_wired: true, total_emitted: 4, last_emitted_at: '2026-11-26T08:03:00Z' },
      { event_type: 'actionability.ready', producer_wired: true, total_emitted: 1, last_emitted_at: '2026-11-26T08:04:00Z' },
    ],
    database_health: {
      nexus_tables_ready: true,
      workbench_cache_rows: 12,
      cross_verified_clusters: 4,
      realtime_adjustments_rows: 1,
      arena_ratings_rows: 62,
      discovered_candidates_rows: 1,
      performance_indexes_ready: true,
    },
  };
}

export function handleBfNexus(path: string, q: URLSearchParams, now: number, rel: ReleaseRec | undefined): MockResponse {
  if (!rel && q.get('release_id')) return errorResponse(now, 404, 'RELEASE_NOT_FOUND', '发布版本不存在');
  const meta = buildMeta(now, {
    releaseId: rel?.id ?? null,
    decisionAsOf: rel ? iso(rel.decisionAsOf) : null,
    quality: rel ? 'valid' : 'partial',
    freshness: rel ? 'current' : 'not_expected',
  });
  if (path === '/v1/x/bf/nexus/hub') {
    const tr = (q.get('track') ?? 'composite') as 'composite' | 'short' | 'swing' | 'long';
    return { status: 200, body: { meta, data: buildMockNexusHub(rel ?? null, tr) } };
  }
  const secMatch = path.match(/^\/v1\/x\/bf\/nexus\/sectors\/([^/]+)\/deep-dive$/);
  if (secMatch) {
    if (!rel) return errorResponse(now, 404, 'RELEASE_NOT_FOUND', '发布版本不存在');
    return {
      status: 200,
      body: {
        meta,
        data: buildMockSectorDeepDive(
          rel,
          decodeURIComponent(secMatch[1]!),
          q.get('taxonomy_id') ?? 'industry',
          q.get('horizon_id') ?? 'short_term',
        ),
      },
    };
  }
  const proMatch = path.match(/^\/v1\/x\/bf\/nexus\/stocks\/([^/]+)\/pro$/);
  if (proMatch) {
    if (!rel) return errorResponse(now, 404, 'RELEASE_NOT_FOUND', '发布版本不存在');
    const gm = (q.get('grid_mode') ?? 'atr_adaptive') as 'atr_adaptive' | 'geometric' | 'arithmetic';
    const tf = q.get('timeframe') ?? '1d';
    const adj = q.get('adjustment') ?? 'raw';
    return { status: 200, body: { meta, data: buildMockStockPro(rel, decodeURIComponent(proMatch[1]!), tf, gm, adj) } };
  }
  if (path === '/v1/x/bf/nexus/arena') {
    const data = buildMockArena();
    const l = q.get('layer');
    const hb = q.get('horizon_bucket');
    const p = q.get('paradigm');
    const role = q.get('role');
    data.strategies = data.strategies.filter((s) =>
      (!l || s.layer === l)
      && (!hb || s.horizon_bucket === hb)
      && (!p || s.paradigm === p)
      && (!role || s.portfolio_role === role),
    );
    data.total_strategies = data.strategies.length;
    return { status: 200, body: { meta, data } };
  }
  if (path === '/v1/x/bf/nexus/integrity') {
    return { status: 200, body: { meta, data: buildMockIntegrity() } };
  }
  return errorResponse(now, 404, 'NOT_FOUND', `未找到 Nexus 接口 ${path}`);
}
