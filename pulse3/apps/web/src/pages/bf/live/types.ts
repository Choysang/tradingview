/** Wire types of the live lane (src/pulse/contracts/data/backfill_live.schema.json). Every figure is estimated from 60 s snapshots. */
import type { Resp } from '../../ext/a/types';

export type { Resp };
export interface SealSource { snapshot_id: string; content_hash: string; input_snapshot_id: string | null; available_at: string | null }
export interface SealClockBucket { clock: string; first_seal_count: number; cumulative_count: number; cumulative_ratio: string | null }
export interface SealClockData {
  session_date: string; estimation: 'estimated_from_60s_snapshots'; policy_version: string; clock_basis: 'Asia/Shanghai'; bucket_minutes: number;
  buckets: SealClockBucket[]; sealed_ever_count: number; touched_limit_count: number; sealed_at_close_count: number; broken_count_total: number;
  instruments_observed: number; source: SealSource; notes: string[];
}
export interface SealFact {
  instrument_id: string; code: string; name: string | null; session_date: string; limit_up: string | null; snapshots_in_window: number; touched_limit: boolean;
  first_seal_at: string | null; last_seal_at: string | null; broken_count: number; sealed_at_close: boolean | null; close_seal_order_amount_cny: string | null;
  first_seal_uncertainty_seconds: number | null; max_gap_seconds: number | null; estimation: 'estimated_from_60s_snapshots'; policy_version: string;
}
export interface SealFactsData {
  instrument_id: string; session_date: string | null; status: 'ok' | 'not_observed' | 'no_dataset'; fact: SealFact | null; source: SealSource | null; notes: string[];
}
