import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactElement } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ToastProvider } from '../src/components/Toast';
import { AlertRuleEditor } from '../src/pages/ext/c/AlertRuleEditor';
import { BooksPage } from '../src/pages/ext/c/BooksPage';
import { pctText } from '../src/pages/ext/c/bfOps';
import { RunJobForm } from '../src/pages/ext/c/RunJobForm';

const META = {
  request_id: 'r', generated_at: '2026-11-12T01:00:00Z', trace_id: null, contract_version: '3.0.0', release_id: null, decision_as_of: '2026-11-12T00:00:00Z',
  availability_policy_version: 'p', input_snapshot_refs: [], quality_status: 'valid', freshness_state: 'current',
  coverage: { policy_version: 'c', expected_count: null, observed_count: 1, missing_count: null, unverifiable_reasons: [] }, warnings: [], next_cursor: null, actionability_id: null,
};
const ok = (data: unknown): Response => new Response(JSON.stringify({ meta: META, data }), { status: 200 });
const fail = (status: number, code: string): Response => new Response(JSON.stringify({ meta: META, error: { code, message: code, retryable: false, field_errors: [] } }), { status });

type Handler = (url: URL, init?: RequestInit) => Response | undefined;
function route(handlers: Record<string, Handler | Response>) {
  const calls: Array<{ path: string; url: URL; init?: RequestInit }> = [];
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    const url = new URL(input, 'http://localhost');
    const path = url.pathname.replace('/v1/x/', '');
    calls.push({ path, url, init });
    const h = handlers[path];
    const res = typeof h === 'function' ? h(url, init) : h;
    return res ? res.clone() : fail(404, 'NOT_FOUND');
  }));
  return calls;
}
function renderPage(el: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><ToastProvider><MemoryRouter>{el}</MemoryRouter></ToastProvider></QueryClientProvider>);
}
afterEach(() => { vi.unstubAllGlobals(); });

const BASE = {
  release_id: 'r1', horizon_sessions: 5, decision_as_of: '2026-10-07T08:00:00Z', decision_session: '2026-10-07', entry_session: null, exit_session: null, sessions_after: null,
  n_ranked: null, n_evaluable: null, top_n: null, top_evaluable: null, rank_ic: null, top_decile_mean_gross: null, universe_median_gross: null, top_decile_excess: null,
  missing_reason: null, return_basis: 'gross', method_version: 'forward-ic-v1', record_kind: null, evaluated_at: null, snapshot_id: null, pending_reason: null,
};
const RESOLVED = { ...BASE, ranking_id: 'k1', horizon_id: '5td', status: 'resolved', entry_session: '2026-10-08', exit_session: '2026-10-14', n_ranked: 10, n_evaluable: 10, top_n: 1, top_evaluable: 1,
  rank_ic: '0.551515', top_decile_mean_gross: '0.155963', universe_median_gross: '0.040000', top_decile_excess: '0.115963', record_kind: 'live_forward', evaluated_at: '2026-10-20T11:00:00Z', snapshot_id: 's1' };
const PENDING = { ...BASE, ranking_id: 'k2', horizon_id: '20td', horizon_sessions: 20, status: 'pending', pending_reason: 'HORIZON_NOT_REACHED', sessions_after: 9 };
const MISSING = { ...RESOLVED, ranking_id: 'k3', horizon_id: '60td', horizon_sessions: 60, rank_ic: null, top_decile_excess: null, top_decile_mean_gross: null, missing_reason: 'TOO_FEW_EVALUABLE', n_evaluable: 3 };
const monitor = (rows: Array<{ status: string }>) => ok({ slot_key: 'main', method_version: 'forward-ic-v1', return_basis: 'gross', note: 'n', resolved_count: rows.filter((r) => r.status === 'resolved').length, pending_count: rows.filter((r) => r.status === 'pending').length, rows });
const NO_BOOKS = { 'c/forward-books': ok({ books: [] }), 'list-authorized-accounts': ok({ accounts: [], page: { next_cursor: null, snapshot_hash: 'h'.repeat(64) } }), 'c/readmodel-context': ok({ contexts: [] }) };

describe('pctText', () => {
  it('formats a decimal ratio and keeps missing as missing', () => {
    expect(pctText('0.115963')).toBe('11.60%');
    expect(pctText('-0.03')).toBe('-3.00%');
    expect(pctText(null)).toBeNull();
  });
});

describe('ForwardMonitor on the books page', () => {
  it('shows resolved IC and gross excess, keeps pending as pending and missing as missing (never 0)', async () => {
    const calls = route({ ...NO_BOOKS, 'bf/ops/forward-monitor': monitor([RESOLVED, PENDING, MISSING]) });
    renderPage(<BooksPage />);
    const sec = await screen.findByRole('region', { name: '前向监控' });
    expect(await within(sec).findByText('0.551515')).toBeInTheDocument();
    expect(within(sec).getByText('11.60%')).toBeInTheDocument();
    expect(within(sec).getByText(/毛收益/, { selector: 'caption' })).toBeInTheDocument();
    expect(within(sec).getByText('待定')).toBeInTheDocument();
    expect(within(sec).getByText(/已观察 9\/20 个交易日/)).toBeInTheDocument();
    expect(within(sec).getAllByText('可评估股票太少').length).toBeGreaterThan(0);
    expect(within(sec).queryByText('0.00%')).not.toBeInTheDocument();
    expect(within(sec).queryByText('0.000000')).not.toBeInTheDocument();
    expect(calls.some((c) => c.path === 'bf/ops/forward-monitor')).toBe(true);
  });

  it('a failing monitor read does not take the other books down', async () => {
    route({ ...NO_BOOKS, 'bf/ops/forward-monitor': fail(500, 'INTERNAL') });
    renderPage(<BooksPage />);
    expect(await screen.findByText('没有可用的前向主书。')).toBeInTheDocument();
    const sec = await screen.findByRole('region', { name: '前向监控' });
    expect(await within(sec).findByRole('alert')).toBeInTheDocument();
  });

  it('empty list says so', async () => {
    route({ ...NO_BOOKS, 'bf/ops/forward-monitor': monitor([]) });
    renderPage(<BooksPage />);
    expect(await screen.findByText('还没有已发布的排名可监控。')).toBeInTheDocument();
  });
});

describe('RunJobForm: 重新生成分析发布', () => {
  const DEF = { job_id: 'eeeeeeee-0000-4000-8000-000000000000', job_version: 1, application_operation: 'ReleaseService.build_release', manual_trigger: true, enabled: true, definition_hash: 'd'.repeat(64), parameter_contract_id: 'pulse.job_parameters', parameter_schema_version: '1' };
  it('lists the build job under its plain name, prefills its parameters and confirms before sending', async () => {
    const calls = route({ 'c/job-definitions': ok({ definitions: [DEF] }), 'run-registered-job': ok({ receipt: { job_run_id: 'dddddddd-0000-4000-8000-000000000000', resource_id: 'r', run_state: 'queued', poll_operation_id: 'getJobRun' } }) });
    renderPage(<RunJobForm />);
    const form = await screen.findByRole('form', { name: '运行任务表单' });
    const opt = within(form).getByRole('option', { name: /重新生成分析发布（ReleaseService\.build_release）/ });
    await userEvent.selectOptions(within(form).getByLabelText('已登记任务'), opt);
    expect(within(form).getByLabelText('任务参数（JSON）')).toHaveValue('{"as_of":"latest","top_n":10}');
    expect(within(form).getByRole('note')).toHaveTextContent(/构建守卫/);
    await userEvent.click(within(form).getByRole('button', { name: '运行' }));
    const dlg = await screen.findByRole('alertdialog');
    expect(dlg).toHaveTextContent('重新生成分析发布');
    expect(calls.some((c) => c.path === 'run-registered-job')).toBe(false);
    await userEvent.click(within(dlg).getByRole('button', { name: '确认运行' }));
    await screen.findByText(/已提交：运行编号/);
    const sent = calls.find((c) => c.path === 'run-registered-job');
    expect(JSON.parse(String(sent?.init?.body)).job_id).toBe(DEF.job_id);
  });
});

describe('AlertRuleEditor: test push', () => {
  const rules = ok({
    rules: [], webhook: { configured: true, kind: 'wecom', host: 'hooks.example' },
    destinations: [{ destination_id: 'd1', channel: 'in_app', enabled: true, endpoint: 'configured', label: '站内收件箱' },
      { destination_id: 'd2', channel: 'webhook', enabled: false, endpoint: 'not_configured', label: 'Webhook 推送（未配置）' }],
  });
  const result = (status: string, error_code: string | null = null) => ok({ alert_id: 'a1', destination_id: 'd1', channel: 'in_app', status, error_code, recorded_at: '2026-11-13T01:00:00Z', is_test: true });
  const key = (c: { init?: RequestInit }) => (c.init?.headers as Record<string, string>)['Idempotency-Key'];

  it('sends one test with its own idempotency key, shows the answer, and disables unconfigured destinations', async () => {
    const calls = route({ 'c/alert-rules': rules, 'bf/ops/test-push': result('confirmed') });
    renderPage(<AlertRuleEditor />);
    const btn = await screen.findByRole('button', { name: /向站内 站内收件箱发送测试消息/ });
    expect(screen.getByRole('button', { name: /向Webhook Webhook 推送（未配置）发送测试消息/ })).toBeDisabled();
    await userEvent.click(btn);
    expect(await screen.findByText('已写入站内收件箱')).toBeInTheDocument();
    const first = calls.filter((c) => c.path === 'bf/ops/test-push');
    expect(first).toHaveLength(1);
    expect(JSON.parse(String(first[0]?.init?.body))).toEqual({ destination_id: 'd1' });
    expect(key(first[0] as { init?: RequestInit })).toMatch(/^[0-9a-f-]{36}$/);
    await userEvent.click(btn);
    await vi.waitFor(() => expect(calls.filter((c) => c.path === 'bf/ops/test-push')).toHaveLength(2));
    const keys = calls.filter((c) => c.path === 'bf/ops/test-push').map(key);
    expect(new Set(keys).size).toBe(2);
  });

  it('a provider failure is shown with its code, not hidden', async () => {
    route({ 'c/alert-rules': rules, 'bf/ops/test-push': result('failed', 'HTTP_500') });
    renderPage(<AlertRuleEditor />);
    await userEvent.click(await screen.findByRole('button', { name: /向站内 站内收件箱发送测试消息/ }));
    expect(await screen.findByText(/发送失败（HTTP_500）/)).toBeInTheDocument();
  });

  it('shows the error when the server refuses', async () => {
    route({ 'c/alert-rules': rules, 'bf/ops/test-push': fail(409, 'SEND_DISABLED') });
    renderPage(<AlertRuleEditor />);
    await userEvent.click(await screen.findByRole('button', { name: /向站内 站内收件箱发送测试消息/ }));
    expect(await screen.findByText(/测试失败/)).toBeInTheDocument();
  });
});
