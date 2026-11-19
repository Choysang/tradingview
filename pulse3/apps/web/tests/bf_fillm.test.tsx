/**
 * Fill-market lane (legacy backfill) web checks: crowd panel (#32/#90/#155), stats additions (#43), watchlist tape (#39) and the
 * bear-mode banner (#13). Data is contract-shaped (mock builders); missing and partial states are asserted explicitly.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import type { ReactElement } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { buildCrowd, buildStatsPlus } from '../mock/bf_fillm';
import { BEAR_TOOLTIP, BearBanner, bearBannerOf } from '../src/pages/bf/fillm/BearBanner';
import { CrowdPanel, netText, pctText, spreadCell } from '../src/pages/bf/fillm/CrowdPanel';
import { StatsPlusRows, coverageText, formatPlus } from '../src/pages/bf/fillm/StatsPlus';
import { WatchTape, filterTapeToWatch } from '../src/pages/bf/fillm/WatchTape';
import type { StatPlusField } from '../src/pages/bf/fillm/api';
import type { Regime, TapeItem } from '../src/pages/ext/a/types';

afterEach(() => vi.unstubAllGlobals());

const wrap = (ui: ReactElement) => (
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider>
);
const ok = (data: unknown) => new Response(JSON.stringify({ meta: {}, data }), { status: 200 });

describe('formatting never turns a missing value into a number', () => {
  it('pct and net texts keep null as null', () => {
    expect(pctText(null)).toBeNull();
    expect(pctText('3.5')).toBe('+3.50%');
    expect(pctText('-1')).toBe('-1.00%');
    expect(netText(null)).toBeNull();
    expect(netText('200000000.00')).toBe('+2.00 亿元');
    expect(netText('-110000000')).toBe('-1.10 亿元');
  });
  it('a spread cell without a plausible move shows no share', () => {
    expect(spreadCell({ session: '2026-11-10', observed: 0, limit_ups: 0, up_share: null, strong_followers: 0 })).toBe('0 板 · — 上涨');
    expect(spreadCell({ session: '2026-11-10', observed: 6, limit_ups: 2, up_share: '66.6667', strong_followers: 1 })).toBe('2 板 · 67% 上涨');
  });
});

describe('crowd panel', () => {
  it('shows industries with leader and followers, the institutional net, a missing net with its reason, and the retired name cluster row', async () => {
    const data = buildCrowd('r1', '2026-11-12');
    vi.stubGlobal('fetch', vi.fn(async () => ok(data)));
    render(wrap(<CrowdPanel releaseId="r1" />));
    await waitFor(() => expect(screen.getByText('模拟行业')).toBeInTheDocument());
    expect(screen.getByText('龙头')).toBeInTheDocument();
    expect(screen.getByText('跟风')).toBeInTheDocument();
    const spread = screen.getByLabelText(/模拟行业 近 5 个交易日扩散/);
    expect(within(spread).getAllByRole('listitem')).toHaveLength(5);
    expect(screen.getByText('+2.00 亿元')).toBeInTheDocument();
    expect(screen.getByText('披露行没有卖出金额，不猜净额')).toBeInTheDocument();
    expect(screen.getByText(/暂无数据 · 建议退役/)).toBeInTheDocument();
    expect(screen.getAllByText('缺失').length).toBeGreaterThanOrEqual(1);
  });

  it('an empty answer says so instead of drawing an empty table', async () => {
    const data = { ...buildCrowd('r1', '2026-11-12'), industries: [], institutions: { status: 'no_seat_reports', session_date: '2026-11-12', rows: [], reports_seen: 0, basis: 'b', notes: ['没有这个交易日的龙虎榜报告'] } };
    vi.stubGlobal('fetch', vi.fn(async () => ok(data)));
    render(wrap(<CrowdPanel releaseId="r1" />));
    await waitFor(() => expect(screen.getByText(/当天没有行业出现 2 只以上涨停/)).toBeInTheDocument());
    expect(screen.getByText('没有这个交易日的龙虎榜报告')).toBeInTheDocument();
  });

  it('a failed read is an error state, not an empty panel', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ meta: {}, error: { code: 'NOT_FOUND', message: 'x', retryable: false, field_errors: [] } }), { status: 404 })));
    render(wrap(<CrowdPanel releaseId="r1" />));
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
  });
});

describe('stats additions', () => {
  const field = (over: Partial<StatPlusField>): StatPlusField => ({ ...buildStatsPlus('i', 'r', 't').fields[2]!, ...over });

  it('the seal success rate always states how many sessions it covers and never calls a partial window a year', () => {
    const f = field({});
    const d = formatPlus(f);
    expect(d.text).toBe('50.00%');
    expect(d.estimated).toBe(true);
    expect(d.coverageText).toContain('覆盖 3 个交易日');
    expect(d.coverageText).toContain('不足 250 个交易日，不是1年的值');
    expect(coverageText(null)).toBeNull();
    expect(coverageText({ ...f.coverage!, sessions_covered: 250 })).not.toContain('不是1年的值');
    expect(coverageText({ ...f.coverage!, sessions_covered: 0 })).toBe('封板事实覆盖 0 个交易日（窗口 250 个）');
  });

  it('an unavailable field has no number', () => {
    const d = formatPlus(field({ status: 'unavailable', value: null, value_kind: null, reason: '封板事实数据集里没有这只股票' }));
    expect(d.missing).toBe(true);
    expect(d.text).toBeNull();
    expect(d.note).toBe('封板事实数据集里没有这只股票');
  });

  it('renders the three rows with estimate flags and the coverage line', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ok(buildStatsPlus('i', 'r', '2026-11-12T07:00:00Z'))));
    render(wrap(<StatsPlusRows instrumentId="i" releaseId="r" />));
    await waitFor(() => expect(screen.getByText('量比')).toBeInTheDocument());
    expect(screen.getByText('1.82 倍')).toBeInTheDocument();
    expect(screen.getByText('50.00 亿元')).toBeInTheDocument();
    expect(screen.getByText(/封板成功率（3 个交易日）/)).toBeInTheDocument();
    expect(screen.getAllByText('估算')).toHaveLength(2);
    expect(screen.getByText(/自由流通市值/)).toBeInTheDocument();
  });
});

const tape = (id: string, instrument: string): TapeItem => ({
  event_id: id, recorded_at: '2026-11-12T02:00:00Z', event_kind: 'seal', instrument_id: instrument, code: '600000', name: `股${instrument}`, price: '10.00', pct_chg: '10.00',
  limit_streak: 1, detail: 'd', themes: [], trade_date: '2026-11-12', event_time: null, from_snapshot_id: 'a', to_snapshot_id: 'b', tape_version: 'tape-v1',
});

describe('watchlist tape', () => {
  it('keeps only watched instruments, newest first, and an empty watchlist matches nothing', () => {
    const events = [tape('1', 'a'), tape('2', 'b'), tape('3', 'a')];
    expect(filterTapeToWatch(events, ['a']).map((e) => e.event_id)).toEqual(['3', '1']);
    expect(filterTapeToWatch(events, [])).toEqual([]);
    expect(filterTapeToWatch(events, ['zzz'])).toEqual([]);
  });

  it('renders the watched events and says how many were read when none match', async () => {
    const page = { events: [tape('1', 'a'), tape('2', 'b')], resume_cursor: 'c', reset_required: true, stream_epoch: 'e' };
    vi.stubGlobal('fetch', vi.fn(async () => ok(page)));
    const { unmount } = render(wrap(<WatchTape watchIds={['a']} />));
    await waitFor(() => expect(screen.getByText('股a')).toBeInTheDocument());
    expect(screen.queryByText('股b')).toBeNull();
    expect(screen.getByText(/最近 2 条盘口异动中属于自选的 1 条/)).toBeInTheDocument();
    unmount();
    render(wrap(<WatchTape watchIds={['q']} />));
    await waitFor(() => expect(screen.getByText(/最近 2 条异动里没有自选股票的/)).toBeInTheDocument());
    expect(screen.getByText(/不代表没有发生/)).toBeInTheDocument();
  });

  it('renders nothing for an empty watchlist and does not fetch', () => {
    const f = vi.fn();
    vi.stubGlobal('fetch', f);
    const { container } = render(wrap(<WatchTape watchIds={[]} />));
    expect(container.textContent).toBe('');
    expect(f).not.toHaveBeenCalled();
  });
});

const regime = (trend: string, basis: string, over: Partial<Regime> = {}): Regime => ({
  version: 'v', available: true, reason: null, index: { symbol: '000985', name: '中证全指', through: '2026-11-12', fallback: false }, points: [], rules: [],
  current: { date: '2026-11-12', index_close: '1', trend, liquidity: 'steady', sentiment: 'ebb', style: 'balanced', breadth_ma20: null, limit_up_count: null, broken_rate: null,
    max_height: null, amount_ratio: null, premium: null, lu_percentile: null, index_ma60: null, ma60_slope: null, style_spread: null,
    basis: { trend: basis, liquidity: 'ok', sentiment: 'ok', style: 'ok' }, label: 'x', explanation: [] },
  ...over,
} as unknown as Regime);

describe('bear-mode banner', () => {
  it('shows only for a measured bear trend', () => {
    expect(bearBannerOf(regime('bear', 'ok'))).toEqual({ date: '2026-11-12', through: '2026-11-12' });
    expect(bearBannerOf(regime('bull', 'ok'))).toBeNull();
    expect(bearBannerOf(regime('range', 'ok'))).toBeNull();
  });
  it('never shows on an insufficient-history label, a missing index or an unavailable regime', () => {
    expect(bearBannerOf(regime('bear', 'insufficient_history'))).toBeNull();
    expect(bearBannerOf(regime('bear', 'missing_index'))).toBeNull();
    expect(bearBannerOf(regime('bear', 'ok', { available: false }))).toBeNull();
    expect(bearBannerOf(regime('bear', 'ok', { current: null }))).toBeNull();
    expect(bearBannerOf(null)).toBeNull();
  });
  it('cites E016 as a state label and not a trading rule', () => {
    expect(BEAR_TOOLTIP).toContain('E016');
    expect(BEAR_TOOLTIP).toContain('mixed');
    expect(BEAR_TOOLTIP).toContain('不是交易规则');
    render(<BearBanner regime={regime('bear', 'ok')} />);
    expect(screen.getByRole('status', { name: '熊市状态提示' })).toBeInTheDocument();
    expect(screen.getAllByTitle(BEAR_TOOLTIP).length).toBeGreaterThanOrEqual(1);
  });
  it('renders nothing when the rule says not to show', () => {
    const { container } = render(<BearBanner regime={regime('bear', 'insufficient_history')} />);
    expect(container.textContent).toBe('');
  });
});
