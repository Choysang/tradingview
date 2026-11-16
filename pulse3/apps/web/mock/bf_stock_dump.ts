/**
 * Dump the stock backfill mock responses to mock/out/bf_stock.json so scripts/validate_bf_stock_mock.py can check them against
 * backfill_stock.schema.json. Run: pnpm mock:dump:bf
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
for (const tf of ['1d', '1w', '5m']) {
  for (const adjustment of ['raw', 'forward_adjusted']) {
    call('StockIndicators', '/v1/x/bf/stock/indicators', { instrument_id: inst.id, release_id: r.id, timeframe: tf, adjustment, set: 'ma,ema,boll,macd,kdj,rsi' });
  }
}
call('StockIndicators', '/v1/x/bf/stock/indicators', { instrument_id: inst.id, timeframe: '1d', adjustment: 'raw', set: 'macd' });
call('StockStats', '/v1/x/bf/stock/stats', { instrument_id: inst.id, release_id: r.id });
call('ErrorResponse', '/v1/x/bf/stock/indicators', { instrument_id: inst.id, release_id: r.id, set: 'zigzag' });
mkdirSync(new URL('./out/', import.meta.url), { recursive: true });
writeFileSync(new URL('./out/bf_stock.json', import.meta.url), JSON.stringify(out));
process.stdout.write(`dumped ${out.length} stock backfill responses\n`);
