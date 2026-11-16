import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactElement } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ops } from '../src/api/ops';
import { ToastProvider } from '../src/components/Toast';
import type { AttributionCell, AttributionDimension, ExperimentItem, PlaybookReplay, RegimeHeatmap, ReplayTrade } from '../src/pages/bf/research/api';
import { ExperimentArchive } from '../src/pages/bf/research/ExperimentArchive';
import { cellState, exitText, heat, pctText, sortByColumn } from '../src/pages/bf/research/format';
import { RegimeHeatmapView } from '../src/pages/bf/research/RegimeHeatmap';
import { ReplayPanel, ReplayView } from '../src/pages/bf/research/ReplayPanel';
import { CATALOG, FIELD_BY_ID, compare } from '../src/pages/ext/b/helpers';
import { LabPage } from '../src/pages/ext/b/LabPage';
import { ScreenPage } from '../src/pages/ext/b/ScreenPage';
import { StrategiesPage } from '../src/pages/StrategiesPage';

vi.mock('../src/api/ops', () => ({ ops: { listStrategies: vi.fn(), getEvaluation: vi.fn(), getCurrentRelease: vi.fn(), getLeague: vi.fn() } }));

const META = {
  request_id: 'r', generated_at: '2026-11-12T01:00:00Z', trace_id: null, contract_version: '3.0.0', release_id: null, decision_as_of: null,
  availability_policy_version: null, input_snapshot_refs: [], quality_status: 'partial', freshness_state: 'not_expected',
  coverage: { policy_version: 'operational-v1', expected_count: null, observed_count: 0, missing_count: null, unverifiable_reasons: [] }, warnings: [], next_cursor: null, actionability_id: null,
};
const json = (status: number, body: unknown): Response => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
const ok = (data: unknown): Response => json(200, { meta: META, data });
const fail = (status: number, code: string): Response => json(status, { meta: META, error: { code, message: code, retryable: false, field_errors: [] } });
const IID = (n: number) => `00000000-0000-0000-0000-00000000000${n}`;

type Handler = (url: URL) => Response | undefined;
function route(handlers: Record<string, Handler | Response>) {
  const calls: URL[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: string | Request) => {
    const url = new URL(typeof input === 'string' ? input : input.url, 'http://localhost');
    calls.push(url);
    const h = handlers[url.pathname.replace('/v1/x/', '')];
    const res = typeof h === 'function' ? h(url) : h;
    return res ? res.clone() : fail(404, 'NOT_FOUND');
  }));
  return calls;
}
function renderPage(el: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><ToastProvider><MemoryRouter>{el}</MemoryRouter></ToastProvider></QueryClientProvider>);
}
afterEach(() => { vi.unstubAllGlobals(); });

// ---- fixtures ---------------------------------------------------------------------------------------------------------
const cell = (bucket: string, label: string, n: number, avg: string | null): AttributionCell => ({ bucket, label, trade_count: n, avg_return: avg, win_rate: avg === null ? null : '0.5', insufficient_sample: n < 10 });
const TREND: AttributionDimension = { dimension: 'trend', label: '趋势', cells: [cell('bull', '牛市', 12, '0.031'), cell('bear', '熊市', 3, '-0.02'), cell('range', '震荡', 0, null)], unclassified: { count: 2, reasons: { insufficient_history: 2 } } };
const STYLE: AttributionDimension = { dimension: 'style', label: '风格', cells: [cell('small', '小盘占优', 10, '0.01'), cell('large', '大盘占优', 0, null)], unclassified: { count: 0, reasons: {} } };
const trade = (n: number, over: Partial<ReplayTrade> = {}): ReplayTrade => ({
  instrument_id: IID(n), code: `60000${n}`, entry_day: `2026-03-0${n}`, exit_day: `2026-03-1${n}`, quantity: 100, entry_price: '10', exit_price: '11', gross_return: '0.1', holding_sessions: 5,
  exit_reason: 'holding_days', status: 'closed', regime_at_entry: null, ...over,
});
const points = [['2026-03-02', '1000000', '0'], ['2026-03-03', '1100000', '0'], ['2026-03-04', '990000', '-0.1'], ['2026-03-05', null, null], ['2026-03-06', '1050000', '-0.045454545455']] as const;
const REPLAY: PlaybookReplay = {
  experiment: { experiment_id: IID(1), instance_id: IID(2), catalog_id: 'PB:turtle_55', playbook_id: 'turtle_55', completed_at: '2026-11-12T01:00:00Z', cost_policy_version: 'user-account-2026', price_basis: 'legacy_forward_adjusted_research_only', artifact_ref: 'playbook-replay:x' },
  initial_cash: '1000000', window: { start: '2026-03-02', end: '2026-03-06', sessions: 5 },
  equity: { points: points.map(([d, nav, drawdown]) => ({ d, nav, drawdown })), max_drawdown: '-0.1', final_nav: '1050000', net_return: '0.05' },
  trades: { total: 3, closed: 2, open: 1, shown: 3, items: [trade(3, { status: 'open', exit_day: null, exit_price: null, gross_return: null, holding_sessions: null, exit_reason: null }), trade(2, { exit_reason: 'stop_loss', gross_return: '-0.052' }), trade(1)] },
  attribution: { regime: { available: true, reason: null, regime_version: 'regime-v1', release_id: IID(5), through: '2026-11-10', basis: 'last completed session before the entry day' }, version: 'regime-attribution-v1', min_trades: 10, closed_trades: 2, open_trades: 1, dimensions: [TREND, STYLE] },
  caveats: ['净值曲线是执行内核按用户费用口径回放的结果'],
};
const HEATMAP: RegimeHeatmap = {
  version: 'regime-attribution-v1', min_trades: 10, note: '每格 = 平均毛收益',
  columns: [{ dimension: 'trend', label: '趋势', buckets: [{ bucket: 'bull', label: '牛市' }, { bucket: 'bear', label: '熊市' }, { bucket: 'range', label: '震荡' }] }, { dimension: 'style', label: '风格', buckets: [{ bucket: 'small', label: '小盘占优' }, { bucket: 'large', label: '大盘占优' }] }],
  rows: [{ catalog_id: 'PB:turtle_55', playbook_id: 'turtle_55', experiment_id: IID(1), completed_at: null, regime_available: true, closed_trades: 15, dimensions: [TREND, STYLE] }],
};
const exp = (n: number, over: Partial<ExperimentItem> = {}): ExperimentItem => ({
  experiment_id: IID(n), instance_id: IID(8), display_name: `实验${n}`, catalog_id: 'PB:turtle_55', kind: 'playbook', run_purpose: 'screening', run_state: 'completed', started_at: '2026-11-12T01:00:00Z',
  completed_at: '2026-11-12T01:05:00Z', error_code: null, net_return_validation: '0.0123', max_drawdown: '-0.08', trade_count: '12', has_replay: true, ...over,
});

// ---- pure helpers -------------------------------------------------------------------------------------------------------
describe('research helpers', () => {
  it('cell state: empty has no trades, thin is below the gate, ok otherwise', () => {
    expect([cellState(cell('a', 'a', 0, null)), cellState(cell('a', 'a', 9, '0.1')), cellState(cell('a', 'a', 10, '0.1'))]).toEqual(['empty', 'thin', 'ok']);
    expect(heat(cell('a', 'a', 5, '-0.5'))).toEqual({ tone: 'neg', alpha: 0.5 });
    expect(heat(cell('a', 'a', 5, null)).tone).toBe('none');
  });
  it('missing stays missing in text', () => {
    expect(pctText(null)).toBe('缺失');
    expect(pctText('0.0312')).toBe('3.12%');
    expect(exitText(null, 'open')).toBe('持仓中');
    expect(exitText('stop_loss', 'closed')).toBe('止损');
  });
  it('sortByColumn is stable, supports both directions and always puts missing last', () => {
    const rows = [{ n: 'a', v: 3 }, { n: 'b', v: null }, { n: 'c', v: 9 }, { n: 'd', v: 3 }];
    expect(sortByColumn(rows, (r) => r.v, 'desc').map((r) => r.n)).toEqual(['c', 'a', 'd', 'b']);
    expect(sortByColumn(rows, (r) => r.v, 'asc').map((r) => r.n)).toEqual(['a', 'd', 'c', 'b']);
    expect(sortByColumn(rows, (r) => r.n, 'asc').map((r) => r.n)).toEqual(['a', 'b', 'c', 'd']);
    expect(rows.map((r) => r.n)).toEqual(['a', 'b', 'c', 'd']);                 // input untouched
  });
});

// ---- replay panel -------------------------------------------------------------------------------------------------------
describe('ReplayView', () => {
  it('shows the equity summary, the recent trades with exit reasons and open positions without a return', () => {
    renderPage(<ReplayView data={REPLAY} />);
    expect(screen.getByText('净收益 5.00%')).toBeInTheDocument();
    expect(screen.getByText('最大回撤 -10.00%')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: /净值 2026-03-02 至 2026-03-06/ })).toBeInTheDocument();
    const trades = screen.getAllByRole('table')[0]!;
    expect(within(trades).getByText('止损')).toBeInTheDocument();
    expect(within(trades).getByText('持有到期')).toBeInTheDocument();
    const open = within(trades).getByText('600003').closest('tr')!;
    expect(within(open).getByText('持仓中', { selector: 'td' })).toBeInTheDocument();
    expect(within(open).getAllByText('缺失').length).toBeGreaterThanOrEqual(2);      // exit price and return are missing, not 0
    expect(within(trades).getByText('-5.20%')).toBeInTheDocument();
  });
  it('regime cells under 10 trades are faded and flagged 样本不足; 10+ are not; empty buckets show a dash', () => {
    renderPage(<ReplayView data={REPLAY} />);
    const trend = screen.getByRole('table', { name: '趋势归因' });
    const cells = within(trend).getAllByRole('cell');
    expect(cells.map((c) => c.getAttribute('data-state'))).toEqual(['ok', 'thin', 'empty']);
    expect(within(cells[0]!).queryByText('样本不足')).not.toBeInTheDocument();
    expect(within(cells[1]!).getByText('样本不足')).toBeInTheDocument();
    expect(within(cells[1]!).getByText('-2.0%')).toBeInTheDocument();                // still shown, not hidden
    expect(within(cells[2]!).getByText('—')).toBeInTheDocument();
    expect(screen.getAllByText(/未归类 2 笔（历史不足 2）/).length).toBe(1);
    const style = within(screen.getByRole('table', { name: '风格归因' })).getAllByRole('cell');
    expect(style[0]!.getAttribute('data-state')).toBe('ok');                         // exactly 10 trades passes the gate
  });
  it('without a regime at evaluation time it says so instead of drawing empty cells', () => {
    const d: PlaybookReplay = { ...REPLAY, attribution: { ...REPLAY.attribution, regime: { ...REPLAY.attribution.regime, available: false, reason: 'NO_RELEASE' } } };
    renderPage(<ReplayView data={d} />);
    expect(screen.getByText(/没有可用的市场环境（NO_RELEASE）/)).toBeInTheDocument();
    expect(screen.queryByRole('table', { name: '趋势归因' })).not.toBeInTheDocument();
  });
});

describe('ReplayPanel', () => {
  it('requests the catalog id and trade limit', async () => {
    const calls = route({ 'bf/research/playbook-replay': ok(REPLAY) });
    renderPage(<ReplayPanel catalogId="PB:turtle_55" tradesLimit={20} />);
    expect(await screen.findByText('净收益 5.00%')).toBeInTheDocument();
    expect(calls[0]!.searchParams.get('catalog_id')).toBe('PB:turtle_55');
    expect(calls[0]!.searchParams.get('trades_limit')).toBe('20');
  });
  it('404 means no stored replay yet: an instruction, not an error or an empty curve', async () => {
    route({ 'bf/research/playbook-replay': fail(404, 'NOT_FOUND') });
    renderPage(<ReplayPanel catalogId="PB:turtle_55" />);
    expect(await screen.findByText(/还没有回放结果/)).toBeInTheDocument();
    expect(screen.getByText(/ext-b-run --ids PB:turtle_55/)).toBeInTheDocument();
  });
  it('any other failure is an error state', async () => {
    route({ 'bf/research/playbook-replay': fail(500, 'INTERNAL') });
    renderPage(<ReplayPanel catalogId="PB:turtle_55" />);
    expect(await screen.findByText(/回放结果读取失败/)).toBeInTheDocument();
  });
});

// ---- heatmap ------------------------------------------------------------------------------------------------------------
describe('RegimeHeatmapView (lab page tab)', () => {
  it('draws one row per playbook, switches dimension and flags thin cells', async () => {
    route({ 'bf/research/regime-heatmap': ok(HEATMAP) });
    renderPage(<RegimeHeatmapView />);
    const table = await screen.findByRole('table');
    const row = within(table).getByText(CATALOG.playbooks.find((p) => p.id === 'turtle_55')!.name).closest('tr')!;
    expect(within(row).getAllByRole('cell').map((c) => c.getAttribute('data-state')).filter(Boolean)).toEqual(['ok', 'thin', 'empty']);
    expect(within(row).getByText('样本不足')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('tab', { name: '风格' }));
    expect(within(screen.getByRole('table')).getByText('小盘占优')).toBeInTheDocument();
    expect(within(screen.getByRole('table')).queryByText('牛市')).not.toBeInTheDocument();
  });
  it('is reachable from the lab page and says what to run when nothing was replayed', async () => {
    route({ 'bf/research/regime-heatmap': ok({ ...HEATMAP, rows: [] }) });
    renderPage(<LabPage />);
    await userEvent.click(screen.getByRole('tab', { name: '战法×市场环境' }));
    expect(await screen.findByText(/还没有战法有回放结果/)).toBeInTheDocument();
  });
});

// ---- archive ------------------------------------------------------------------------------------------------------------
describe('ExperimentArchive (strategies page tab)', () => {
  it('lists newest first with filters sent to the server, shows failures with their code and pages with the cursor', async () => {
    const calls = route({
      'bf/research/experiments': (u) => (u.searchParams.get('cursor') === 'c2'
        ? ok({ items: [exp(3)], page: { next_cursor: null, limit: 30 }, order: 'o' })
        : ok({ items: [exp(1), exp(2, { run_state: 'failed', error_code: 'data_not_available:fundamentals_pit', net_return_validation: null, max_drawdown: null, trade_count: null, has_replay: false, completed_at: null })], page: { next_cursor: 'c2', limit: 30 }, order: '按完成时间由新到旧' })),
    });
    renderPage(<ExperimentArchive />);
    expect(await screen.findByText('实验1')).toBeInTheDocument();
    expect(screen.getByText('data_not_available:fundamentals_pit')).toBeInTheDocument();
    expect(screen.getByText('1.23%')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: '加载更多' }));
    expect(await screen.findByText('实验3')).toBeInTheDocument();
    expect(calls.at(-1)!.searchParams.get('cursor')).toBe('c2');
    await userEvent.selectOptions(screen.getByLabelText('状态'), 'failed');
    await waitFor(() => expect(calls.at(-1)!.searchParams.get('status')).toBe('failed'));
    await userEvent.selectOptions(screen.getByLabelText('目的'), 'screening');
    await waitFor(() => expect(calls.at(-1)!.searchParams.get('purpose')).toBe('screening'));
    await userEvent.type(screen.getByLabelText(/策略实例/), 'not-a-uuid');
    expect(await screen.findByText(/36 位 UUID/)).toBeInTheDocument();
  });
  it('is a tab of the strategies page', async () => {
    route({ 'bf/research/experiments': ok({ items: [], page: { next_cursor: null, limit: 30 }, order: 'o' }) });
    vi.mocked(ops.listStrategies).mockResolvedValue({ meta: META, data: { items: [] } } as never);
    renderPage(<StrategiesPage />);
    await userEvent.click(screen.getByRole('tab', { name: '实验档案' }));
    expect(await screen.findByText('没有符合条件的实验。')).toBeInTheDocument();
  });
});

// ---- pattern family filter (#33) ------------------------------------------------------------------------------------------
const PAGE = { next_cursor: null, snapshot_hash: 'h'.repeat(64) };
const CTX = {
  input_snapshot_refs: [{ snapshot_id: IID(9), dataset_id: 'bars_legacy_fwd_adjusted_1d', manifest_version: 1, content_hash: 'c'.repeat(64) }], decision_as_of: '2026-11-12T00:00:00Z',
  universe_policy_versions: ['screen-universe-main-v1'], field_catalog_version: 'screen-fields-v1', playbook_instances: [], replay: { evaluation_context: null, cost_policy_versions: [], instances: [] },
};
describe('pattern family filter', () => {
  it('shows only the cards of the chosen family and lists every family with its count', async () => {
    route({ 'b/context': ok(CTX), 'run-screen': ok({ receipt: { job_run_id: IID(5), resource_id: IID(7), run_state: 'queued', poll_operation_id: 'queryScreenRun' } }), 'query-screen-run': ok({ rows: [], page: PAGE, capability_gaps: [] }) });
    renderPage(<LabPage />);
    await screen.findByText(CATALOG.patterns[0]!.name);
    const select = screen.getByLabelText('形态类别');
    const families = [...new Set(CATALOG.patterns.map((p) => p.family))];
    expect(within(select).getAllByRole('option').length).toBe(families.length + 1);
    const fam = families[0]!;
    await userEvent.selectOptions(select, fam);
    const mine = CATALOG.patterns.filter((p) => p.family === fam);
    const others = CATALOG.patterns.filter((p) => p.family !== fam);
    expect(mine.every((p) => screen.queryAllByText(p.name).length > 0)).toBe(true);
    expect(others.every((p) => screen.queryAllByText(p.name).length === 0)).toBe(true);
    await userEvent.selectOptions(select, '');
    expect(others.every((p) => screen.queryAllByText(p.name).length > 0)).toBe(true);
  });
});

// ---- sortable screen results (#29) ----------------------------------------------------------------------------------------
const AST = { condition: compare('limit_streak', 'eq', 1), allowed_field_catalog_version: 'screen-fields-v1', sort: [{ field_id: 'pct_chg', direction: 'desc', nulls: 'last' }] };
const rowFor = (n: number, truth: string, value: string | null) => ({
  instrument_id: IID(n), condition_value: truth, sort_position: n,
  features: [{ feature_id: 'pct_chg', definition_version: 'v1', key_hash: 'k', result: { feature_key_hash: 'k', artifact: { ref: `screen-value://r/i/pct_chg?${value === null ? 'missing=x' : `value=${value}&unit=pct`}`, content_hash: 'h', contract_id: 'c', schema_version: '1' }, quality_status: 'valid', result_state: 'READY', computed_at: '2026-11-12T00:00:00Z' } }],
});
describe('sortable screen results', () => {
  it('header click sorts the loaded rows (null last), says it only sorts loaded rows, and restores the server order on the third click', async () => {
    route({
      'b/context': ok(CTX), 'list-saved-screens': ok({ screens: [], page: PAGE }),
      'parse-screen-text': ok({ ast: AST, unresolved: [], complete: true }),
      'run-screen': ok({ receipt: { job_run_id: IID(5), resource_id: IID(7), run_state: 'queued', poll_operation_id: 'queryScreenRun' } }),
      'query-screen-run': ok({ rows: [rowFor(1, 'true', '3.00'), rowFor(2, 'true', '9.98'), rowFor(3, 'unknown', null)], page: { next_cursor: 'more', snapshot_hash: 'h'.repeat(64) }, capability_gaps: [] }),
      'b/instrument-labels': ok([1, 2, 3].map((n) => ({ instrument_id: IID(n), code: `60000${n}`, name: `名${n}`, is_risk_warning: false }))),
    });
    renderPage(<ScreenPage />);
    await userEvent.click(screen.getByRole('button', { name: '首板加速' }));
    await screen.findByText('连板数 = 1');
    await userEvent.click(screen.getByRole('button', { name: '运行筛选' }));
    const table = await screen.findByRole('table', {}, { timeout: 8000 });
    const order = (): string[] => within(table).getAllByRole('row').slice(1).map((r) => within(r).getAllByRole('cell')[1]!.textContent ?? '');
    await waitFor(() => expect(order()).toEqual(['600001', '600002', '600003']));
    const label = FIELD_BY_ID.pct_chg!.label;
    await userEvent.click(within(table).getByRole('button', { name: label }));
    expect(order()).toEqual(['600002', '600001', '600003']);                         // desc, the missing value last
    expect(screen.getByText(/只排当前已加载的行/)).toBeInTheDocument();
    expect(within(table).getByRole('columnheader', { name: new RegExp(label) }).getAttribute('aria-sort')).toBe('descending');
    await userEvent.click(within(table).getByRole('button', { name: new RegExp(label) }));
    expect(order()).toEqual(['600001', '600002', '600003']);                         // asc, the missing value still last
    await userEvent.click(within(table).getByRole('button', { name: new RegExp(label) }));
    expect(screen.queryByText(/只排当前已加载的行/)).not.toBeInTheDocument();
    expect(order()).toEqual(['600001', '600002', '600003']);                         // server order restored
    expect(screen.getByRole('button', { name: '加载更多' })).toBeInTheDocument();    // server paging stays
  }, 20000);
});
