/**
 * Dump the Nexus backfill mock responses to mock/out/bf_nexus.json so scripts/validate_bf_nexus_mock.py
 * can check them against backfill_nexus.schema.json (v2.0.0) and the v3 ResponseMeta.
 * Run: npx tsx mock/bf_nexus_dump.ts
 */
import { mkdirSync, writeFileSync } from 'node:fs';
import { handleBfNexus } from './bf_nexus.ts';
import { createMockState, currentRelease, handle } from './handler.ts';
import { INSTRUMENTS } from './market.ts';

const state = createMockState();
const r = currentRelease(state);
const inst = INSTRUMENTS[0]!;
const out: Array<{ definition: string; status: number; body: unknown }> = [];

const call = (definition: string, path: string, params: Record<string, string> = {}): void => {
  const res = handle(state, { method: 'GET', path, query: new URLSearchParams(params), headers: {}, body: undefined });
  out.push({ definition: res.status < 300 ? definition : 'ErrorResponse', status: res.status, body: res.body });
};

call('NexusHubOverview', '/v1/x/bf/nexus/hub', { track: 'composite', limit: '20', release_id: r.id });
call('NexusHubOverview', '/v1/x/bf/nexus/hub', { track: 'short', limit: '10', release_id: r.id });
// Also validate awaiting-first-release (release_id = null) Hub response against contract
const awaitingRes = handleBfNexus('/v1/x/bf/nexus/hub', new URLSearchParams({ track: 'composite' }), Date.now(), undefined);
out.push({ definition: 'NexusHubOverview', status: awaitingRes.status, body: awaitingRes.body });

call('NexusSectorDeepDive', '/v1/x/bf/nexus/sectors/合成板块·半导体/deep-dive', { taxonomy_id: 'industry', horizon_id: 'short_term', release_id: r.id });
call('NexusStockProAnalysis', `/v1/x/bf/nexus/stocks/${inst.id}/pro`, { timeframe: '1d', adjustment: 'raw', release_id: r.id });
call('NexusStockProAnalysis', `/v1/x/bf/nexus/stocks/${inst.id}/pro`, {
  timeframe: '1w',
  adjustment: 'forward_adjusted',
  release_id: r.id,
  base_shares: '2000',
  available_cash: '80000',
  grid_mode: 'geometric',
});
call('NexusArenaOverview', '/v1/x/bf/nexus/arena', {});
call('NexusIntegrityAudit', '/v1/x/bf/nexus/integrity', {});
call('ErrorResponse', '/v1/x/bf/nexus/hub', { release_id: '00000000-0000-4000-8000-999999999999' });

mkdirSync(new URL('./out/', import.meta.url), { recursive: true });
writeFileSync(new URL('./out/bf_nexus.json', import.meta.url), JSON.stringify(out));
process.stdout.write(`dumped ${out.length} nexus backfill responses\n`);
