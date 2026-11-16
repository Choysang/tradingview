import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { BarWindow } from '../src/api/types';
import { StockStatsCard } from '../src/pages/bf/stock/StockStatsCard';
import type { StatField, StockIndicators } from '../src/pages/bf/stock/api';
import { indicatorQuery } from '../src/pages/bf/stock/api';
import {
  barTime, checkAlignment, overlaySeries, requestedSets, seriesPoints, subPaneSeries, warmupNote,
} from '../src/pages/bf/stock/indicatorDraw';
import { formatStat } from '../src/pages/bf/stock/statsFormat';
import { buildIndicators, buildStats, INSTRUMENT_ID, RELEASE_ID } from '../mock/bf_stock';
import { createMockState, currentRelease, handle } from '../mock/handler';
import { INSTRUMENTS } from '../mock/market';
import { LOOKBACK_MS } from '../src/lib/barsParams';

const win = (ind: StockIndicators, over: Partial<BarWindow> = {}): BarWindow => ({
  snapshot_id: ind.snapshot_id, window_hash: ind.window_hash,
  bars: ind.bars.map((b) => ({ trade_date: b.trade_date, bar_open_time: b.bar_open_time })),
  ...over,
} as unknown as BarWindow);

describe('indicator drawing logic (the browser only draws)', () => {
  const ind = buildIndicators({ timeframe: '1d', bars: 70, set: ['ma', 'ema', 'boll', 'macd', 'kdj', 'rsi'] });

  it('null values are gaps: no point at warm-up, no 0, no fill', () => {
    const ma20 = ind.series.find((s) => s.key === 'ma20')!;
    const pts = seriesPoints(ind, ma20);
    expect(ma20.first_valid_index).toBe(19);
    expect(pts).toHaveLength(70 - 19);
    expect(pts[0]!.time).toBe(ind.bars[19]!.trade_date);
    expect(pts.every((p) => Number.isFinite(p.value))).toBe(true);
    // a hole inside the series stays a hole
    const holed = { values: ma20.values.map((v, i) => (i === 40 ? null : v)) };
    const p2 = seriesPoints(ind, holed);
    expect(p2).toHaveLength(pts.length - 1);
    expect(p2.some((p) => p.time === ind.bars[40]!.trade_date)).toBe(false);
  });

  it('uses the same time key as the candles (daily: trade date, intraday: CST wall clock)', () => {
    const bar = { trade_date: '2026-11-02', bar_open_time: '2026-11-02T01:31:00Z' };
    expect(barTime(bar, false)).toBe('2026-11-02');
    expect(barTime(bar, true)).toBe(Math.floor(Date.parse('2026-11-02T01:31:00Z') / 1000) + 8 * 3600);
    const intra = buildIndicators({ timeframe: '5m', bars: 30, set: ['ma'] });
    const first = seriesPoints(intra, intra.series.find((s) => s.key === 'ma5')!)[0]!;
    expect(first.time).toBe(barTime(intra.bars[4]!, true));
  });

  it('draws only when the payload was computed from the candle window on screen', () => {
    expect(checkAlignment(ind, win(ind))).toEqual({ ok: true });
    expect(checkAlignment(ind, win(ind, { window_hash: 'x'.repeat(64) }))).toMatchObject({ ok: false });
    expect(checkAlignment(ind, win(ind, { snapshot_id: 'other' }))).toMatchObject({ ok: false, reason: expect.stringContaining('快照') });
    expect(checkAlignment(ind, win(ind, { bars: [] }))).toMatchObject({ ok: false });
    expect(checkAlignment(null, win(ind))).toMatchObject({ ok: false });
  });

  it('puts MA/EMA/BOLL on the price pane and one selectable sub-pane beneath', () => {
    const on = overlaySeries(ind, { ma: true, ema: false, boll: true }).map((s) => s.key);
    expect(on).toEqual(['ma5', 'ma10', 'ma20', 'ma60', 'boll_mid', 'boll_upper', 'boll_lower']);
    expect(overlaySeries(ind, { ma: false, ema: false, boll: false })).toEqual([]);
    expect(subPaneSeries(ind, 'macd').map((s) => [s.key, s.kind])).toEqual([['macd', 'line'], ['macd_signal', 'line'], ['macd_hist', 'histogram']]);
    expect(subPaneSeries(ind, 'kdj').map((s) => s.key)).toEqual(['kdj_k', 'kdj_d', 'kdj_j']);
    expect(subPaneSeries(ind, 'rsi').map((s) => s.key)).toEqual(['rsi6', 'rsi14']);
  });

  it('requests only what is switched on and explains the warm-up gap', () => {
    expect(requestedSets({ ma: true, ema: false, boll: true }, 'kdj')).toEqual(['ma', 'boll', 'kdj']);
    expect(requestedSets({ ma: false, ema: false, boll: false }, 'none')).toEqual([]);
    expect(indicatorQuery({ instrument_id: 'i', release_id: null, timeframe: '1d', adjustment: 'raw', set: ['ma', 'macd'] })).toBe('instrument_id=i&timeframe=1d&adjustment=raw&set=ma%2Cmacd');
    const note = warmupNote(ind, overlaySeries(ind, { ma: true, ema: false, boll: false }));
    expect(note).toContain('没有补 0');
    const short = buildIndicators({ timeframe: '1d', bars: 30, set: ['ma'] });
    expect(warmupNote(short, overlaySeries(short, { ma: true, ema: false, boll: false }))).toContain('MA60');
  });
});

describe('statistics formatting', () => {
  const f = (over: Partial<StatField>): StatField => ({ key: 'turnover_rate', label: '换手率', status: 'ok', value: '1.5', value_kind: 'decimal', unit: '%', as_of: '2026-11-12', basis: null, reason: null, ...over });
  it('never turns a missing field into a number', () => {
    const s = formatStat(f({ key: 'seal_amount', status: 'unavailable', value: null, value_kind: null, unit: '元', reason: '没有分时数据' }));
    expect(s).toEqual({ text: null, estimated: false, missing: true, note: '没有分时数据' });
  });
  it('flags estimates and scales amounts', () => {
    const rate = formatStat(f({ status: 'estimated', value: '1.2345', reason: '按成交额估算' }));
    expect(rate).toMatchObject({ text: '1.23%', estimated: true, missing: false, note: '按成交额估算' });
    expect(formatStat(f({ key: 'turnover_amount', unit: '元', value: '123456789.5', value_kind: 'decimal' })).text).toBe('1.23 亿元');
    expect(formatStat(f({ key: 'dist_to_60d_high', value: '-3.14159' })).text).toBe('-3.14%');
    expect(formatStat(f({ key: 'dist_to_60d_high', value: '0.5' })).text).toBe('+0.50%');
    expect(formatStat(f({ key: 'limit_streak', value: '2', value_kind: 'integer', unit: '板' })).text).toBe('2 板');
    expect(formatStat(f({ key: 'listing_date', value: '2003-06-09', value_kind: 'date', unit: null })).text).toBe('2003-06-09');
    expect(formatStat(f({ value: '1e3' }))).toMatchObject({ missing: true });
  });
});

describe('stats card', () => {
  afterEach(() => vi.unstubAllGlobals());
  it('shows 暂无数据 with the reason for seal fields and 估算 for the turnover estimate', async () => {
    const body = { meta: {}, data: buildStats() };
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(body), { status: 200 })));
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={qc}><StockStatsCard instrumentId={INSTRUMENT_ID} releaseId={RELEASE_ID} /></QueryClientProvider>);
    await waitFor(() => expect(screen.getByText('首次封板时间')).toBeInTheDocument());
    expect(screen.getAllByText('暂无数据').length).toBeGreaterThanOrEqual(4);
    expect(screen.getAllByText(/分时与盘口/).length).toBeGreaterThanOrEqual(4);
    expect(screen.getByText('估算')).toBeInTheDocument();
    expect(screen.getByText('上市日期')).toBeInTheDocument();
  });
});

describe('dev mock keeps the two reads on one window', () => {
  it('indicator series are computed for the very bars the mock bars handler serves (same window_hash)', () => {
    const state = createMockState();
    const r = currentRelease(state);
    const inst = INSTRUMENTS[0]!;
    const end = new Date(r.decisionAsOf).toISOString().replace(/\.\d{3}Z$/, 'Z');
    const start = new Date(r.decisionAsOf - LOOKBACK_MS['1d']).toISOString().replace(/\.\d{3}Z$/, 'Z');
    const bars = handle(state, { method: 'GET', path: `/v1/stocks/${inst.id}/bars`, headers: {}, body: undefined, query: new URLSearchParams({
      snapshot_id: 's', decision_as_of: end, availability_policy_version: 'p', timeframe: '1d', session_policy_version: 'cn-a-continuous-v1', adjustment_mode: 'raw', start, end }) });
    const ind = handle(state, { method: 'GET', path: '/v1/x/bf/stock/indicators', headers: {}, body: undefined, query: new URLSearchParams({ instrument_id: inst.id, release_id: r.id, timeframe: '1d', adjustment: 'raw', set: 'ma' }) });
    const win = (bars.body as { data: BarWindow }).data;
    const data = (ind.body as { data: StockIndicators }).data;
    expect(data.window_hash).toBe(win.window_hash);
    expect(checkAlignment(data, win)).toEqual({ ok: true });
    expect(data.series.every((s) => s.values.length === win.bars.length)).toBe(true);
  });
});
