/**
 * Client + wire types of the fusion backfill lane (/v1/x/bf/fusion/*, contract backfill_fusion.schema.json).
 * Why: the frozen round-5 score, its two forward books and the three forward experiments are extension reads the generated core client does
 * not know. Same error handling as the other extension clients: a non-2xx is an ApiError, a network failure is status 0.
 * Ratios, NAVs and scores are decimal strings; a missing value is null (shown as 缺失, never 0). The browser only draws what the server computed.
 */
import { ApiError } from '../../../api/client';
import type { Envelope, ErrorBody, ResponseMeta } from '../../../api/types';
import { API_ORIGIN } from '../../../config';

export type ExperimentKey = 'main' | 'h10' | 'monitor' | 'x20' | 'os20';
export interface DataFile { file: string; sha256: string }
export interface Preregistration { key: ExperimentKey; ledger_id: string; document: string; sha256: string | null }
export interface DatasetSpan { rows: number; dates: number; first_date: string | null; last_date: string | null }
export interface RunInfo { run_id: string; written_on: string; status: 'done' | 'stopped'; code: string | null; detail: string | null; session: string | null; seconds: string | null; peak_memory_mb: string | null }
export interface BookStatus { key: 'main' | 'h10'; last_date: string | null; rows: number }
export interface FusionStatus {
  score_name: string; implementation: string; current: DatasetSpan; asof: DatasetSpan; last_run: RunInfo | null; books: BookStatus[];
  frozen_data: DataFile[]; preregistrations: Preregistration[];
}
export interface ScoreRow { rank: number; code: string; display_name: string | null; score: string }
export interface FusionScores { date: string | null; kind: 'asof' | 'current' | null; available_dates: string[]; rule: string; rows: ScoreRow[] }
export interface BookRow {
  date: string; gate_w: string; new_buys_allowed: number; try_buy: string[]; offer_top10: string[]; holdings: string[]; nav: string; ungated_nav: string;
  nav_since_freeze_pct: string | null; scores_sha256_16: string;
}
export interface Book { key: 'main' | 'h10'; label: string; rule: string; ledger_id: string; rows: BookRow[] }
export interface FusionBooks { books: Book[] }
export type SummaryValue = string | number | boolean | null;
export interface ExperimentHead { label: string; ledger_id: string; document: string; document_sha256: string | null; summary: Record<string, SummaryValue> }
export interface MonitorRow { date: string; n: number; ic: string; decile_spread: string; top_ex: string; top_sv: string; flagged: boolean; coverage_fail_days: string[] }
export interface BasketRow { date: string; x20: string; ew: string; excess_pct: string; n_x20: number; n_ew: number; trade_day: boolean; turnover_x20: string | null; turnover_ew: string | null }
export interface Os20Group { group: 'OS20_GAP' | 'OS20_NOGAP' | 'OS5_GAP'; n: number; ex: string | null; raw: string | null }
export interface Os20Row { date: string; mkt20: string | null; flagged: boolean; coverage_fail_days: string[]; groups: Os20Group[] }
export type Experiment =
  | (ExperimentHead & { key: 'monitor'; rows: MonitorRow[] })
  | (ExperimentHead & { key: 'x20'; rows: BasketRow[] })
  | (ExperimentHead & { key: 'os20'; rows: Os20Row[] });
export interface FusionExperiments { experiments: Experiment[] }

type ErrBody = { error?: ErrorBody; meta?: ResponseMeta } | null;
const PREFIX = '/v1/x/bf/fusion/';

async function get<T>(path: string, query: Record<string, string | number | undefined | null> = {}, signal?: AbortSignal): Promise<Envelope<T>> {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) if (v !== undefined && v !== null && v !== '') sp.append(k, String(v));
  const qs = sp.toString();
  let res: Response;
  try {
    res = await fetch(`${API_ORIGIN}${PREFIX}${path}${qs ? `?${qs}` : ''}`, { method: 'GET', headers: { Accept: 'application/json' }, credentials: 'same-origin', signal });
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

export const bfFusion = {
  status: (signal?: AbortSignal) => get<FusionStatus>('status', {}, signal),
  scores: (date?: string | null, limit = 40, signal?: AbortSignal) => get<FusionScores>('scores', { date, limit }, signal),
  books: (signal?: AbortSignal) => get<FusionBooks>('books', {}, signal),
  experiments: (signal?: AbortSignal) => get<FusionExperiments>('experiments', {}, signal),
};
