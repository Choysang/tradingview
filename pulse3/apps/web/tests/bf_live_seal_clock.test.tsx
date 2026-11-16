/** Live lane web checks: the seal clock draws what the server sends (fixtures pass the Python contract test), says "estimated", and shows an explicit empty state. */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import type { ReactElement } from 'react';
import { resolve } from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { REQUEST_FIELDS, sessionParam } from '../src/pages/bf/live/api';
import { SealClockCard, SealClockChart, SealFactsPanel, cstClock, wanYuan } from '../src/pages/bf/live/SealClock';
import type { Resp, SealClockData, SealFactsData } from '../src/pages/bf/live/types';

const fx = (name: string) => JSON.parse(readFileSync(resolve(__dirname, `../src/pages/bf/live/fixtures/${name}`), 'utf-8')) as never;
const clock = fx('seal-clock.json') as Resp<SealClockData>;
const facts = fx('seal-facts.json') as Resp<SealFactsData>;

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function stubFetch(handler: (url: string) => { status: number; body: unknown }) {
  const calls: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    calls.push(String(url));
    const r = handler(String(url));
    return new Response(JSON.stringify(r.body), { status: r.status, headers: { 'content-type': 'application/json' } });
  }));
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

describe('helpers', () => {
  it('formats UTC instants as Beijing wall clock and money in 亿/万', () => {
    expect(cstClock('2026-11-12T01:35:00Z')).toBe('09:35');
    expect(cstClock(null)).toBe('—');
    expect(wanYuan('8800000')).toBe('880 万');
    expect(wanYuan('123456789')).toBe('1.23 亿');
    expect(wanYuan(null)).toBe('—');
  });
  it('sends a session date only when it looks like one', () => {
    expect(sessionParam('2026-11-12')).toBe('2026-11-12');
    expect(sessionParam('PRE_OPEN')).toBeNull();
    expect(sessionParam(undefined)).toBeNull();
    expect(REQUEST_FIELDS.sealFacts).toEqual(['instrument_id', 'session_date']);
  });
});

describe('seal clock', () => {
  it('draws one bar per bucket with the counts the server computed, never a client recomputation', () => {
    const { container } = render(<SealClockChart data={clock.data} />);
    expect(container.querySelectorAll('rect.bl-bar')).toHaveLength(clock.data.buckets.length);
    const titles = [...container.querySelectorAll('rect.bl-bar title')].map((t) => t.textContent);
    expect(titles.some((t) => t?.includes('09:35') && t.includes('首次封板 1 只'))).toBe(true);
    expect(container.querySelector('path.bl-curve')).not.toBeNull();
    expect(screen.getByRole('img').getAttribute('aria-label')).toContain('估算');
  });

  it('card: loads the session, states the figures are estimates and keeps the caveats in the details', async () => {
    const calls = stubFetch(() => ({ status: 200, body: clock }));
    wrap(<SealClockCard sessionDate="2026-11-12" />);
    await screen.findByText('封住过');
    expect(screen.getByText('估算')).toBeInTheDocument();
    expect(screen.getByText(/可能比真实封板晚至多一个采样间隔/)).toBeInTheDocument();
    expect(calls[0]).toContain('/v1/x/bf/live/seal-clock?session_date=2026-11-12');
    expect(screen.getByText('炸板（下限）')).toBeInTheDocument();
  });

  it('card: a non-date session means the latest session (no session_date sent)', async () => {
    const calls = stubFetch(() => ({ status: 200, body: clock }));
    wrap(<SealClockCard sessionDate="CONTINUOUS_AM" />);
    await screen.findByText('封住过');
    expect(calls[0]).not.toContain('session_date');
  });

  it('card: a session without seal facts is an explicit empty state, not a zero chart', async () => {
    stubFetch(() => ({ status: 404, body: { meta: clock.meta, error: { code: 'NOT_FOUND', message: '该交易日没有封板事实', retryable: false, field_errors: [] } } }));
    const { container } = wrap(<SealClockCard sessionDate="2026-11-12" missingNote="首次封板时间需要盘中数据" />);
    await screen.findByText(/首次封板时间需要盘中数据/);
    expect(container.querySelector('svg')).toBeNull();
    expect(screen.getByText(/这里不编造/)).toBeInTheDocument();
  });

  it('card: a server failure is shown as an error with retry', async () => {
    stubFetch(() => ({ status: 500, body: { meta: clock.meta, error: { code: 'INTERNAL', message: '内部错误', retryable: true, field_errors: [] } } }));
    wrap(<SealClockCard sessionDate="2026-11-12" />);
    await screen.findByText(/封板时钟读取失败/, {}, { timeout: 4000 });
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument();
  });
});

describe('stock seal facts panel', () => {
  it('shows first/last seal in Beijing time, the lower-bound break count and the close seal amount', async () => {
    stubFetch(() => ({ status: 200, body: facts }));
    wrap(<SealFactsPanel instrumentId={facts.data.instrument_id} sessionDate="2026-11-12" />);
    await screen.findByText('首次封板');
    expect(screen.getByText(/^09:35/)).toBeInTheDocument();
    expect(screen.getByText('10:20')).toBeInTheDocument();
    expect(screen.getByText('880 万')).toBeInTheDocument();
    expect(screen.getByText('炸板（下限）')).toBeInTheDocument();
  });

  it('says so when the instrument was not observed or the session has no dataset', async () => {
    stubFetch(() => ({ status: 200, body: { meta: facts.meta, data: { ...facts.data, status: 'not_observed', fact: null, notes: ['该证券当日没有快照'] } } }));
    wrap(<SealFactsPanel instrumentId={facts.data.instrument_id} />);
    await waitFor(() => expect(screen.getByText('该证券当日没有快照')).toBeInTheDocument());
  });
});
