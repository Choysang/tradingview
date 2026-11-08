/**
 * The only place the UI talks to the backend. Every call is typed by the
 * generated contract, never cached (a stale snapshot is worse than a slow one),
 * and abortable so a superseded request can never overwrite a newer answer.
 */
import type * as C from './contract';

const BASE = '/api/v1';

export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

export const isAbort = (error: unknown) => error instanceof DOMException && error.name === 'AbortError';

async function request<T>(method: 'GET' | 'POST' | 'PUT', path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(BASE + path, {
    method,
    signal,
    cache: 'no-store',
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = response.statusText || `HTTP ${response.status}`;
    try {
      const payload = await response.json();
      detail = typeof payload.detail === 'string' ? payload.detail : payload.detail?.[0]?.msg ?? detail;
    } catch {
      /* body was not JSON; keep the status text */
    }
    throw new ApiError(response.status, detail);
  }
  return response.json() as Promise<T>;
}

export type FilterInput = Partial<C.ScreenerFilterRequest>;

export const api = {
  overview: (signal?: AbortSignal) => request<C.MacroDashboardSummary>('GET', '/market/overview', undefined, signal),
  events: (since: number | null, signal?: AbortSignal) =>
    request<C.MarketEventsResponse>('GET', `/market/events?limit=150${since ? `&since=${since}` : ''}`, undefined, signal),
  query: (filters: FilterInput, signal?: AbortSignal) => request<C.ScreenerQueryResponse>('POST', '/screener/query', filters, signal),
  parse: (text: string, signal?: AbortSignal) => request<C.ParseResponse>('POST', '/screener/parse', { text }, signal),
  stock: (code: string, signal?: AbortSignal) => request<C.StockDetailResponse>('GET', `/stocks/${code}`, undefined, signal),
  strategies: (signal?: AbortSignal) => request<C.StrategyCatalog>('GET', '/strategies', undefined, signal),
  backtest: (body: Partial<C.BacktestRequest>, signal?: AbortSignal) => request<C.BacktestResponse>('POST', '/research/backtests', body, signal),
  batch: (body: C.BacktestBatchRequest, signal?: AbortSignal) => request<C.BacktestBatchResponse>('POST', '/research/backtests/batch', body, signal),
  runs: (signal?: AbortSignal) => request<C.BacktestRunsResponse>('GET', '/research/backtests', undefined, signal),
  run: (id: string, signal?: AbortSignal) => request<C.BacktestResponse>('GET', `/research/backtests/${id}`, undefined, signal),
  patterns: (signal?: AbortSignal) => request<C.PatternScanResponse>('GET', '/lab/patterns?limit=40', undefined, signal),
  factors: (body: Partial<C.FactorLabRequest>, signal?: AbortSignal) => request<C.FactorLabResponse>('POST', '/lab/factors', body, signal),
  watchlist: (signal?: AbortSignal) => request<C.WatchlistResponse>('GET', '/watchlist', undefined, signal),
  saveWatchlist: (codes: string[]) => request<C.WatchlistResponse>('PUT', '/watchlist', { codes }),
  providers: (signal?: AbortSignal) => request<C.ProvidersResponse>('GET', '/providers', undefined, signal),
  quality: (signal?: AbortSignal) => request<C.QualityResponse>('GET', '/quality', undefined, signal),
  setMode: (mode: 'LIVE' | 'SIM') => request<C.DataProvenance>('POST', '/admin/mode', { mode }),
  analogs: (code: string, horizon: number, signal?: AbortSignal) => request<C.AnalogForecast>('GET', `/stocks/${code}/analogs?horizon=${horizon}`, undefined, signal),
  warehouse: (signal?: AbortSignal) => request<C.WarehouseStatus>('GET', '/data/warehouse', undefined, signal),
  sync: (body: Partial<C.SyncRequest>) => request<C.SyncJob>('POST', '/data/sync', body),
  regime: (days: number, signal?: AbortSignal) => request<C.RegimeHistoryResponse>('GET', `/regime?days=${days}`, undefined, signal),
  playbooks: (signal?: AbortSignal) => request<C.PlaybookBoard>('GET', '/playbooks', undefined, signal),
  playbook: (id: string, signal?: AbortSignal) => request<C.PlaybookEvaluation>('GET', `/playbooks/${id}`, undefined, signal),
  rotation: (signal?: AbortSignal) => request<C.RotationReport>('GET', '/playbooks/rotation', undefined, signal),
  playbookExits: (id: string, signal?: AbortSignal) => request<C.PlaybookExitStudy>('GET', `/playbooks/${id}/exits`, undefined, signal),
  crowd: (signal?: AbortSignal) => request<C.CrowdSnapshot>('GET', '/crowd', undefined, signal),
  runPlaybook: (id: string, body: C.PlaybookRunRequest, signal?: AbortSignal) => request<C.PlaybookEvaluation>('POST', `/playbooks/${id}/run`, body, signal),
  picks: (date: string | null, signal?: AbortSignal) => request<C.DailyReport>('GET', `/picks/${date ?? 'today'}`, undefined, signal),
  generatePicks: () => request<C.DailyReport>('POST', '/picks/generate', {}),
  pickHistory: (signal?: AbortSignal) => request<C.ReportIndex>('GET', '/picks/history', undefined, signal),
  trackRecord: (signal?: AbortSignal) => request<C.TrackRecordResponse>('GET', '/picks/track-record', undefined, signal),
  pushStatus: (signal?: AbortSignal) => request<C.PushStatus>('GET', '/notify/status', undefined, signal),
  pushTest: () => request<C.PushStatus>('POST', '/notify/test', {}),
};

export const STREAM_URL = `${BASE}/stream/sse`;
