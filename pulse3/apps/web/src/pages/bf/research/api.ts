/**
 * Client + wire types of the research backfill lane (/v1/x/bf/research/*, contract backfill_research.schema.json).
 * Why: playbook replay equity / trades, regime attribution, the playbook x regime heatmap and the experiment archive are extension
 * reads that the generated core client does not know. Same error handling as the ext-B client: a non-2xx is an ApiError, network failure is status 0.
 * Money / ratios are decimal strings; a missing value is null (shown as 缺失, never 0). The browser only draws what the server computed.
 */
import { ApiError } from '../../../api/client';
import type { Envelope, ErrorBody, ResponseMeta } from '../../../api/types';
import { API_ORIGIN } from '../../../config';

export type Dimension = 'trend' | 'liquidity' | 'sentiment' | 'style';
export type ExitReason = 'stop_loss' | 'take_profit' | 'trailing_stop' | 'chandelier' | 'below_ma5' | 'stall' | 'bias' | 'shadow' | 'scale_out' | 'holding_days' | 'unknown';
export interface RegimeTag { session: string; trend: string; liquidity: string; sentiment: string; style: string; basis: Record<string, string> }
export interface ReplayTrade {
  instrument_id: string; code: string | null; entry_day: string; exit_day: string | null; quantity: number; entry_price: string; exit_price: string | null;
  gross_return: string | null; holding_sessions: number | null; exit_reason: ExitReason | null; status: 'closed' | 'open'; regime_at_entry: RegimeTag | null;
}
export interface EquityPoint { d: string; nav: string | null; drawdown: string | null }
export interface Equity { points: EquityPoint[]; max_drawdown: string | null; final_nav: string | null; net_return: string | null }
export interface RegimeHeader { available: boolean; reason: string | null; regime_version: string | null; release_id: string | null; through: string | null; basis: string }
export interface AttributionCell { bucket: string; label: string; trade_count: number; avg_return: string | null; win_rate: string | null; insufficient_sample: boolean }
export interface AttributionDimension { dimension: Dimension; label: string; cells: AttributionCell[]; unclassified: { count: number; reasons: Record<string, number> } }
export interface Attribution { regime: RegimeHeader; version: string; min_trades: number; closed_trades: number; open_trades: number; dimensions: AttributionDimension[] }
export interface PlaybookReplay {
  experiment: { experiment_id: string; instance_id: string; catalog_id: string; playbook_id: string; completed_at: string | null; cost_policy_version: string; price_basis: string; artifact_ref: string };
  initial_cash: string; window: { start: string; end: string; sessions: number }; equity: Equity;
  trades: { total: number; closed: number; open: number; shown: number; items: ReplayTrade[] }; attribution: Attribution; caveats: string[];
}
export interface HeatmapColumn { dimension: Dimension; label: string; buckets: Array<{ bucket: string; label: string }> }
export interface HeatmapRow { catalog_id: string; playbook_id: string; experiment_id: string; completed_at: string | null; regime_available: boolean; closed_trades: number; dimensions: AttributionDimension[] }
export interface RegimeHeatmap { version: string; min_trades: number; columns: HeatmapColumn[]; rows: HeatmapRow[]; note: string }
export type RunPurpose = 'screening' | 'validation' | 'shadow' | 'blind_final';
export type RunState = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled';
export interface ExperimentItem {
  experiment_id: string; instance_id: string; display_name: string; catalog_id: string | null; kind: 'playbook' | 'factor' | 'exit_rule' | 'intraday_t' | 'other';
  run_purpose: RunPurpose; run_state: RunState; started_at: string | null; completed_at: string | null; error_code: string | null;
  net_return_validation: string | null; max_drawdown: string | null; trade_count: string | null; has_replay: boolean;
}
export interface ExperimentArchive { items: ExperimentItem[]; page: { next_cursor: string | null; limit: number }; order: string }
export interface ArchiveFilter { instance_id?: string; purpose?: RunPurpose | ''; status?: RunState | '' }

type ErrBody = { error?: ErrorBody; meta?: ResponseMeta } | null;

async function get<T>(path: string, query: Record<string, string | number | undefined | null>, signal?: AbortSignal): Promise<Envelope<T>> {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) if (v !== undefined && v !== null && v !== '') sp.append(k, String(v));
  const qs = sp.toString();
  let res: Response;
  try {
    res = await fetch(`${API_ORIGIN}/v1/x/bf/research/${path}${qs ? `?${qs}` : ''}`, { method: 'GET', headers: { Accept: 'application/json' }, credentials: 'same-origin', signal });
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

export const bfResearch = {
  playbookReplay: (catalogId: string, tradesLimit = 50, signal?: AbortSignal) => get<PlaybookReplay>('playbook-replay', { catalog_id: catalogId, trades_limit: tradesLimit }, signal),
  regimeHeatmap: (signal?: AbortSignal) => get<RegimeHeatmap>('regime-heatmap', {}, signal),
  experiments: (f: ArchiveFilter, limit = 50, cursor?: string | null, signal?: AbortSignal) =>
    get<ExperimentArchive>('experiments', { instance_id: f.instance_id, purpose: f.purpose, status: f.status, limit, cursor }, signal),
};
