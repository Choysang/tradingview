/**
 * Client + wire types of the lab backfill lane (/v1/x/bf/lab/*, contract backfill_lab.schema.json).
 * Why: the historical experiment ledger (#177/#178), the tear sheet (#160/#161) and the batch comparison (#36/#77) are extension reads/commands the
 * generated core client does not know. Same error handling as the other extension clients: a non-2xx is an ApiError, network failure is status 0.
 * Money / ratios are decimal strings; a missing value is null WITH a reason (shown as 缺失 + the reason, never 0). The browser only draws what the server computed.
 */
import { ApiError, newIdempotencyKey } from '../../../api/client';
import type { Envelope, ErrorBody, ResponseMeta } from '../../../api/types';
import { API_ORIGIN } from '../../../config';

export type Verdict = 'works' | 'mixed' | 'inconclusive' | 'fails';
export const VERDICTS: Verdict[] = ['works', 'mixed', 'inconclusive', 'fails'];

export interface LedgerImportInfo { ledger_sha256: string; label: string; entry_count: number; imported_at: string; import_ref: string; is_latest: boolean; source_file: string }
export interface EntryBrief { id: string; name: string | null; verdict: Verdict | null }
export interface LedgerEntry {
  id: string; date: string; round: string; track: string; name: string; hypothesis: string; config: string; results: Record<string, string>; verdict: Verdict;
  why: string; regime: string; reactivate_when: string; artifacts: string[]; supersedes: string[]; superseded_by: string[]; caveat: string | null; retired_families: string[];
}
export interface LedgerCounts { total: number; by_verdict: Record<Verdict, number>; by_track: Record<string, number> }
export interface LedgerList {
  ledger: LedgerImportInfo | null; empty_reason: string | null; counts: LedgerCounts | null; matched: number; offset: number; limit: number; next_offset: number | null;
  entries: LedgerEntry[]; note: string;
}
export interface RetiredFamily {
  family_id: string; name: string; tools: string[]; ledger_ids: string[]; also_see: string[]; old_rows: number[]; reason: string; reason_zh: string; entries: EntryBrief[];
}
export interface RetiredTools { version: string; source: string; ledger: LedgerImportInfo | null; query: { tool: string | null; ledger_id: string | null }; families: RetiredFamily[] }
export interface LedgerEntryDetail {
  ledger: LedgerImportInfo; entry: LedgerEntry; supersedes_entries: EntryBrief[]; superseded_by_entries: EntryBrief[]; retired_families: RetiredFamily[];
}
export interface LedgerFilter { verdict?: Verdict | ''; track?: string; q?: string }

export interface Metric { value: string | null; reason: string | null }
export const METRIC_NAMES = ['total_return', 'annualised_return', 'annualised_volatility', 'sharpe', 'sortino', 'max_drawdown', 'calmar', 'longest_drawdown_sessions',
  'profit_factor', 'win_rate', 'avg_win', 'avg_loss', 'payoff_ratio', 'avg_win_return', 'avg_loss_return', 'exposure'] as const;
export type MetricName = (typeof METRIC_NAMES)[number];
export interface DrawdownSpan { sessions: number; peak_session: string | null; recovery_session: string | null; recovered: boolean }
export interface TearSheet {
  version: string; periods_per_year: number; sessions: number; returns: number; missing_nav_sessions: number; first_session: string | null; last_session: string | null;
  trades: { closed: number; open: number; breakeven: number } | null; longest_drawdown: DrawdownSpan | null; metrics: Record<MetricName, Metric>;
}
export interface TearSheetData {
  subject: { kind: 'replay' | 'book'; id: string; label: string; catalog_id: string | null; experiment_id: string | null };
  window: { start: string | null; end: string | null; sessions: number }; basis: { initial_nav: string | null; trade_pnl: string | null; nav_basis: string };
  tearsheet: TearSheet; caveats: string[];
}
export type TearSheetSource = { catalogId: string } | { experimentId: string } | { bookKey: string };

export type RunState = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';
export interface BatchRow {
  sort_position: number; instrument_id: string; code: string | null; name: string | null; status: 'ok' | 'no_trades' | 'skipped'; skip_reason: string | null; tearsheet: TearSheet | null;
}
export interface BatchResult {
  version: string;
  screen: { screen_run_id: string; decision_as_of: string; universe_policy_version: string; true_rows: number; selected: number; panel_matches_screen: boolean };
  playbook: { id: string; catalog_id: string; name: string; family: string }; window: { start: string; end: string; sessions: number }; initial_cash: string;
  cost_policy_version: string; price_basis: string; rows: BatchRow[]; caveats: string[];
}
export interface BatchRunBrief {
  job_run_id: string; run_state: RunState; error_code: string | null; scheduled_for: string; completed_at: string | null;
  request: { screen_run_id: string; playbook_id: string; top_n: number; window_sessions: number }; summary: { instruments: number; with_trades: number; skipped: number } | null;
}
export interface BatchRun { run: BatchRunBrief; result: BatchResult | null; result_ref: string | null }
export interface BatchRunList { items: BatchRunBrief[]; page: { next_cursor: string | null; limit: number }; order: string }
export interface BatchRequest { screen_run_id: string; playbook_id: string; top_n?: number; window_sessions?: number }
export interface JobReceipt { job_run_id: string; resource_id: string; run_state: RunState; poll_operation_id: string }

type ErrBody = { error?: ErrorBody; meta?: ResponseMeta } | null;
const PREFIX = '/v1/x/bf/lab/';

async function call<T>(method: 'GET' | 'POST', path: string, query: Record<string, string | number | undefined | null>, body?: unknown, key?: string, signal?: AbortSignal): Promise<Envelope<T>> {
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

export const tearSheetQuery = (s: TearSheetSource): Record<string, string> => ('catalogId' in s ? { catalog_id: s.catalogId } : 'experimentId' in s ? { experiment_id: s.experimentId } : { book_key: s.bookKey });

export const bfLab = {
  ledger: (f: LedgerFilter, offset = 0, limit = 100, signal?: AbortSignal) =>
    call<LedgerList>('GET', 'ledger', { verdict: f.verdict, track: f.track, q: f.q?.trim(), offset, limit }, undefined, undefined, signal),
  ledgerEntry: (id: string, signal?: AbortSignal) => call<LedgerEntryDetail>('GET', `ledger/${encodeURIComponent(id)}`, {}, undefined, undefined, signal),
  retiredTools: (q: { tool?: string; ledger_id?: string } = {}, signal?: AbortSignal) => call<RetiredTools>('GET', 'retired-tools', q, undefined, undefined, signal),
  tearSheet: (s: TearSheetSource, signal?: AbortSignal) => call<TearSheetData>('GET', 'tearsheet', tearSheetQuery(s), undefined, undefined, signal),
  startBatch: (b: BatchRequest, key?: string) => {
    const k = key ?? newIdempotencyKey();
    return call<{ receipt: JobReceipt }>('POST', 'batch-compare', {}, { ...b, idempotency_key: k }, k);
  },
  batchRuns: (limit = 20, cursor?: string | null, signal?: AbortSignal) => call<BatchRunList>('GET', 'batch-compare', { limit, cursor }, undefined, undefined, signal),
  batchRun: (id: string, signal?: AbortSignal) => call<BatchRun>('GET', `batch-compare/${encodeURIComponent(id)}`, {}, undefined, undefined, signal),
};
