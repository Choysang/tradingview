/**
 * Synthetic per-stock objects: workspace, trade plans (with T and grid sub-plans),
 * evidence, community, fund flow, seats. Hypothetical values only.
 */
import type {
  Community, Condition, Evidence, FeatureResult, FundFlow, Operand, Seat, TradePlan, Workspace,
} from '../src/api/types';
import { INSTRUMENTS, lastClose, planIdFor, toInstrument, type ReleaseRec, type SynInstrument } from './market.ts';
import { STRATEGY_INSTANCES } from './research.ts';
import { dec, hash64, iso, opaque, uuid } from './util.ts';

const SESSION = 'cn-a-continuous-v1';

export function featureResult(inst: SynInstrument, featureId: string, state: 'READY' | 'FAILED' | 'RUNNING', r: ReleaseRec): FeatureResult {
  const ready = state === 'READY';
  return {
    feature_key_hash: hash64(`fk:${featureId}:${inst.id}:${r.n}`),
    key: {
      feature_id: featureId,
      definition_version: '1',
      implementation_digest: hash64(`impl:${featureId}`),
      parameter_digest: hash64(`param:${featureId}`),
      instrument_ids: [inst.id],
      snapshot_id: uuid('snap:bars'),
      decision_as_of: iso(r.decisionAsOf),
      availability_policy_version: 'synthetic-observed-clock-v1',
      timeframe: '1d',
      session_policy_version: SESSION,
      input_revision: '1',
      input_schema_version: '3.0.0',
      dependency_digest: hash64(`dep:${featureId}`),
      calendar_version: 'synthetic-calendar-v1',
      adjustment_mode: 'raw',
      adjustment_version: 'raw-v1',
      universe_policy_version: 'synthetic-main-board-v1',
      market_rules_version: 'synthetic-rules-v1',
      numeric_policy_version: 'decimal50-scale12-half-even-v1',
    },
    result_state: state,
    artifact: ready
      ? { ref: `artifact:${featureId}:${inst.idx}`, content_hash: hash64(`art:${featureId}:${inst.id}`), contract_id: 'urn:pulse:contracts:v3#/$defs/FeatureResult', schema_version: '3.0.0' }
      : null,
    computed_at: ready ? iso(r.decisionAsOf + 30_000) : null,
    quality_status: ready ? 'valid' : 'invalid',
  };
}

export function buildWorkspace(inst: SynInstrument, r: ReleaseRec, view: 'public_research' | 'account_holding', accountId: string | undefined, actionabilityId: string | null): Workspace {
  const plans = [{ kind: 'main' as const }, ...(inst.idx === 0 ? [{ kind: 't' as const }, { kind: 'grid' as const }] : [])];
  const hasPlan = inst.idx !== 8;
  const contribs = STRATEGY_INSTANCES.slice(0, 3).map((s, i) => ({
    instance_ref: { instance_id: s.instance_id, strategy_id: s.strategy_id, strategy_version: s.strategy_version },
    role: s.role,
    contribution_score: dec(0.62 - i * 0.17, 4),
    explanation_ref: `synthetic://explain/contribution/${inst.id}/${i}`,
  }));
  return {
    instrument: toInstrument(inst),
    release_id: r.id,
    view_kind: view,
    scope_key: view === 'account_holding' ? (accountId ?? 'account') : 'public',
    projection_version: r.n,
    projection_hash: hash64(`proj:${inst.id}:${r.n}`),
    chart_window_refs: [{ ref: `artifact:bar_window:${inst.idx}`, content_hash: hash64(`win:${inst.id}`), contract_id: 'urn:pulse:contracts:v3#/$defs/BarWindow', schema_version: '3.0.0' }],
    features: [
      featureResult(inst, 'price.ma', 'READY', r),
      featureResult(inst, 'price.ema', 'READY', r),
      featureResult(inst, 'price.macd', 'FAILED', r),
      featureResult(inst, 'flow.net_inflow_proxy', 'READY', r),
    ],
    plan_refs: hasPlan ? plans.map((p) => ({ plan_id: planIdFor(inst.id, p.kind), plan_version: r.n })) : [],
    evidence_revision_ids: [uuid(`ev:${inst.id}:1`), uuid(`ev:${inst.id}:2`)],
    strategy_contributions: contribs,
    actionability_id: view === 'account_holding' ? actionabilityId : null,
    generated_at: iso(r.decisionAsOf + 90_000),
    quote_overlay: null,
  };
}

// ---------------------------------------------------------------- plans

function feat(inst: SynInstrument, fieldPath: string, tf = '1d'): Operand {
  return {
    kind: 'feature',
    feature_id: fieldPath === 'macd_hist' ? 'price.macd' : 'price.bar',
    definition_version: '1',
    instrument_id: inst.id,
    timeframe: tf,
    field_path: fieldPath,
    unit: 'CNY/share',
    resolution_policy_version: 'latest-completed-bar-v1',
    initial_feature_key_hash: null,
  };
}
const konst = (v: number | string): Operand => ({ kind: 'constant', value: typeof v === 'number' ? dec(v, 4) : v, unit: 'CNY/share' });
const clockBar = { kind: 'bar_close' as const, timeframe: '1d', session_policy_version: SESSION };
const clockQuote = { kind: 'quote' as const };

const cmp = (inst: SynInstrument, op: 'lt' | 'lte' | 'gt' | 'gte' | 'eq', field: string, k: number | string, clock: Condition['clock'] = clockBar): Condition =>
  ({ node: 'compare', clock, operator: op, left: feat(inst, field), right: konst(k) });

function planFor(inst: SynInstrument, kind: 'main' | 't' | 'grid', r: ReleaseRec, nowMs: number): TradePlan {
  const { last, prev } = lastClose(inst, r.decisionAsOf);
  const stratIdx = kind === 'main' ? 0 : kind === 't' ? 3 : 4;
  const s = STRATEGY_INSTANCES[stratIdx % STRATEGY_INSTANCES.length]!;
  const crossDir = last >= prev ? 'above' : 'below';
  const crossAt = (last + prev) / 2;
  let entry: Condition;
  let exit: Condition;
  let invalid: Condition;
  let sizing: TradePlan['sizing'] = { kind: 'fixed_shares', quantity_shares: 100 };
  if (kind === 'main') {
    entry = {
      node: 'all', clock: clockBar,
      children: [
        cmp(inst, 'lte', 'close_price_raw', last * 1.05),
        { node: 'any', clock: clockBar, children: [cmp(inst, 'gte', 'close_price_raw', last * 1.1), { node: 'within_session', clock: clockQuote, session_kinds: ['continuous'] }] },
        { node: 'not', clock: clockBar, child: cmp(inst, 'lte', 'macd_hist', '0') },
        { node: 'crosses', clock: clockBar, direction: crossDir, left: feat(inst, 'close_price_raw'), right: konst(crossAt), previous_observation: { selection: 'previous_valid', max_age_ms: 259_200_000 } },
      ],
    };
    exit = cmp(inst, 'gte', 'close_price_raw', last * 1.12);
    invalid = cmp(inst, 'lt', 'close_price_raw', last * 0.92);
    sizing = { kind: 'portfolio_fraction', fraction: '0.1000', maximum_quantity_shares: 2000 };
  } else if (kind === 't') {
    entry = { node: 'all', clock: clockQuote, children: [{ ...cmp(inst, 'gte', 'last_price_raw', last * 1.004, clockQuote) }, cmp(inst, 'lte', 'close_price_raw', last * 1.08)] };
    exit = cmp(inst, 'gte', 'last_price_raw', last * 1.015, clockQuote);
    invalid = cmp(inst, 'lt', 'last_price_raw', last * 0.97, clockQuote);
  } else {
    entry = { node: 'any', clock: clockBar, children: [cmp(inst, 'lte', 'close_price_raw', last * 0.98), cmp(inst, 'lte', 'close_price_raw', last * 0.96)] };
    exit = cmp(inst, 'gte', 'close_price_raw', last * 1.03);
    invalid = cmp(inst, 'lt', 'close_price_raw', last * 0.9);
  }
  const finalState: TradePlan['state'] = kind === 'main' && inst.idx === 0 ? 'actionable' : kind === 'main' && inst.idx === 1 ? 'holding' : 'watching';
  const validated = finalState === 'actionable' || finalState === 'holding';
  return {
    plan_id: planIdFor(inst.id, kind),
    plan_version: r.n,
    instrument_id: inst.id,
    based_on_release_id: r.id,
    account_id: null,
    expected_account_version: null,
    state: finalState,
    strategy_refs: [{ instance_id: s.instance_id, strategy_id: s.strategy_id, strategy_version: s.strategy_version }],
    model_run_id: uuid(`modelrun:${inst.id}:${r.n}`),
    evidence_revision_ids: [uuid(`ev:${inst.id}:1`)],
    position_side: 'long',
    entry_conditions: entry,
    entry_price_zone: { lower_price_raw: dec(last * 0.97), upper_price_raw: dec(last * 1.0), currency: 'CNY', adjustment_mode: 'raw' },
    sizing,
    exit_conditions: exit,
    invalidation: invalid,
    holding_horizon: kind === 't' ? { horizon_id: 'intraday', unit: 'sessions', value: 1 } : { horizon_id: 'short_term', unit: 'trading_days', value: 5 },
    trigger_policy: {
      trigger_id: `syn-${kind}-${inst.idx}`,
      fire_mode: kind === 'grid' ? 'repeated_while_true' : 'on_transition_to_true',
      unknown_policy: 'do_not_fire',
      rearm: kind === 'grid' ? { mode: 'after_cooldown', cooldown_ms: 1_800_000 } : { mode: 'never', cooldown_ms: 0 },
      max_fires: kind === 'grid' ? 6 : 1,
      min_interval_ms: 0,
      require_final_bar: kind !== 't',
      pending_order_policy: 'block_retrigger',
    },
    valid_from: iso(r.decisionAsOf + 90_000),
    expires_at: iso(r.decisionAsOf + 5 * 86_400_000),
    replace_policy: 'cancel_remaining_and_reconcile',
    validation: validated
      ? { status: 'passed', policy_version: 'synthetic-plan-validation-v1', evaluation_id: null, result_ref: 'synthetic://validation/ok', validated_at: iso(r.decisionAsOf + 80_000) }
      : { status: 'pending', policy_version: 'synthetic-plan-validation-v1', evaluation_id: null, result_ref: null, validated_at: null },
    available_at: iso(Math.min(r.decisionAsOf + 85_000, nowMs)),
    supersedes: r.n > 1 ? { plan_id: planIdFor(inst.id, kind), plan_version: r.n - 1 } : null,
  };
}

export function buildPlan(planId: string, version: number, r: ReleaseRec, nowMs: number): TradePlan | null {
  for (const inst of INSTRUMENTS) {
    for (const kind of ['main', 't', 'grid'] as const) {
      if (planIdFor(inst.id, kind) === planId) {
        if (kind !== 'main' && inst.idx !== 0) return null;
        return { ...planFor(inst, kind, { ...r, n: version }, nowMs), based_on_release_id: r.id };
      }
    }
  }
  return null;
}

// ---------------------------------------------------------------- evidence blocks

const PAGE = 2;
export const EVIDENCE_PAGE = PAGE;

export function buildEvidence(inst: SynInstrument, r: ReleaseRec, offset: number): { data: Evidence; total: number } {
  const n = inst.idx === 0 ? 5 : 2;
  const docs = Array.from({ length: n }, (_, i) => ({
    document_revision_id: uuid(`doc:${inst.id}:${i}`),
    document_id: uuid(`docid:${inst.id}:${i}`),
    source_id: i % 2 === 0 ? 'syn.exchange_notice' : 'syn.newswire',
    revision: 1 + (i === 1 ? 1 : 0),
    title: [`合成公告：${inst.name}披露阶段性经营进展`, `合成快讯：行业需求回升，${inst.industry}受关注`, `合成研报摘要：${inst.name}订单可见度提升`, `合成转载：${inst.name}产能扩张传闻`, `合成互动平台答复：${inst.name}`][i % 5] as string,
    origin_group: i === 3 ? 'syn.newswire' : `syn.origin.${i}`,
    origin_status: (['confirmed_original', 'confirmed_independent_report', 'confirmed_independent_report', 'republication', 'unknown'] as const)[i % 5] as 'confirmed_original',
    published_at: iso(r.decisionAsOf - (i + 1) * 3_600_000),
    published_time_precision: 'minute' as const,
    first_seen_at: iso(r.decisionAsOf - (i + 1) * 3_600_000 + 20_000),
    available_at: iso(r.decisionAsOf - (i + 1) * 3_600_000 + 40_000),
    quality_status: (i === 4 ? 'partial' : 'valid') as 'partial' | 'valid',
    document_status: (i === 1 ? 'corrected' : i === 4 ? 'unknown' : 'published') as 'corrected' | 'unknown' | 'published',
    raw_object_id: uuid(`raw:${inst.id}:${i}`),
    field_quality: (i === 4 ? { published_at: { status: 'missing' as const, reason: '来源页面未标注发布时间', raw_object_id: null, value_origin: 'not_available' as const } } : {}) as Record<string, { status: 'missing'; reason: string; raw_object_id: null; value_origin: 'not_available' }>,
    policy_usable_at: null,
  }));
  const claims = Array.from({ length: n }, (_, i) => ({
    claim_revision_id: uuid(`claim:${inst.id}:${i}`),
    claim_id: uuid(`claimid:${inst.id}:${i}`),
    revision: 1,
    subject_kind: 'instrument',
    subject_key: inst.symbol,
    predicate: ['company_disclosed_progress', 'sector_demand_recovery', 'order_visibility_up', 'capacity_expansion', 'platform_reply'][i % 5] as string,
    object_payload: opaque({}),
    claim_schema_version: 'synthetic.claim/1',
    assertion_type: (['official_disclosure', 'independently_reported', 'forecast', 'rumor', 'attributed_statement'] as const)[i % 5] as 'official_disclosure',
    claim_status: (['supported', 'supported', 'unverified', 'not_falsifiable_yet', 'disputed'] as const)[i % 5] as 'supported',
    verification_policy_version: 'synthetic-verify-v1',
    available_at: iso(r.decisionAsOf - (i + 1) * 3_600_000 + 60_000),
    quality_status: 'valid' as const,
    evidence: [{
      document_revision_id: uuid(`doc:${inst.id}:${i}`),
      evidence_ordinal: 1,
      relation: (i === 3 ? 'attributes' : i === 4 ? 'refutes' : 'supports') as 'supports',
      evidence_span: opaque({ page: 1 }),
      extraction_version: 'synthetic-extract-v1',
      available_at: iso(r.decisionAsOf - (i + 1) * 3_600_000 + 50_000),
    }],
    policy_usable_at: null,
  }));
  return { data: { instrument_id: inst.id, release_id: r.id, documents: docs.slice(offset, offset + PAGE), claims: claims.slice(offset, offset + PAGE) }, total: n };
}

export function buildCommunity(inst: SynInstrument, r: ReleaseRec, offset: number): { data: Community; total: number } {
  const n = 3;
  const obs = Array.from({ length: n }, (_, i) => ({
    community_observation_id: uuid(`comm:${inst.id}:${i}`),
    source_id: 'syn.forum',
    post_id: `syn-post-${inst.idx}-${i}`,
    post_revision: 1,
    author_pseudonym: i === 2 ? null : `匿名用户${i + 1}`,
    post_created_at: iso(r.decisionAsOf - (i + 1) * 1_800_000),
    sampled_at: iso(r.decisionAsOf - 600_000),
    sampling_entry: 'hot_posts',
    sampling_sort: 'recent',
    sampling_window_start: iso(r.decisionAsOf - 7_200_000),
    sampling_window_end: iso(r.decisionAsOf - 600_000),
    coverage_status: (i === 0 ? 'complete_for_declared_scope' : 'partial') as 'complete_for_declared_scope' | 'partial',
    like_count: i === 2 ? null : 40 - i * 11,
    reply_count: 12 - i * 4,
    unique_participant_count: 9 - i * 2,
    analysis_schema_version: 'synthetic.community/1',
    analysis_payload: opaque({ stance: ['偏乐观', '分歧', '偏谨慎'][i], topic: ['业绩预期', '板块轮动', '估值'][i] }),
    available_at: iso(r.decisionAsOf - 500_000),
    quality_status: (i === 0 ? 'valid' : 'partial') as 'valid' | 'partial',
    policy_usable_at: null,
  }));
  return { data: { instrument_id: inst.id, release_id: r.id, observations: obs.slice(offset, offset + PAGE) }, total: n };
}

export function buildFundFlow(inst: SynInstrument, r: ReleaseRec, offset: number): { data: FundFlow; total: number } {
  const specs = [
    { ns: 'syn.margin', name: 'margin_balance_change', cls: 'disclosed_observation', method: 'exchange_margin_v1', val: '12345678.00', unit: 'CNY', src: 'syn.exchange' },
    { ns: 'syn.provider_a', name: 'main_net_inflow', cls: 'provider_derived', method: 'provider_a_large_order_v2', val: '-2300000.00', unit: 'CNY', src: 'syn.provider_a' },
    { ns: 'syn.provider_b', name: 'main_net_inflow', cls: 'provider_derived', method: 'provider_b_big_trade_v1', val: null, unit: 'CNY', src: 'syn.provider_b' },
    { ns: 'syn.holder', name: 'holder_count_change', cls: 'disclosed_observation', method: 'periodic_report_v1', val: '-320', unit: 'count', src: 'syn.exchange' },
  ] as const;
  const obs = specs.map((s, i) => ({
    funding_observation_id: uuid(`ff:${inst.id}:${i}`),
    source_id: s.src,
    instrument_id: inst.id,
    market_key: null,
    metric_namespace: s.ns,
    metric_name: s.name,
    methodology_id: s.method,
    methodology_version: '1',
    measurement_class: s.cls,
    side_convention: 'net_buy_positive',
    trade_or_order_basis: (s.cls === 'provider_derived' ? 'trade' : 'report') as 'trade' | 'report',
    session_scope: 'regular',
    market_direction: 'domestic' as const,
    observation_window_start: iso(r.decisionAsOf - 86_400_000),
    observation_window_end: iso(r.decisionAsOf),
    raw_value: s.val,
    raw_unit: s.unit,
    canonical_value: s.val,
    canonical_unit: s.unit,
    currency: s.unit === 'CNY' ? 'CNY' : null,
    missing_reason: s.val === null ? '平台当日未返回该指标' : null,
    available_at: iso(r.decisionAsOf + 30_000),
    quality_status: (s.val === null ? 'partial' : 'valid') as 'partial' | 'valid',
    field_quality: {},
    policy_usable_at: null,
  }));
  return { data: { instrument_id: inst.id, release_id: r.id, observations: obs.slice(offset, offset + PAGE) }, total: obs.length };
}

export function buildSeats(inst: SynInstrument, r: ReleaseRec, offset: number): { data: Seat; total: number } {
  const has = inst.idx <= 5;
  const reports = has
    ? [{
        report_revision_id: uuid(`seat:${inst.id}:1`),
        instrument_id: inst.id,
        source_id: 'syn.exchange_lhb',
        revision: 1,
        window_start: iso(r.decisionAsOf - 86_400_000).slice(0, 10),
        window_end: iso(r.decisionAsOf).slice(0, 10),
        disclosure_reason: '日涨幅偏离值达到 7%（合成）',
        rule_version: 'synthetic-lhb-rule-v1',
        published_at: iso(r.decisionAsOf + 3_600_000),
        available_at: iso(r.decisionAsOf + 3_700_000),
        quality_status: 'valid' as const,
        rows: [
          { side: 'buy' as const, rank: 1, raw_seat_name: '合成证券某地第一营业部', canonical_amount_cny: '15230000.00', resolution_status: 'resolved' as const, seat_id: uuid('seat:1'), seat_version: 1, resolution_evidence: opaque({ match: 'exact_name' }) },
          { side: 'buy' as const, rank: 2, raw_seat_name: '机构专用（合成）', canonical_amount_cny: '9100000.00', resolution_status: 'report_local_label' as const, seat_id: null, seat_version: null, resolution_evidence: opaque({}) },
          { side: 'sell' as const, rank: 1, raw_seat_name: '合成证券某区营业部', canonical_amount_cny: '12800000.00', resolution_status: 'ambiguous' as const, seat_id: null, seat_version: null, resolution_evidence: opaque({ candidates: 2 }) },
          { side: 'sell' as const, rank: 2, raw_seat_name: '无法对应的营业部名称', canonical_amount_cny: '4500000.00', resolution_status: 'unresolved' as const, seat_id: null, seat_version: null, resolution_evidence: opaque({}) },
        ],
        policy_usable_at: null,
      }]
    : [];
  return { data: { instrument_id: inst.id, release_id: r.id, reports: reports.slice(offset, offset + PAGE) }, total: reports.length };
}
