/**
 * One function per contract operationId (31 operations, spec 11 / api_bindings).
 * Why: a single place maps operationId -> HTTP call; pages and hooks never build
 * URLs. Invariants: paths and parameter shapes come from the generated schema, so
 * a contract change breaks `tsc` here first. Not covered: caching/retry policy.
 */
import { guard, http } from './client';
import type { components } from './schema';

type S = components['schemas'];
type Sig = { signal?: AbortSignal };

export const ops = {
  // --- release / rankings ---
  getCurrentRelease: (slot_key: string, o: Sig = {}) =>
    guard(() => http.GET('/v1/releases/current', { params: { query: { slot_key } }, signal: o.signal })),
  getRelease: (release_id: string, o: Sig = {}) =>
    guard(() => http.GET('/v1/releases/{release_id}', { params: { path: { release_id } }, signal: o.signal })),
  getMarketRanking: (q: { release_id: string; horizon_id: string; forecast_target: string; cursor?: string }, o: Sig = {}) =>
    guard(() => http.GET('/v1/rankings', { params: { query: q }, signal: o.signal })),
  getSectorOverview: (
    q: { release_id: string; taxonomy_id: string; taxonomy_version: number; rank_kind: S['SectorRankEntry']['rank_kind']; horizon_id: string; forecast_target: string; cursor?: string },
    o: Sig = {},
  ) => guard(() => http.GET('/v1/sectors/rankings', { params: { query: q }, signal: o.signal })),
  getSectorRanking: (
    node_id: string,
    q: { release_id: string; taxonomy_id: string; taxonomy_version: number; horizon_id: string; forecast_target: string; cursor?: string },
    o: Sig = {},
  ) => guard(() => http.GET('/v1/sectors/{node_id}/rankings', { params: { path: { node_id }, query: q }, signal: o.signal })),
  getCandidatePool: (pool_id: string, q: { release_id: string; cursor?: string }, o: Sig = {}) =>
    guard(() => http.GET('/v1/candidate-pools/{pool_id}', { params: { path: { pool_id }, query: q }, signal: o.signal })),

  // --- stock workbench ---
  getStockWorkspace: (
    instrument_id: string,
    q: { release_id: string; view_kind: 'public_research' | 'account_holding'; account_id?: string },
    o: Sig = {},
  ) => guard(() => http.GET('/v1/stocks/{instrument_id}/workspace', { params: { path: { instrument_id }, query: q }, signal: o.signal })),
  getStockQuote: (instrument_id: string, q: { quote_policy_version: string; quote_snapshot_id?: string }, o: Sig = {}) =>
    guard(() => http.GET('/v1/stocks/{instrument_id}/quote', { params: { path: { instrument_id }, query: q }, signal: o.signal })),
  getStockBars: (
    instrument_id: string,
    q: {
      snapshot_id: string; decision_as_of: string; availability_policy_version: string; timeframe: string;
      session_policy_version: string; adjustment_mode: S['AdjustmentMode']; adjustment_version?: string; start: string; end: string;
    },
    o: Sig = {},
  ) => guard(() => http.GET('/v1/stocks/{instrument_id}/bars', { params: { path: { instrument_id }, query: q }, signal: o.signal })),
  getStockEvidence: (instrument_id: string, q: { release_id: string; cursor?: string }, o: Sig = {}) =>
    guard(() => http.GET('/v1/stocks/{instrument_id}/evidence', { params: { path: { instrument_id }, query: q }, signal: o.signal })),
  getStockCommunity: (instrument_id: string, q: { release_id: string; cursor?: string }, o: Sig = {}) =>
    guard(() => http.GET('/v1/stocks/{instrument_id}/community', { params: { path: { instrument_id }, query: q }, signal: o.signal })),
  getStockFundFlow: (instrument_id: string, q: { release_id: string; cursor?: string }, o: Sig = {}) =>
    guard(() => http.GET('/v1/stocks/{instrument_id}/fund-flow', { params: { path: { instrument_id }, query: q }, signal: o.signal })),
  getStockSeats: (instrument_id: string, q: { release_id: string; cursor?: string }, o: Sig = {}) =>
    guard(() => http.GET('/v1/stocks/{instrument_id}/seats', { params: { path: { instrument_id }, query: q }, signal: o.signal })),
  getTradePlan: (plan_id: string, plan_version: number, o: Sig = {}) =>
    guard(() => http.GET('/v1/plans/{plan_id}/versions/{plan_version}', { params: { path: { plan_id, plan_version } }, signal: o.signal })),

  // --- strategies / research ---
  listStrategies: (q: { role?: S['StrategyRole']; horizon_id?: string; method_family?: string; lifecycle?: S['StrategyLifecycle']; cursor?: string }, o: Sig = {}) =>
    guard(() => http.GET('/v1/strategies', { params: { query: q }, signal: o.signal })),
  getLeague: (league_id: string, league_version: number, o: Sig = {}) =>
    guard(() => http.GET('/v1/leagues/{league_id}/versions/{league_version}', { params: { path: { league_id, league_version } }, signal: o.signal })),
  getEvaluation: (experiment_id: string, o: Sig = {}) =>
    guard(() => http.GET('/v1/experiments/{experiment_id}', { params: { path: { experiment_id } }, signal: o.signal })),
  createExperiment: (body: S['ExperimentRequest'], idempotencyKey: string) =>
    guard(() => http.POST('/v1/experiments', { params: { header: { 'Idempotency-Key': idempotencyKey } }, body })),
  listPapers: (q: { publication_year?: number; publication_status?: S['PaperMethodCard']['publication_status']; read_depth?: S['PaperMethodCard']['read_depth']; cursor?: string }, o: Sig = {}) =>
    guard(() => http.GET('/v1/research/papers', { params: { query: q }, signal: o.signal })),
  getModelRun: (model_run_id: string, o: Sig = {}) =>
    guard(() => http.GET('/v1/analysis-runs/{model_run_id}', { params: { path: { model_run_id } }, signal: o.signal })),
  getJobRun: (job_run_id: string, o: Sig = {}) =>
    guard(() => http.GET('/v1/jobs/{job_run_id}', { params: { path: { job_run_id } }, signal: o.signal })),

  // --- data / account ---
  getDataStatus: (o: Sig = {}) => guard(() => http.GET('/v1/data-status', { signal: o.signal })),
  getAccountProjection: (account_id: string, q: { account_version: number; valuation_snapshot_id: string }, o: Sig = {}) =>
    guard(() => http.GET('/v1/accounts/{account_id}', { params: { path: { account_id }, query: q }, signal: o.signal })),
  getAccountActionability: (account_id: string, actionability_id: string, q: { cursor?: string }, o: Sig = {}) =>
    guard(() => http.GET('/v1/accounts/{account_id}/actionability/{actionability_id}', { params: { path: { account_id, actionability_id }, query: q }, signal: o.signal })),

  // --- persistent user state ---
  getLayout: (layout_id: string, o: Sig = {}) =>
    guard(() => http.GET('/v1/workspace-layouts/{layout_id}', { params: { path: { layout_id } }, signal: o.signal })),
  saveLayout: (layout_id: string, body: S['LayoutUpdate'], idempotencyKey: string) =>
    guard(() => http.PUT('/v1/workspace-layouts/{layout_id}', { params: { path: { layout_id }, header: { 'Idempotency-Key': idempotencyKey } }, body })),
  listWatchlists: (o: Sig = {}) => guard(() => http.GET('/v1/watchlists', { signal: o.signal })),
  saveWatchlist: (watchlist_id: string, body: S['WatchlistUpdate'], idempotencyKey: string) =>
    guard(() => http.PUT('/v1/watchlists/{watchlist_id}', { params: { path: { watchlist_id }, header: { 'Idempotency-Key': idempotencyKey } }, body })),
  listAnnotations: (q: { instrument_id?: string; watchlist_id?: string; cursor?: string }, o: Sig = {}) =>
    guard(() => http.GET('/v1/annotations', { params: { query: q }, signal: o.signal })),
  saveAnnotation: (annotation_id: string, body: S['AnnotationUpdate'], idempotencyKey: string) =>
    guard(() => http.PUT('/v1/annotations/{annotation_id}', { params: { path: { annotation_id }, header: { 'Idempotency-Key': idempotencyKey } }, body })),

  // --- updates ---
  pollUpdates: (slot_key: string, cursor: string | undefined, o: Sig = {}) =>
    guard(() => http.GET('/v1/updates', { params: { query: { slot_key, cursor } }, signal: o.signal })),
} as const;

export type OpName = keyof typeof ops;
