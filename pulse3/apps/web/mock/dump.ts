/**
 * Dump every mock response (one or more per operationId) to mock/out/responses.json so
 * scripts/validate_mock.py can check them against the contract (`pulse.contracts.check`).
 * Run: pnpm mock:dump
 */
import { mkdirSync, writeFileSync } from 'node:fs';
import { HORIZON_IDS, INSTRUMENTS, POOL_ID } from './market.ts';
import { createMockState, currentRelease, handle } from './handler.ts';
import { ACCOUNT_ID, ACCOUNT_VERSION, LEAGUE_ID, VALUATION_SNAPSHOT_ID } from './research.ts';
import { planIdFor } from './market.ts';
import { uuid } from './util.ts';

const state = createMockState();
const r = currentRelease(state);
const out: Array<{ operation: string; status: number; definition: string; body: unknown }> = [];

function call(operation: string, definition: string, method: string, path: string, params: Record<string, string> = {}, body?: unknown, headers: Record<string, string> = {}): unknown {
  const res = handle(state, { method, path, query: new URLSearchParams(params), headers, body });
  out.push({ operation, status: res.status, definition: res.status < 300 ? definition : 'ErrorResponse', body: res.body });
  return res.body;
}

const inst = INSTRUMENTS[0]!;
const decision = new Date(r.decisionAsOf).toISOString().replace(/\.\d{3}Z$/, 'Z');
const end = decision;
const startOf = (ms: number): string => new Date(r.decisionAsOf - ms).toISOString().replace(/\.\d{3}Z$/, 'Z');
const DAY = 86_400_000;
const q = { release_id: r.id };

call('getCurrentRelease', 'getCurrentReleaseResponse', 'GET', '/v1/releases/current', { slot_key: 'market.default' });
call('getRelease', 'getReleaseResponse', 'GET', `/v1/releases/${r.id}`);
for (const h of HORIZON_IDS) {
  const first = call('getMarketRanking', 'getMarketRankingResponse', 'GET', '/v1/rankings', { ...q, horizon_id: h, forecast_target: 'excess_return_5d' }) as { meta: { next_cursor: string | null } };
  if (first.meta.next_cursor) call('getMarketRanking', 'getMarketRankingResponse', 'GET', '/v1/rankings', { ...q, horizon_id: h, forecast_target: 'excess_return_5d', cursor: first.meta.next_cursor });
}
for (const tax of ['industry', 'concept']) {
  for (const kind of ['sector_opportunity_rank', 'sector_attention_rank', 'sector_rising_rank']) {
    call('getSectorOverview', 'getSectorOverviewResponse', 'GET', '/v1/sectors/rankings', { ...q, taxonomy_id: tax, taxonomy_version: '1', rank_kind: kind, horizon_id: 'short_term', forecast_target: 'excess_return_5d' });
  }
}
call('getSectorRanking', 'getSectorRankingResponse', 'GET', '/v1/sectors/合成板块·新能源/rankings', { ...q, taxonomy_id: 'industry', taxonomy_version: '1', horizon_id: 'short_term', forecast_target: 'excess_return_5d' });
call('getCandidatePool', 'getCandidatePoolResponse', 'GET', `/v1/candidate-pools/${POOL_ID}`, q);
call('getStockWorkspace', 'getStockWorkspaceResponse', 'GET', `/v1/stocks/${inst.id}/workspace`, { ...q, view_kind: 'public_research' });
call('getStockWorkspace', 'getStockWorkspaceResponse', 'GET', `/v1/stocks/${inst.id}/workspace`, { ...q, view_kind: 'account_holding', account_id: ACCOUNT_ID });
call('getStockWorkspace', 'getStockWorkspaceResponse', 'GET', `/v1/stocks/${INSTRUMENTS[8]!.id}/workspace`, { ...q, view_kind: 'public_research' });
call('getStockQuote', 'getStockQuoteResponse', 'GET', `/v1/stocks/${inst.id}/quote`, { quote_policy_version: 'synthetic-l1-v1' });
for (const [tf, span] of [['1m', 0.4 * DAY], ['5m', 0.4 * DAY], ['15m', 0.4 * DAY], ['1h', 0.4 * DAY], ['1d', 400 * DAY], ['1w', 800 * DAY], ['1mo', 1500 * DAY], ['1y', 4000 * DAY]] as const) {
  for (const mode of ['raw', 'forward_adjusted', 'backward_adjusted']) {
    call('getStockBars', 'getStockBarsResponse', 'GET', `/v1/stocks/${inst.id}/bars`, {
      snapshot_id: uuid('snap:bars'), decision_as_of: decision, availability_policy_version: 'synthetic-observed-clock-v1', timeframe: tf,
      session_policy_version: 'cn-a-continuous-v1', adjustment_mode: mode, ...(mode === 'raw' ? {} : { adjustment_version: 'adj-v1' }), start: startOf(span), end,
    });
  }
}
for (const [op, seg, def] of [['getStockEvidence', 'evidence', 'getStockEvidenceResponse'], ['getStockCommunity', 'community', 'getStockCommunityResponse'], ['getStockFundFlow', 'fund-flow', 'getStockFundFlowResponse'], ['getStockSeats', 'seats', 'getStockSeatsResponse']] as const) {
  const first = call(op, def, 'GET', `/v1/stocks/${inst.id}/${seg}`, q) as { meta: { next_cursor: string | null } };
  if (first.meta.next_cursor) call(op, def, 'GET', `/v1/stocks/${inst.id}/${seg}`, { ...q, cursor: first.meta.next_cursor });
}
for (const kind of ['main', 't', 'grid'] as const) call('getTradePlan', 'getTradePlanResponse', 'GET', `/v1/plans/${planIdFor(inst.id, kind)}/versions/${r.n}`);
call('getTradePlan', 'getTradePlanResponse', 'GET', `/v1/plans/${planIdFor(INSTRUMENTS[1]!.id, 'main')}/versions/${r.n}`);
call('listStrategies', 'listStrategiesResponse', 'GET', '/v1/strategies');
call('getLeague', 'getLeagueResponse', 'GET', `/v1/leagues/${LEAGUE_ID}/versions/1`);
call('listPapers', 'listPapersResponse', 'GET', '/v1/research/papers');
call('getDataStatus', 'getDataStatusResponse', 'GET', '/v1/data-status');
call('getAccountProjection', 'getAccountProjectionResponse', 'GET', `/v1/accounts/${ACCOUNT_ID}`, { account_version: String(ACCOUNT_VERSION), valuation_snapshot_id: VALUATION_SNAPSHOT_ID });
call('getAccountActionability', 'getAccountActionabilityResponse', 'GET', `/v1/accounts/${ACCOUNT_ID}/actionability/${uuid(`act:${r.id}`)}`);
call('getModelRun', 'getModelRunResponse', 'GET', `/v1/analysis-runs/${uuid('modelrun:x')}`);
const created = call('createExperiment', 'createExperimentResponse', 'POST', '/v1/experiments', {}, {
  instance_id: uuid('instance:trend'), hypothesis_id: uuid('hyp:main'), hypothesis_version: 1, evaluation_context_id: uuid('evalctx:main'), run_purpose: 'validation', random_seed: 7,
}, { 'idempotency-key': 'dump-1' }) as { data: { job_run_id: string; resource_id: string } };
call('getJobRun', 'getJobRunResponse', 'GET', `/v1/jobs/${created.data.job_run_id}`);
call('getEvaluation', 'getEvaluationResponse', 'GET', `/v1/experiments/${uuid('exp:a')}`);
call('getEvaluation', 'getEvaluationResponse', 'GET', `/v1/experiments/${created.data.resource_id}`);
const lid = uuid('layout:test');
call('saveLayout', 'saveLayoutResponse', 'PUT', `/v1/workspace-layouts/${lid}`, {}, { expected_layout_version: 0, display_name: '默认布局', layout_document: { schema: 'pulse.web.layout/1', timeframe: '1d' } }, { 'idempotency-key': 'dump-layout' });
call('getLayout', 'getLayoutResponse', 'GET', `/v1/workspace-layouts/${lid}`);
call('listWatchlists', 'listWatchlistsResponse', 'GET', '/v1/watchlists');
const wl = [...state.watchlists.values()][0]!;
call('saveWatchlist', 'saveWatchlistResponse', 'PUT', `/v1/watchlists/${wl.watchlist_id}`, {}, {
  expected_watchlist_version: wl.watchlist_version, display_name: wl.display_name, description: null,
  members: [...wl.members.map((m) => ({ instrument_id: m.instrument_id, sort_position: m.sort_position, note: m.note, added_release_id: m.added_release_id })), { instrument_id: inst.id, sort_position: 3, note: null, added_release_id: r.id }],
}, { 'idempotency-key': 'dump-wl' });
call('listAnnotations', 'listAnnotationsResponse', 'GET', '/v1/annotations', { instrument_id: inst.id });
call('saveAnnotation', 'saveAnnotationResponse', 'PUT', `/v1/annotations/${uuid('annotation:new')}`, {}, {
  expected_annotation_version: 0, instrument_id: inst.id, watchlist_id: null, annotation_kind: 'drawing', anchor_release_id: r.id, anchor_snapshot_id: uuid('snap:bars'),
  body: { type: 'hline', price: '18.50', adjustment_mode: 'raw' },
}, { 'idempotency-key': 'dump-ann' });
call('pollUpdates', 'pollUpdatesResponse', 'GET', '/v1/updates', { slot_key: 'market.default' });
handle(state, { method: 'POST', path: '/__mock/publish', query: new URLSearchParams(), headers: {}, body: undefined });
call('pollUpdates', 'pollUpdatesResponse', 'GET', '/v1/updates', { slot_key: 'market.default', cursor: 'epoch-1:0' });
call('getRelease', 'getReleaseResponse', 'GET', `/v1/releases/${currentRelease(state).id}`);
call('getMarketRanking', 'getMarketRankingResponse', 'GET', '/v1/rankings', { release_id: currentRelease(state).id, horizon_id: 'swing', forecast_target: 'excess_return_20d' });
call('getTradePlan', 'getTradePlanResponse', 'GET', `/v1/plans/${planIdFor(inst.id, 'main')}/versions/2`);

const ops = new Set(out.map((o) => o.operation));
mkdirSync(new URL('./out/', import.meta.url), { recursive: true });
writeFileSync(new URL('./out/responses.json', import.meta.url), JSON.stringify(out));
process.stdout.write(`dumped ${out.length} responses covering ${ops.size} operations\n`);
