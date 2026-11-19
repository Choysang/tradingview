/**
 * Dump the fill-market backfill mock responses to mock/out/bf_fillm.json so scripts/validate_bf_fillm_mock.py can check them against
 * backfill_fillm.schema.json. Run: pnpm mock:dump:fillm
 */
import { mkdirSync, writeFileSync } from 'node:fs';
import { createMockState, currentRelease, handle } from './handler.ts';
import { INSTRUMENTS } from './market.ts';

const state = createMockState();
const r = currentRelease(state);
const inst = INSTRUMENTS[0]!;
const out: Array<{ definition: string; status: number; body: unknown }> = [];
const call = (definition: string, path: string, params: Record<string, string>): void => {
  const res = handle(state, { method: 'GET', path, query: new URLSearchParams(params), headers: {}, body: undefined });
  out.push({ definition: res.status < 300 ? definition : 'ErrorResponse', status: res.status, body: res.body });
};
call('CrowdSnapshot', '/v1/x/bf/fillm/crowd', { release_id: r.id });
call('StockStatsPlus', '/v1/x/bf/fillm/stock-stats-plus', { instrument_id: inst.id, release_id: r.id });
call('ErrorResponse', '/v1/x/bf/fillm/stock-stats-plus', { release_id: r.id });
mkdirSync(new URL('./out/', import.meta.url), { recursive: true });
writeFileSync(new URL('./out/bf_fillm.json', import.meta.url), JSON.stringify(out));
process.stdout.write(`dumped ${out.length} fill-market backfill responses\n`);
