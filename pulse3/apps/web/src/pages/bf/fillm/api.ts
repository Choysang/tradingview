/**
 * Typed client of the fill-market lane reads (/v1/x/bf/fillm/*, contract backfill_fillm.schema.json) and the shapes they return.
 * Why: these reads are not in the generated v3 client; they reuse the extension GET helper (same envelope, same ApiError).
 * Decimals stay strings; null means missing and is never drawn as 0.
 */
import { extGet } from '../../ext/a/api';

export interface CrowdStock { instrument_id: string; code: string | null; name: string | null; pct_change: string | null; is_limit_up: boolean | null; limit_streak: number }
export interface DiffusionDay { session: string; observed: number; limit_ups: number; up_share: string | null; strong_followers: number }
export interface IndustryDiffusion {
  node_id: string; name: string; members: number; observed: number; limit_ups: number; up_share: string | null; avg_pct: string | null;
  leader: CrowdStock | null; followers: CrowdStock[]; spread: DiffusionDay[];
}
export interface InstitutionRow {
  instrument_id: string; code: string | null; name: string | null; institution_rows: number; buy_cny: string | null; sell_cny: string | null;
  net_cny: string | null; net_reason: string | null;
}
export interface InstitutionNet {
  status: 'ok' | 'no_seat_reports' | 'no_institution_rows'; session_date: string | null; rows: InstitutionRow[]; reports_seen: number; basis: string; notes: string[];
}
export interface NameClusters { status: 'unavailable'; reason: string; retire_recommended: true }
export interface CrowdSnapshot {
  release_id: string; session_date: string | null; taxonomy_id: string; taxonomy_version: number; membership_snapshot_id: string | null; method: string;
  sessions_in_spread: string[]; industries: IndustryDiffusion[]; institutions: InstitutionNet; name_clusters: NameClusters; notes: string[];
}

export interface PlusCoverage {
  window_sessions: number; sessions_covered: number; first_session: string | null; last_session: string | null; touched: number; sealed_at_close: number;
  policy_version: string; sessions_excluded_other_policy: number;
}
export type PlusKey = 'volume_ratio' | 'float_cap' | 'seal_success_rate_1y';
export interface StatPlusField {
  key: PlusKey; label: string; status: 'ok' | 'estimated' | 'unavailable'; value: string | null; value_kind: 'decimal' | 'integer' | null; unit: string | null;
  as_of: string | null; basis: string | null; reason: string | null; coverage: PlusCoverage | null;
}
export interface StockStatsPlus { instrument_id: string; release_id: string; decision_as_of: string; fields: StatPlusField[] }

const BASE = '/v1/x/bf/fillm';
export const fillm = {
  crowd: (q: { release_id: string; taxonomy_id?: string | null; taxonomy_version?: number | null }, signal?: AbortSignal) =>
    extGet<CrowdSnapshot>(`${BASE}/crowd`, q, signal),
  statsPlus: (q: { instrument_id: string; release_id: string }, signal?: AbortSignal) => extGet<StockStatsPlus>(`${BASE}/stock-stats-plus`, q, signal),
};
