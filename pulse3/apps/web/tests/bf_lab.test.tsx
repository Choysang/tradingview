/** Lab lane web checks: the ledger page puts verdict / why / when-to-retry first, the tear sheet shows missing figures WITH their reason (never 0), the batch
 * comparison submits with an Idempotency-Key and draws the server's columns. Fixtures pass the Python contract test (tests/bf_lab/test_web_fixtures.py). */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import type { ReactElement } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ToastProvider } from '../src/components/Toast';
import type { BatchRun, BatchRunList, LedgerEntryDetail, LedgerList, RetiredTools, TearSheetData } from '../src/pages/bf/lab/api';
import { METRIC_NAMES, tearSheetQuery } from '../src/pages/bf/lab/api';
import { BatchComparePage, TABLE_COLUMNS } from '../src/pages/bf/lab/BatchComparePage';
import { METRIC_META, metricText, reasonText, skipText } from '../src/pages/bf/lab/format';
import { LedgerPage } from '../src/pages/bf/lab/LedgerPage';
import { TearSheetCard, TearSheetView } from '../src/pages/bf/lab/TearSheetCard';

const fx = <T,>(name: string): T => JSON.parse(readFileSync(resolve(__dirname, `../src/pages/bf/lab/fixtures/${name}.json`), 'utf-8')) as T;
const LIST = fx<LedgerList>('ledger-list');
const EMPTY = fx<LedgerList>('ledger-empty');
const ENTRY = fx<LedgerEntryDetail>('ledger-entry');
const RETIRED = fx<RetiredTools>('retired-tools');
const TEAR = fx<TearSheetData>('tearsheet-replay');
const BOOK = fx<TearSheetData>('tearsheet-book');
const RUN = fx<BatchRun>('batch-run');
const RUNS = fx<BatchRunList>('batch-list');

const META = {
  request_id: 'r', generated_at: '2026-11-17T01:00:00Z', trace_id: null, contract_version: '3.0.0', release_id: null, decision_as_of: null, availability_policy_version: null,
  input_snapshot_refs: [], quality_status: 'valid', freshness_state: 'not_expected', coverage: { policy_version: 'operational-v1', expected_count: null, observed_count: 0, missing_count: null, unverifiable_reasons: [] },
  warnings: [], next_cursor: null, actionability_id: null,
};
const json = (status: number, body: unknown): Response => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
const ok = (data: unknown, status = 200): Response => json(status, { meta: META, data });
const fail = (status: number, code: string): Response => json(status, { meta: META, error: { code, message: code, retryable: false, field_errors: [] } });

interface Call { url: URL; method: string; headers: Headers; body: unknown }
type Handler = (c: Call) => Response | undefined;
function route(handlers: Record<string, Handler | Response>): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: string | Request, init?: RequestInit) => {
    const url = new URL(typeof input === 'string' ? input : input.url, 'http://localhost');
    const call: Call = { url, method: init?.method ?? 'GET', headers: new Headers(init?.headers), body: init?.body ? JSON.parse(String(init.body)) : undefined };
    calls.push(call);
    const h = handlers[`${call.method} ${url.pathname.replace('/v1/x/bf/lab/', '')}`];
    const res = typeof h === 'function' ? h(call) : h;
    return res ? res.clone() : fail(404, 'NOT_FOUND');
  }));
  return calls;
}
function renderAt(el: ReactElement, path = '/') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><ToastProvider><MemoryRouter initialEntries={[path]}>{el}</MemoryRouter></ToastProvider></QueryClientProvider>);
}
afterEach(() => { vi.unstubAllGlobals(); });

// ---- helpers ----------------------------------------------------------------------------------------------------------------------------
describe('format helpers', () => {
  it('formats by kind and never turns a missing value into zero', () => {
    expect(metricText('total_return', { value: '0.041', reason: null })).toBe('4.10%');
    expect(metricText('sharpe', { value: '2.543620135666', reason: null })).toBe('2.54');
    expect(metricText('longest_drawdown_sessions', { value: '5', reason: null })).toBe('5');
    expect(metricText('avg_win', { value: '45000', reason: null })).toBe('45,000');
    expect(metricText('sortino', { value: null, reason: 'no_downside' })).toBe('缺失');
    expect(reasonText('no_downside')).toBe('没有下跌日');
    expect(reasonText('something_new')).toBe('something_new');
    expect(reasonText(null)).toBe('缺失');
    expect(skipText('replay_error:ValueError')).toContain('ValueError');
    expect(skipText('not_in_panel')).toContain('没有这只股票');
  });
  it('knows every metric the contract has and maps a source to one query parameter', () => {
    expect(METRIC_NAMES.every((n) => METRIC_META[n])).toBe(true);
    expect(Object.keys(TEAR.tearsheet.metrics).sort()).toEqual([...METRIC_NAMES].sort());
    expect(tearSheetQuery({ catalogId: 'PB:x' })).toEqual({ catalog_id: 'PB:x' });
    expect(tearSheetQuery({ experimentId: 'e' })).toEqual({ experiment_id: 'e' });
    expect(tearSheetQuery({ bookKey: 'main_h5' })).toEqual({ book_key: 'main_h5' });
  });
});

// ---- tear sheet -------------------------------------------------------------------------------------------------------------------------
describe('tear sheet', () => {
  it('draws the server figures with their labels and flags a short sample', () => {
    renderAt(<TearSheetView data={TEAR} />);
    expect(screen.getByText('4.10%')).toBeInTheDocument();                       // total return 0.041
    expect(screen.getByText('最长回撤(交易日)')).toBeInTheDocument();
    expect(screen.getByText(/样本短/)).toBeInTheDocument();                        // 8 sessions
    expect(screen.getByText(/已平仓 3 笔，持仓中 1 笔/)).toBeInTheDocument();
    expect(screen.getByText(/毛收益/)).toBeInTheDocument();
  });

  it('a book without a trade list shows 缺失 and the reason for every trade figure, and says so', () => {
    renderAt(<TearSheetView data={BOOK} />);
    expect(screen.getByText(/成交明细不可用/)).toBeInTheDocument();
    expect(screen.getAllByText('没有成交明细').length).toBeGreaterThanOrEqual(6);
    expect(screen.queryByText('0.00%')).toBeNull();
  });

  it('the card asks the server for its source and shows an empty state on 404', async () => {
    const calls = route({ 'GET tearsheet': (c) => (c.url.searchParams.get('catalog_id') === 'PB:turtle_55' ? ok(TEAR) : fail(404, 'NOT_FOUND')) });
    renderAt(<TearSheetCard source={{ catalogId: 'PB:turtle_55' }} />);
    await screen.findByText('战法回放 turtle_55');
    expect(calls[0]?.url.search).toBe('?catalog_id=PB%3Aturtle_55');
    renderAt(<TearSheetCard source={{ catalogId: 'PB:none' }} />);
    await screen.findByText(/还没有可用于业绩报告/);
  });
});

// ---- ledger -----------------------------------------------------------------------------------------------------------------------------
describe('ledger page', () => {
  const handlers = (): Record<string, Handler | Response> => ({
    'GET ledger': (c) => (c.url.searchParams.get('verdict') === 'fails' ? ok({ ...LIST, matched: 1, entries: LIST.entries.filter((e) => e.verdict === 'fails').slice(0, 1) }) : ok(LIST)),
    'GET ledger/E003': ok(ENTRY), 'GET retired-tools': ok(RETIRED),
  });

  it('shows verdict, why and when-to-retry on every card, with the read-only framing', async () => {
    route(handlers());
    renderAt(<LedgerPage />);
    const cards = await screen.findAllByText('为什么');
    expect(cards).toHaveLength(LIST.entries.length);
    expect(screen.getAllByText('何时再试')).toHaveLength(LIST.entries.length);
    const e3 = LIST.entries.find((e) => e.id === 'E003')!;
    expect(screen.getByText(e3.why)).toBeInTheDocument();
    expect(screen.getByText(e3.reactivate_when)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /无效 28/ })).toBeInTheDocument();
    expect(screen.getByText(/不是 pulse3 的实验/)).toBeInTheDocument();
  });

  it('a verdict filter goes to the server', async () => {
    const calls = route(handlers());
    renderAt(<LedgerPage />);
    await screen.findAllByText('为什么');
    await userEvent.click(screen.getByRole('button', { name: /无效 28/ }));
    await waitFor(() => expect(calls.some((c) => c.url.pathname.endsWith('/ledger') && c.url.searchParams.get('verdict') === 'fails')).toBe(true));
  });

  it('a ledger that is not imported says how to import it instead of showing an empty list', async () => {
    route({ 'GET ledger': ok(EMPTY), 'GET retired-tools': ok({ ...RETIRED, ledger: null }) });
    renderAt(<LedgerPage />);
    await screen.findByText(/台账还没有导入/);
    expect(screen.getByText(/pulse\.workers\.lab import-ledger/)).toBeInTheDocument();
  });

  it('?entry= opens the entry and names the retired tool it explains', async () => {
    route(handlers());
    renderAt(<LedgerPage />, '/lab/ledger?entry=E003');
    const aside = await screen.findByLabelText('实验 E003');
    expect(within(aside).getByText(/被退役的依据/)).toBeInTheDocument();
    expect(within(aside).getByText(/Fail against|不成立|同日匹配基准/)).toBeInTheDocument();
  });

  it('the retired-tool table links every family to its ledger entries with the reason', async () => {
    const calls = route(handlers());
    renderAt(<LedgerPage />, '/lab/ledger?tool=rl_fqi');
    await screen.findByText('已退役的研究工具');
    await waitFor(() => expect(calls.some((c) => c.url.pathname.endsWith('/retired-tools') && c.url.searchParams.get('tool') === 'rl_fqi')).toBe(true));
    const fam = RETIRED.families[0]!;
    expect(await screen.findByText(fam.reason_zh)).toBeInTheDocument();
    for (const t of fam.tools) expect(screen.getAllByText(t).length).toBeGreaterThan(0);
    const link = screen.getAllByRole('link', { name: new RegExp(fam.ledger_ids[0] ?? "") })[0];
    expect(link?.getAttribute('href')).toContain(`entry=${fam.ledger_ids[0]}`);
  });
});

// ---- batch comparison --------------------------------------------------------------------------------------------------------------------
describe('batch comparison', () => {
  const SCREEN = '00000000-0000-0000-0000-0000000000a1';

  it('submits with an Idempotency-Key equal to the body key, then shows the finished run with the tear-sheet columns', async () => {
    const calls = route({
      'POST batch-compare': ok({ receipt: { job_run_id: RUN.run.job_run_id, resource_id: RUN.run.job_run_id, run_state: 'queued', poll_operation_id: 'getJobRun' } }, 202),
      [`GET batch-compare/${RUN.run.job_run_id}`]: ok(RUN), 'GET batch-compare': ok(RUNS),
    });
    renderAt(<BatchComparePage />, `/lab/batch?screen_run=${SCREEN}`);
    const form = await screen.findByRole('form', { name: '批量对比表单' });
    await userEvent.click(within(form).getByRole('button', { name: '开始对比' }));
    await screen.findByLabelText('对比结果');
    const post = calls.find((c) => c.method === 'POST')!;
    const body = post.body as Record<string, unknown>;
    expect(post.headers.get('Idempotency-Key')).toBe(body.idempotency_key);
    expect(body).toMatchObject({ screen_run_id: SCREEN, top_n: 10 });
    expect(typeof body.playbook_id).toBe('string');
    for (const c of TABLE_COLUMNS) expect(screen.getAllByText(METRIC_META[c].label).length).toBeGreaterThan(0);
    const rows = screen.getAllByRole('row');
    const first = rows.find((r) => within(r).queryByText('600001'))!;
    expect(within(first).getByText('有成交')).toBeInTheDocument();
    const skipped = rows.find((r) => within(r).queryByText(/没有这只股票/))!;
    expect(within(skipped).getByText('未回放')).toBeInTheDocument();
    expect(within(skipped).getAllByText('—').length).toBe(TABLE_COLUMNS.length);       // missing, not 0
    expect(screen.getByText(/不是样本外检验/)).toBeInTheDocument();
  });

  it('the form refuses a screen id that is not a UUID', async () => {
    route({ 'GET batch-compare': ok(RUNS) });
    renderAt(<BatchComparePage />);
    const form = await screen.findByRole('form', { name: '批量对比表单' });
    const button = within(form).getByRole('button', { name: '开始对比' });
    expect(button).toBeDisabled();
    await userEvent.type(within(form).getByPlaceholderText('36 位 UUID'), 'not-a-uuid');
    expect(button).toBeDisabled();
    await userEvent.clear(within(form).getByPlaceholderText('36 位 UUID'));
    await userEvent.type(within(form).getByPlaceholderText('36 位 UUID'), SCREEN);
    expect(button).toBeEnabled();
  });

  it('a failed run shows its error code and no result; the archive lists runs with their state', async () => {
    const failedId = RUNS.items[1]!.job_run_id;
    route({
      [`GET batch-compare/${failedId}`]: ok({ run: RUNS.items[1], result: null, result_ref: null }), 'GET batch-compare': ok(RUNS),
    });
    renderAt(<BatchComparePage />, `/lab/batch?run=${failedId}`);
    await screen.findByText(/对比失败/);
    expect(screen.getAllByText(/NO_MATCHING_ROWS/).length).toBeGreaterThan(0);
    expect(screen.queryByText(/不是样本外检验/)).toBeNull();
    const archive = await screen.findByLabelText('对比档案');
    await within(archive).findByText('3 只，1 只有成交，1 只未回放');
  });
});
