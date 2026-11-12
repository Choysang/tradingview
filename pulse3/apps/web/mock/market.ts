/**
 * Synthetic market world: instruments, sectors, releases, rankings, bars, quotes.
 * All values are hypothetical. Bars are aggregated here only so the mock can answer
 * getStockBars; the web app itself never aggregates or computes indicators.
 */
import type {
  AdjustedBar, AnalysisRelease, BarWindow, CandidatePool, CanonicalBar, Instrument, QuoteSnapshot, Ranking,
  ReleaseComponent, SectorOverview,
} from '../src/api/types';
import { dec, hash64, iso, rng, uuid } from './util.ts';

export const SLOT_KEY_HINT = 'market.default';

export interface SynInstrument {
  idx: number;
  id: string;
  name: string;
  symbol: string;
  venue: 'XSHG' | 'XSHE';
  industry: string;
  concept: string | null;
  base: number;
  risk: boolean;
}

const INDUSTRY = ['合成板块·新能源', '合成板块·半导体', '合成板块·医药', '合成板块·消费', '合成板块·金融'] as const;
const CONCEPT = ['合成题材·AI算力', '合成题材·储能'] as const;

const RAW: Array<[string, number, number, string | null]> = [
  ['合成·光伏组件甲', 0, 18.4, '合成题材·储能'],
  ['合成·锂电材料乙', 0, 32.1, '合成题材·储能'],
  ['合成·储能系统丙', 0, 12.7, '合成题材·储能'],
  ['合成·芯片设计丁', 1, 45.6, '合成题材·AI算力'],
  ['合成·半导体设备戊', 1, 27.9, '合成题材·AI算力'],
  ['合成·存储模组己', 1, 15.3, '合成题材·AI算力'],
  ['合成·创新药庚', 2, 21.8, null],
  ['合成·医疗器械辛', 2, 38.2, null],
  ['合成·食品饮料壬', 3, 9.6, null],
  ['合成·家电制造癸', 3, 24.4, null],
  ['合成·券商子丑', 4, 11.2, null],
  ['合成·银行寅卯', 4, 6.8, null],
];

export const INSTRUMENTS: SynInstrument[] = RAW.map(([name, ind, base, concept], i) => ({
  idx: i,
  id: uuid(`inst:${i + 1}`),
  name,
  symbol: `SAMPLE_${String(i + 1).padStart(3, '0')}`,
  venue: i % 2 === 0 ? 'XSHG' : 'XSHE',
  industry: INDUSTRY[ind] as string,
  concept,
  base,
  risk: i === 5,
}));

export const instrumentById = (id: string): SynInstrument | undefined => INSTRUMENTS.find((x) => x.id === id);

export function toInstrument(i: SynInstrument): Instrument {
  return {
    instrument_id: i.id,
    venue_code: i.venue,
    instrument_type: 'equity',
    currency: 'CNY',
    exchange_timezone: 'Asia/Shanghai',
    display_name: i.name,
    state_version: 1,
    source_symbols: [{ source_id: 'syn.synthetic', source_symbol: i.symbol }],
    trading_status: 'active',
    is_risk_warning: i.risk,
    board_code: 'main',
  };
}

export const industryNodes = [...INDUSTRY];
export const conceptNodes = [...CONCEPT];

export const taxonomyNodes = (taxonomyId: string): string[] => (taxonomyId === 'concept' ? [...CONCEPT] : [...INDUSTRY]);
export const nodeMembers = (taxonomyId: string, node: string): SynInstrument[] =>
  INSTRUMENTS.filter((i) => (taxonomyId === 'concept' ? i.concept === node : i.industry === node));

// ---------------------------------------------------------------- releases

export interface ReleaseRec {
  n: number;
  id: string;
  decisionAsOf: number;
  publishedAt: number;
}

export const HORIZON_IDS = ['short_term', 'swing', 'long_term'] as const;
export const POOL_ID = uuid('pool:heating');

export const HEATING_COMPONENTS: Array<{ key: string; state: ReleaseComponent['component_state'] }> = [
  { key: 'heating.relative_strength_change', state: 'READY' },
  { key: 'heating.volume_turnover_acceleration', state: 'READY' },
  { key: 'heating.breadth_diffusion', state: 'READY' },
  { key: 'heating.leader_confirmation', state: 'MISSING' },
  { key: 'heating.news_novelty', state: 'STALE' },
  { key: 'heating.community_growth', state: 'READY' },
  { key: 'heating.fund_proxy', state: 'FAILED' },
];

export function buildRelease(r: ReleaseRec): AnalysisRelease {
  const comp = (key: string, state: ReleaseComponent['component_state']): ReleaseComponent => ({
    component_key: key,
    component_state: state,
    artifact: {
      ref: state === 'MISSING' ? `absent:${key}` : `artifact:${key}:${r.n}`,
      content_hash: state === 'MISSING' ? '0'.repeat(64) : hash64(`${key}:${r.n}`),
      contract_id: 'urn:pulse:contracts:v3#/$defs/FeatureResult',
      schema_version: '3.0.0',
    },
    snapshot_id: state === 'MISSING' ? null : uuid('snap:membership'),
    input_version_hash: hash64(`in:${key}:${r.n}`),
    computed_at: state === 'MISSING' ? null : iso(r.decisionAsOf + 60_000),
    valid_until: state === 'STALE' ? iso(r.decisionAsOf - 3_600_000) : iso(r.decisionAsOf + 86_400_000),
  });
  const required = ['market_ranking', 'sector_ranking', 'candidate_pool', 'workspace', 'bar_window'];
  return {
    release_id: r.id,
    release_state: 'READY',
    decision_as_of: iso(r.decisionAsOf),
    availability_policy_version: 'synthetic-observed-clock-v1',
    universe_policy_version: 'synthetic-main-board-v1',
    market_rules_version: 'synthetic-rules-v1',
    required_components: required,
    release_hash: hash64(`release:${r.n}`),
    published_at: iso(r.publishedAt),
    components: [
      ...required.map((k) => comp(k, 'READY')),
      comp(`candidate_pool:${POOL_ID}`, 'READY'),
      ...HEATING_COMPONENTS.map((c) => comp(c.key, c.state)),
    ],
  };
}

// ---------------------------------------------------------------- rankings

function shuffled<T>(arr: T[], seed: string): T[] {
  const rnd = rng(seed);
  const out = [...arr];
  for (let i = out.length - 1; i > 0; i -= 1) {
    const j = Math.floor(rnd() * (i + 1));
    [out[i], out[j]] = [out[j] as T, out[i] as T];
  }
  return out;
}

export function planIdFor(instId: string, kind: 'main' | 't' | 'grid'): string {
  return uuid(`plan:${instId}:${kind}`);
}
export const planVersionFor = (r: ReleaseRec): number => r.n;

export function rankingId(r: ReleaseRec, horizon: string, scope = 'market'): string {
  return uuid(`rank:${r.id}:${horizon}:${scope}`);
}

export function orderedInstruments(r: ReleaseRec, horizon: string, members: SynInstrument[] = INSTRUMENTS): SynInstrument[] {
  return shuffled(members, `${r.id}|${horizon}`);
}

export function buildRanking(r: ReleaseRec, horizon: string, target: string, scope: { kind: 'market' } | { kind: 'sector'; taxonomy: string; version: number; node: string }, entriesSlice: [number, number]): Ranking {
  const members = scope.kind === 'market' ? INSTRUMENTS : nodeMembers(scope.taxonomy, scope.node);
  const ordered = orderedInstruments(r, horizon, members);
  const all = ordered.map((inst, i) => {
    const rank = i + 1;
    const hasPlan = inst.idx !== 8; // one instrument without a plan: missing data
    const status = inst.idx === 8 ? 'missing_data' : inst.idx === 2 ? 'blocked' : rank <= 2 || inst.idx === 0 ? 'actionable' : 'watching';
    return {
      instrument_id: inst.id,
      alpha_rank: rank,
      action_rank: status === 'actionable' ? rank : null,
      relative_rank_score: dec(1 - i * 0.071, 4),
      calibrated_probability: horizon === 'short_term' && rank <= 2 ? dec(0.56 - rank * 0.01, 4) : null,
      action_status: status as 'actionable' | 'blocked' | 'missing_data' | 'watching',
      explanation_ref: `synthetic://explain/${horizon}/${inst.id}`,
      plan_ref: hasPlan ? { plan_id: planIdFor(inst.id, 'main'), plan_version: planVersionFor(r) } : null,
    };
  });
  const rid = scope.kind === 'market' ? rankingId(r, horizon) : rankingId(r, horizon, `sector:${scope.taxonomy}:${scope.node}`);
  return {
    ranking_id: rid,
    release_id: r.id,
    ranking_scope: scope.kind,
    scope_key: scope.kind === 'market' ? 'syn.market' : scope.node,
    horizon_id: horizon,
    forecast_target: target,
    ranking_policy_version: 'synthetic-ranking-v1',
    taxonomy_node: scope.kind === 'market' ? null : { taxonomy_id: scope.taxonomy, taxonomy_version: scope.version, node_id: scope.node },
    membership_snapshot_id: scope.kind === 'market' ? null : uuid(`members:${scope.taxonomy}:${scope.node}`),
    entries: all.slice(entriesSlice[0], entriesSlice[1]),
  };
}

export function rankingTotal(scope: { kind: 'market' } | { kind: 'sector'; taxonomy: string; node: string }): number {
  return scope.kind === 'market' ? INSTRUMENTS.length : nodeMembers(scope.taxonomy, scope.node).length;
}

export function buildSectorOverview(r: ReleaseRec, taxonomy: string, version: number, rankKind: SectorOverview['entries'][number]['rank_kind'], horizon: string, target: string): SectorOverview {
  const nodes = shuffled(taxonomyNodes(taxonomy), `${r.id}|${taxonomy}|${rankKind}|${horizon}`);
  return {
    ranking_id: uuid(`sov:${r.id}:${taxonomy}:${rankKind}:${horizon}`),
    release_id: r.id,
    ranking_scope: 'sector_overview',
    taxonomy_id: taxonomy,
    taxonomy_version: version,
    horizon_id: horizon,
    forecast_target: target,
    ranking_policy_version: 'synthetic-sector-ranking-v1',
    entries: nodes.map((node, i) => {
      const members = nodeMembers(taxonomy, node);
      const observed = node.includes('新能源') ? Math.max(members.length - 1, 0) : members.length;
      return {
        taxonomy_node: { taxonomy_id: taxonomy, taxonomy_version: version, node_id: node },
        rank_kind: rankKind,
        rank: i + 1,
        score: dec(0.9 - i * 0.12, 4),
        member_count: members.length,
        observed_member_count: observed,
        membership_snapshot_id: uuid(`members:${taxonomy}:${node}`),
        explanation_ref: `synthetic://explain/sector/${node}`,
        quality_status: observed < members.length ? 'partial' : 'valid',
      };
    }),
  };
}

export function buildPool(r: ReleaseRec): CandidatePool {
  const picks = [INSTRUMENTS[0], INSTRUMENTS[3], INSTRUMENTS[4], INSTRUMENTS[1], INSTRUMENTS[6], INSTRUMENTS[9]] as SynInstrument[];
  const states = ['actionable', 'qualified', 'qualified', 'observing', 'observing', 'cooling'] as const;
  const triggers = ['relative_strength_accel', 'volume_burst', 'breadth_diffusion', 'news_novelty', 'community_growth', 'leader_lost'];
  return {
    pool_id: POOL_ID,
    release_id: r.id,
    policy_id: 'synthetic.heating',
    policy_version: 'synthetic-heating-v1',
    items: picks.map((inst, i) => ({
      pool_event_id: uuid(`pool-ev:${r.id}:${inst.id}`),
      instrument_id: inst.id,
      event_sequence: i + 1,
      taxonomy_node: { taxonomy_id: 'industry', taxonomy_version: 1, node_id: inst.industry },
      membership_snapshot_id: uuid(`members:industry:${inst.industry}`),
      current_state: states[i] as CandidatePool['items'][number]['current_state'],
      trigger_code: triggers[i] as string,
      plan_ref: i < 2 ? { plan_id: planIdFor(inst.id, 'main'), plan_version: planVersionFor(r) } : null,
      available_at: iso(r.decisionAsOf + (i + 1) * 120_000),
    })),
  };
}

// ---------------------------------------------------------------- bars

const MIN = 60_000;
const DAY = 86_400_000;
const CST = 8 * 3_600_000;

interface RawMin {
  open: number; high: number; low: number; close: number; vol: number; t: number; missing: boolean;
}
interface RawDay {
  date: string; open: number; high: number; low: number; close: number; vol: number; t: number;
}

function fmtDate(ms: number): string {
  return new Date(ms + CST).toISOString().slice(0, 10);
}

/** trading days (weekdays) ending at the CST date of `endMs`, oldest first. */
function tradingDays(endMs: number, n: number): number[] {
  const out: number[] = [];
  let t = Math.floor((endMs + CST) / DAY) * DAY - CST; // CST midnight in UTC ms
  while (out.length < n) {
    const wd = new Date(t + CST).getUTCDay();
    if (wd !== 0 && wd !== 6) out.push(t);
    t -= DAY;
  }
  return out.reverse();
}

const round2 = (n: number): number => Math.round(n * 100) / 100;

export function dailySeries(inst: SynInstrument, endMs: number): RawDay[] {
  const rnd = rng(`daily:${inst.id}`);
  const days = tradingDays(endMs, 520);
  let price = inst.base * 0.82;
  return days.map((t) => {
    const drift = 0.0006 + (inst.idx % 3 === 0 ? 0.0004 : -0.0001);
    const ret = drift + (rnd() - 0.5) * 0.045;
    const open = price;
    const close = Math.max(1, price * (1 + ret));
    const high = Math.max(open, close) * (1 + rnd() * 0.012);
    const low = Math.min(open, close) * (1 - rnd() * 0.012);
    price = close;
    return { date: fmtDate(t), open: round2(open), high: round2(high), low: round2(low), close: round2(close), vol: Math.round((8e5 + rnd() * 6e5) / 100) * 100, t };
  });
}

export function minuteSeries(inst: SynInstrument, endMs: number): RawMin[] {
  const dayStart = Math.floor((endMs + CST) / DAY) * DAY - CST;
  const daily = dailySeries(inst, endMs);
  const last = daily[daily.length - 1] as RawDay;
  const rnd = rng(`min:${inst.id}`);
  const sessions: Array<[number, number]> = [[dayStart + 9.5 * 3_600_000, 120], [dayStart + 13 * 3_600_000, 120]];
  let price = last.open;
  const out: RawMin[] = [];
  let idx = 0;
  for (const [start, count] of sessions) {
    for (let i = 0; i < count; i += 1) {
      const open = price;
      const close = Math.max(1, price * (1 + (rnd() - 0.5) * 0.004));
      const high = Math.max(open, close) * (1 + rnd() * 0.0015);
      const low = Math.min(open, close) * (1 - rnd() * 0.0015);
      price = close;
      out.push({
        open: round2(open), high: round2(high), low: round2(low), close: round2(close),
        vol: Math.round((2000 + rnd() * 6000) / 100) * 100, t: start + i * MIN,
        missing: idx === 77, // one source-missing minute (never forward-filled)
      });
      idx += 1;
    }
  }
  return out;
}

export function lastClose(inst: SynInstrument, endMs: number): { last: number; prev: number } {
  const d = dailySeries(inst, endMs);
  return { last: (d[d.length - 1] as RawDay).close, prev: (d[d.length - 2] as RawDay).close };
}

interface Agg {
  open: number; high: number; low: number; close: number; vol: number; start: number; end: number; date: string; final: boolean; missing: boolean;
}

function aggregate<T extends { t: number }>(rows: T[], keyFn: (r: T) => string, pick: (r: T) => { open: number; high: number; low: number; close: number; vol: number; missing?: boolean }, spanMs: (first: T) => number): Agg[] {
  const groups = new Map<string, T[]>();
  for (const r of rows) {
    const k = keyFn(r);
    const g = groups.get(k);
    if (g) g.push(r); else groups.set(k, [r]);
  }
  return [...groups.values()].map((g) => {
    const live = g.filter((x) => !pick(x).missing);
    const first = g[0] as T;
    const lastRow = g[g.length - 1] as T;
    const prices = live.map((x) => pick(x));
    return {
      open: prices.length ? (prices[0] as { open: number }).open : NaN,
      high: prices.length ? Math.max(...prices.map((p) => p.high)) : NaN,
      low: prices.length ? Math.min(...prices.map((p) => p.low)) : NaN,
      close: prices.length ? (prices[prices.length - 1] as { close: number }).close : NaN,
      vol: prices.reduce((a, p) => a + p.vol, 0),
      start: first.t,
      end: first.t + spanMs(first),
      date: fmtDate(lastRow.t),
      final: true,
      missing: live.length === 0,
    };
  });
}

function weekKey(t: number): string {
  const d = new Date(t + CST);
  const day = Math.floor((d.getTime()) / DAY);
  const monday = day - ((d.getUTCDay() + 6) % 7);
  return String(monday);
}

export function barsFor(inst: SynInstrument, tf: string, endMs: number): Agg[] {
  if (tf.endsWith('m') && !tf.endsWith('mo')) {
    const n = Number(tf.slice(0, -1));
    const mins = minuteSeries(inst, endMs);
    if (n === 1) {
      return mins.map((m) => ({ open: m.open, high: m.high, low: m.low, close: m.close, vol: m.vol, start: m.t, end: m.t + MIN, date: fmtDate(m.t), final: true, missing: m.missing }));
    }
    return aggregate(mins, (m) => String(Math.floor(m.t / (n * MIN))), (m) => m, () => n * MIN).map((a) => a);
  }
  if (tf === '1h') {
    return aggregate(minuteSeries(inst, endMs), (m) => String(Math.floor(m.t / (60 * MIN))), (m) => m, () => 60 * MIN);
  }
  const daily = dailySeries(inst, endMs);
  const dayAgg = (keyFn: (d: RawDay) => string, last: boolean): Agg[] => {
    const aggs = aggregate(daily, keyFn, (d) => d, () => DAY);
    if (aggs.length && last) (aggs[aggs.length - 1] as Agg).final = false;
    return aggs;
  };
  switch (tf) {
    case '1d': return daily.map((d) => ({ open: d.open, high: d.high, low: d.low, close: d.close, vol: d.vol, start: d.t, end: d.t + DAY, date: d.date, final: true, missing: false }));
    case '1w': return dayAgg((d) => weekKey(d.t), true);
    case '1mo': return dayAgg((d) => d.date.slice(0, 7), true);
    case '1y': return dayAgg((d) => d.date.slice(0, 4), true);
    default: return [];
  }
}

const EX_DATE_IDX = 0.62; // fraction of the daily history where a synthetic ex-rights event sits
const ADJ = 0.94;

export function buildBarWindow(inst: SynInstrument, tf: string, mode: 'raw' | 'forward_adjusted' | 'backward_adjusted', adjVersion: string | undefined, sessionVersion: string, startMs: number, endMs: number, decisionMs: number): BarWindow {
  const all = barsFor(inst, tf, decisionMs);
  const exCut = all.length ? (all[Math.floor(all.length * EX_DATE_IDX)] as Agg).start : 0;
  const rows = all.filter((a) => a.start >= startMs && a.start <= endMs);
  const snapshotId = uuid('snap:bars');
  const bars: Array<CanonicalBar | AdjustedBar> = rows.map((a) => {
    const common = {
      instrument_id: inst.id,
      trade_date: a.date,
      timeframe: tf,
      session_scope: 'regular',
      bar_open_time: iso(a.start),
      bar_close_time: iso(a.end),
      snapshot_id: snapshotId,
      currency: 'CNY' as const,
      is_final: a.final,
      available_at: iso(Math.min(a.end + 5_000, decisionMs)),
      quality_status: (a.missing ? 'partial' : 'valid') as 'partial' | 'valid',
    };
    const miss = a.missing;
    const fq = miss
      ? Object.fromEntries(['open', 'high', 'low', 'close'].map((k) => [`${k}_price${mode === 'raw' ? '_raw' : ''}`, { status: 'missing' as const, reason: '数据源未提供该时段', raw_object_id: null, value_origin: 'not_available' as const }]))
      : {};
    if (mode === 'raw') {
      const bar: CanonicalBar = {
        ...common,
        adjustment_mode: 'raw',
        open_price_raw: miss ? null : dec(a.open),
        high_price_raw: miss ? null : dec(a.high),
        low_price_raw: miss ? null : dec(a.low),
        close_price_raw: miss ? null : dec(a.close),
        volume_shares: miss ? null : a.vol,
        turnover_amount_cny: miss ? null : dec(a.vol * a.close, 2),
        exchange_reference_price_raw: miss ? null : dec(a.open),
        field_quality: fq,
        policy_usable_at: null,
      };
      return bar;
    }
    const f = mode === 'forward_adjusted' ? (a.start < exCut ? ADJ : 1) : (a.start >= exCut ? 1 / ADJ : 1);
    const px = (v: number): string | null => (miss ? null : dec(v * f, 4));
    const bar: AdjustedBar = {
      ...common,
      adjustment_mode: mode,
      adjustment_version: adjVersion ?? 'adj-v1',
      open_price: px(a.open), high_price: px(a.high), low_price: px(a.low), close_price: px(a.close),
      volume_shares: miss ? null : a.vol,
      turnover_amount_cny: miss ? null : dec(a.vol * a.close, 2),
      raw_snapshot_id: snapshotId,
      field_quality: fq,
      policy_usable_at: null,
    };
    return bar;
  });
  const hasHole = bars.some((b) => b.quality_status !== 'valid');
  return {
    instrument_id: inst.id,
    snapshot_id: snapshotId,
    timeframe: tf,
    session_policy_version: sessionVersion,
    adjustment_mode: mode,
    adjustment_version: mode === 'raw' ? null : adjVersion ?? 'adj-v1',
    start: iso(startMs),
    end: iso(endMs),
    window_complete: !hasHole,
    window_hash: hash64(`win:${inst.id}:${tf}:${mode}:${startMs}:${endMs}`),
    bars,
  };
}

// ---------------------------------------------------------------- quote

let quoteVersion = 100;

export function buildQuote(inst: SynInstrument, now: number, decisionMs: number, stale: boolean): QuoteSnapshot {
  const { last } = lastClose(inst, decisionMs);
  const rnd = rng(`quote:${inst.id}:${Math.floor(now / 5000)}`);
  const price = round2(last * (1 + (rnd() - 0.5) * 0.02));
  const observed = stale ? now - 20 * 60_000 : now - 2_000;
  quoteVersion += 1;
  return {
    quote_snapshot_id: uuid(`quote:${inst.id}:${Math.floor(observed / 5000)}`),
    quote_policy_version: 'synthetic-l1-v1',
    quote: {
      instrument_id: inst.id,
      source_id: 'syn.synthetic',
      endpoint_id: 'synthetic_l1',
      source_schema_version: '1',
      mapping_version: '1',
      observation_id: uuid(`obs:${inst.id}:${observed}`),
      live_quote_version: quoteVersion,
      trade_date: fmtDate(observed),
      source_event_time: iso(observed),
      first_seen_at: iso(observed + 300),
      available_at: iso(observed + 600),
      currency: 'CNY',
      adjustment_mode: 'raw',
      last_price_raw: dec(price),
      bid_price_raw: dec(price - 0.01),
      ask_price_raw: dec(price + 0.01),
      bid_volume_shares: 1200,
      ask_volume_shares: 900,
      quality_status: 'valid',
      field_quality: {},
    },
    freshness_state: stale ? 'stale' : 'current',
    freshness_evaluated_at: iso(now),
  };
}
