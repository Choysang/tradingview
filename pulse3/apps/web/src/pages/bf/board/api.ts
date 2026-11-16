/**
 * Typed client of the board-lane reads (/v1/x/bf/board/*, contract backfill_board.schema.json) and the shapes they return.
 * Why: these reads are not in the generated v3 client; they reuse the extension GET helper (same envelope, same ApiError).
 * Decimals stay strings; null means missing and is never drawn as 0.
 */
import { extGet } from '../../ext/a/api';

export type PctMissingReason = 'no_bar' | 'no_previous_bar' | 'unverifiable_move';
export interface WatchRank { alpha_rank: number; total: number; horizon_id: string; forecast_target: string }
export interface WatchCardData {
  instrument_id: string; session: string | null; last_close: string | null; pct_change: string | null; pct_missing_reason: PctMissingReason | null;
  amount_estimate_cny: string | null; spark: Array<{ d: string; c: string }>; rank: WatchRank | null;
}
export interface WatchCardsData { release_id: string; amount_is_estimate: true; method: string; cards: WatchCardData[] }
export interface ThemeMapNode { node_id: string; name: string; members: number; observed: number; pct_samples: number; amount_estimate_cny: string | null; avg_pct: string | null }
export interface ThemeMapData {
  release_id: string; taxonomy_id: string; taxonomy_version: number; membership_snapshot_id: string; session: string | null;
  amount_is_estimate: true; method: string; nodes: ThemeMapNode[];
}
export interface InstrumentHit { instrument_id: string; code: string | null; name: string | null; is_risk_warning: boolean | null }
export interface InstrumentSearchData { query: string; items: InstrumentHit[] }

const BASE = '/v1/x/bf/board';
export const boardOps = {
  watchCards: (q: { release_id: string; ids: string[]; horizon_id: string; forecast_target: string }, signal?: AbortSignal) =>
    extGet<WatchCardsData>(`${BASE}/watch-cards`, { ...q, ids: q.ids.join(',') }, signal),
  themeMap: (q: { release_id: string; taxonomy_id: string; taxonomy_version: number }, signal?: AbortSignal) =>
    extGet<ThemeMapData>(`${BASE}/theme-map`, q, signal),
  instrumentSearch: (q: string, signal?: AbortSignal) => extGet<InstrumentSearchData>(`${BASE}/instrument-search`, { q }, signal),
};
