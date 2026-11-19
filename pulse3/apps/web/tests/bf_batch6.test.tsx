/** Batch6 lane web checks: the page draws the server's numbers (the fixture is a real wire payload that passes the Python contract test), marks progress values, never shows a verdict early. */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, within } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import type { ReactElement } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Batch6Page, Batch6View, ForwardCard, HistoryTable, num, pct, progressShare } from '../src/pages/bf/batch6/Batch6Page';
import type { Overview, Resp } from '../src/pages/bf/batch6/types';

const fx = JSON.parse(readFileSync(resolve(__dirname, '../src/pages/bf/batch6/fixtures/overview.json'), 'utf-8')) as Resp<Overview>;
const data = fx.data;

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

afterEach(() => vi.unstubAllGlobals());

describe('helpers', () => {
  it('formats ratios as signed percents and keeps a missing value a dash', () => {
    expect(pct('0.0123')).toBe('+1.23%');
    expect(pct('-0.004', 3)).toBe('-0.400%');
    expect(pct(null)).toBe('—');
    expect(num('0.0200')).toBe('0.02');
    expect(num(null)).toBe('—');
    expect(progressShare(21, 40)).toBe(53);
    expect(progressShare(90, 40)).toBe(100);
  });
});

describe('history table', () => {
  it('shows PRE and DEV rows per hypothesis; Holm and the verdict only on PRE', () => {
    const h = data.history!;
    render(<HistoryTable rows={h.rows} verdicts={h.verdicts} />);
    const rows = screen.getAllByRole('row').slice(1);
    expect(rows).toHaveLength(h.rows.length);
    const h1pre = rows.find((r) => within(r).queryByText('PRE') && r.textContent!.includes('H1'))!;
    expect(h1pre.textContent).toContain('-0.400%');
    expect(within(h1pre).getByText('通过')).toBeTruthy();
    const h1dev = rows.find((r) => within(r).queryByText('DEV') && r.textContent!.includes('H1'))!;
    expect(within(h1dev).queryByText('通过')).toBeNull();
  });
});

describe('forward cards', () => {
  it('a collecting card shows progress and no verdict', () => {
    const row = data.forward.find((r) => r.hypothesis === 'H7')!;
    render(<ForwardCard r={row} />);
    expect(screen.getByText('收集中')).toBeTruthy();
    expect(screen.getByText(/满 40 个信号日之前不判定/)).toBeTruthy();
    expect(screen.queryByText('通过')).toBeNull();
  });
  it('a descriptive card labels its numbers as progress values, not a verdict', () => {
    const row = data.forward.find((r) => r.hypothesis === 'H5')!;
    render(<ForwardCard r={row} />);
    expect(screen.getByText('仅描述（已满 20 日）')).toBeTruthy();
    expect(screen.getAllByText(/进度值/).length).toBeGreaterThan(0);
    expect(screen.queryByText('已判定')).toBeNull();
  });
});

describe('page', () => {
  it('draws history, forward and the field check from the API response', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(fx), { status: 200, headers: { 'content-type': 'application/json' } })));
    wrap(<Batch6Page />);
    expect(await screen.findByText(/E093 \/ E094 历史回测/)).toBeTruthy();
    expect(screen.getByText(/unmatched_buy：可计算/)).toBeTruthy();
    expect(screen.getByText(new RegExp(data.spec_sha256))).toBeTruthy();
  });
  it('without history or field check shows the empty states, not zeros', () => {
    render(<Batch6View data={{ ...data, history: null, fieldcheck: null, forward: [] }} />);
    expect(screen.getByText(/尚未运行历史回测/)).toBeTruthy();
    expect(screen.getByText(/尚无前向记录/)).toBeTruthy();
    expect(screen.getByText(/尚未核对/)).toBeTruthy();
  });
});
