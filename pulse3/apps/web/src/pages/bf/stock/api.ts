/**
 * Typed client for the stock backfill reads (`/v1/x/bf/stock/*`, contract urn:pulse:contracts:backfill:stock:v1).
 * Why: chart indicator series and the stock statistics card are computed on the server; the page only fetches and draws.
 * Conventions follow the other extension clients: every non-2xx becomes an ApiError with the typed code; null/undefined
 * query values are omitted. Types mirror `backfill_stock.schema.json` (kept by hand: the generated client knows only
 * the 31 core operations).
 */
import { ApiError } from '../../../api/client';
import type { Envelope, ErrorBody, ResponseMeta } from '../../../api/types';
import { API_ORIGIN } from '../../../config';

export type IndicatorSet = 'ma' | 'ema' | 'boll' | 'macd' | 'kdj' | 'rsi';
export type SubPaneId = 'macd' | 'kdj' | 'rsi';

export interface IndicatorBar { bar_open_time: string; trade_date: string; is_final: boolean }
export interface IndicatorSeries {
  key: string; indicator: IndicatorSet; output: string; pane: 'price' | 'sub'; label: string; params: Record<string, number>;
  definition_version: string; implementation_digest: string; warmup_bars: number; first_valid_index: number | null; valid_count: number;
  values: Array<string | null>;
}
export interface StockIndicators {
  instrument_id: string; release_id: string; decision_as_of: string; availability_policy_version: string; timeframe: string;
  adjustment_mode: 'raw' | 'forward_adjusted' | 'backward_adjusted'; adjustment_version: string | null; snapshot_id: string; window_hash: string;
  window_start: string; window_end: string; window_complete: boolean; numeric_policy: string; requested: IndicatorSet[];
  bars: IndicatorBar[]; series: IndicatorSeries[];
}

export type StatKey = 'turnover_amount' | 'turnover_rate' | 'dist_to_60d_high' | 'limit_up_count_1y' | 'limit_streak' | 'up_streak' | 'listing_date'
  | 'first_seal_time' | 'last_seal_time' | 'seal_amount' | 'open_board_count';
export interface StatField {
  key: StatKey; label: string; status: 'ok' | 'estimated' | 'unavailable'; value: string | null; value_kind: 'decimal' | 'integer' | 'date' | null;
  unit: string | null; as_of: string | null; basis: string | null; reason: string | null;
}
export interface StockStats {
  instrument_id: string; release_id: string; decision_as_of: string; availability_policy_version: string;
  price_basis: 'raw' | 'forward_adjusted' | 'backward_adjusted'; bars_snapshot_id: string | null; last_trade_date: string | null; fields: StatField[];
}

type ErrBody = { error?: ErrorBody; meta?: ResponseMeta } | null;

export interface IndicatorRequest { instrument_id: string; release_id: string | null; timeframe: string; adjustment: string; set: IndicatorSet[] }

export function indicatorQuery(q: IndicatorRequest): string {
  const sp = new URLSearchParams({ instrument_id: q.instrument_id, timeframe: q.timeframe, adjustment: q.adjustment, set: q.set.join(',') });
  if (q.release_id) sp.set('release_id', q.release_id);
  return sp.toString();
}

async function get<T>(path: string, qs: string, signal?: AbortSignal): Promise<Envelope<T>> {
  let res: Response;
  try {
    res = await fetch(`${API_ORIGIN}/v1/x/bf/stock/${path}?${qs}`, { headers: { Accept: 'application/json' }, credentials: 'same-origin', signal });
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw e;
    throw new ApiError(0, null, '网络不可用或服务无响应');
  }
  let body: unknown = null;
  try { body = await res.json(); } catch { body = null; }
  if (!res.ok) throw new ApiError(res.status, typeof body === 'object' && body !== null ? (body as ErrBody) : null);
  if (typeof body !== 'object' || body === null || !('data' in body)) throw new ApiError(res.status, null, '服务返回了无法识别的内容');
  return body as Envelope<T>;
}

export const bfStock = {
  indicators: (q: IndicatorRequest, signal?: AbortSignal) => get<StockIndicators>('indicators', indicatorQuery(q), signal),
  stats: (instrument_id: string, release_id: string | null, signal?: AbortSignal) => {
    const sp = new URLSearchParams({ instrument_id });
    if (release_id) sp.set('release_id', release_id);
    return get<StockStats>('stats', sp.toString(), signal);
  },
};
