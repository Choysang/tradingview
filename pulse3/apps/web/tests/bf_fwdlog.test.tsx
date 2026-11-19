/** Fwdlog lane web checks: the page draws the server's cards (fixtures are real API responses that pass the Python contract test), marks provisional numbers, never shows a verdict early. */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import type { ReactElement } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { REQUEST_FIELDS } from '../src/pages/bf/fwdlog/api';
import { COLUMNS, ExperimentCardView, ForwardLogPage, RowsTable, metricValue, num, progressText } from '../src/pages/bf/fwdlog/ForwardLogPage';
import type { ExperimentsData, Resp, RowsData } from '../src/pages/bf/fwdlog/types';

const fx = <T,>(name: string) => JSON.parse(readFileSync(resolve(__dirname, `../src/pages/bf/fwdlog/fixtures/${name}`), 'utf-8')) as T;
const experiments = fx<Resp<ExperimentsData>>('experiments.json');
const rowsOf = (k: string) => fx<Resp<RowsData>>(`rows-${k}.json`);
const card = (key: string) => experiments.data.experiments.find((c) => c.experiment_key === key)!;

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
  it('formats decimals without inventing digits and keeps a missing value a dash', () => {
    expect(num('0.1000')).toBe('0.1');
    expect(num('100', 0)).toBe('100');
    expect(num('8')).toBe('8');
    expect(num(null)).toBe('—');
    expect(num('')).toBe('—');
    expect(metricValue({ name: 'x', value: null, provisional: false, pending_reason: 'p' })).toBe('—');
    expect(metricValue({ name: 'x', value: false, provisional: false, pending_reason: null })).toBe('否');
    expect(metricValue({ name: 'x', value: 4, provisional: false, pending_reason: null })).toBe('4');
    expect(REQUEST_FIELDS.rows).toEqual(['from', 'to', 'limit', 'offset']);
  });
  it('states progress toward the decision day with what is left', () => {
    expect(progressText(card('E077'))).toBe('1 / 40 个决策日（还差 39）');
    expect(progressText(card('PAPER'))).toBe('2 / 20 个录制日（还差 18）');
  });
});

describe('experiment card', () => {
  it('never shows a verdict before the decision day: metrics are marked as progress and the server pending reason is shown', () => {
    wrap(<ExperimentCardView card={card('E077')} />);
    expect(screen.getByTestId('decision').textContent).toMatch(/pending until 40/);
    expect(screen.queryByText(/works|fails/)).toBeNull();
    const table = screen.getByRole('table', { name: '预注册指标' });
    expect(within(table).getAllByText('进度值，非结论').length).toBeGreaterThanOrEqual(3);
    expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('1');
    expect(screen.getByText(/影子运行（running）/)).toBeTruthy();
    expect(screen.getByText(/等待中的交易日 1/)).toBeTruthy();
  });

  it('paper card lists the sell rules side by side with the shadow flag', () => {
    wrap(<ExperimentCardView card={card('PAPER')} />);
    const t = screen.getByRole('table', { name: '卖出规则并排' });
    expect(within(t).getByText('POLICY_v2')).toBeTruthy();
    expect(within(t).getByText('HOLD_close')).toBeTruthy();
  });

  it('reads the daily rows only when opened and draws the old log columns', async () => {
    const calls = stubFetch(() => ({ status: 200, body: rowsOf('e077') }));
    wrap(<ExperimentCardView card={card('E077')} />);
    expect(calls).toHaveLength(0);
    await userEvent.click(screen.getByRole('button', { name: '看每日记录' }));
    const t = await screen.findByRole('table', { name: 'E077 每日记录' });
    expect(calls[0]).toContain('/v1/x/bf/fwdlog/experiments/E077/rows');
    expect(within(t).getByText('600110 600109 600108 600107 600106')).toBeTruthy();
    expect(within(t).getByText('7.748')).toBeTruthy();
  });

  it('every experiment has a column set that renders its fixture rows without a crash', () => {
    for (const k of ['E077', 'E078', 'E089', 'PAPER'] as const) {
      const rows = rowsOf(k.toLowerCase()).data.rows;
      expect(rows.length).toBeGreaterThan(0);
      const { container, unmount } = render(<RowsTable experimentKey={k} rows={rows} />);
      expect(container.querySelectorAll('tbody tr')).toHaveLength(rows.length);
      expect(COLUMNS[k].length).toBeGreaterThan(2);
      unmount();
    }
  });

  it('an empty log says so instead of drawing zeros', () => {
    wrap(<RowsTable experimentKey="E078" rows={[]} />);
    expect(screen.getByText(/还没有记录/)).toBeTruthy();
  });
});

describe('page', () => {
  it('lists the four experiments from one read', async () => {
    const calls = stubFetch(() => ({ status: 200, body: experiments }));
    wrap(<ForwardLogPage />);
    await waitFor(() => expect(screen.getAllByRole('region').length).toBe(4));
    expect(calls).toHaveLength(1);
    expect(calls[0]).toContain('/v1/x/bf/fwdlog/experiments');
    expect(screen.getByText(/前向实验日志/)).toBeTruthy();
  });

  it('shows the error state when the server fails', async () => {
    stubFetch(() => ({ status: 500, body: { meta: experiments.meta, error: { code: 'INTERNAL', message: 'boom', retryable: false, field_errors: [] } } }));
    wrap(<ForwardLogPage />);
    expect(await screen.findByText(/前向实验.*失败|boom|读取/)).toBeTruthy();
  });
});
