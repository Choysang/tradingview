/** Fusion lane web checks: the page draws what the server sent (holdings, the names a book will try to buy, flagged dates kept out), shows missing
 * values as 缺失 and never as 0, and asks for a day's scores with the chosen date. Fixtures pass the Python contract test (tests/bf_fusion/test_web_fixtures.py). */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import type { ReactElement } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { FusionBooks, FusionExperiments, FusionScores, FusionStatus } from '../src/pages/bf/fusion/api';
import { FusionPage, NavLine, num, pct, signed } from '../src/pages/bf/fusion/FusionPage';

const fx = <T,>(name: string): T => JSON.parse(readFileSync(resolve(__dirname, `../src/pages/bf/fusion/fixtures/${name}.json`), 'utf-8')) as T;
const STATUS = fx<FusionStatus>('status');
const SCORES = fx<FusionScores>('scores');
const BOOKS = fx<FusionBooks>('books');
const EXPS = fx<FusionExperiments>('experiments');

const META = {
  request_id: 'r', generated_at: '2026-11-17T01:00:00Z', trace_id: null, contract_version: '3.0.0', release_id: null, decision_as_of: null, availability_policy_version: null,
  input_snapshot_refs: [], quality_status: 'partial', freshness_state: 'not_expected', coverage: { policy_version: 'operational-v1', expected_count: null, observed_count: 0, missing_count: null, unverifiable_reasons: [] },
  warnings: [], next_cursor: null, actionability_id: null,
};
const json = (status: number, body: unknown): Response => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
const ok = (data: unknown): Response => json(200, { meta: META, data });
const fail = (status: number, code: string): Response => json(status, { meta: META, error: { code, message: code, retryable: false, field_errors: [] } });

interface Call { url: URL }
function route(handlers: Record<string, Response | ((c: Call) => Response)>): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: string | Request) => {
    const url = new URL(typeof input === 'string' ? input : input.url, 'http://localhost');
    calls.push({ url });
    const h = handlers[url.pathname.replace('/v1/x/bf/fusion/', '')];
    const res = typeof h === 'function' ? h({ url }) : h;
    return res ? res.clone() : fail(404, 'NOT_FOUND');
  }));
  return calls;
}
function renderPage(el: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><MemoryRouter>{el}</MemoryRouter></QueryClientProvider>);
}
afterEach(() => { vi.unstubAllGlobals(); });

describe('format helpers', () => {
  it('never turns a missing value into zero', () => {
    expect(num(null)).toBe('缺失');
    expect(num('')).toBe('缺失');
    expect(num('0')).toBe('0.00');
    expect(pct(null)).toBe('缺失');
    expect(pct('0.458')).toBe('+0.46%');
    expect(signed(undefined)).toBe('缺失');
    expect(signed('-0.184')).toBe('-0.184');
  });
  it('draws no line for fewer than two NAV points', () => {
    const { container } = render(<NavLine label="x" points={[{ d: '2026-11-02', v: 100 }]} />);
    expect(container.querySelector('polyline')).toBeNull();
    expect(screen.getByText(/净值点数不足/)).toBeTruthy();
  });
});

describe('FusionPage', () => {
  const all = {
    status: ok(STATUS), scores: ok(SCORES), books: ok(BOOKS), experiments: ok(EXPS),
  };

  it('shows the run, both books with the names they will try to buy, and the three experiments', async () => {
    route(all);
    renderPage(<FusionPage />);
    expect(await screen.findByText(/最近一次运行/)).toBeTruthy();
    expect(screen.getByText(/峰值内存 1750 MB/)).toBeTruthy();
    await waitFor(() => expect(screen.getByTestId('holdings-main').textContent).toBe('002632 603937'));
    expect(screen.getByTestId('trybuy-main').textContent).toBe('600513');
    expect(screen.getAllByText(/降仓/).length).toBeGreaterThan(0);            // gate weight 0.25 is shown as a reduced book
    expect(screen.getByTestId('holdings-h10')).toBeTruthy();
    expect(screen.getByRole('table', { name: '前向监控' })).toBeTruthy();
    expect(screen.getByRole('table', { name: 'X20 篮子' })).toBeTruthy();
    expect(screen.getByRole('table', { name: 'OS20' })).toBeTruthy();
  });

  it('keeps a flagged monitor date out and says why, and shows a missing score name and group excess as 缺失', async () => {
    route(all);
    renderPage(<FusionPage />);
    const mon = await screen.findByRole('table', { name: '前向监控' });
    const flagged = within(mon).getByText('2026-11-03').closest('tr') as HTMLElement;
    expect(within(flagged).getByText(/不计入：覆盖率不足 2026-11-05/)).toBeTruthy();
    const counted = within(mon).getByText('2026-11-02').closest('tr') as HTMLElement;
    expect(within(counted).getByText('计入')).toBeTruthy();
    const scores = await screen.findByRole('table', { name: '分数前列' });
    expect(within(scores).getAllByText('缺失').length).toBe(1);                // the name of rank 3
    const os = screen.getByRole('table', { name: 'OS20' });
    expect(within(os).getByText(/106 \/ 缺失/)).toBeTruthy();                  // OS5_GAP excess is null: never 0
    const basket = screen.getByRole('table', { name: 'X20 篮子' });
    expect(within(basket).getByText('换手 0.500')).toBeTruthy();
  });

  it('asks for the scores of the date the user picks', async () => {
    const calls = route(all);
    renderPage(<FusionPage />);
    const pick = await screen.findByLabelText('选择日期');
    await userEvent.selectOptions(pick, '2026-11-05');
    await waitFor(() => expect(calls.some((c) => c.url.pathname.endsWith('/scores') && c.url.searchParams.get('date') === '2026-11-05')).toBe(true));
  });

  it('shows empty states, not zeros, before the first run', async () => {
    route({ status: ok(fx('status-empty')), scores: ok(fx('scores-empty')), books: ok(fx('books-empty')), experiments: ok({ experiments: [] }) });
    renderPage(<FusionPage />);
    expect(await screen.findByText(/还没有运行/)).toBeTruthy();
    expect(await screen.findByText('这一天还没有分数。')).toBeTruthy();
    expect((await screen.findAllByText(/还没有决策行/)).length).toBe(2);
  });

  it('shows an error for the part that failed and keeps the others', async () => {
    route({ ...all, books: fail(500, 'INTERNAL') });
    renderPage(<FusionPage />);
    expect(await screen.findByText(/账本读取失败/)).toBeTruthy();
    expect(await screen.findByRole('table', { name: '分数前列' })).toBeTruthy();
  });
});
