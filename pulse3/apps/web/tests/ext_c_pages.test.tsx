import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactElement } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ToastProvider } from '../src/components/Toast';
import { AlertsPage } from '../src/pages/ext/c/AlertsPage';
import { BooksPage } from '../src/pages/ext/c/BooksPage';
import { CalendarPage } from '../src/pages/ext/c/CalendarPage';
import { HealthPage } from '../src/pages/ext/c/HealthPage';
import { SeatsPage } from '../src/pages/ext/c/SeatsPage';

const META = {
  request_id: 'r', generated_at: '2026-11-12T01:00:00Z', trace_id: null, contract_version: '3.0.0', release_id: null, decision_as_of: '2026-11-12T00:00:00Z',
  availability_policy_version: 'p', input_snapshot_refs: [], quality_status: 'valid', freshness_state: 'current',
  coverage: { policy_version: 'c', expected_count: null, observed_count: 1, missing_count: null, unverifiable_reasons: [] }, warnings: [], next_cursor: null, actionability_id: null,
};
const PAGE = { next_cursor: null, snapshot_hash: 'h'.repeat(64) };
const REF = (r: string) => ({ ref: r, content_hash: 'a'.repeat(64), contract_id: 'c', schema_version: '1' });
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
    return (res ? res.clone() : fail(404, 'NOT_FOUND'));
  }));
  return calls;
}
function renderPage(el: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><ToastProvider><MemoryRouter>{el}</MemoryRouter></ToastProvider></QueryClientProvider>);
}
afterEach(() => { vi.unstubAllGlobals(); });

describe('AlertsPage', () => {
  const alerts = [
    { alert_id: 'a1', rule_id: 'r1', rule_version: 1, raised_at: '2026-11-11T01:00:00Z', evidence_artifact: REF('ev1'), delivery_states: ['webhook:failed', 'in_app:confirmed'], outcome_artifact: null },
    { alert_id: 'a2', rule_id: 'r1', rule_version: 1, raised_at: '2026-11-10T01:00:00Z', evidence_artifact: REF('ev2'), delivery_states: ['in_app:pending'], outcome_artifact: null },
    { alert_id: 'a3', rule_id: 'r9', rule_version: 1, raised_at: '2026-11-09T01:00:00Z', evidence_artifact: REF('ev3'), delivery_states: ['webhook:expired'], outcome_artifact: null },
  ];
  const bodies: Record<string, unknown> = {
    ev1: { tier: 'P0', headline: '持仓公司被立案调查', direction: 'negative', instruments: [{ instrument_id: 'i1', code: '600000', name: '浦发银行' }], reason: '持仓负面', claim_status: 'supported', source_claim_revision_ids: [], industries: [], already_reacted: null, rule_id: 'r1' },
    ev2: { tier: 'P2', headline: '自选股获得订单', direction: 'positive', instruments: [], industries: [], source_claim_revision_ids: [] },
    ev3: { tier: 'RULE', headline: '规则命中：银行板块异动', direction: 'negative', instruments: [], industries: ['银行'], held_in_industry: true, verification_note: '该说法尚未被独立来源证实', source_claim_revision_ids: [] },
  };
  const handlers = {
    'list-user-alerts': ok({ alerts, page: PAGE }),
    'c/artifact': (u: URL) => ok({ ref: u.searchParams.get('ref'), content_hash: 'x', contract_id: 'c', schema_version: '1', body: bodies[u.searchParams.get('ref') ?? ''] }),
    'c/alert-rules': ok({ rules: [{ rule_id: 'r1', rule_version: 3, updated_at: '2026-11-10T00:00:00Z', body: { scope: { scope_kind: 'news_tier', scope_key: 'P0', membership_snapshot_id: null }, condition: {}, policy_version: 'alert-rule-v1', annotation_id: null, annotation_version: null, destination_ids: ['d1'], enabled: true } }], destinations: [{ destination_id: 'd1', channel: 'in_app', enabled: true, endpoint: 'configured', label: '站内' }, { destination_id: 'd2', channel: 'webhook', enabled: true, endpoint: 'not_configured', label: '机器人' }], webhook: { configured: false, kind: null, host: null } }),
    'save-alert-rule': ok({ rule: { object_id: 'r1', version: 4, content_hash: 'h' } }),
  };

  it('groups alerts by tier, filters, shows channel badges and the webhook note', async () => {
    const calls = route(handlers);
    renderPage(<AlertsPage />);
    expect(await screen.findByText('持仓公司被立案调查')).toBeInTheDocument();
    expect(screen.getByRole('region', { name: /P0 持仓负面/ })).toBeInTheDocument();
    expect(screen.getByText('Webhook · 失败')).toBeInTheDocument();
    expect(screen.getByText('站内 · 已确认')).toBeInTheDocument();
    expect(screen.getByText(/只有在服务器配置了推送地址后才会生效/)).toBeInTheDocument();
    const listCall = calls.find((c) => c.path === 'list-user-alerts');
    expect(JSON.parse(listCall?.url.searchParams.get('window') ?? '{}')).toHaveProperty('start');
    expect(screen.getByRole('region', { name: /规则（你对个股/ })).toBeInTheDocument();
    expect(screen.getByText('该说法尚未被独立来源证实')).toBeInTheDocument();
    expect(screen.getByText('Webhook · 已过期')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /^P2 / }));
    await waitFor(() => expect(screen.queryByText('持仓公司被立案调查')).not.toBeInTheDocument());
    expect(screen.getByText('自选股获得订单')).toBeInTheDocument();
  });

  it('toggles a rule with expected_rule_version = current version', async () => {
    const calls = route(handlers);
    renderPage(<AlertsPage />);
    await userEvent.click(await screen.findByRole('button', { name: /新闻分级：P0：点击停用/ }));
    await waitFor(() => expect(calls.some((c) => c.path === 'save-alert-rule')).toBe(true));
    const post = calls.find((c) => c.path === 'save-alert-rule');
    const body = JSON.parse(String(post?.init?.body));
    expect(body).toMatchObject({ rule_id: 'r1', expected_rule_version: 3, body: { enabled: false } });
    expect((post?.init?.headers as Record<string, string>)['Idempotency-Key']).toBeTruthy();
  });

  it('shows an empty state', async () => {
    route({ ...handlers, 'list-user-alerts': ok({ alerts: [], page: PAGE }) });
    renderPage(<AlertsPage />);
    expect(await screen.findByText('这段时间没有告警。')).toBeInTheDocument();
  });
});

describe('CalendarPage', () => {
  const events = [
    { calendar_event_id: 'e1111111-0000-4000-8000-000000000001', revision: 2, scheduled_at: null, scheduled_date: '2026-11-20', time_precision: 'date', status: 'postponed', expected: { value: '2.5', text_value: null, unit: 'percent', reference_period: '2026-10', source_revision_id: 's' }, actual: null, available_at: '2026-11-01T00:00:00Z' },
    { calendar_event_id: 'e2222222-0000-4000-8000-000000000002', revision: 1, scheduled_at: '2026-12-02T01:30:00Z', scheduled_date: null, time_precision: 'minute', status: 'scheduled', expected: null, actual: null, available_at: '2026-11-01T00:00:00Z' },
  ];
  it('groups by month, uses the zero snapshot, shows no fake time for date-only, expands links', async () => {
    const calls = route({
      'query-calendar-events': ok({ events, page: PAGE }),
      'c/calendar-names': ok({ names: { [events[0]?.calendar_event_id as string]: { name: '中国CPI年率', category: '通胀', region: '中国', source_id: 'x' } } }),
      'c/calendar-links': ok({ links: [{ industry_node_id: 'n1', industry_name: '银行', link_rule_version: 'v1', instruments: [{ instrument_id: 'i1', code: '600000', name: '浦发银行' }] }], decision_as_of: '2026-11-12T00:00:00Z' }),
    });
    renderPage(<CalendarPage />);
    expect(await screen.findByText('2026-11-20（仅日期）')).toBeInTheDocument();
    expect(screen.getByRole('region', { name: '2026 年 11 月' })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: '2026 年 12 月' })).toBeInTheDocument();
    expect(screen.getByText('已延期')).toBeInTheDocument();
    expect(await screen.findByText('中国CPI年率')).toBeInTheDocument();
    expect(screen.getByText('通胀 · 中国')).toBeInTheDocument();
    const nameCall = calls.find((c) => c.path === 'c/calendar-names');
    expect(nameCall?.url.searchParams.get('ids')).toBe(events.map((e) => e.calendar_event_id).join(','));
    expect(screen.getByText(/事件 e2222222/)).toBeInTheDocument(); // no name returned -> id fallback
    expect(calls.find((c) => c.path === 'query-calendar-events')?.url.searchParams.get('snapshot_id')).toBe('00000000-0000-0000-0000-000000000000');
    await userEvent.click(screen.getAllByRole('button', { name: /关联行业/ })[0] as HTMLElement);
    const link = await screen.findByRole('link', { name: /浦发银行/ });
    expect(link).toHaveAttribute('href', '/stock/i1');
    expect(screen.getByText('银行')).toBeInTheDocument();
  });

  it('shows a typed error with retry', async () => {
    route({ 'query-calendar-events': fail(403, 'FORBIDDEN') });
    renderPage(<CalendarPage />);
    expect(await screen.findByText('没有权限执行此操作')).toBeInTheDocument();
  });
});

describe('SeatsPage', () => {
  const report = {
    report_revision_id: 'rr1', instrument_id: '22222222-2222-4222-8222-222222222222', source_id: 'exch', revision: 1, window_start: '2026-11-11', window_end: '2026-11-11', disclosure_reason: '日涨幅偏离值达7%', rule_version: 'v1', published_at: null, available_at: '2026-11-11T10:00:00Z', quality_status: 'valid',
    rows: [
      { side: 'buy', rank: 1, raw_seat_name: '某某证券深圳营业部', canonical_amount_cny: '123456789.00', resolution_status: 'resolved', seat_id: 's1', seat_version: 2, resolution_evidence: {} },
      { side: 'sell', rank: 1, raw_seat_name: '某某证券深圳营业部', canonical_amount_cny: '5000000', resolution_status: 'ambiguous', seat_id: null, seat_version: null, resolution_evidence: {} },
    ],
  };
  it('lists reports with both sides as separate rows and opens a profile with sample size', async () => {
    route({
      'query-seat-reports': ok({ reports: [report], page: PAGE }),
      'query-seat-profile': ok({ profile: REF('prof1'), sample_count: 12, limitations: ['样本少于30，仅供参考'] }),
      'c/artifact': ok({ ref: 'prof1', content_hash: 'x', contract_id: 'c', schema_version: '1', body: { policy_version: 'seat-profile-v1', sample_count: 12, limitations: [], metrics: [{ name: '次日收益均值', definition_version: 'v1', value: null, unit: 'ratio', sample_count: 3, missing_reason: null }, { name: '胜率', definition_version: 'v1', value: '0.5833', unit: 'ratio', sample_count: 12, missing_reason: null }] } }),
    });
    renderPage(<SeatsPage />);
    expect(await screen.findByText('日涨幅偏离值达7%')).toBeInTheDocument();
    expect(screen.getAllByText('某某证券深圳营业部')).toHaveLength(2);
    expect(screen.getByText('1.23 亿元')).toBeInTheDocument();
    expect(screen.getByText('可能对应多个席位')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /查看席位画像/ }));
    expect(await screen.findByText('样本数 12')).toBeInTheDocument();
    expect(screen.getByText('样本少于30，仅供参考')).toBeInTheDocument();
    expect(screen.getByText('样本不足')).toBeInTheDocument();
    expect(screen.getByText('58.33%')).toBeInTheDocument();
  });

  it('searches the directory', async () => {
    const calls = route({
      'query-seat-reports': ok({ reports: [], page: PAGE }),
      'query-seat-directory': ok({ seats: [{ seat_id: null, seat_version: null, display_name: '买一', resolution_status: 'report_local_label', report_local_ref: { report_revision_id: 'rr1', side: 'buy', rank: 1 } }], page: PAGE }),
    });
    renderPage(<SeatsPage />);
    await userEvent.type(await screen.findByLabelText('席位名称'), '买');
    await userEvent.click(screen.getByRole('button', { name: '搜索席位' }));
    expect(await screen.findByText(/没有对应的稳定席位/)).toBeInTheDocument();
    expect(calls.find((c) => c.path === 'query-seat-directory')?.url.searchParams.get('name_query')).toBe('买');
  });
});

describe('BooksPage', () => {
  const book = {
    account_id: 'acc-main', book_key: 'main_h5', label: '主书 H5', status_label: '检验中', tracking_kind: 'LEGACY_FORWARD', ranking_weight: 'none',
    rule: { hold_days: 5, slots: 20, new_per_day: 4, offer_top: 40, per_name_cny: 50000 }, account_version: 1, base_currency: 'CNY', frozen_on: '2026-10-30',
    latest: { trade_date: '2026-11-11', release_id: 'rel', nav_cny: '1002345.67', cash_cny: null, holdings: [{ instrument_id: 'i1', code: '600000', name: '浦发银行', quantity: 1000, cost_cny: '10.5', mark_price: null }], planned_buys: [{ instrument_id: 'i2', code: '000001', name: '平安银行', alpha_rank: 3 }], missing_marks: 1 },
    history: [{ trade_date: '2026-11-09', nav_cny: '1000000', positions: 1 }, { trade_date: '2026-11-10', nav_cny: null, positions: 1 }, { trade_date: '2026-11-11', nav_cny: '1002345.67', positions: 1 }],
    notes: ['本账本仅为前向检验，不构成建议'],
  };
  const ACCT = '33333333-3333-4333-8333-333333333333';
  const accounts = ok({ accounts: [{ account_id: ACCT, account_version: 4, base_currency: 'CNY' }], page: PAGE });
  const snap = { snapshot_id: 'aaaaaaaa-0000-4000-8000-000000000001', dataset_id: 'quotes', manifest_version: 1, content_hash: 'b'.repeat(64) };
  const contexts = ok({ contexts: [
    { projection_kind: 'orders', account_version: 7, window: { start: '2026-10-01T00:00:00Z', end: '2026-11-01T00:00:00Z' }, knowledge_as_of: '2026-11-02T00:00:00Z', valuation_snapshot_refs: [] },
    { projection_kind: 'performance', account_version: 6, window: { start: '2026-10-02T00:00:00Z', end: '2026-11-02T00:00:00Z' }, knowledge_as_of: '2026-11-03T00:00:00Z', valuation_snapshot_refs: [snap] },
    { projection_kind: 'performance', account_version: 5, window: { start: '2026-09-01T00:00:00Z', end: '2026-10-01T00:00:00Z' }, knowledge_as_of: '2026-10-03T00:00:00Z', valuation_snapshot_refs: [] },
  ] });

  it('shows the 检验中 badge, the no-weight line, missing marks and notes', async () => {
    route({ 'c/forward-books': ok({ books: [book] }), 'list-authorized-accounts': accounts, 'c/readmodel-context': ok({ contexts: [] }) });
    renderPage(<BooksPage />);
    expect(await screen.findByText('检验中')).toBeInTheDocument();
    expect(screen.getByText(/仅用于前向检验，不参与排名权重/)).toBeInTheDocument();
    expect(screen.getByText('5日持有 / 20只 / 每日4只 / 前40 / 每只5.00 万元', { exact: false })).toBeInTheDocument();
    expect(screen.getByText('缺价')).toBeInTheDocument();
    expect(screen.getByText('本账本仅为前向检验，不构成建议')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: /1 天净值缺失/ })).toBeInTheDocument();
  });

  it('says so when no read model has been built', async () => {
    route({ 'c/forward-books': ok({ books: [] }), 'list-authorized-accounts': accounts, 'c/readmodel-context': ok({ contexts: [] }) });
    renderPage(<BooksPage />);
    expect(await screen.findByText('尚无已生成的读模型（由任务 account_readmodel_build 或 CLI 生成）')).toBeInTheDocument();
  });

  it('queries orders and performance with the newest matching read-model context', async () => {
    const calls = route({
      'c/forward-books': ok({ books: [] }), 'list-authorized-accounts': accounts, 'c/readmodel-context': contexts,
      'query-account-orders': ok({ orders: [], coverage: META.coverage, page: PAGE }),
      'query-account-performance': (u) => (u.searchParams.get('performance_policy_version') === 'twr-cost-x3-v1' ? fail(409, 'RESULT_NOT_READY') : ok({ performance: { account_id: ACCT, account_version: 6, window: { start: 'a', end: 'b' }, performance_policy_version: 'p', valuation_policy_version: 'v', valuation_snapshot_refs: [snap], metrics: [{ name: '时间加权收益', definition_version: 'v1', value: '0.0123', unit: 'ratio', sample_count: 20, missing_reason: null }, { name: '最大回撤', definition_version: 'v1', value: null, unit: 'ratio', sample_count: 0, missing_reason: '样本不足' }], coverage: META.coverage, artifact: REF('perf1') } })),
      'c/artifact': ok({ ref: 'perf1', content_hash: 'x', contract_id: 'c', schema_version: '1', body: {} }),
    });
    renderPage(<BooksPage />);
    expect(await screen.findByText('这个读模型窗口内没有委托。')).toBeInTheDocument();
    const o = calls.find((c) => c.path === 'query-account-orders');
    expect(o?.url.searchParams.get('account_version')).toBe('7');
    expect(o?.url.searchParams.get('knowledge_as_of')).toBe('2026-11-02T00:00:00Z');
    expect(JSON.parse(o?.url.searchParams.get('window') ?? '{}').start).toBe('2026-10-01T00:00:00Z');
    await userEvent.click(screen.getByRole('tab', { name: '绩效' }));
    expect(await screen.findByText('时间加权收益')).toBeInTheDocument();
    expect(screen.getByText('1.23%')).toBeInTheDocument();
    expect(screen.getByText('缺失')).toBeInTheDocument();
    const perf = calls.find((c) => c.path === 'query-account-performance');
    expect(perf?.url.searchParams.get('performance_policy_version')).toBe('twr-cost-x1-v1');
    expect(perf?.url.searchParams.get('valuation_policy_version')).toBe('last-raw-close-in-snapshot-v1');
    expect(JSON.parse(perf?.url.searchParams.get('valuation_snapshot_refs') ?? '[]')).toEqual([snap]);
    expect(perf?.url.searchParams.get('account_version')).toBe('6');
    await userEvent.selectOptions(screen.getByLabelText('交易成本假设'), 'x3');
    expect(await screen.findByText('该成本档位尚未生成')).toBeInTheDocument();
  });

  it('renders opportunities, denominators and the incremental value with inventory shortfall', async () => {
    const body = {
      as_of: '2026-11-12T00:00:00Z', account_id: ACCT, experiment_id: null,
      opportunities: [{ opportunity_id: 'op1', opportunity_version: 1, instrument_id: 'i1', side: 'sell', scenario_id: 'sc1', state: 'partially_filled', target_quantity: 1000, filled_quantity: null, reason_code: 'STOP', account_event_ids: [], available_at: '2026-11-12T00:00:00Z' }],
      denominators: { total: 5, by_state: { partially_filled: 1, open: 4 } },
      incremental_vs_hold: { status: 'ok', incremental_pnl_cny: '-1234.50', active_fees_cny: '20.00', baseline_fees_cny: null, inventory_shortfall: { i9: 300 }, inventory_value_delta_cny: null, bridge_holds: [] },
    };
    route({
      'c/forward-books': ok({ books: [] }), 'list-authorized-accounts': accounts, 'c/readmodel-context': contexts,
      'query-opportunities': ok({ opportunity_artifact: REF('opp1'), page: PAGE }),
      'c/artifact': ok({ ref: 'opp1', content_hash: 'x', contract_id: 'c', schema_version: '1', body }),
    });
    renderPage(<BooksPage />);
    await userEvent.click(await screen.findByRole('tab', { name: '机会' }));
    expect(await screen.findByText('总数 5')).toBeInTheDocument();
    expect(screen.getByText('open：4')).toBeInTheDocument();
    expect(screen.getByText('STOP')).toBeInTheDocument();
    expect(screen.getByText('-1,234.50 元')).toBeInTheDocument();
    expect(screen.getByText('已卖未买回的库存缺口')).toBeInTheDocument();
    expect(screen.getByText('300')).toBeInTheDocument();
    expect(screen.getAllByText('缺失').length).toBeGreaterThan(0); // null fees / filled quantity are 缺失, never 0
  });

  it('shows a non-ok incremental status as text only', async () => {
    const body = { opportunities: [], denominators: { total: 0, by_state: {} }, incremental_vs_hold: { status: 'mark_missing', incremental_pnl_cny: null, active_fees_cny: null, baseline_fees_cny: null, inventory_shortfall: {}, inventory_value_delta_cny: null, bridge_holds: [] } };
    route({
      'c/forward-books': ok({ books: [] }), 'list-authorized-accounts': accounts, 'c/readmodel-context': contexts,
      'query-opportunities': ok({ opportunity_artifact: REF('opp2'), page: PAGE }),
      'c/artifact': ok({ ref: 'opp2', content_hash: 'x', contract_id: 'c', schema_version: '1', body }),
    });
    renderPage(<BooksPage />);
    await userEvent.click(await screen.findByRole('tab', { name: '机会' }));
    expect((await screen.findAllByText(/缺少估值价格，无法与持有比较/)).length).toBeGreaterThan(0);
    expect(screen.queryByText('增量盈亏')).not.toBeInTheDocument();
  });
});

describe('HealthPage', () => {
  const dataset = { dataset_id: 'quotes', snapshot_id: null, manifest_version: 3, manifest_state: 'ready', ready_snapshot: null, row_count: null, available_watermark: null, coverage: { policy_version: 'c', expected_count: 10, observed_count: 0, missing_count: 10, unverifiable_reasons: [] }, issues: [{ dataset_id: 'quotes', requirement: '日线', reason_code: 'NO_ROWS' }] };
  const job = { job_run_id: 'aaaaaaaa-0000-4000-8000-000000000000', job_id: 'bbbbbbbb-0000-4000-8000-000000000000', job_version: 2, stage_id: null, run_state: 'failed', lease_epoch: 1, attempt_count: 2, scheduled_for: '2026-11-11T00:00:00Z', heartbeat_at: null, completed_at: null, error_code: 'TIMEOUT' };
  const DEF = { job_id: 'bbbbbbbb-0000-4000-8000-000000000000', job_version: 2, application_operation: 'account_readmodel_build', manual_trigger: true, enabled: true, definition_hash: 'c'.repeat(64), parameter_contract_id: 'pulse.job.account_readmodel_build', parameter_schema_version: '3' };
  const DEF2 = { ...DEF, job_id: 'cccccccc-0000-4000-8000-000000000000', application_operation: 'nightly_only', manual_trigger: false };
  it('flags a dataset without data, runs a chosen definition after confirmation and polls the run', async () => {
    const calls = route({
      'query-dataset-status': ok({ datasets: [dataset], page: PAGE }),
      'query-job-runs': ok({ jobs: [job], page: PAGE }),
      'c/job-definitions': ok({ definitions: [DEF, DEF2] }),
      'run-registered-job': ok({ receipt: { job_run_id: 'dddddddd-0000-4000-8000-000000000000', resource_id: 'r', run_state: 'queued', poll_operation_id: 'getJobRun' } }),
      'jobs': ok({}),
    });
    const inner = (globalThis.fetch as unknown as (i: string, n?: RequestInit) => Promise<Response>);
    vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      if (url.pathname.startsWith('/v1/jobs/')) { calls.push({ path: 'jobs', url, init }); return ok({ run_state: 'running', error_code: null }); }
      return inner(input, init);
    }));
    renderPage(<HealthPage />);
    expect(await screen.findByText('尚未收到数据')).toBeInTheDocument();
    expect(calls.find((c) => c.path === 'query-dataset-status')?.url.searchParams.get('dataset_id')).toBe('all');
    expect(screen.getByText('NO_ROWS')).toBeInTheDocument();
    expect(await screen.findByText('TIMEOUT')).toBeInTheDocument();

    const form = await screen.findByRole('form', { name: '运行任务表单' });
    expect(within(form).queryByRole('option', { name: /nightly_only/ })).not.toBeInTheDocument(); // manual_trigger=false is not offered
    await userEvent.selectOptions(within(form).getByLabelText('已登记任务'), 'account_readmodel_build v2');
    await userEvent.click(within(form).getByRole('button', { name: '运行' }));
    const dlg = await screen.findByRole('alertdialog');
    expect(calls.some((c) => c.path === 'run-registered-job')).toBe(false); // nothing sent before confirmation
    await userEvent.click(within(dlg).getByRole('button', { name: '确认运行' }));
    expect(await screen.findByText('运行中')).toBeInTheDocument();
    const post = calls.find((c) => c.path === 'run-registered-job');
    const body = JSON.parse(String(post?.init?.body));
    expect(body).toMatchObject({ job_id: DEF.job_id, job_version: 2, expected_definition_hash: DEF.definition_hash, input_snapshot_refs: [] });
    expect(body.parameter_artifact).toMatchObject({ contract_id: 'pulse.job.account_readmodel_build', schema_version: '3', ref: 'inline-json:e30' });
    expect(body.parameter_artifact.content_hash).toMatch(/^[0-9a-f]{64}$/);
    expect(calls.some((c) => c.path === 'jobs')).toBe(true);
  });

  it('shows 无运行权限 when the run is forbidden', async () => {
    route({
      'query-dataset-status': ok({ datasets: [], page: PAGE }), 'query-job-runs': ok({ jobs: [], page: PAGE }),
      'c/job-definitions': ok({ definitions: [DEF] }), 'run-registered-job': fail(403, 'FORBIDDEN'),
    });
    renderPage(<HealthPage />);
    const form = await screen.findByRole('form', { name: '运行任务表单' });
    await userEvent.selectOptions(within(form).getByLabelText('已登记任务'), 'account_readmodel_build v2');
    await userEvent.click(within(form).getByRole('button', { name: '运行' }));
    await userEvent.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: '确认运行' }));
    expect(await screen.findByText('无运行权限', { selector: 'p' })).toBeInTheDocument();
  });

  it('shows 无查看任务定义权限 when definitions are forbidden', async () => {
    route({ 'query-dataset-status': ok({ datasets: [], page: PAGE }), 'query-job-runs': ok({ jobs: [], page: PAGE }), 'c/job-definitions': fail(403, 'FORBIDDEN') });
    renderPage(<HealthPage />);
    expect(await screen.findByText('无查看任务定义权限')).toBeInTheDocument();
    expect(screen.queryByRole('form', { name: '运行任务表单' })).not.toBeInTheDocument();
  });

  it('shows form validation errors without sending anything', async () => {
    const calls = route({ 'query-dataset-status': ok({ datasets: [], page: PAGE }), 'query-job-runs': ok({ jobs: [], page: PAGE }), 'c/job-definitions': ok({ definitions: [DEF] }) });
    renderPage(<HealthPage />);
    await userEvent.click(await screen.findByRole('button', { name: '运行' }));
    expect(await screen.findByText('请选择要运行的任务')).toBeInTheDocument();
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(calls.some((c) => c.path === 'run-registered-job')).toBe(false);
  });
});
