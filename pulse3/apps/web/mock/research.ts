/**
 * Synthetic strategies, league, evaluations, papers, data status and account.
 * Hypothetical values only; league points / metrics are NOT win probabilities.
 */
import type {
  AccountProjection, ActionabilitySnapshot, Evaluation, JobRun, League, ModelRun, PaperMethodCard, SourceHealth, Strategy,
} from '../src/api/types';
import { INSTRUMENTS, lastClose, planIdFor, type ReleaseRec } from './market.ts';
import { dec, hash64, iso, opaque, uuid } from './util.ts';

type Lifecycle = Strategy['instances'][number]['lifecycle'];
type Role = Strategy['role'];

interface StratSpec {
  key: string; name: string; role: Role; family: string; horizon: string; target: string;
  lifecycle: Lifecycle; impl: 'not_implemented' | 'prototype' | 'implemented';
  repro: 'not_started' | 'running' | 'reproduced' | 'failed'; health: 'healthy' | 'degraded' | 'unavailable';
}

const SPECS: StratSpec[] = [
  { key: 'trend', name: '趋势跟随·日线', role: 'selection', family: 'rule_based', horizon: 'short_term', target: 'excess_return_5d', lifecycle: 'active', impl: 'implemented', repro: 'reproduced', health: 'healthy' },
  { key: 'flow', name: '资金流动量', role: 'selection', family: 'ml_ranker', horizon: 'swing', target: 'excess_return_20d', lifecycle: 'shadow', impl: 'implemented', repro: 'reproduced', health: 'healthy' },
  { key: 'pullback', name: '突破回踩入场', role: 'entry', family: 'rule_based', horizon: 'short_term', target: 'excess_return_5d', lifecycle: 'validated', impl: 'implemented', repro: 'running', health: 'healthy' },
  { key: 'tday', name: '日内做T·区间', role: 'execution', family: 't_trade', horizon: 'short_term', target: 'excess_return_5d', lifecycle: 'screened', impl: 'prototype', repro: 'not_started', health: 'degraded' },
  { key: 'grid', name: '网格·震荡', role: 'execution', family: 'grid_trade', horizon: 'swing', target: 'excess_return_20d', lifecycle: 'specified', impl: 'not_implemented', repro: 'not_started', health: 'unavailable' },
  { key: 'news', name: '消息冲击事件', role: 'selection', family: 'llm_assisted', horizon: 'short_term', target: 'excess_return_5d', lifecycle: 'retired', impl: 'implemented', repro: 'failed', health: 'unavailable' },
  { key: 'value', name: '估值回归', role: 'selection', family: 'rule_based', horizon: 'long_term', target: 'excess_return_120d', lifecycle: 'discovered', impl: 'not_implemented', repro: 'not_started', health: 'healthy' },
  { key: 'stop', name: '波动率止损退出', role: 'exit', family: 'rule_based', horizon: 'swing', target: 'excess_return_20d', lifecycle: 'active', impl: 'implemented', repro: 'reproduced', health: 'healthy' },
];

export const STRATEGIES: Strategy[] = SPECS.map((s) => ({
  strategy_id: uuid(`strategy:${s.key}`),
  strategy_version: 1,
  display_name: s.name,
  role: s.role,
  method_family: s.family,
  forecast_target: s.target,
  horizon_id: s.horizon,
  definition_hash: hash64(`def:${s.key}`),
  specification: opaque({}),
  tags: [],
  instances: [{
    instance_id: uuid(`instance:${s.key}`),
    strategy_id: uuid(`strategy:${s.key}`),
    strategy_version: 1,
    parameter_hash: hash64(`param:${s.key}`),
    universe_policy_version: 'synthetic-main-board-v1',
    lifecycle: s.lifecycle,
    implementation_status: s.impl,
    reproduction_status: s.repro,
    health: s.health,
  }],
}));

export const STRATEGY_INSTANCES = STRATEGIES.map((s) => ({ ...(s.instances[0] as Strategy['instances'][number]), role: s.role }));

export const LEAGUE_ID = uuid('league:main');
export const EVAL_CONTEXT_ID = uuid('evalctx:main');

export function buildLeague(): League {
  const inst = (i: number): string => (STRATEGY_INSTANCES[i] as { instance_id: string }).instance_id;
  return {
    league_id: LEAGUE_ID,
    league_version: 1,
    evaluation_context_id: EVAL_CONTEXT_ID,
    display_name: '合成联赛·短线选股赛区',
    pairing_policy_version: 'synthetic-pairing-v1',
    promotion_policy_version: 'synthetic-promotion-v1',
    rounds: [
      {
        round_no: 1, round_state: 'completed',
        pairings: [
          { pairing_id: uuid('pair:1'), left_instance_id: inst(0), right_instance_id: inst(1), left_experiment_id: uuid('exp:a'), right_experiment_id: uuid('exp:b'), outcome: 'left', reason_ref: 'synthetic://league/r1/p1' },
          { pairing_id: uuid('pair:2'), left_instance_id: inst(2), right_instance_id: inst(3), left_experiment_id: uuid('exp:c'), right_experiment_id: null, outcome: 'incomparable', reason_ref: 'synthetic://league/r1/p2' },
        ],
      },
      {
        round_no: 2, round_state: 'running',
        pairings: [
          { pairing_id: uuid('pair:3'), left_instance_id: inst(0), right_instance_id: inst(2), left_experiment_id: uuid('exp:d'), right_experiment_id: uuid('exp:e'), outcome: 'pending', reason_ref: 'synthetic://league/r2/p1' },
          { pairing_id: uuid('pair:4'), left_instance_id: inst(7), right_instance_id: null, left_experiment_id: null, right_experiment_id: null, outcome: 'bye', reason_ref: 'synthetic://league/r2/p2' },
        ],
      },
    ],
  };
}

export const KNOWN_EXPERIMENTS: Record<string, string> = {
  [uuid('exp:a')]: 'trend', [uuid('exp:b')]: 'flow', [uuid('exp:c')]: 'pullback', [uuid('exp:d')]: 'trend', [uuid('exp:e')]: 'pullback',
};

export interface CreatedExperiment {
  experimentId: string;
  jobRunId: string;
  request: { instance_id: string; hypothesis_id: string; hypothesis_version: number; evaluation_context_id: string; run_purpose: Evaluation['run_purpose']; random_seed: number };
  createdAt: number;
}

export function buildEvaluation(experimentId: string, instanceId: string, purpose: Evaluation['run_purpose'], state: Evaluation['run_state'], now: number, hypothesis?: { id: string; version: number }): Evaluation {
  const done = state === 'completed';
  return {
    experiment_id: experimentId,
    instance_id: instanceId,
    evaluation_context_id: EVAL_CONTEXT_ID,
    context_hash: hash64('evalctx'),
    hypothesis_id: hypothesis?.id ?? uuid('hyp:main'),
    hypothesis_version: hypothesis?.version ?? 1,
    run_purpose: purpose,
    run_state: state,
    policy_versions: {
      market_rules_version: 'synthetic-rules-v1', execution_policy_version: 'synthetic-exec-v1', cost_policy_version: 'synthetic-cost-v1',
      ledger_policy_version: 'synthetic-ledger-v1', performance_policy_version: 'synthetic-perf-v1', numeric_policy_version: 'decimal50-scale12-half-even-v1',
    },
    started_at: state === 'queued' ? null : iso(now - 120_000),
    completed_at: done ? iso(now - 30_000) : null,
    result: done ? { ref: `artifact:eval:${experimentId}`, content_hash: hash64(`eval:${experimentId}`), contract_id: 'urn:pulse:contracts:v3#/$defs/Evaluation', schema_version: '3.0.0' } : null,
    metrics: done
      ? [
          { metric_name: 'net_return_after_cost', metric_definition_version: '1', slice_key: 'all', value: '0.0312', unit: 'ratio', lower_bound: '-0.0120', upper_bound: '0.0710', confidence_level: '0.9500', sample_count: 184, quality_status: 'valid', missing_reason: null },
          { metric_name: 'max_drawdown', metric_definition_version: '1', slice_key: 'all', value: '0.0840', unit: 'ratio', lower_bound: null, upper_bound: null, confidence_level: null, sample_count: 184, quality_status: 'valid', missing_reason: null },
          { metric_name: 'hit_rate', metric_definition_version: '1', slice_key: 'all', value: null, unit: 'ratio', lower_bound: null, upper_bound: null, confidence_level: null, sample_count: 0, quality_status: 'partial', missing_reason: '样本不足，未计算' },
        ]
      : [],
    error_code: state === 'failed' ? 'SYNTHETIC_FAILURE' : null,
    context: {
      evaluation_context_id: EVAL_CONTEXT_ID,
      context_hash: hash64('evalctx'),
      forecast_target: 'excess_return_5d',
      horizon_id: 'short_term',
      decision_as_of: iso(now - 86_400_000),
      availability_policy: { availability_policy_version: 'synthetic-observed-clock-v1', mode: 'observed', available_at_semantics: 'actual_processing_completion', rule_ref: { ref: 'synthetic://policy/availability', content_hash: hash64('avail'), contract_id: 'urn:pulse:contracts:v3#/$defs/AvailabilityPolicy', schema_version: '3.0.0' }, assumed_ingestion_delay_ms: null, assumption_note: null },
      universe_policy_version: 'synthetic-main-board-v1',
      input_snapshot_refs: [{ snapshot_id: uuid('snap:bars'), dataset_id: 'syn.raw_bars', manifest_version: 1, content_hash: hash64('snap:bars') }],
      train_start: iso(now - 400 * 86_400_000),
      train_end: iso(now - 200 * 86_400_000),
      validation_start: iso(now - 200 * 86_400_000),
      validation_end: iso(now - 60 * 86_400_000),
      blind_start: null,
      blind_end: null,
      embargo_seconds: 432_000,
      sealed_at: iso(now - 30 * 86_400_000),
    },
  };
}

export function buildJobRun(jobRunId: string, createdAt: number, now: number): JobRun {
  const age = now - createdAt;
  const state: JobRun['run_state'] = age < 3_000 ? 'queued' : age < 8_000 ? 'running' : 'succeeded';
  return {
    job_run_id: jobRunId,
    job_id: uuid('job:experiment'),
    job_version: 1,
    stage_id: null,
    run_state: state,
    lease_epoch: 1,
    attempt_count: state === 'queued' ? 0 : 1,
    scheduled_for: iso(createdAt),
    heartbeat_at: state === 'running' ? iso(now - 1_000) : null,
    completed_at: state === 'succeeded' ? iso(createdAt + 8_000) : null,
    error_code: null,
  };
}

export function buildModelRun(id: string, now: number): ModelRun {
  return {
    model_run_id: id, model_policy_id: 'synthetic.analysis', model_policy_version: 1, stage_id: null,
    input_context_hash: hash64(`mr:${id}`), analysis_as_of: iso(now - 1_800_000), run_state: 'succeeded',
    publication_eligibility: 'eligible', output: { ref: `artifact:model_run:${id}`, content_hash: hash64(`mro:${id}`), contract_id: 'urn:pulse:contracts:v3#/$defs/ModelRun', schema_version: '3.0.0' },
    started_at: iso(now - 1_800_000), completed_at: iso(now - 1_700_000), error_code: null,
  };
}

export function buildPapers(): PaperMethodCard[] {
  const rows: Array<[string, PaperMethodCard['publication_status'], PaperMethodCard['read_depth'], number, string | null]> = [
    ['合成论文：截面动量与反转的稳定性', 'published', 'reproduced', 2024, '合成期刊 A'],
    ['合成论文：订单流不平衡与短期收益', 'accepted', 'full_text', 2025, '合成会议 B'],
    ['合成论文：网格交易的成本敏感性', 'preprint', 'abstract', 2025, null],
    ['合成论文：新闻情绪与次日波动', 'preprint', 'full_text_sections', 2026, null],
    ['合成论文：强化学习执行的样本外表现', 'withdrawn', 'metadata', 2023, '合成会议 C'],
    ['合成论文：板块轮动的领先指标', 'submitted', 'code_review', 2026, null],
  ];
  return rows.map(([title, status, depth, year, venue], i) => ({
    paper_id: uuid(`paper:${i}`), paper_revision: 1, canonical_id: `synthetic:paper-${i + 1}`, title,
    primary_url: `https://example.invalid/synthetic/paper/${i + 1}`, venue, publication_year: year, publication_status: status, read_depth: depth,
    method_card: opaque({}), available_at: iso(Date.UTC(2026, 10, 1) + i * 3_600_000),
  }));
}

export function buildDataStatus(now: number, stale: boolean): SourceHealth[] {
  const ago = (min: number): string => iso(now - min * 60_000);
  const mk = (id: string, ep: string, p: Partial<SourceHealth>): SourceHealth => ({
    source_id: id, endpoint_id: ep, source_schema_version: '1', mapping_version: '1', partition_key: 'all', enabled: true,
    runtime_state: 'healthy', freshness_state: 'current', last_attempt_at: ago(1), last_transport_ok_at: ago(1), last_new_record_at: ago(2), last_canonical_ready_at: ago(2),
    consecutive_failures: 0, next_attempt_at: iso(now + 60_000), last_error_code: null, ...p,
  });
  return [
    mk('syn.quote_feed', 'l1_snapshot', stale ? { freshness_state: 'stale', last_new_record_at: ago(25), last_canonical_ready_at: ago(25) } : {}),
    mk('syn.daily_bars', 'daily', { freshness_state: 'not_expected', last_new_record_at: ago(400), last_canonical_ready_at: ago(398) }),
    mk('syn.minute_bars', 'minute', { runtime_state: 'degraded', freshness_state: 'stale', last_transport_ok_at: ago(2), last_new_record_at: ago(48), last_canonical_ready_at: ago(50), consecutive_failures: 3, last_error_code: 'EMPTY_PAYLOAD' }),
    mk('syn.exchange_notice', 'announcements', { last_new_record_at: ago(35), last_canonical_ready_at: ago(34) }),
    mk('syn.newswire', 'rss', { runtime_state: 'auth_required', freshness_state: 'unknown', last_transport_ok_at: ago(600), last_new_record_at: ago(620), last_canonical_ready_at: null, consecutive_failures: 12, last_error_code: 'HTTP_401', next_attempt_at: null }),
    mk('syn.forum', 'hot_posts', { runtime_state: 'healthy', freshness_state: 'current', last_new_record_at: ago(9), last_canonical_ready_at: ago(8) }),
    mk('syn.exchange_lhb', 'seat_report', { freshness_state: 'not_expected', last_new_record_at: ago(1100), last_canonical_ready_at: ago(1090) }),
    mk('syn.provider_b', 'fund_flow', { enabled: false, runtime_state: 'paused', freshness_state: 'unknown', last_attempt_at: null, last_transport_ok_at: null, last_new_record_at: null, last_canonical_ready_at: null, next_attempt_at: null }),
    mk('syn.foreign_calendar', 'calendar', { enabled: true, runtime_state: 'unconfigured', freshness_state: 'unknown', last_attempt_at: null, last_transport_ok_at: null, last_new_record_at: null, last_canonical_ready_at: null, next_attempt_at: null }),
  ];
}

// ---------------------------------------------------------------- account

export const ACCOUNT_ID = uuid('account:main');
export const ACCOUNT_VERSION = 8;
export const VALUATION_SNAPSHOT_ID = uuid('snap:valuation');

export function buildAccount(r: ReleaseRec): AccountProjection {
  const pos = [
    { inst: INSTRUMENTS[0]!, qty: 1000, sellable: 600, cost: 17.9 },
    { inst: INSTRUMENTS[1]!, qty: 500, sellable: 500, cost: 33.4 },
  ].map((p) => {
    const mark = lastClose(p.inst, r.decisionAsOf).last;
    return {
      instrument_id: p.inst.id,
      quantity_shares: p.qty,
      sellable_quantity_shares: p.sellable,
      reserved_sell_quantity_shares: 0,
      cost_basis_cny: dec(p.qty * p.cost, 2),
      mark_price_raw: dec(mark),
      market_value_cny: dec(p.qty * mark, 2),
      mark_quality_status: 'valid' as const,
      _unreal: p.qty * (mark - p.cost),
    };
  });
  const unrealized = pos.reduce((a, p) => a + p._unreal, 0);
  const marketValue = pos.reduce((a, p) => a + Number(p.market_value_cny), 0);
  const costBasis = pos.reduce((a, p) => a + Number(p.cost_basis_cny), 0);
  const external = 100_000;
  const realized = 1200.5;
  const dividends = 300;
  const txCost = 85.2;
  const nav = external + realized + unrealized + dividends - txCost;
  return {
    account_id: ACCOUNT_ID,
    account_version: ACCOUNT_VERSION,
    base_currency: 'CNY',
    trading_date: iso(r.decisionAsOf).slice(0, 10),
    ledger_policy_version: 'synthetic-ledger-v1',
    numeric_policy_version: 'decimal50-scale12-half-even-v1',
    valuation_policy_version: 'synthetic-raw-mark-v1',
    valuation_snapshot_id: VALUATION_SNAPSHOT_ID,
    cash_settled_cny: dec(Math.max(nav - marketValue - 2000, 0), 2),
    trade_receivable_cny: '2000.00',
    trade_payable_cny: '0.00',
    dividend_receivable_cny: '0.00',
    reserved_cash_cny: '1800.00',
    cash_available_cny: dec(Math.max(nav - marketValue - 2000 - 1800, 0), 2),
    withdrawable_cash_cny: null,
    nav_cny: dec(nav, 2),
    realized_gross_cny: dec(realized, 2),
    dividend_income_cny: dec(dividends, 2),
    pnl_from_cost_origin_cny: dec(nav - external, 2),
    cost_basis_cny: dec(costBasis, 2),
    unrealized_cny: dec(unrealized, 2),
    transaction_cost_cny: dec(txCost, 2),
    withholding_tax_cny: '0.00',
    net_external_capital_cny: dec(external, 2),
    positions: pos.map(({ _unreal: _u, ...rest }) => rest),
  };
}

export function buildActionability(r: ReleaseRec, now: number, offset: number, pageSize: number): { data: ActionabilitySnapshot; total: number } {
  const all = INSTRUMENTS.map((inst, i) => {
    const status: ActionabilitySnapshot['entries'][number]['actionability_status'] = (['eligible', 'not_triggered', 'blocked', 'unknown'] as const)[i % 4] ?? 'unknown';
    return {
      instrument_id: inst.id,
      plan_ref: inst.idx === 8 ? null : { plan_id: planIdFor(inst.id, 'main'), plan_version: r.n },
      frozen_action_rank: i + 1,
      account_action_rank: status === 'eligible' || status === 'not_triggered' ? i + 1 : null,
      actionability_status: status,
      side: status === 'unknown' ? null : (i % 5 === 1 ? ('sell' as const) : ('buy' as const)),
      reason_codes: status === 'blocked' ? ['LIMIT_UP_NO_COUNTERPARTY'] : status === 'unknown' ? ['QUOTE_MISSING'] : status === 'eligible' ? ['CASH_OK', 'CONDITION_TRUE'] : ['CONDITION_NOT_TRUE'],
      checked_quantity_shares: status === 'unknown' ? null : 100,
    };
  });
  return {
    total: all.length,
    data: {
      actionability_id: uuid(`act:${r.id}`),
      release_id: r.id,
      ranking_id: uuid(`rank:${r.id}:short_term:market`),
      account_id: ACCOUNT_ID,
      account_version: ACCOUNT_VERSION,
      quote_snapshot_id: uuid('quote:snapshot:account'),
      actionability_policy_version: 'synthetic-actionability-v1',
      checked_at: iso(now - 20_000),
      valid_until: iso(now + 70_000),
      entries: all.slice(offset, offset + pageSize),
    },
  };
}
