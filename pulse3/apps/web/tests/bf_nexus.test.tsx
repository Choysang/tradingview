import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactElement } from 'react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  buildMockArena,
  buildMockIntegrity,
  buildMockNexusHub,
  buildMockSectorDeepDive,
  buildMockStockPro,
} from '../mock/bf_nexus';
import { createMockState, currentRelease } from '../mock/handler';
import { ToastProvider } from '../src/components/Toast';
import { PapersAndBenchmarkView, SwissArenaView } from '../src/pages/bf/nexus/ArenaAndPapersPanel';
import { NexusHubPage } from '../src/pages/bf/nexus/NexusHubPage';
import { SectorTopPicksPanel } from '../src/pages/bf/nexus/SectorTopPicksPanel';
import {
  NexusActionSummaryCard,
  NexusExecutionSection,
  NexusMicrostructureSection,
  NexusTvProSection,
} from '../src/pages/bf/nexus/StockProPanels';
import { StockPage } from '../src/pages/StockPage';

// Mock lightweight-charts canvas creation in jsdom while keeping PriceChart logic intact
vi.mock('lightweight-charts', () => ({
  CandlestickSeries: 'CandlestickSeries',
  HistogramSeries: 'HistogramSeries',
  LineSeries: 'LineSeries',
  ColorType: { Solid: 'solid' },
  CrosshairMode: { Normal: 0 },
  LineStyle: { Solid: 0, Dashed: 2 },
  createSeriesMarkers: () => ({ setMarkers: vi.fn() }),
  createChart: () => ({
    addSeries: () => ({
      applyOptions: vi.fn(),
      setData: vi.fn(),
      createPriceLine: () => ({}),
      removePriceLine: vi.fn(),
      coordinateToPrice: () => 18.5,
    }),
    removeSeries: vi.fn(),
    applyOptions: vi.fn(),
    subscribeClick: vi.fn(),
    panes: () => [{ setStretchFactor: vi.fn() }, { setStretchFactor: vi.fn() }],
    timeScale: () => ({ fitContent: vi.fn() }),
    remove: vi.fn(),
  }),
}));

const state = createMockState();
const rel = currentRelease(state);

const META = {
  request_id: '00000000-0000-4000-8000-000000000099',
  generated_at: '2026-11-26T08:05:00Z',
  trace_id: null,
  contract_version: '3.0.0',
  release_id: rel.id,
  decision_as_of: '2026-11-26T08:00:00Z',
  availability_policy_version: 'observed-first-seen-v1',
  input_snapshot_refs: [],
  quality_status: 'valid',
  freshness_state: 'current',
  coverage: { policy_version: 'c', expected_count: 2, observed_count: 2, missing_count: 0, unverifiable_reasons: [] },
  warnings: [],
  next_cursor: null,
  actionability_id: null,
};

const ok = (data: unknown): Response => new Response(JSON.stringify({ meta: META, data }), { status: 200, headers: { 'Content-Type': 'application/json' } });
const fail404 = (): Response =>
  new Response(
    JSON.stringify({ meta: META, error: { code: 'NOT_FOUND', message: '未找到投影', retryable: false, field_errors: [] } }),
    { status: 404, headers: { 'Content-Type': 'application/json' } },
  );

function renderApp(el: ReactElement, initialEntry = '/') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ToastProvider>
        <MemoryRouter initialEntries={[initialEntry]}>{el}</MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('Nexus 3.5 Unified Hub, Sector Top Picks, Stock Pro Workbench & Swiss Arena', () => {
  it('renders NexusHubPage with ordered picks, RAPTOR adjustments, 5D macro matrix and object source_polling_status', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request) => {
        const u = new URL(typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url, 'http://localhost');
        if (u.pathname === '/v1/releases/current') {
          return ok({ slot_key: 'main', current_release_id: rel.id, pointer_generation: 1, issued_stage_generation: 1, published_stage_generation: 1, updated_at: '2026-11-26T08:00:00Z' });
        }
        if (u.pathname.startsWith('/v1/releases/')) {
          return ok({ release_id: rel.id, decision_as_of: '2026-11-26T08:00:00Z', published_at: '2026-11-26T08:01:00Z', components: [] });
        }
        if (u.pathname === '/v1/x/bf/nexus/hub') {
          const tr = (u.searchParams.get('track') ?? 'composite') as 'composite' | 'short' | 'swing' | 'long';
          return ok(buildMockNexusHub(rel, tr));
        }
        return fail404();
      }),
    );

    renderApp(<NexusHubPage />, `/nexus?release=${rel.id}`);
    expect(await screen.findByText(/精选个股推荐总榜/)).toBeInTheDocument();
    expect(screen.getByText('#1')).toBeInTheDocument();
    expect(screen.getByText(/新口径M1剪刀差/)).toBeInTheDocument();
    expect(screen.getByText(/北向日度停更后五大高频资金替代锚/)).toBeInTheDocument();
    expect(screen.getByText(/新华社与巨潮交叉核验/)).toBeInTheDocument();
    expect(screen.getByText(/RAPTOR 实时消息面调权/)).toBeInTheDocument();
    expect(screen.getAllByText(/gpt-6-luna/).length).toBeGreaterThan(0);
    expect(screen.getByText(/注册总源 4/)).toBeInTheDocument();
    const pollingSec = screen.getByRole('region', { name: '多信息源防静默轮询健康监控' });
    expect(within(pollingSec).getByText('eastmoney_guba_comments')).toBeInTheDocument();

    // Switch to short-term track
    await userEvent.click(screen.getByRole('tab', { name: /短线爆发/ }));
    expect((await screen.findAllByText(/SHORT/)).length).toBeGreaterThan(0);
  });

  it('renders friendly awaiting-release state on NexusHubPage when release_id is null (no infinite spinner)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request) => {
        const u = new URL(typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url, 'http://localhost');
        if (u.pathname === '/v1/releases/current') {
          return ok({ slot_key: 'main', current_release_id: null, pointer_generation: 0, issued_stage_generation: 0, published_stage_generation: 0, updated_at: '2026-11-26T08:00:00Z' });
        }
        if (u.pathname === '/v1/x/bf/nexus/hub') {
          return ok(buildMockNexusHub(null, 'composite'));
        }
        return fail404();
      }),
    );

    renderApp(<NexusHubPage />, '/nexus');
    expect(await screen.findByText(/等待首次分析发布快照/)).toBeInTheDocument();
    expect(screen.getByText(/尚未生成个股推荐榜/)).toBeInTheDocument();
    expect(screen.getByText(/新口径M1剪刀差/)).toBeInTheDocument();
  });

  it('renders SectorTopPicksPanel with leader_1 & core_capacity badges, 7 surge components (missing kept as 缺失), and watch pool', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ok(buildMockSectorDeepDive(rel, '半导体'))));
    renderApp(<SectorTopPicksPanel nodeId="半导体" taxonomyId="industry" horizonId="short_term" horizonKey="short" releaseId={rel.id} />);

    const sec = await screen.findByRole('region', { name: '板块内最推荐买入榜单' });
    expect(within(sec).getByText('板块龙一')).toBeInTheDocument();
    expect(within(sec).getByText('容量中军')).toBeInTheDocument();
    expect(within(sec).getByText(/火速晋升 · 提前锁定备选池/)).toBeInTheDocument();
    expect(within(sec).getByText(/板块升温 7 大组成项/)).toBeInTheDocument();
    expect(within(sec).getAllByText('缺失').length).toBe(2);
  });

  it('renders TradingView 26-indicator ratings, RAPTOR card, VPVR, Intraday-T, Adaptive Grid, 4-session Guba & LHB personas while keeping null as 缺失', async () => {
    const pro = buildMockStockPro(rel, '11111111-1111-4111-8111-111111111111');
    renderApp(
      <div>
        <NexusActionSummaryCard pro={pro} />
        <NexusTvProSection pro={pro} />
        <NexusExecutionSection instrumentId={pro.instrument_id} releaseId={rel.id} initialPro={pro} />
        <NexusMicrostructureSection pro={pro} />
      </div>,
    );

    expect(screen.getByText(/RAPTOR 实时消息调权/)).toBeInTheDocument();
    expect(screen.getAllByText(/gpt-6-luna/).length).toBeGreaterThan(0);
    expect(screen.getByText(/日\/周\/月\/年四周期共振/)).toBeInTheDocument();
    expect(screen.getByText(/VPVR 筹码分布与 70% 价值区/)).toBeInTheDocument();
    expect(screen.getByText(/震荡箱体·A-S库存偏斜双边做T/)).toBeInTheDocument();
    expect(screen.getByText(/ICAIF护栏 全部通过/)).toBeInTheDocument();
    expect(screen.getByText(/陈小群常用席位/)).toBeInTheDocument();
    expect(screen.getByText(/硬数据证实/)).toBeInTheDocument();
    // 1y timeframe has only 4 bars -> score is null -> shown as 预热/缺失, never 0
    expect(screen.getAllByText(/预热\/缺失/).length).toBeGreaterThan(0);
    // Expanding 26-indicator table shows both moving_averages.items and oscillators.items
    expect(screen.getByText('EMA(20)')).toBeInTheDocument();
    expect(screen.getByText('SMA(200)')).toBeInTheDocument();
    expect(screen.getByText('MACD(12,26,9)')).toBeInTheDocument();
  });

  it('seamlessly falls back to NexusFallbackStockWorkbench with PriceChart K-line workbench when /v1/stocks/:id/workspace returns 404', async () => {
    const nonTop50Id = '99999999-9999-4999-8999-999999999999';
    const seenUrls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request) => {
        const u = new URL(typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url, 'http://localhost');
        seenUrls.push(u.pathname + u.search);
        if (u.pathname === `/v1/stocks/${nonTop50Id}/workspace`) return fail404();
        if (u.pathname === `/v1/x/bf/nexus/stocks/${nonTop50Id}/pro`) return ok(buildMockStockPro(rel, nonTop50Id));
        if (u.pathname === '/v1/releases/current') return ok({ slot_key: 'main', current_release_id: rel.id, pointer_generation: 1, issued_stage_generation: 1, published_stage_generation: 1, updated_at: '2026-11-26T08:00:00Z' });
        if (u.pathname.startsWith('/v1/releases/')) return ok({ release_id: rel.id, decision_as_of: '2026-11-26T08:00:00Z', published_at: '2026-11-26T08:01:00Z', components: [] });
        if (u.pathname === '/v1/watchlists') return ok({ items: [], next_cursor: null });
        if (u.pathname === '/v1/annotations') return ok({ items: [], next_cursor: null });
        if (u.pathname.startsWith('/v1/workspace-layouts/')) return fail404();
        return fail404();
      }),
    );

    renderApp(
      <Routes>
        <Route path="/stock/:instrumentId" element={<StockPage />} />
      </Routes>,
      `/stock/${nonTop50Id}?release=${rel.id}`,
    );

    expect(await screen.findByText('全市场覆盖样本股')).toBeInTheDocument();
    expect(screen.getByText(/全市场覆盖 · PostgreSQL 工作台持久化/)).toBeInTheDocument();
    expect(screen.getByText(/K线图与买卖锚点工作台（1d · 20 根）/)).toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'K线图' })).toBeInTheDocument();
    expect(screen.getByText(/Nexus 多维综合买卖点与持有计划/)).toBeInTheDocument();
    expect(seenUrls.some((s) => s.includes('adjustment=raw'))).toBe(true);
  });

  it('renders SwissArenaView and PapersAndBenchmarkView with real metrics, AlphaPROBE candidates, and keeps null PFS as 缺失', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request) => {
        const u = new URL(typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url, 'http://localhost');
        if (u.pathname === '/v1/x/bf/nexus/arena') return ok(buildMockArena());
        if (u.pathname === '/v1/x/bf/nexus/integrity') return ok(buildMockIntegrity());
        return fail404();
      }),
    );

    renderApp(
      <div>
        <SwissArenaView />
        <PapersAndBenchmarkView />
      </div>,
    );

    expect(await screen.findByText(/Glicko-2 保守下界/)).toBeInTheDocument();
    expect(screen.getAllByText('1628.20').length).toBeGreaterThan(0);
    expect(screen.getAllByText(/18\.4200%/).length).toBeGreaterThan(0);
    // paperStrat has pfs_robustness: null -> rendered as 0.910 / 缺失, never 0.000
    expect(screen.getByText('0.910 / 缺失')).toBeInTheDocument();
    expect(screen.getByText(/AlphaPROBE 自动挖掘与正交门控候选池/)).toBeInTheDocument();
    expect(screen.getByText(/缩量乖离正交残差因子 v2/)).toBeInTheDocument();
    expect(await screen.findByText(/AlphaSAGE: Structure-Aware Alpha Mining/)).toBeInTheDocument();
    expect((await screen.findAllByText(/GIPS-AShare-2026-v1/)).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/2026-07-06 主板 ST 调为 ±10%；双创 20%；北交所 30%/).length).toBeGreaterThan(0);
    expect(screen.getByText(/Outbox 事件发布通道自检/)).toBeInTheDocument();
  });
});
