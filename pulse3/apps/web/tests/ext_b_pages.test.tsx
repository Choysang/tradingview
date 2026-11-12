import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactElement } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ops } from '../src/api/ops';
import { ToastProvider } from '../src/components/Toast';
import { encodeQuery } from '../src/pages/ext/b/api';
import { CATALOG, FIELD_BY_ID, allOf, cellOf, chipsOf, compare, condFromChips, describeCond, emptyRail, makeAst, mergeConds, outcomeSummary, railToCond, showValue } from '../src/pages/ext/b/helpers';
import { LabPage } from '../src/pages/ext/b/LabPage';
import { PlaybooksPage } from '../src/pages/ext/b/PlaybooksPage';
import { ScreenPage } from '../src/pages/ext/b/ScreenPage';

vi.mock('../src/api/ops', () => ({ ops: { listStrategies: vi.fn(), getEvaluation: vi.fn(), getCurrentRelease: vi.fn() } }));

const META = {
  request_id: 'r', generated_at: '2026-11-12T01:00:00Z', trace_id: null, contract_version: '3.0.0', release_id: null, decision_as_of: '2026-11-12T00:00:00Z',
  availability_policy_version: 'p', input_snapshot_refs: [], quality_status: 'partial', freshness_state: 'current',
  coverage: { policy_version: 'screen-run-v1', expected_count: 3214, observed_count: 2, missing_count: 1, unverifiable_reasons: [] }, warnings: [], next_cursor: null, actionability_id: null,
};
const PAGE = { next_cursor: null, snapshot_hash: 'h'.repeat(64) };
const ok = (data: unknown): Response => new Response(JSON.stringify({ meta: META, data }), { status: 200, headers: { 'content-type': 'application/json' } });
const fail = (status: number, code: string): Response => new Response(JSON.stringify({ meta: META, error: { code, message: code, retryable: false, field_errors: [] } }), { status, headers: { 'content-type': 'application/json' } });
const IID = (n: number) => `00000000-0000-0000-0000-00000000000${n}`;

type Handler = (url: URL, init?: RequestInit) => Response | undefined;
function route(handlers: Record<string, Handler | Response>) {
  const calls: Array<{ path: string; url: URL; init?: RequestInit }> = [];
  vi.stubGlobal('fetch', vi.fn(async (input: string | Request, init?: RequestInit) => {
    const url = new URL(typeof input === 'string' ? input : input.url, 'http://localhost');
    const path = url.pathname.replace('/v1/x/', '').replace('/v1/', '');
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

describe('ext-B helpers', () => {
  it('builds AST nodes whose constant unit equals the catalog unit and describes them in Chinese', () => {
    const c = compare('pct_chg', 'gte', 3);
    expect(c.right).toMatchObject({ kind: 'constant', value: '3', unit: FIELD_BY_ID.pct_chg?.unit });
    expect(describeCond(c)).toBe('涨幅 ≥ 3%');
    expect(describeCond(compare('is_st', 'eq', 0))).toBe('非ST');
    expect(() => compare('nope', 'gt', 1)).toThrow(/不在目录/);
  });
  it('rail -> condition: empty rail sets nothing, bounds become gte/lte, boards become an OR', () => {
    expect(railToCond({ ...emptyRail(), excludeSt: false })).toBeNull();
    const r = railToCond({ ...emptyRail(), excludeSt: false, bounds: { volume_ratio_5: { lo: '1.5', hi: '' }, rsi_14: { lo: '', hi: '30' } }, boards: ['main', 'chinext'], patterns: ['ma_bull'] });
    const chips = chipsOf(r!);
    expect(chips.map((x) => x.text)).toEqual(['量比 ≥ 1.5', 'RSI14 ≤ 30', '沪深主板 或 创业板', '均线多头']);
    expect(condFromChips(chips.slice(0, 1))).toEqual(chips[0]!.cond);
    expect(mergeConds(null, null)).toBeNull();
    expect(makeAst(allOf(compare('close', 'gt', 1))).allowed_field_catalog_version).toBe(CATALOG.catalog_version);
  });
  it('stored feature values ride in the artifact ref; missing stays missing, never 0', () => {
    expect(cellOf('screen-value://r/i/pct_chg?value=3.5&unit=pct')).toEqual({ value: '3.5', unit: 'pct', missing: null });
    const miss = cellOf('screen-value://r/i/rsi_14?missing=input_missing_or_history_short');
    expect(miss.value).toBeNull();
    expect(showValue(miss)).toBe('缺失');
    expect(showValue({ value: '12345.6', unit: null, missing: null })).toBe('12346');
  });
  it('empirical outcome summary counts immature outcomes separately and is not a probability', () => {
    const s = outcomeSummary(['0.10', '-0.05', null, '0.02']);
    expect(s).toMatchObject({ known: 3, immature: 1 });
    expect(s.upShare).toBeCloseTo(2 / 3);
    expect(outcomeSummary([null]).upShare).toBeNull();
  });
  it('query encoding: objects as JSON text, undefined omitted', () => {
    expect(encodeQuery({ a: 1, b: undefined, c: { limit: 5 } })).toBe('a=1&c=%7B%22limit%22%3A5%7D');
  });
  it('catalog carries 12 patterns, 6 factors, 5 presets and 52 playbooks', () => {
    expect([CATALOG.patterns.length, CATALOG.factors.length, CATALOG.presets.length, CATALOG.playbooks.length]).toEqual([12, 6, 5, 52]);
  });
});

const CTX = {
  input_snapshot_refs: [{ snapshot_id: IID(9), dataset_id: 'bars_legacy_fwd_adjusted_1d', manifest_version: 1, content_hash: 'c'.repeat(64) }], decision_as_of: '2026-11-12T00:00:00Z',
  universe_policy_versions: ['screen-universe-main-v1', 'screen-universe-v1'], field_catalog_version: 'screen-fields-v1', playbook_instances: [],
  replay: { evaluation_context: null, cost_policy_versions: [], instances: [] },
};
const AST = { condition: compare('limit_streak', 'eq', 1), allowed_field_catalog_version: 'screen-fields-v1', sort: [{ field_id: 'pct_chg', direction: 'desc', nulls: 'last' }] };
const rowFor = (n: number, truth: string, value: string | null) => ({
  instrument_id: IID(n), condition_value: truth, sort_position: n,
  features: [{ feature_id: 'pct_chg', definition_version: 'v1', key_hash: 'k', result: { feature_key_hash: 'k', artifact: { ref: `screen-value://r/i/pct_chg?${value === null ? 'missing=x' : `value=${value}&unit=pct`}`, content_hash: 'h', contract_id: 'c', schema_version: '1' }, quality_status: 'valid', result_state: 'READY' } }],
});

describe('ScreenPage', () => {
  it('parses a sentence into removable chips and flags unmatched fragments before running', async () => {
    const calls = route({
      'b/context': ok(CTX),
      'parse-screen-text': ok({ ast: { ...AST, condition: { node: 'all', clock: {}, children: [compare('limit_streak', 'eq', 1), compare('volume_ratio_5', 'gt', 1.5)] } }, unresolved: [{ source_start: 10, source_end: 17, text: '换手率大于5%', reason_code: 'unsupported' }], complete: false }),
      'list-saved-screens': ok({ screens: [], page: PAGE }),
    });
    renderPage(<ScreenPage />);
    await userEvent.type(screen.getByLabelText('问句'), '首板 量比大于1.5 换手率大于5%');
    await userEvent.click(screen.getByRole('button', { name: '解析' }));
    expect(await screen.findByText('连板数 = 1')).toBeInTheDocument();
    expect(screen.getByText('量比 > 1.5')).toBeInTheDocument();
    const alert = screen.getByRole('alert', { name: '未识别片段' });
    expect(within(alert).getByText('换手率大于5%')).toBeInTheDocument();
    expect(within(alert).getByText(/暂不支持/)).toBeInTheDocument();
    const runBtn = screen.getByRole('button', { name: '运行筛选' });
    await waitFor(() => expect(runBtn).toBeDisabled());               // blocked until the user acknowledges the dropped fragments
    await userEvent.click(screen.getByLabelText(/这些片段不会参与筛选/));
    expect(runBtn).toBeEnabled();
    await userEvent.click(screen.getByRole('button', { name: '移除 量比 > 1.5' }));
    expect(screen.queryByText('量比 > 1.5')).not.toBeInTheDocument();
  });

  it('runs a screen, polls until ready and shows unknown rows as 未知 with 缺失 values (never 0)', async () => {
    let polls = 0;
    const calls = route({
      'b/context': ok(CTX),
      'parse-screen-text': ok({ ast: AST, unresolved: [], complete: true }),
      'list-saved-screens': ok({ screens: [], page: PAGE }),
      'run-screen': ok({ receipt: { job_run_id: IID(5), resource_id: IID(7), run_state: 'queued', poll_operation_id: 'queryScreenRun' } }),
      'query-screen-run': () => (++polls < 2 ? fail(409, 'RESULT_NOT_READY') : ok({ rows: [rowFor(1, 'true', '9.98'), rowFor(2, 'unknown', null)], page: PAGE, capability_gaps: [{ dataset_id: 'x', requirement: 'rsi_14', reason_code: 'field_unknown_for_every_instrument' }] })),
      'b/instrument-labels': ok([{ instrument_id: IID(1), code: '600001', name: '测试一号', is_risk_warning: false }, { instrument_id: IID(2), code: '600002', name: '测试二号', is_risk_warning: true }]),
    });
    renderPage(<ScreenPage />);
    await userEvent.click(screen.getByRole('button', { name: '首板加速' }));
    await screen.findByText('连板数 = 1');
    await userEvent.click(screen.getByRole('button', { name: '运行筛选' }));
    expect(await screen.findByText('满足 2', {}, { timeout: 8000 })).toBeInTheDocument();
    expect(screen.getByText('未知 1')).toBeInTheDocument();
    const table = await screen.findByRole('table');
    expect(within(table).getByText('600001')).toBeInTheDocument();
    expect(within(table).getByText('9.98')).toBeInTheDocument();
    const row2 = within(table).getByText('600002').closest('tr')!;
    expect(within(row2).getByText('未知')).toBeInTheDocument();
    expect(within(row2).getByText('缺失')).toBeInTheDocument();
    expect(screen.getByText(/无法评估的部分/)).toBeInTheDocument();
    const post = calls.find((c) => c.path === 'run-screen')!;
    const body = JSON.parse(String(post.init?.body));
    expect((post.init?.headers as Record<string, string>)['Idempotency-Key']).toBe(body.idempotency_key);   // header and body key agree
    expect(body.input_snapshot_refs[0].dataset_id).toBe('bars_legacy_fwd_adjusted_1d');
    expect(screen.getByRole('link', { name: /导出 CSV/ }).getAttribute('href')).toContain('screen-run-csv');
  }, 15000);

  it('a failed run is an error state, not an empty list', async () => {
    route({
      'b/context': ok(CTX), 'parse-screen-text': ok({ ast: AST, unresolved: [], complete: true }), 'list-saved-screens': ok({ screens: [], page: PAGE }),
      'run-screen': ok({ receipt: { job_run_id: IID(5), resource_id: IID(7), run_state: 'queued', poll_operation_id: 'queryScreenRun' } }),
      'query-screen-run': fail(422, 'SCREEN_RUN_FAILED'),
    });
    renderPage(<ScreenPage />);
    await userEvent.click(screen.getByRole('button', { name: '首板加速' }));
    await screen.findByText('连板数 = 1');
    await userEvent.click(screen.getByRole('button', { name: '运行筛选' }));
    expect(await screen.findByText(/筛选结果读取失败/)).toBeInTheDocument();
    expect(screen.queryByText(/没有满足条件/)).not.toBeInTheDocument();
  });
});

describe('PlaybooksPage', () => {
  it('lists the 52 playbooks as 未检验, filters by data status and shows failed experiments as failures', async () => {
    const pb = CATALOG.playbooks.find((p) => p.id === 'value_pe_pb')!;
    const strategies = CATALOG.playbooks.map((p) => ({ strategy_id: IID(1), strategy_version: 1, display_name: p.name, role: 'selection', method_family: 'rule', forecast_target: 't', horizon_id: '20td', definition_hash: 'h'.repeat(64), specification: { playbook: { id: p.id } }, tags: [], instances: [{ instance_id: IID(2), strategy_id: IID(1), strategy_version: 1, parameter_hash: 'p', universe_policy_version: 'u', lifecycle: 'specified', implementation_status: 'implemented', reproduction_status: 'not_started', health: 'healthy' }] }));
    route({ 'query-strategy-version': ok({ strategy: strategies[0], evaluation_ids: [IID(8)] }) });
    vi.mocked(ops.listStrategies).mockResolvedValue({ meta: META, data: { items: strategies } } as never);
    vi.mocked(ops.getEvaluation).mockResolvedValue({ meta: META, data: { experiment_id: IID(8), instance_id: IID(2), evaluation_context_id: IID(3), context_hash: 'c'.repeat(64), hypothesis_id: IID(4), hypothesis_version: 1, run_purpose: 'screening', run_state: 'failed', policy_versions: {}, started_at: null, completed_at: null, result: null, metrics: [], error_code: 'data_not_available:fundamentals_pit', context: {} } } as never);
    renderPage(<PlaybooksPage />);
    expect(await screen.findByText(pb.name)).toBeInTheDocument();
    expect(screen.getAllByText('未检验').length).toBeGreaterThanOrEqual(52);
    await userEvent.selectOptions(screen.getByLabelText('数据'), 'missing');
    expect(screen.getAllByRole('row').length - 1).toBe(CATALOG.playbooks.filter((p) => p.requires.length).length);
    await userEvent.click(screen.getByRole('button', { name: pb.name }));
    expect(await screen.findByText('data_not_available:fundamentals_pit')).toBeInTheDocument();
    expect(screen.getByText(/· 失败/)).toBeInTheDocument();
  });
});

describe('LabPage', () => {
  it('pattern radar shows 12 rules, honest zero counts and the omitted conditions', async () => {
    route({
      'b/context': ok(CTX),
      'run-screen': ok({ receipt: { job_run_id: IID(5), resource_id: IID(7), run_state: 'queued', poll_operation_id: 'queryScreenRun' } }),
      'query-screen-run': ok({ rows: [], page: PAGE, capability_gaps: [] }),
    });
    renderPage(<LabPage />);
    expect(await screen.findByText('停机坪')).toBeInTheDocument();
    expect(CATALOG.patterns.every((p) => screen.queryAllByText(p.name).length > 0)).toBe(true);
    await waitFor(() => expect(screen.getAllByText('2 只').length).toBe(12));
    expect(screen.getAllByText(/另有 1 只历史不足/).length).toBe(12);
    expect(screen.getAllByText(/省略：/).length).toBeGreaterThan(0);
  });
});
