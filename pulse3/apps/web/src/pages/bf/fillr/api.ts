/**
 * Client + wire types of the fill-research lane (/v1/x/bf/fillr/*, contract backfill_fillr.schema.json).
 * Why: the playbook board (fitness / verdict / out-of-sample average / sparkline), the custom run, monthly returns, the factor daily series and the
 * on-demand analog study are extension reads / commands the generated core client does not know. Same error handling as the other extension clients:
 * a non-2xx is an ApiError, network failure is status 0. Ratios are decimal strings; a missing value is null WITH a reason (shown as 缺失, never 0).
 * Every command only QUEUES a job: the state stays queued until the worker `pulse.workers.fillr run` ran; the browser only draws what the server computed.
 */
import { ApiError, newIdempotencyKey } from '../../../api/client';
import type { Envelope, ErrorBody, ResponseMeta } from '../../../api/types';
import { API_ORIGIN } from '../../../config';

export type Verdict = 'effective' | 'regime_dependent' | 'weak' | 'overfit' | 'insufficient';
export type RunState = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';
export interface JobReceipt { job_run_id: string; resource_id: string; run_state: RunState; poll_operation_id: string }
export interface JobBrief { job_run_id: string; run_state: string; error_code: string | null }

export interface SampleStats { trades: number; win_rate: string | null; avg_return: string | null; t_stat: string | null; sharpe: string | null; sessions: number }
export interface BlockInfo { p_value: string | null; blocks: number; block: number; reliable: boolean; reason: string | null }
export interface EvaluationBlock {
  version: string; split_day: string | null; sample_label: 'out_of_sample' | 'in_sample_only'; full: SampleStats; in_sample: SampleStats; out_of_sample: SampleStats | null;
  recent: SampleStats; verdict: Verdict | null; verdict_reason: string; fitness: string | null; fitness_reasons: string[]; block: BlockInfo | null; missing: string[];
}
export interface RegimeNow { available: boolean; reason: string | null; release_id: string | null; trend: string | null; sentiment: string | null; through: string | null }
export type BoardState = 'evaluated' | 'not_run' | 'unavailable' | 'failed';
export interface BoardItem {
  catalog_id: string; playbook_id: string; name: string; family: string; horizon: string; state: BoardState; reason: string | null; experiment_id: string | null;
  completed_at: string | null; evaluation: EvaluationBlock | null; sparkline: string[]; sparkline_start: string | null; sparkline_end: string | null;
}
export interface PlaybookBoard {
  version: string; regime_now: RegimeNow; items: BoardItem[]; counts: { total: number; evaluated: number; not_run: number; unavailable: number; failed: number }; notes: string[];
}

export interface Overrides {
  params?: Record<string, number>; holding_days?: number; stop_loss_pct?: number | null; take_profit_pct?: number | null; trailing_stop_pct?: number | null;
  max_positions?: number; max_new_per_day?: number;
}
export interface CustomRunRequest { playbook_id: string; overrides: Overrides; label?: string }
export interface CustomRunReceipt { receipt: JobReceipt; draft: { strategy_id: string; strategy_version: number; instance_id: string | null }; worker_hint: string }
export interface EffectiveRules {
  params: Record<string, number>; holding_days: number; stop_loss_pct: number | null; take_profit_pct: number | null; trailing_stop_pct: number | null; max_positions: number; max_new_per_day: number;
}
export interface CustomTrade { code: string | null; entry_day: string; exit_day: string | null; entry_price: string; exit_price: string | null; gross_return: string | null; status: 'closed' | 'open'; exit_reason: string | null }
export interface CustomMetric { name: string; slice: string; value: string | null; unit: string; missing_reason: string | null }
export interface CustomRunResult {
  version: string; exploratory: boolean; playbook: { id: string; catalog_id: string; name: string }; request: Overrides; effective: EffectiveRules; base: EffectiveRules;
  window: { start: string; end: string; sessions: number; decision_as_of: string }; initial_cash: string; cost_policy_version: string; evaluation: EvaluationBlock;
  metrics: CustomMetric[]; equity: Array<{ d: string; nav: string | null }>; trades: { total: number; closed: number; shown: number; items: CustomTrade[] }; caveats: string[];
}
export interface CustomRunBrief {
  job_run_id: string; run_state: RunState; error_code: string | null; scheduled_for: string; completed_at: string | null; playbook_id: string; label: string | null;
  request: Overrides; strategy: { strategy_id: string; strategy_version: number } | null;
  summary: { verdict: Verdict | null; fitness: string | null; net_return: string | null; trades: number } | null;
}
export interface CustomRunList { items: CustomRunBrief[]; page: { next_cursor: string | null; limit: number }; order: string; worker_hint: string }
export interface CustomRun { run: CustomRunBrief; result: CustomRunResult | null; result_ref: string | null; worker_hint: string }

export interface MonthReturn { year: number; month: number; value: string | null; sessions: number; reason: string | null }
export interface MonthlyReturns {
  version: string; subject: { label: string; catalog_id: string; experiment_id: string }; window: { start: string | null; end: string | null; sessions: number };
  months: MonthReturn[]; basis: string; caveats: string[];
}

export type QueueState = 'ready' | 'queued' | 'running';
export interface JobQueued { state: QueueState; receipt: JobReceipt | null; worker_hint: string }
export interface FactorSeriesOne {
  horizon: number; slice: 'train' | 'validation'; days: number; ic_mean: string | null; icir: string | null; periods: number; long_short_return: string | null;
  ic: Array<{ d: string; ic: string | null }>; long_short: Array<{ d: string; period_return: string; cumulative: string }>; notes: string[];
}
export type FactorSeriesState = 'ready' | 'not_computed' | 'queued' | 'running' | 'failed' | 'no_experiment';
export interface FactorSeriesStatus {
  state: FactorSeriesState; reason: string | null; experiment_id: string | null; factor_id: string | null; job: JobBrief | null;
  series: { version: string; computed_from: string; items: FactorSeriesOne[] } | null; worker_hint: string;
}
export type AnalogState = 'ready' | 'not_built' | 'queued' | 'running' | 'failed';
export interface AnalogStatus {
  state: AnalogState; instrument_id: string; release_id: string; selection_policy_version: string; feature_key_hash: string; decision_as_of: string; job: JobBrief | null;
  study: { snapshot_id: string; content_hash: string; horizon_sessions: number; neighbors: number } | null; worker_hint: string;
}

type ErrBody = { error?: ErrorBody; meta?: ResponseMeta } | null;
const PREFIX = '/v1/x/bf/fillr/';
type Query = Record<string, string | number | undefined | null>;

async function call<T>(method: 'GET' | 'POST', path: string, query: Query, body?: unknown, key?: string, signal?: AbortSignal): Promise<Envelope<T>> {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) if (v !== undefined && v !== null && v !== '') sp.append(k, String(v));
  const qs = sp.toString();
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (method === 'POST') { headers['Content-Type'] = 'application/json'; headers['Idempotency-Key'] = key ?? newIdempotencyKey(); }
  let res: Response;
  try {
    res = await fetch(`${API_ORIGIN}${PREFIX}${path}${qs ? `?${qs}` : ''}`, { method, headers, credentials: 'same-origin', signal, body: method === 'POST' ? JSON.stringify(body ?? {}) : undefined });
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw e;
    throw new ApiError(0, null, '网络不可用或服务无响应');
  }
  let parsed: unknown = null;
  try { parsed = await res.json(); } catch { parsed = null; }
  if (!res.ok) throw new ApiError(res.status, typeof parsed === 'object' && parsed !== null ? (parsed as ErrBody) : null);
  if (typeof parsed !== 'object' || parsed === null || !('data' in parsed)) throw new ApiError(res.status, null, '服务返回了无法识别的内容');
  return parsed as Envelope<T>;
}

export const bfFillr = {
  board: (signal?: AbortSignal) => call<PlaybookBoard>('GET', 'playbook-board', {}, undefined, undefined, signal),
  startCustomRun: (b: CustomRunRequest, key?: string) => {
    const k = key ?? newIdempotencyKey();
    return call<CustomRunReceipt>('POST', 'custom-run', {}, { ...b, idempotency_key: k }, k);
  },
  customRuns: (limit = 20, signal?: AbortSignal) => call<CustomRunList>('GET', 'custom-runs', { limit }, undefined, undefined, signal),
  customRun: (id: string, signal?: AbortSignal) => call<CustomRun>('GET', `custom-runs/${encodeURIComponent(id)}`, {}, undefined, undefined, signal),
  monthlyReturns: (catalogId: string, signal?: AbortSignal) => call<MonthlyReturns>('GET', 'monthly-returns', { catalog_id: catalogId }, undefined, undefined, signal),
  factorSeries: (q: { factorId?: string; experimentId?: string }, signal?: AbortSignal) =>
    call<FactorSeriesStatus>('GET', 'factor-series', { factor_id: q.factorId, experiment_id: q.experimentId }, undefined, undefined, signal),
  queueFactorSeries: (experimentId: string, key?: string) => {
    const k = key ?? newIdempotencyKey();
    return call<JobQueued>('POST', 'factor-series', {}, { experiment_id: experimentId, idempotency_key: k }, k);
  },
  analogStatus: (q: { instrumentId: string; releaseId: string; policy: string }, signal?: AbortSignal) =>
    call<AnalogStatus>('GET', 'analog-study', { instrument_id: q.instrumentId, release_id: q.releaseId, selection_policy_version: q.policy }, undefined, undefined, signal),
  queueAnalog: (q: { instrumentId: string; releaseId: string; policy: string }, key?: string) => {
    const k = key ?? newIdempotencyKey();
    return call<JobQueued>('POST', 'analog-study', {}, { instrument_id: q.instrumentId, release_id: q.releaseId, selection_policy_version: q.policy, idempotency_key: k }, k);
  },
};
