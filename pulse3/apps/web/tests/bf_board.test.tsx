/**
 * Board lane (legacy backfill) web checks: plan summary rows (#10/#14), click-throughs into the screen (#3/#8/#31), theme
 * treemap (#30), watchlist cards (#38/#39), command palette and shortcuts (#50/#51). Data is contract-shaped; missing and
 * failed states are asserted explicitly.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactElement } from 'react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ops } from '../src/api/ops';
import type { RankingEntry, TradePlan } from '../src/api/types';
import { CommandPalette } from '../src/components/CommandPalette';
import { RankingRows } from '../src/components/Lists';
import { Shell } from '../src/components/Shell';
import { ToastProvider } from '../src/components/Toast';
import { UpdatesProvider } from '../src/hooks/useUpdates';
import { GrowthView, VitalsTiles } from '../src/pages/ext/a/MarketPage';
import type { GrowthStrip, Vitals } from '../src/pages/ext/a/types';
import { ScreenPage } from '../src/pages/ext/b/ScreenPage';
import { ThemeMapPage, buildTiles } from '../src/pages/bf/board/ThemeMapPage';
import { WatchCards, SuggestedCards, sparkPath } from '../src/pages/bf/board/WatchCards';
import { matchPages, shortcutFor, stockTarget } from '../src/pages/bf/board/palette';
import { conditionText, summarizePlan } from '../src/pages/bf/board/planText';
import { growthQuery, linkText, parseScreenLink, screenHref, VITAL_QUERY } from '../src/pages/bf/board/screenLink';
import { changeColor, squarify } from '../src/pages/bf/board/treemap';

vi.mock('../src/api/ops', () => ({
  ops: { getStockWorkspace: vi.fn(), listWatchlists: vi.fn(), saveWatchlist: vi.fn(), getTradePlan: vi.fn(), getMarketRanking: vi.fn(), getCurrentRelease: vi.fn(), getRelease: vi.fn(), getSectorOverview: vi.fn(), getDataStatus: vi.fn(), pollUpdates: vi.fn() },
}));
const o = vi.mocked(ops, true);

const META = {
  request_id: 'r', generated_at: '2026-11-12T01:00:00Z', trace_id: null, contract_version: '3.0.0', release_id: null, decision_as_of: '2026-11-12T00:00:00Z',
  availability_policy_version: 'p', input_snapshot_refs: [], quality_status: 'valid', freshness_state: 'current',
  coverage: { policy_version: 'bf-board-v1', expected_count: 3, observed_count: 2, missing_count: 1, unverifiable_reasons: [] }, warnings: [], next_cursor: null, actionability_id: null,
};
const PAGE = { next_cursor: null, snapshot_hash: 'h'.repeat(64) };
const env = <T,>(data: T) => ({ meta: META, data });
const ok = (data: unknown): Response => new Response(JSON.stringify(env(data)), { status: 200, headers: { 'content-type': 'application/json' } });
const fail = (status: number, code: string): Response => new Response(JSON.stringify({ meta: META, error: { code, message: code, retryable: false, field_errors: [] } }), { status, headers: { 'content-type': 'application/json' } });
const IID = (n: number) => `00000000-0000-0000-0000-0000000000${String(n).padStart(2, '0')}`;
const RID = '11111111-1111-1111-1111-111111111111';

type Handler = (url: URL, init?: RequestInit) => Response | undefined;
function route(handlers: Record<string, Handler | Response>) {
  const calls: Array<{ path: string; url: URL; init?: RequestInit }> = [];
  vi.stubGlobal('fetch', vi.fn(async (input: string | Request, init?: RequestInit) => {
    const url = new URL(typeof input === 'string' ? input : input.url, 'http://localhost');
    const path = url.pathname.replace('/v1/x/', '');
    calls.push({ path, url, init });
    const h = handlers[path];
    const res = typeof h === 'function' ? h(url, init) : h;
    return res ? res.clone() : fail(404, 'NOT_FOUND');
  }));
  return calls;
}
function Where() { const l = useLocation(); return <div data-testid="where">{l.pathname}{l.search}</div>; }
function renderAt(el: ReactElement, entry = '/') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><ToastProvider><MemoryRouter initialEntries={[entry]}><Routes><Route path="*" element={<>{el}<Where /></>} /></Routes></MemoryRouter></ToastProvider></QueryClientProvider>);
}

beforeEach(() => {
  o.getStockWorkspace.mockImplementation(async (id: string) => env({ instrument: { display_name: `名称${id.slice(-2)}`, source_symbols: [{ source_symbol: `6000${id.slice(-2)}` }], is_risk_warning: false, trading_status: 'active' } }) as never);
  o.listWatchlists.mockResolvedValue(env({ items: [] }) as never);
});
afterEach(() => { vi.unstubAllGlobals(); vi.clearAllMocks(); });

// ------------------------------------------------------------------------------------------------- plan summary (#10, #14)
const clock = { kind: 'bar_close', timeframe: '1d', session_policy_version: 's' } as const;
const feat = (field: string) => ({ kind: 'feature', feature_id: 'price.bar', definition_version: '1', instrument_id: IID(1), timeframe: '1d', field_path: field, unit: 'CNY/share', resolution_policy_version: 'r', initial_feature_key_hash: null }) as const;
const konst = (v: string) => ({ kind: 'constant', value: v, unit: 'CNY/share' }) as const;
const cmp = (op: 'lt' | 'lte' | 'gt' | 'gte' | 'eq', field: string, v: string) => ({ node: 'compare', clock, operator: op, left: feat(field), right: konst(v) }) as const;
const plan = (over: Partial<TradePlan> = {}): TradePlan => ({
  plan_id: IID(50), plan_version: 3, instrument_id: IID(1), based_on_release_id: RID, account_id: null, expected_account_version: null, state: 'watching', strategy_refs: [],
  model_run_id: null, evidence_revision_ids: [], position_side: 'long', entry_conditions: cmp('lte', 'close_price_raw', '10'),
  entry_price_zone: { lower_price_raw: '9.5', upper_price_raw: '10', currency: 'CNY', adjustment_mode: 'raw' }, sizing: { kind: 'fixed_shares', quantity_shares: 100 },
  exit_conditions: cmp('gte', 'close_price_raw', '11.2'), invalidation: cmp('lt', 'close_price_raw', '9.2'), holding_horizon: { horizon_id: 'short_term', unit: 'trading_days', value: 5 },
  trigger_policy: { trigger_id: 't', fire_mode: 'on_transition_to_true', unknown_policy: 'do_not_fire', rearm: { mode: 'never', cooldown_ms: 0 }, max_fires: 1, min_interval_ms: 0, require_final_bar: true, pending_order_policy: 'block_retrigger' },
  valid_from: '2026-11-12T00:00:00Z', expires_at: '2026-11-19T00:00:00Z', replace_policy: 'keep_existing', validation: { status: 'passed', policy_version: 'v', evaluation_id: null, result_ref: null, validated_at: null },
  available_at: '2026-11-12T00:00:00Z', supersedes: null, ...over,
}) as TradePlan;

describe('plan summary', () => {
  it('reads entry zone, stop, target, horizon and exit words from the plan; a price is shown only when the tree says it plainly', () => {
    const s = summarizePlan(plan());
    expect(s).toMatchObject({ entryZone: { lower: '9.5', upper: '10' }, stopPrice: '9.2', targetPrice: '11.2', horizon: '5 个交易日' });
    expect(s.exitText).toContain('收盘价(原始价)不低于11.20 元/股');
    const flipped = summarizePlan(plan({ invalidation: { node: 'compare', clock, operator: 'gt', left: konst('9'), right: feat('close_price_raw') } as never }));
    expect(flipped.stopPrice).toBe('9');                                           // 9 > close  ==  close < 9
    const noTarget = summarizePlan(plan({ exit_conditions: { node: 'all', clock, children: [cmp('gte', 'close_price_raw', '12'), cmp('gt', 'volume_shares', '1')] } as never, entry_price_zone: null }));
    expect(noTarget.targetPrice).toBeNull();                                      // a conjunction is not a target
    expect(noTarget.entryZone).toBeNull();
    expect(noTarget.exitText).toContain(' 且 ');
    const two = summarizePlan(plan({ invalidation: { node: 'any', clock, children: [cmp('lt', 'close_price_raw', '9'), cmp('lt', 'close_price_raw', '8')] } as never }));
    expect(two.stopPrice).toBeNull();                                             // two alternative stops: no single price, words only
    expect(conditionText({ node: 'within_session', clock: { kind: 'quote' }, session_kinds: ['continuous'] })).toBe('处于连续竞价时段');
  });

  const entry = (over: Partial<RankingEntry>): RankingEntry => ({ instrument_id: IID(1), alpha_rank: 1, action_rank: null, relative_rank_score: '0.5', calibrated_probability: null, action_status: 'watching', explanation_ref: 'x', plan_ref: { plan_id: IID(50), plan_version: 3 }, ...over }) as RankingEntry;

  it('each ranking row with a plan_ref shows its plan summary; a row without one shows none', async () => {
    o.getTradePlan.mockResolvedValue(env(plan()) as never);
    renderAt(<RankingRows entries={[entry({}), entry({ instrument_id: IID(2), alpha_rank: 2, plan_ref: null })]} releaseId={RID} />);
    const box = await screen.findByLabelText('交易计划摘要');
    expect(within(box).getByText('9.50 ~ 10.00 元')).toBeInTheDocument();
    expect(within(box).getByText('9.20 元以下')).toBeInTheDocument();
    expect(within(box).getByText('11.20 元')).toBeInTheDocument();
    expect(within(box).getByText('5 个交易日')).toBeInTheDocument();
    expect(screen.getAllByLabelText('交易计划摘要')).toHaveLength(1);
    expect(screen.getByText('无交易计划')).toBeInTheDocument();
    expect(o.getTradePlan).toHaveBeenCalledTimes(1);
  });

  it('a plan of another release is hidden and a failed read says so instead of showing nothing', async () => {
    o.getTradePlan.mockResolvedValueOnce(env(plan({ based_on_release_id: '22222222-2222-2222-2222-222222222222' })) as never);
    const { unmount } = renderAt(<RankingRows entries={[entry({})]} releaseId={RID} />);
    expect(await screen.findByText(/基于另一个发布版本/)).toBeInTheDocument();
    unmount();
    o.getTradePlan.mockRejectedValueOnce(new Error('x'));
    renderAt(<RankingRows entries={[entry({ plan_ref: { plan_id: IID(51), plan_version: 1 } })]} releaseId={RID} />);
    expect(await screen.findByText(/计划暂时读取失败/)).toBeInTheDocument();
  });
});

// ------------------------------------------------------------------------------------------------- click-throughs (#3, #8, #31)
const vitals = (over: Partial<Vitals> = {}): Vitals => ({
  session: '2026-11-12', coverage: { expected_instruments: 3000, observed: 2990, pct_valid: 2900, pct_unverifiable: 10, limit_verified: 2900, limit_unverifiable: 0, missing: 10 },
  limit_up: 60, broken: 20, limit_down: 3, seal_rate_pct: '75.0', max_height: 5, max_height_stocks: [{ instrument_id: IID(1), code: '600001', name: '龙头', }],
  premium: { close_pct: '1.2', open_pct: '0.8', sample_count: 40, definition: 'd' }, up: 2000, down: 900, flat: 90, mean_pct: '0.3',
  volume: { shares: 1, instruments: 1, ratio_vs_20d_mean: null, amount_estimate_cny: '9000000000', amount_is_estimate: true, amount_method: 'm' }, ...over,
});

describe('click-throughs into the screen', () => {
  it('builds screen links from supported questions only', () => {
    expect(screenHref({ q: '涨停' })).toBe('/screen?q=%E6%B6%A8%E5%81%9C');
    expect(screenHref({ sector: 'N1', tax: 'industry', release: RID })).toBe(`/screen?sector=N1&tax=industry&release=${RID}`);
    expect(screenHref({})).toBe('/screen');
    expect(parseScreenLink(new URLSearchParams('q=%E9%A6%96%E6%9D%BF&preset=x&sector=%20&release=r'))).toEqual({ q: '首板', preset: 'x', sector: null, tax: null, release: 'r' });
    expect(linkText({ q: null, preset: 'ladder_leader', sector: null, tax: null, release: null }, [{ id: 'ladder_leader', text: '3连板以上，剔除ST' }])).toBe('3连板以上，剔除ST');
    expect(linkText({ q: null, preset: 'nope', sector: null, tax: null, release: null }, [])).toBeNull();
    expect([VITAL_QUERY.broken, VITAL_QUERY.limitDown, VITAL_QUERY.maxHeight(null), VITAL_QUERY.maxHeight(0)]).toEqual([null, null, null, null]);
    expect(VITAL_QUERY.maxHeight(5)).toBe('5连板以上');
    expect(growthQuery(4)).toBe('4连阳以上');
  });

  it('vitals tiles open /screen with a matching question; tiles the language cannot express carry no link', () => {
    renderAt(<VitalsTiles v={vitals()} />);
    const hrefs = screen.getAllByRole('link').map((a) => decodeURIComponent(a.getAttribute('href') ?? ''));
    expect(hrefs).toEqual(['/screen?q=涨停', '/screen?q=5连板以上', '/screen?q=上涨', '/screen?q=下跌']);
    expect(screen.getByText(/筛选暂不支持炸板/)).toBeInTheDocument();
  });

  it('a missing max height gives no link', () => {
    renderAt(<VitalsTiles v={vitals({ max_height: null, max_height_stocks: [] })} />);
    expect(screen.getAllByRole('link').map((a) => decodeURIComponent(a.getAttribute('href') ?? ''))).toEqual(['/screen?q=涨停', '/screen?q=上涨', '/screen?q=下跌']);
  });

  it('every growth-streak cell links to "at least N" in the screen', () => {
    const g: GrowthStrip = { session: 's', ge3_total: 7, definition: 'd', buckets: [1, 2, 3, 4].map((k) => ({ streak: k, label: `${k}连阳`, count: k, top: [] })) };
    renderAt(<GrowthView g={g} />);
    expect(screen.getAllByRole('link').map((a) => decodeURIComponent(a.getAttribute('href') ?? ''))).toEqual(['/screen?q=1连阳以上', '/screen?q=2连阳以上', '/screen?q=3连阳以上', '/screen?q=4连阳以上']);
  });

  const CTX = {
    input_snapshot_refs: [{ snapshot_id: IID(9), dataset_id: 'bars', manifest_version: 1, content_hash: 'c'.repeat(64) }], decision_as_of: '2026-11-12T00:00:00Z',
    universe_policy_versions: ['screen-universe-main-v1'], field_catalog_version: 'screen-fields-v1', playbook_instances: [], replay: { evaluation_context: null, cost_policy_versions: [], instances: [] },
  };
  const AST = { condition: { node: 'compare', clock: {}, operator: 'gte', left: { kind: 'feature', feature_id: 'x', definition_version: 'v', instrument_id: IID(0), timeframe: '1d', field_path: 'value', unit: 'count', resolution_policy_version: 'r', initial_feature_key_hash: null }, right: { kind: 'constant', value: '1', unit: 'count' } }, allowed_field_catalog_version: 'screen-fields-v1', sort: [] };
  const row = (n: number) => ({ instrument_id: IID(n), condition_value: 'true', sort_position: n, features: [] });
  const screenRoutes = (extra: Record<string, Handler | Response> = {}) => route({
    'b/context': ok(CTX), 'list-saved-screens': ok({ screens: [], page: PAGE }),
    'parse-screen-text': (u) => ok({ ast: AST, unresolved: u.searchParams.get('text') === '题材火热' ? [{ source_start: 0, source_end: 4, text: '题材', reason_code: 'unsupported' }] : [], complete: true }),
    'run-screen': ok({ receipt: { job_run_id: IID(5), resource_id: IID(7), run_state: 'queued', poll_operation_id: 'queryScreenRun' } }),
    'query-screen-run': ok({ rows: [row(1), row(2), row(3)], page: PAGE, capability_gaps: [] }),
    'b/instrument-labels': ok([1, 2, 3].map((n) => ({ instrument_id: IID(n), code: `6000${n}`, name: `名${n}`, is_risk_warning: false }))),
    ...extra,
  });

  it('arriving with ?q= parses that question once and runs it without a click', async () => {
    const calls = screenRoutes();
    renderAt(<ScreenPage />, '/screen?q=%E6%B6%A8%E5%81%9C');
    await waitFor(() => expect(calls.filter((c) => c.path === 'run-screen')).toHaveLength(1));
    expect(calls.filter((c) => c.path === 'parse-screen-text')).toHaveLength(1);
    expect(calls.find((c) => c.path === 'parse-screen-text')?.url.searchParams.get('text')).toBe('涨停');
    expect(screen.getByLabelText('问句')).toHaveValue('涨停');
    expect(await screen.findByRole('table')).toBeInTheDocument();
  });

  it('arriving with ?preset= uses the preset text; an unknown preset does nothing', async () => {
    const calls = screenRoutes();
    const { unmount } = renderAt(<ScreenPage />, '/screen?preset=ladder_leader');
    await waitFor(() => expect(calls.some((c) => c.path === 'parse-screen-text')).toBe(true));
    expect(calls.find((c) => c.path === 'parse-screen-text')?.url.searchParams.get('text')).toBe('3连板以上，剔除ST');
    unmount();
    const none = screenRoutes();
    renderAt(<ScreenPage />, '/screen?preset=nope');
    await screen.findByText('设定条件后点击「运行筛选」。结果是一次冻结的扫描：同一次运行的分页顺序固定。', { exact: false });
    expect(none.some((c) => c.path === 'parse-screen-text' || c.path === 'run-screen')).toBe(false);
  });

  it('a question with unrecognised parts is parsed but not run until the user acknowledges them', async () => {
    const calls = screenRoutes();
    renderAt(<ScreenPage />, `/screen?q=${encodeURIComponent('题材火热')}`);
    expect(await screen.findByRole('alert', { name: '未识别片段' })).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 50));
    expect(calls.some((c) => c.path === 'run-screen')).toBe(false);
  });

  it('?sector= keeps only the sector members in the shown result and says how many were found', async () => {
    const calls = screenRoutes({ 'query-sector-details': ok({ sector: { taxonomy_id: 'industry', taxonomy_version: 1, node_id: 'N1' }, members: [{ instrument_id: IID(1), role_feature: null }, { instrument_id: IID(3), role_feature: null }, { instrument_id: IID(8), role_feature: null }], ranking: {}, candidate_pools: [], related_event_artifacts: [] }) });
    renderAt(<ScreenPage />, `/screen?q=${encodeURIComponent('沪深主板')}&sector=N1&tax=industry&release=${RID}`);
    expect(await screen.findByText('60001')).toBeInTheDocument();
    expect(screen.getByText('60003')).toBeInTheDocument();
    expect(screen.queryByText('60002')).not.toBeInTheDocument();
    expect(screen.getByText(/显示 2 \/ 3 只/)).toBeInTheDocument();
    const sector = calls.find((c) => c.path === 'query-sector-details');
    expect(JSON.parse(sector?.url.searchParams.get('sector') ?? '{}').node_id).toBe('N1');
    await userEvent.click(screen.getByRole('button', { name: '取消板块过滤' }));
    expect(await screen.findByText('60002')).toBeInTheDocument();
  });

  it('a failed sector read says the result is unfiltered instead of hiding rows', async () => {
    screenRoutes({ 'query-sector-details': fail(500, 'INTERNAL') });
    renderAt(<ScreenPage />, `/screen?q=${encodeURIComponent('沪深主板')}&sector=N1&release=${RID}`);
    expect(await screen.findByText(/板块成分读取失败，结果未过滤/)).toBeInTheDocument();
  });
});

// ------------------------------------------------------------------------------------------------- theme map (#30)
describe('theme treemap', () => {
  it('squarify places every positive item inside the box, proportional to value, without overlap, deterministically', () => {
    const items = [{ key: 'a', value: 50 }, { key: 'b', value: 30 }, { key: 'c', value: 15 }, { key: 'd', value: 5 }, { key: 'zero', value: 0 }, { key: 'nan', value: Number.NaN }];
    const rects = squarify(items);
    expect(rects.map((r) => r.key).sort()).toEqual(['a', 'b', 'c', 'd']);
    expect(rects.reduce((s, r) => s + r.w * r.h, 0)).toBeCloseTo(100 * 100, 4);
    for (const r of rects) {
      expect(r.x).toBeGreaterThanOrEqual(-1e-9); expect(r.y).toBeGreaterThanOrEqual(-1e-9);
      expect(r.x + r.w).toBeLessThanOrEqual(100 + 1e-9); expect(r.y + r.h).toBeLessThanOrEqual(100 + 1e-9);
      expect(r.w * r.h / 10000).toBeCloseTo((items.find((i) => i.key === r.key)?.value ?? 0) / 100, 4);
    }
    for (const a of rects) for (const b of rects) if (a !== b) expect(a.x < b.x + b.w - 1e-9 && b.x < a.x + a.w - 1e-9 && a.y < b.y + b.h - 1e-9 && b.y < a.y + a.h - 1e-9).toBe(false);
    expect(squarify([...items].reverse())).toEqual(rects);
    expect(squarify([])).toEqual([]);
  });

  it('colour: red up, green down, grey when the change is missing (never "flat")', () => {
    const rgb = (s: string) => s.match(/\d+/g)!.map(Number) as [number, number, number];
    const up = rgb(changeColor(4)); const down = rgb(changeColor(-4)); const none = rgb(changeColor(null));
    expect(up[0]).toBeGreaterThan(up[1]); expect(down[1]).toBeGreaterThan(down[0]);
    expect(none[0]).toBeLessThan(up[0]); expect(changeColor(null)).not.toBe(changeColor(0));
    expect(changeColor(10)).toBe(changeColor(5));
  });

  const node = (id: string, amount: string | null, pct: string | null) => ({ node_id: id, name: `板块${id}`, members: 10, observed: 8, pct_samples: pct ? 8 : 0, amount_estimate_cny: amount, avg_pct: pct });
  it('only overview sectors with an amount are drawn; the rest are listed, not drawn as zero', () => {
    const { tiles, undrawn } = buildTiles(['A', 'B', 'C', 'Z'], [node('A', '300', '1'), node('B', null, null), node('C', '100', '-2'), node('X', '999', '0')]);
    expect(tiles.map((t) => t.node.node_id).sort()).toEqual(['A', 'C']);
    expect(undrawn.map((u) => u.id)).toEqual(['B', 'Z']);
    expect(undrawn.find((u) => u.id === 'Z')?.node).toBeNull();
  });

  const setupRelease = () => {
    o.getCurrentRelease.mockResolvedValue(env({ current_release_id: RID }) as never);
    o.getRelease.mockResolvedValue(env({ release_id: RID, published_at: '2026-11-12T00:00:00Z', decision_as_of: '2026-11-12T00:00:00Z', release_hash: null, universe_policy_version: 'u', market_rules_version: 'm', required_components: [], components: [] }) as never);
    o.getSectorOverview.mockResolvedValue(env({ ranking_id: IID(60), release_id: RID, ranking_scope: 'sector_overview', taxonomy_id: 'industry', taxonomy_version: 1, horizon_id: 'short_term', forecast_target: 'excess_return_5d', ranking_policy_version: 'p', entries: ['A', 'B', 'C'].map((id, i) => ({ taxonomy_node: { taxonomy_id: 'industry', taxonomy_version: 1, node_id: id }, rank_kind: 'sector_opportunity_rank', rank: i + 1, score: '1', member_count: 10, observed_member_count: 8, membership_snapshot_id: IID(61), explanation_ref: 'x', quality_status: 'valid' })) }) as never);
  };
  it('renders tiles with name, change and a link to the sector page of the same release; the undrawn sector is listed', async () => {
    setupRelease();
    route({ 'bf/board/theme-map': ok({ release_id: RID, taxonomy_id: 'industry', taxonomy_version: 1, membership_snapshot_id: IID(61), session: '2026-11-12', amount_is_estimate: true, method: 'm', nodes: [node('A', '30000000000', '2.5'), node('B', null, null), node('C', '10000000000', '-1.5')] }) });
    renderAt(<ThemeMapPage />, '/themes');
    const map = await screen.findByRole('list', { name: '题材图谱' });
    const tiles = within(map).getAllByRole('listitem');
    expect(tiles).toHaveLength(2);
    expect(tiles[0]).toHaveTextContent('板块A');
    expect(tiles[0]).toHaveTextContent('+2.50%');
    const href = tiles[0]!.getAttribute('href') ?? '';
    expect(href.startsWith('/sector/A?')).toBe(true);
    expect(href).toContain(`release=${RID}`);
    expect(screen.getByText(/未画出的板块（1）/)).toBeInTheDocument();
    expect(screen.getByText('成交额为估算')).toBeInTheDocument();
  });

  it('a failed theme-map read is an error state, not an empty map', async () => {
    setupRelease();
    route({ 'bf/board/theme-map': fail(404, 'NOT_FOUND') });
    renderAt(<ThemeMapPage />, '/themes');
    expect(await screen.findByText(/题材图谱读取失败/)).toBeInTheDocument();
    expect(screen.queryByRole('list', { name: '题材图谱' })).not.toBeInTheDocument();
  });
});

// ------------------------------------------------------------------------------------------------- watch cards (#38, #39)
const card = (n: number, over: Record<string, unknown> = {}) => ({
  instrument_id: IID(n), session: '2026-11-12', last_close: '10.50', pct_change: '2.5000', pct_missing_reason: null, amount_estimate_cny: '250000000.00',
  spark: [{ d: '2026-11-10', c: '10.00' }, { d: '2026-11-11', c: '10.24' }, { d: '2026-11-12', c: '10.50' }], rank: { alpha_rank: 3, total: 120, horizon_id: 'short_term', forecast_target: 'excess_return_5d' }, ...over,
});

describe('watchlist cards', () => {
  it('sparkPath draws two or more points and nothing for fewer (no fake flat line)', () => {
    expect(sparkPath(['1', '2', '3'])).toMatch(/^M2\.0,\d+\.\d L60\.0,\d+\.\d L118\.0,\d+\.\d$/);
    expect(sparkPath(['1'])).toBeNull();
    expect(sparkPath([])).toBeNull();
  });

  it('shows price, change, sparkline, amount (labelled estimate) and rank, pinned to the release', async () => {
    const calls = route({ 'bf/board/watch-cards': ok({ release_id: RID, amount_is_estimate: true, method: 'm', cards: [card(1), card(2, { pct_change: '-1.2000', rank: null })] }) });
    renderAt(<WatchCards releaseId={RID} ids={[IID(1), IID(2)]} />);
    const cards = await screen.findAllByRole('listitem');
    expect(cards).toHaveLength(2);
    expect(within(cards[0]!).getByText('10.50')).toBeInTheDocument();
    expect(within(cards[0]!).getByText('+2.50%')).toBeInTheDocument();
    expect(within(cards[0]!).getByRole('img', { name: '近期收盘价走势' })).toBeInTheDocument();
    expect(within(cards[0]!).getByText(/成交额 2\.5 亿/)).toBeInTheDocument();
    expect(within(cards[0]!).getByText(/估算/)).toBeInTheDocument();
    expect(within(cards[0]!).getByText('3')).toBeInTheDocument();
    expect(within(cards[1]!).getByText('-1.20%')).toBeInTheDocument();
    expect(within(cards[1]!).getByText('不在本期榜内')).toBeInTheDocument();
    const q = calls.find((c) => c.path === 'bf/board/watch-cards')!.url.searchParams;
    expect(q.get('release_id')).toBe(RID);
    expect(q.get('ids')).toBe(`${IID(1)},${IID(2)}`);
  });

  it('missing values stay missing: no bar -> dashes and a reason, never 0', async () => {
    route({ 'bf/board/watch-cards': ok({ release_id: RID, amount_is_estimate: true, method: 'm', cards: [card(1, { session: null, last_close: null, pct_change: null, pct_missing_reason: 'no_bar', amount_estimate_cny: null, spark: [], rank: null })] }) });
    renderAt(<WatchCards releaseId={RID} ids={[IID(1)]} />);
    const c = (await screen.findAllByRole('listitem'))[0]!;
    expect(within(c).getByText('—')).toBeInTheDocument();
    expect(within(c).getByText('涨跌幅缺失')).toHaveAttribute('title', expect.stringContaining('没有这只股票的日线'));
    expect(within(c).getByText('走势数据不足')).toBeInTheDocument();
    expect(within(c).getByText(/成交额 缺失/)).toBeInTheDocument();
    expect(c).not.toHaveTextContent('0.00%');
  });

  it('a failed read is an error state', async () => {
    route({ 'bf/board/watch-cards': fail(500, 'INTERNAL') });
    renderAt(<WatchCards releaseId={RID} ids={[IID(1)]} />);
    expect(await screen.findByText(/自选卡片读取失败/)).toBeInTheDocument();
  });

  it('an empty list offers the top 8 of the release ranking (first page, server order)', async () => {
    const entries = Array.from({ length: 10 }, (_, i) => ({ instrument_id: IID(i + 1), alpha_rank: i + 1, action_rank: null, relative_rank_score: '1', calibrated_probability: null, action_status: 'watching', explanation_ref: 'x', plan_ref: null }));
    o.getMarketRanking.mockResolvedValue(env({ ranking_id: IID(70), release_id: RID, ranking_scope: 'market', scope_key: 'market', horizon_id: 'short_term', forecast_target: 'excess_return_5d', ranking_policy_version: 'p', taxonomy_node: null, membership_snapshot_id: null, entries }) as never);
    const calls = route({ 'bf/board/watch-cards': (u) => ok({ release_id: RID, amount_is_estimate: true, method: 'm', cards: (u.searchParams.get('ids') ?? '').split(',').map((id) => card(Number(id.slice(-2)))) }) });
    renderAt(<SuggestedCards releaseId={RID} />);
    expect(await screen.findAllByRole('listitem')).toHaveLength(8);
    expect(calls.find((c) => c.path === 'bf/board/watch-cards')!.url.searchParams.get('ids')).toBe(Array.from({ length: 8 }, (_, i) => IID(i + 1)).join(','));
    expect(screen.getByText(/自选为空，下面是本期排名前 8/)).toBeInTheDocument();
  });

  it('an empty ranking offers nothing and says so', async () => {
    o.getMarketRanking.mockResolvedValue(env({ entries: [] }) as never);
    renderAt(<SuggestedCards releaseId={RID} />);
    expect(await screen.findByText(/本期榜单为空/)).toBeInTheDocument();
  });
});

// ------------------------------------------------------------------------------------------------- palette and shortcuts (#50, #51)
const key = (k: string, over: Partial<Parameters<typeof shortcutFor>[0]> = {}) => ({ key: k, ctrlKey: false, metaKey: false, altKey: false, shiftKey: false, target: document.body, ...over });

describe('palette helpers', () => {
  const pages = [{ path: '/', label: '首页' }, { path: '/screen', label: '问句选股' }, { path: '/themes', label: '题材图谱' }];
  it('matches page labels and paths by every term; empty query lists all', () => {
    expect(matchPages(pages, '').map((p) => p.path)).toEqual(['/', '/screen', '/themes']);
    expect(matchPages(pages, '题材').map((p) => p.path)).toEqual(['/themes']);
    expect(matchPages(pages, 'SCREEN').map((p) => p.path)).toEqual(['/screen']);
    expect(matchPages(pages, '选股 题材')).toEqual([]);
  });
  it('Ctrl/Cmd+K toggles anywhere; "/" focuses the ask bar only on /screen and never while typing', () => {
    expect(shortcutFor(key('k', { ctrlKey: true }), '/')).toBe('toggle-palette');
    expect(shortcutFor(key('K', { metaKey: true }), '/stock/x')).toBe('toggle-palette');
    expect(shortcutFor(key('k'), '/')).toBeNull();
    expect(shortcutFor(key('k', { ctrlKey: true, shiftKey: true }), '/')).toBeNull();
    expect(shortcutFor(key('/'), '/screen')).toBe('focus-ask');
    expect(shortcutFor(key('/'), '/')).toBeNull();
    const input = document.createElement('input');
    expect(shortcutFor(key('/', { target: input }), '/screen')).toBeNull();
    expect(shortcutFor(key('/', { isComposing: true }), '/screen')).toBeNull();
  });
  it('a picked stock opens its page carrying the pinned release', () => {
    expect(stockTarget(IID(1), null)).toBe(`/stock/${IID(1)}`);
    expect(stockTarget(IID(1), RID)).toBe(`/stock/${IID(1)}?release=${RID}`);
  });
});

describe('command palette', () => {
  const pages = [{ path: '/', label: '首页' }, { path: '/screen', label: '问句选股' }, { path: '/themes', label: '题材图谱' }];
  const hit = (n: number, name: string) => ({ instrument_id: IID(n), code: `6000${n}`, name, is_risk_warning: false });
  const mount = (onClose = vi.fn(), release: string | null = RID) => { renderAt(<CommandPalette open onClose={onClose} pages={pages} release={release} />, '/'); return onClose; };

  it('lists pages with an empty query and jumps with Enter; Esc closes', async () => {
    const onClose = mount();
    const input = screen.getByRole('combobox', { name: '搜索股票或页面' });
    expect(input).toHaveFocus();
    expect(screen.getAllByRole('option')).toHaveLength(3);
    await userEvent.type(input, '题材');
    expect(screen.getAllByRole('option')).toHaveLength(1);
    await userEvent.keyboard('{Enter}');
    expect(onClose).toHaveBeenCalled();
    expect(screen.getByTestId('where')).toHaveTextContent('/themes');
    await userEvent.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it('searches stocks by code or name through the read, ranks them above pages and opens the stock with the release', async () => {
    const calls = route({ 'bf/board/instrument-search': (u) => ok({ query: u.searchParams.get('q'), items: [hit(1, '浦发银行'), hit(2, '浦发科技')] }) });
    mount();
    await userEvent.type(screen.getByRole('combobox'), '浦发');
    const options = await screen.findAllByRole('option', { name: /浦发/ });
    expect(options).toHaveLength(2);
    expect(calls.some((c) => c.path === 'bf/board/instrument-search' && c.url.searchParams.get('q') === '浦发')).toBe(true);
    expect(screen.getAllByRole('option')[0]).toHaveTextContent('浦发银行');
    await userEvent.keyboard('{ArrowDown}{Enter}');
    expect(screen.getByTestId('where')).toHaveTextContent(`/stock/${IID(2)}?release=${RID}`);
  });

  it('a failed search says so and pages stay usable; no match says no match', async () => {
    route({ 'bf/board/instrument-search': fail(500, 'INTERNAL') });
    mount();
    await userEvent.type(screen.getByRole('combobox'), '首页');
    expect(await screen.findByText(/股票搜索失败/)).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /首页/ })).toBeInTheDocument();
    vi.unstubAllGlobals();
    route({ 'bf/board/instrument-search': ok({ query: 'zzz', items: [] }) });
    await userEvent.clear(screen.getByRole('combobox'));
    await userEvent.type(screen.getByRole('combobox'), 'zzz');
    expect(await screen.findByText('没有匹配项。')).toBeInTheDocument();
  });

  it('renders nothing while closed', () => {
    renderAt(<CommandPalette open={false} onClose={() => undefined} pages={pages} release={null} />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});

describe('shell shortcuts', () => {
  function shell(entry: string) {
    o.getDataStatus.mockResolvedValue(env({ items: [] }) as never);
    o.pollUpdates.mockReturnValue(new Promise(() => undefined) as never);        // the update channel stays quiet in these tests
    route({ 'bf/board/instrument-search': ok({ query: '', items: [] }) });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={qc}><ToastProvider><UpdatesProvider intervalMs={1e9}><MemoryRouter initialEntries={[entry]}>
        <Routes><Route element={<Shell />}>
          <Route path="screen" element={<input aria-label="问句" />} />
          <Route path="*" element={<input aria-label="其他输入" />} />
        </Route></Routes>
      </MemoryRouter></UpdatesProvider></ToastProvider></QueryClientProvider>,
    );
  }

  it('Ctrl+K opens the palette, Esc closes it, and the nav has the theme map entry', async () => {
    shell('/');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: '题材图谱' })).toHaveAttribute('href', '/themes');
    await userEvent.keyboard('{Control>}k{/Control}');
    expect(await screen.findByRole('dialog', { name: '命令面板' })).toBeInTheDocument();
    await userEvent.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await userEvent.click(screen.getByRole('button', { name: /打开搜索/ }));
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
  });

  it('"/" focuses the ask bar on /screen, but not when already typing and not on other pages', async () => {
    const { unmount } = shell('/screen');
    expect(screen.getByLabelText('问句')).not.toHaveFocus();
    await userEvent.keyboard('/');
    expect(screen.getByLabelText('问句')).toHaveFocus();
    await userEvent.keyboard('x/');                                              // typing a slash inside the field stays text
    expect(screen.getByLabelText('问句')).toHaveValue('x/');
    unmount();
    shell('/somewhere');
    await userEvent.keyboard('/');
    expect(screen.getByLabelText('其他输入')).not.toHaveFocus();
  });
});
