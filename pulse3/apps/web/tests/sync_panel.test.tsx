import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ToastProvider } from '../src/components/Toast';
import { SyncPanel } from '../src/pages/ext/c/SyncPanel';
import { EMPTY_SYNC_FORM, buildSyncParameters, describeSyncParameters } from '../src/pages/ext/c/syncJobs';

const META = {
  request_id: 'r', generated_at: '2026-11-19T01:00:00Z', trace_id: null, contract_version: '3.0.0', release_id: null, decision_as_of: '2026-11-19T00:00:00Z',
  availability_policy_version: 'p', input_snapshot_refs: [], quality_status: 'valid', freshness_state: 'current',
  coverage: { policy_version: 'c', expected_count: null, observed_count: 1, missing_count: null, unverifiable_reasons: [] }, warnings: [], next_cursor: null, actionability_id: null,
};
const BOUNDS = { max_codes: 300, max_since_days: 400, max_count: 320, max_last: 30, max_refdata_codes: 5000 };
const KINDS = ['sync_daily_bars', 'sync_legacy_import', 'sync_reconcile', 'sync_refdata'];
const job = (over: Record<string, unknown> = {}) => ({
  job_run_id: 'aaaaaaaa-0000-4000-8000-000000000000', kind: 'sync_reconcile', run_state: 'failed', parameters: { codes: ['000001'], last: 5 }, requested_at: '2026-11-19T00:00:00Z',
  started_at: '2026-11-19T00:05:00Z', finished_at: '2026-11-19T00:06:00Z', exit_status: 2, error_code: 'SOURCE_BLOCKED', summary: 'BLOCKED: circuit open', ...over,
});
const list = (items: unknown[], executor = true) => ({ items, kinds: KINDS, defaults: {}, bounds: BOUNDS, executor_enabled: executor, worker_hint: 'uv run python -m pulse.workers.sync_jobs run --max 1' });
const ok = (data: unknown, status = 200): Response => new Response(JSON.stringify({ meta: META, data }), { status });
const fail = (status: number, code: string): Response => new Response(JSON.stringify({ meta: META, error: { code, message: code, retryable: false, field_errors: [] } }), { status });

function stub(get: () => Response, post?: (body: unknown) => Response) {
  const posts: Array<{ body: Record<string, unknown>; key: string | null }> = [];
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    const url = new URL(input, 'http://localhost');
    if (url.pathname !== '/v1/x/bf/sync/jobs') return fail(404, 'NOT_FOUND');
    if (init?.method === 'POST') {
      const body = JSON.parse(String(init.body));
      posts.push({ body, key: (init.headers as Record<string, string>)['Idempotency-Key'] ?? null });
      return (post ?? (() => ok({}, 202)))(body);
    }
    return get();
  }));
  return posts;
}
function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><ToastProvider><MemoryRouter><SyncPanel /></MemoryRouter></ToastProvider></QueryClientProvider>);
}
afterEach(() => { vi.unstubAllGlobals(); });

describe('buildSyncParameters', () => {
  it('builds bounded daily parameters and normalises the code list', () => {
    const r = buildSyncParameters({ ...EMPTY_SYNC_FORM, codes: '000001, 600000\n000001' }, BOUNDS);
    expect(r).toEqual({ ok: true, parameters: { codes: ['000001', '600000'], source: 'sina', count: 320 } });
    expect(buildSyncParameters({ ...EMPTY_SYNC_FORM, mode: 'universe', count: '10' }, BOUNDS)).toEqual({ ok: true, parameters: { universe: true, source: 'sina', count: 10 } });
  });
  it('refuses missing, malformed and too many codes and out-of-range numbers', () => {
    expect(buildSyncParameters(EMPTY_SYNC_FORM, BOUNDS)).toMatchObject({ ok: false, errors: { codes: expect.any(String) } });
    expect(buildSyncParameters({ ...EMPTY_SYNC_FORM, codes: '12345' }, BOUNDS)).toMatchObject({ ok: false });
    const many = Array.from({ length: 301 }, (_, i) => String(i).padStart(6, '0')).join(' ');
    expect(buildSyncParameters({ ...EMPTY_SYNC_FORM, codes: many }, BOUNDS)).toMatchObject({ ok: false, errors: { codes: expect.stringContaining('300') } });
    expect(buildSyncParameters({ ...EMPTY_SYNC_FORM, codes: '000001', count: '321' }, BOUNDS)).toMatchObject({ ok: false, errors: { count: expect.any(String) } });
    expect(buildSyncParameters({ ...EMPTY_SYNC_FORM, kind: 'sync_legacy_import', sinceDays: '0' }, BOUNDS)).toMatchObject({ ok: false, errors: { sinceDays: expect.any(String) } });
    expect(buildSyncParameters({ ...EMPTY_SYNC_FORM, kind: 'sync_reconcile', codes: '000001', last: '31' }, BOUNDS)).toMatchObject({ ok: false, errors: { last: expect.any(String) } });
  });
  it('refdata: backfills carry a bounded max_codes, daily and weekly carry none', () => {
    const f = { ...EMPTY_SYNC_FORM, kind: 'sync_refdata' as const };
    expect(buildSyncParameters(f, BOUNDS)).toEqual({ ok: true, parameters: { mode: 'daily' } });
    expect(buildSyncParameters({ ...f, refdataMode: 'weekly', maxCodes: '99999' }, BOUNDS)).toEqual({ ok: true, parameters: { mode: 'weekly' } });
    expect(buildSyncParameters({ ...f, refdataMode: 'adjusted_backfill', maxCodes: '5000' }, BOUNDS)).toEqual({ ok: true, parameters: { mode: 'adjusted_backfill', max_codes: 5000 } });
    expect(buildSyncParameters({ ...f, refdataMode: 'raw_backfill', maxCodes: '5001' }, BOUNDS)).toMatchObject({ ok: false, errors: { maxCodes: expect.any(String) } });
    expect(describeSyncParameters('sync_refdata', { mode: 'raw_backfill', max_codes: 600 })).toBe('原始日线回填（每只 1 次请求） · 最多 600 只');
  });
  it('legacy import allows an empty code list and describes parameters', () => {
    const r = buildSyncParameters({ ...EMPTY_SYNC_FORM, kind: 'sync_legacy_import' }, BOUNDS);
    expect(r).toEqual({ ok: true, parameters: { since_days: 30, boards: 'main' } });
    expect(describeSyncParameters('sync_legacy_import', { since_days: 30, boards: 'main' })).toBe('最近 30 天 · 主板');
  });
});

describe('SyncPanel', () => {
  it('shows the run log with exit status, error and summary', async () => {
    stub(() => ok(list([job()])));
    renderPanel();
    expect(await screen.findByText('数据源封禁，已立即停止')).toBeInTheDocument();
    expect(screen.getByText('失败')).toBeInTheDocument();
    expect(screen.getByText('BLOCKED: circuit open')).toBeInTheDocument();
    expect(screen.getByText(/000001 · 最近 5 个日期/)).toBeInTheDocument();
  });

  it('queues a sync only after confirmation, with an idempotency key and the bounded parameters', async () => {
    const receipt = { receipt: { job_run_id: 'dddddddd-0000-4000-8000-000000000000', resource_id: 'r', run_state: 'queued', poll_operation_id: 'getJobRun' }, kind: 'sync_daily_bars', parameters: {}, worker_hint: 'h' };
    const posts = stub(() => ok(list([])), () => ok(receipt, 202));
    renderPanel();
    const form = await screen.findByRole('form', { name: '数据同步表单' });
    await userEvent.type(within(form).getByLabelText(/股票代码/), '000001 600000');
    await userEvent.click(within(form).getByRole('button', { name: '提交同步' }));
    const dlg = await screen.findByRole('alertdialog');
    expect(posts).toHaveLength(0);
    await userEvent.click(within(dlg).getByRole('button', { name: '确认排队' }));
    expect(await screen.findByText(/已排队：运行编号/)).toBeInTheDocument();
    expect(posts[0]?.body).toEqual({ kind: 'sync_daily_bars', parameters: { codes: ['000001', '600000'], source: 'sina', count: 320 } });
    expect(posts[0]?.key).toBeTruthy();
  });

  it('shows validation errors without sending anything', async () => {
    const posts = stub(() => ok(list([])));
    renderPanel();
    await userEvent.click(await screen.findByRole('button', { name: '提交同步' }));
    expect(await screen.findByText('请填写至少一个代码')).toBeInTheDocument();
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(posts).toHaveLength(0);
  });

  it('blocks a second submission of a kind that is queued but not of another kind', async () => {
    stub(() => ok(list([job({ kind: 'sync_daily_bars', run_state: 'queued', parameters: { codes: ['000001'], source: 'sina', count: 320 }, started_at: null, finished_at: null, exit_status: null, error_code: null, summary: null })])));
    renderPanel();
    expect(await screen.findByText(/已有一个排队中的任务/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '提交同步' })).toBeDisabled();
    await userEvent.selectOptions(screen.getByLabelText('同步类型'), 'sync_reconcile');
    await waitFor(() => expect(screen.getByRole('button', { name: '提交同步' })).toBeEnabled());
  });

  it('disables submission and says 切换后才会执行 while no executor is enabled', async () => {
    const posts = stub(() => ok(list([job()], false)));
    renderPanel();
    expect(await screen.findByText(/切换后才会执行/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '提交同步' })).toBeDisabled();
    expect(screen.getByText('数据源封禁，已立即停止')).toBeInTheDocument(); // the history stays readable
    expect(posts).toHaveLength(0);
  });

  it('offers sync_refdata with its live-fetch note, queues a bounded backfill and marks the legacy import', async () => {
    const receipt = { receipt: { job_run_id: 'dddddddd-0000-4000-8000-000000000000', resource_id: 'r', run_state: 'queued', poll_operation_id: 'getJobRun' }, kind: 'sync_refdata', parameters: {}, worker_hint: 'h' };
    const posts = stub(() => ok(list([])), () => ok(receipt, 202));
    renderPanel();
    const form = await screen.findByRole('form', { name: '数据同步表单' });
    expect(within(form).getByRole('option', { name: /旧系统遗留，随旧站下线/ })).toBeInTheDocument();
    await userEvent.selectOptions(within(form).getByLabelText('同步类型'), 'sync_refdata');
    expect(screen.getByRole('note')).toHaveTextContent('只在切换到新系统后才会执行');
    await userEvent.selectOptions(within(form).getByLabelText('模式'), 'adjusted_backfill');
    const max = within(form).getByLabelText(/本次最多处理的代码数/);
    await userEvent.clear(max);
    await userEvent.type(max, '5001');
    await userEvent.click(within(form).getByRole('button', { name: '提交同步' }));
    expect(await screen.findByText(/代码数需在 1\.\.5000/)).toBeInTheDocument();
    expect(posts).toHaveLength(0);
    await userEvent.clear(max);
    await userEvent.type(max, '700');
    await userEvent.click(within(form).getByRole('button', { name: '提交同步' }));
    await userEvent.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: '确认排队' }));
    expect(await screen.findByText(/已排队：运行编号/)).toBeInTheDocument();
    expect(posts[0]?.body).toEqual({ kind: 'sync_refdata', parameters: { mode: 'adjusted_backfill', max_codes: 700 } });
  });

  it('says 无同步权限 when the caller has no ops permission', async () => {
    stub(() => fail(403, 'FORBIDDEN'));
    renderPanel();
    expect(await screen.findByText('无同步权限（需要运行权限）')).toBeInTheDocument();
    expect(screen.queryByRole('form', { name: '数据同步表单' })).not.toBeInTheDocument();
  });
});
