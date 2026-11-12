/**
 * Typed client for the ext-B operations (/v1/x/<kebab-op>) and the three supporting reads (/v1/x/b/*).
 * Why: the generated openapi-fetch client only knows the 31 core operations; this mirrors its error handling (every non-2xx is
 * an ApiError with the typed code, network failures are status 0). Conventions fixed by the backend: GET -> every request field
 * is a query parameter (objects as JSON text, null/undefined omitted); POST -> JSON body (`idempotency_key` inside) plus the
 * `Idempotency-Key` header with the SAME value. Types follow extension.schema.json (PEX10 / PEX04 / PEX06 / PEX11).
 */
import { ApiError, newIdempotencyKey } from '../../../api/client';
import type { Envelope, ErrorBody, ResponseMeta } from '../../../api/types';
import { API_ORIGIN } from '../../../config';

export type Params = Record<string, unknown>;
export type Truth = 'true' | 'false' | 'unknown';
export interface PageToken { next_cursor: string | null; snapshot_hash: string }
export interface SnapshotRef { snapshot_id: string; dataset_id: string; manifest_version: number; content_hash: string }
export interface ArtifactRef { ref: string; content_hash: string; contract_id: string; schema_version: string }
export interface JobReceipt { job_run_id: string; resource_id: string; run_state: string; poll_operation_id: string }
export interface StrategyInstanceRef { instance_id: string; strategy_id: string; strategy_version: number }

/** Condition subset used by screens (all / any / not / compare). Kept loose on purpose: the server validates the whitelist. */
export type Cond = { node: 'all' | 'any'; clock: unknown; children: Cond[] } | { node: 'not'; clock: unknown; child: Cond } | CompareCond;
export interface FeatureOperand { kind: 'feature'; feature_id: string; definition_version: string; instrument_id: string; timeframe: string; field_path: string; unit: string; resolution_policy_version: string; initial_feature_key_hash: string | null }
export interface ConstantOperand { kind: 'constant'; value: string; unit: string }
export interface CompareCond { node: 'compare'; clock: unknown; operator: 'lt' | 'lte' | 'gt' | 'gte' | 'eq'; left: FeatureOperand | ConstantOperand; right: FeatureOperand | ConstantOperand }
export interface SortSpec { field_id: string; direction: 'asc' | 'desc'; nulls: 'first' | 'last' }
export interface ScreenAst { condition: Cond; allowed_field_catalog_version: string; sort: SortSpec[] }
export interface ScreenParseIssue { source_start: number; source_end: number; text: string; reason_code: 'unsupported' | 'ambiguous' | 'unit_required' | string }
export interface ParseResult { ast: ScreenAst | null; unresolved: ScreenParseIssue[]; complete: boolean }
export interface TypedFeature { feature_id: string; definition_version: string; key_hash: string; result: { feature_key_hash: string; artifact: ArtifactRef | null; quality_status: string; result_state: string } }
export interface ScreenResultRow { instrument_id: string; condition_value: Truth; features: TypedFeature[]; sort_position: number }
export interface CapabilityGap { dataset_id: string; requirement: string; reason_code: string }
export interface ScreenRunPage { rows: ScreenResultRow[]; page: PageToken; capability_gaps: CapabilityGap[] }
export interface SavedScreen { screen_id: string; screen_version: number; display_name: string; ast: ScreenAst; universe_policy_version: string }
export interface Metric { name: string; definition_version: string; value: string | null; unit: string; sample_count: number; missing_reason: string | null }
export interface AnalogNeighbor { instrument_id: string; case_as_of: string; feature_key_hash: string; distance: number; outcome_end: string; outcome: Metric | null }
export interface OpeningLot { instrument_id: string; quantity: number; eligible_on: string; cost_cny: string }
export interface ReplaySpec {
  instrument_ids: string[]; window: { start: string; end: string }; cash_cny: string; opening_lots: OpeningLot[]; execution_policy_version: string;
  cost_policy_version: string; benchmark_policy_version: string; training_mode: 'fitted' | 'frozen' | 'no_fit'; random_seed: number;
}
export interface ScreenContext {
  input_snapshot_refs: SnapshotRef[]; decision_as_of: string; universe_policy_versions: string[]; field_catalog_version: string;
  playbook_instances: Array<StrategyInstanceRef & { display_name: string; playbook_id: string }>;
  replay: {
    evaluation_context: { evaluation_context_id: string; train_start: string; train_end: string; validation_start: string; validation_end: string; cost_policy_version: string; decision_as_of: string } | null;
    cost_policy_versions: string[];
    instances: Array<StrategyInstanceRef & { display_name: string; tactic: { kind: string; variant?: string; mode?: string; label?: string }; parameters: Record<string, unknown>; hypothesis_id: string; hypothesis_version: number }>;
  };
}
export interface InstrumentLabel { instrument_id: string; code: string; name: string | null; is_risk_warning: boolean | null }

// ---- transport ---------------------------------------------------------------------------------------------------
export function encodeQueryValue(v: unknown): string {
  return typeof v === 'object' ? JSON.stringify(v) : String(v);
}
export function encodeQuery(params: Params): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null) continue;
    sp.append(k, encodeQueryValue(v));
  }
  return sp.toString();
}
type ErrBody = { error?: ErrorBody; meta?: ResponseMeta } | null;
const asErrBody = (b: unknown): ErrBody => (typeof b === 'object' && b !== null ? (b as ErrBody) : null);

async function request<T>(method: 'GET' | 'POST', path: string, init: { query?: Params; body?: unknown; key?: string; signal?: AbortSignal }): Promise<Envelope<T>> {
  const qs = init.query ? encodeQuery(init.query) : '';
  const url = `${API_ORIGIN}/v1/x/${path}${qs ? `?${qs}` : ''}`;
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (method === 'POST') {
    headers['Content-Type'] = 'application/json';
    headers['Idempotency-Key'] = init.key ?? newIdempotencyKey();
  }
  let res: Response;
  try {
    res = await fetch(url, { method, headers, credentials: 'same-origin', body: method === 'POST' ? JSON.stringify(init.body ?? {}) : undefined, signal: init.signal });
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw e;
    throw new ApiError(0, null, '网络不可用或服务无响应');
  }
  let body: unknown = null;
  try { body = await res.json(); } catch { body = null; }
  if (!res.ok) throw new ApiError(res.status, asErrBody(body));
  if (typeof body !== 'object' || body === null || !('data' in body)) throw new ApiError(res.status, null, '服务返回了无法识别的内容');
  return body as Envelope<T>;
}
const get = <T>(path: string, query?: Params, signal?: AbortSignal) => request<T>('GET', path, { query, signal });
/** POST with a body carrying `idempotency_key`; the header repeats it (the server rejects a mismatch). */
function post<T>(path: string, body: Record<string, unknown>, key = newIdempotencyKey()) {
  return request<T>('POST', path, { body: { ...body, idempotency_key: key }, key });
}

export const isNotReady = (e: unknown): boolean => e instanceof ApiError && e.code === 'RESULT_NOT_READY';
export function explain(e: unknown): string {
  if (e instanceof ApiError) return `${e.message}（${e.code}）`;
  return e instanceof Error ? e.message : '未知错误';
}

// ---- operations --------------------------------------------------------------------------------------------------
export const FIELD_CATALOG_VERSION = 'screen-fields-v1';
export const extB = {
  parseScreenText: (text: string, signal?: AbortSignal) =>
    get<ParseResult>('parse-screen-text', { text, field_catalog_version: FIELD_CATALOG_VERSION, locale: 'zh-CN' }, signal),
  runScreen: (b: { ast: ScreenAst; universe_policy_version: string; input_snapshot_refs: SnapshotRef[]; decision_as_of: string; strategy_instances: StrategyInstanceRef[] }, key?: string) =>
    post<{ receipt: JobReceipt }>('run-screen', b, key),
  saveScreen: (b: { screen_id: string; expected_screen_version: number; display_name: string; ast: ScreenAst; universe_policy_version: string }, key?: string) =>
    post<{ screen: { object_id: string; version: number; content_hash: string } }>('save-screen', b, key),
  queryScreenRun: (screen_run_id: string, limit = 50, cursor?: string | null, signal?: AbortSignal) =>
    get<ScreenRunPage>('query-screen-run', { screen_run_id, page: { limit, ...(cursor ? { cursor } : {}) } }, signal),
  listSavedScreens: (limit = 50, cursor?: string | null, signal?: AbortSignal) =>
    get<{ screens: SavedScreen[]; page: PageToken }>('list-saved-screens', { page: { limit, ...(cursor ? { cursor } : {}) } }, signal),
  queryAnalogCases: (q: { instrument_id: string; release_id: string; feature_key_hash: string; selection_policy_version: string }, limit = 50, cursor?: string | null) =>
    get<{ study: ArtifactRef; neighbors: AnalogNeighbor[]; page: PageToken }>('query-analog-cases', { ...q, page: { limit, ...(cursor ? { cursor } : {}) } }),
  queryIntervalStatistics: (q: { instrument_id: string; snapshot_id: string; window: { start: string; end: string }; adjustment_mode: 'raw' | 'forward_adjusted' | 'backward_adjusted'; metric_policy_version: string }) =>
    get<{ metrics: Metric[]; exploratory: boolean; input_snapshot: SnapshotRef }>('query-interval-statistics', q),
  scheduleTacticReplay: (b: { instance_id: string; hypothesis_id: string; hypothesis_version: number; evaluation_context_id: string; replay_spec: ReplaySpec }, key?: string) =>
    post<{ receipt: JobReceipt; context_contract_version: string }>('schedule-tactic-replay', b, key),
  registerStrategyDraft: (b: { strategy_id: string; expected_latest_version: number; draft: Record<string, unknown> }, key?: string) =>
    post<{ strategy: { strategy_id: string; strategy_version: number }; instance_id: string | null }>('register-strategy-draft', b, key),
  queryStrategyVersion: (strategy_id: string, strategy_version: number) =>
    get<{ strategy: StrategyLite; evaluation_ids: string[] }>('query-strategy-version', { strategy_id, strategy_version }),
  // supporting reads
  context: (signal?: AbortSignal) => get<ScreenContext>('b/context', undefined, signal),
  instrumentLabels: (q: { ids?: string[]; codes?: string[] }) => get<InstrumentLabel[]>('b/instrument-labels', { ids: q.ids?.join(','), codes: q.codes?.join(',') }),
  analogKey: (instrument_id: string, release_id: string) => get<{ feature_key_hash: string; selection_policy_versions: string[]; default_horizon_sessions: number; decision_as_of: string }>('b/analog-key', { instrument_id, release_id }),
  csvUrl: (screen_run_id: string) => `${API_ORIGIN}/v1/x/b/screen-run-csv?${encodeQuery({ screen_run_id })}`,
};
export interface StrategyLite {
  strategy_id: string; strategy_version: number; display_name: string; role: string; method_family: string; horizon_id: string;
  specification: Record<string, unknown>; tags: Array<{ taxonomy_id: string; node_id: string }>;
  instances: Array<{ instance_id: string; lifecycle: string; implementation_status: string; reproduction_status: string; health: string }>;
}
