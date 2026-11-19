/** Fill-research lane web checks: the board cells show fitness / verdict / out-of-sample average / sparkline and say so honestly when a playbook has no replay or no
 * recorded split; the custom run sends only what changed with an Idempotency-Key and shows 排队中 until the worker ran; the monthly heatmap hatches a missing month;
 * the factor series and the analog tab queue a job and show its state. Fixtures pass the Python contract test (tests/bf_fillr/test_web_fixtures.py). */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import type { ReactElement } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ToastProvider } from '../src/components/Toast';
import { CATALOG } from '../src/pages/ext/b/helpers';
import type { AnalogStatus, CustomRun, CustomRunList, FactorSeriesStatus, MonthlyReturns, PlaybookBoard } from '../src/pages/bf/fillr/api';
import { AnalogBody, AnalogTab } from '../src/pages/bf/fillr/AnalogTab';
import { BoardCells, BoardDetail } from '../src/pages/bf/fillr/BoardCells';
import { CustomRunPanel, formFor, overridesOf, RunResult } from '../src/pages/bf/fillr/CustomRunForm';
import { FactorSeriesPanel, FactorSeriesView } from '../src/pages/bf/fillr/FactorSeriesPanel';
import { monthAlpha, monthGrid, ringDash, sparkPoints, verdictLabel } from '../src/pages/bf/fillr/format';
import { MonthlyHeatmapView } from '../src/pages/bf/fillr/MonthlyHeatmap';

const fx = <T,>(name: string): T => JSON.parse(readFileSync(resolve(__dirname, `../src/pages/bf/fillr/fixtures/${name}.json`), 'utf-8')) as T;
const BOARD = fx<PlaybookBoard>('board');
const META = {
  request_id: 'r', generated_at: '2026-11-18T01:00:00Z', trace_id: null, contract_version: '3.0.0', release_id: null, decision_as_of: null, availability_policy_version: null,
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
    const h = handlers[`${call.method} ${url.pathname.replace('/v1/x/bf/fillr/', '')}`];
    const res = typeof h === 'function' ? h(call) : h;
    return res ? res.clone() : fail(404, 'NOT_FOUND');
  }));
  return calls;
}
function renderAt(el: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><ToastProvider><MemoryRouter>{el}</MemoryRouter></ToastProvider></QueryClientProvider>);
}
const inRow = (el: ReactElement) => render(<table><tbody><tr>{el}</tr></tbody></table>);
afterEach(() => { vi.unstubAllGlobals(); });

// ---- helpers ----------------------------------------------------------------------------------------------------------------------------
describe('format helpers', () => {
  it('draws the ring and the sparkline only from real values', () => {
    expect(ringDash(null)).toBeNull();
    const r = ringDash('50')!;
    expect(r.filled / r.circumference).toBeCloseTo(0.5, 6);
    expect(ringDash('150')!.filled).toBeCloseTo(ringDash('100')!.filled, 6);
    expect(sparkPoints(['1'])).toBeNull();
    expect(sparkPoints([])).toBeNull();
    expect(sparkPoints(['1', '3', '2'])!.split(' ')).toHaveLength(3);
    expect(sparkPoints(['2', '2'])).not.toBeNull();                                             // a flat series is still a line
  });
  it('builds a year x month grid with holes for months outside the replay', () => {
    const g = monthGrid(fx<MonthlyReturns>('monthly').months);
    expect(g.map((r) => r.year)).toEqual([2026, 2025]);
    expect(g[0]!.cells[0]!.value).toBe('-0.034500000000');
    expect(g[0]!.cells[5]).toBeNull();
    expect(g[1]!.cells[11]!.value).toBe('0.021000000000');
    expect(monthAlpha(null)).toBe(0);
    expect(monthAlpha('0.2')).toBeCloseTo(0.55, 6);
  });
  it('names the verdict states, including no verdict without a split', () => {
    const [turtle, kdj, unavailable, notRun, failed] = BOARD.items;
    expect(verdictLabel(turtle!)).toBe('有效');
    expect(verdictLabel(kdj!)).toBe('仅样本内');
    expect(verdictLabel(unavailable!)).toBe('缺数据');
    expect(verdictLabel(notRun!)).toBe('未回测');
    expect(verdictLabel(failed!)).toBe('失败');
  });
});

// ---- board cells ------------------------------------------------------------------------------------------------------------------------
describe('board cells', () => {
  it('shows fitness, verdict, out-of-sample average and a sparkline for an evaluated playbook', () => {
    inRow(<BoardCells item={BOARD.items[0]} />);
    expect(screen.getByRole('img', { name: '适配度 69 / 100' })).toBeInTheDocument();           // 68.5 rounds half away on toFixed(0)
    expect(screen.getByText('有效')).toBeInTheDocument();
    expect(screen.getByText('1.10%')).toBeInTheDocument();
    expect(screen.getByText(/23笔/)).toBeInTheDocument();
    expect(screen.getByRole('img', { name: '海龟55日突破 净值走势' })).toBeInTheDocument();
    expect(screen.getByText(/块自助仅供参考/)).toBeInTheDocument();
  });
  it('labels a playbook without a recorded split as sample-only and gives no verdict', () => {
    inRow(<BoardCells item={BOARD.items[1]} />);
    expect(screen.getAllByText('仅样本内').length).toBeGreaterThan(0);
    expect(screen.getByText('无样本外')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: '适配度缺失' })).toBeInTheDocument();
    expect(screen.queryByText('有效')).toBeNull();
  });
  it('says why a playbook has no figures instead of scoring it', () => {
    inRow(<BoardCells item={BOARD.items[2]} />);
    expect(screen.getByText('缺数据')).toBeInTheDocument();
    expect(screen.getByText(/fundamentals_pit/)).toBeInTheDocument();
  });
  it('shows not-run and failed states', () => {
    inRow(<BoardCells item={BOARD.items[3]} />);
    expect(screen.getByText('未回测')).toBeInTheDocument();
    expect(screen.getByText(/ext-b-run/)).toBeInTheDocument();
  });
  it('lists the sample table and every fitness component in the detail', () => {
    render(<BoardDetail item={BOARD.items[0]} />);
    expect(screen.getByText('样本外')).toBeInTheDocument();
    expect(screen.getByText(/2026-05-06 起/)).toBeInTheDocument();
    expect(screen.getByText(/块自助（块长 20 个交易日，6 块）/)).toBeInTheDocument();
    expect(screen.getByText(/构成/)).toBeInTheDocument();
  });
  it('shows missing samples as 缺失 with a reason when there is no split', () => {
    render(<BoardDetail item={BOARD.items[1]} />);
    expect(screen.getByText('实验没有记录样本外切分')).toBeInTheDocument();
  });
});

// ---- custom run -------------------------------------------------------------------------------------------------------------------------
describe('custom run', () => {
  it('sends only what changed; a cleared stop becomes null and a set target is sent', () => {
    const pb = CATALOG.playbooks.find((p) => p.requires.length === 0 && p.stop_loss_pct != null)!;
    const base = formFor(pb);
    expect(overridesOf(pb, base)).toEqual({});
    expect(overridesOf(pb, { ...base, holding: String(pb.holding_days + 1), stop: '', take: '12.5', maxNew: '2' })).toEqual({ holding_days: pb.holding_days + 1, stop_loss_pct: null, take_profit_pct: 12.5, max_new_per_day: 2 });
    const first = pb.params[0];
    if (first) expect(overridesOf(pb, { ...base, params: { ...base.params, [first.key]: String(first.value + 1) } })).toEqual({ params: { [first.key]: first.value + 1 } });
  });

  it('registers a draft with an Idempotency-Key, then shows 排队中 with the worker hint until a worker ran', async () => {
    const queued = fx<CustomRun>('custom-run-queued');
    const calls = route({
      'GET custom-runs': ok(fx<CustomRunList>('custom-run-list')),
      'POST custom-run': ok(fx<unknown>('custom-receipt'), 202),
      [`GET custom-runs/${queued.run.job_run_id}`]: ok(queued),
    });
    renderAt(<CustomRunPanel />);
    const holding = await screen.findByLabelText('持有天数');
    await userEvent.clear(holding);
    await userEvent.type(holding, '4');
    await userEvent.click(screen.getByRole('button', { name: '登记草案并排队回放' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true));
    const post = calls.find((c) => c.method === 'POST')!;
    expect(post.headers.get('Idempotency-Key')).toBeTruthy();
    expect((post.body as { idempotency_key: string }).idempotency_key).toBe(post.headers.get('Idempotency-Key'));
    expect((post.body as { overrides: unknown }).overrides).toEqual({ holding_days: 4 });
    const hint = await screen.findByText('uv run python -m pulse.workers.fillr run');
    const note = hint.closest('.queued-note') as HTMLElement;
    expect(within(note).getByText('排队中')).toBeInTheDocument();
    expect(screen.queryByText('探索性')).toBeNull();                                            // no result is invented while queued
  });

  it('shows the finished result as exploratory with rules vs base, the verdict and the trades', () => {
    render(<RunResult data={fx<CustomRun>('custom-run-done')} />);
    expect(screen.getByText('探索性')).toBeInTheDocument();
    expect(screen.getByText(/改后：持有 3 日/)).toBeInTheDocument();
    expect(screen.getByText(/原版：持有 60 日/)).toBeInTheDocument();
    expect(screen.getByText('8.32%')).toBeInTheDocument();
    expect(screen.getByText('持有到期')).toBeInTheDocument();
    expect(screen.getAllByText('持仓中').length).toBeGreaterThan(0);
    expect(screen.getAllByText('缺失').length).toBeGreaterThan(0);                              // the missing benchmark excess is 缺失, not 0
  });

  it('shows a failed run with its error code', () => {
    const d = fx<CustomRun>('custom-run-done');
    render(<RunResult data={{ ...d, result: null, run: { ...d.run, run_state: 'failed', error_code: 'NO_SEALED_CONTEXT', summary: null } }} />);
    expect(screen.getByRole('alert')).toHaveTextContent('NO_SEALED_CONTEXT');
  });
});

// ---- monthly heatmap --------------------------------------------------------------------------------------------------------------------
describe('monthly heatmap', () => {
  it('draws each month, hatches a month without NAV with its reason and never shows 0', () => {
    render(<MonthlyHeatmapView data={fx<MonthlyReturns>('monthly')} />);
    expect(screen.getByText('2.1%')).toBeInTheDocument();
    expect(screen.getByText('-3.5%')).toBeInTheDocument();
    const missing = screen.getByText('缺失');
    expect(missing).toHaveAttribute('title', expect.stringContaining('no_nav_in_month'));
    expect(screen.queryByText('0.0%')).toBeNull();
    expect(screen.getByRole('columnheader', { name: '12月' })).toBeInTheDocument();
  });
});

// ---- factor series ----------------------------------------------------------------------------------------------------------------------
describe('factor series', () => {
  it('queues the computation from the not-computed state and then shows 排队中', async () => {
    let state: FactorSeriesStatus = fx('factor-not-computed');
    const calls = route({
      'GET factor-series': () => ok(state),
      'POST factor-series': () => { state = fx('factor-queued'); return ok(fx('job-queued'), 202); },
    });
    renderAt(<FactorSeriesPanel factorId="FL:momentum_20" />);
    await userEvent.click(await screen.findByRole('button', { name: '生成逐日序列' }));
    await waitFor(() => expect(screen.getByText('排队中')).toBeInTheDocument());
    const post = calls.find((c) => c.method === 'POST')!;
    expect(post.headers.get('Idempotency-Key')).toBeTruthy();
    expect(post.body).toMatchObject({ experiment_id: '66666666-6666-4666-8666-666666666666' });
    expect(screen.getByText('uv run python -m pulse.workers.fillr run')).toBeInTheDocument();
  });

  it('draws the IC and the cumulative long-short chart per horizon and slice, with gaps for missing IC', () => {
    render(<FactorSeriesView data={fx<FactorSeriesStatus>('factor-ready')} />);
    expect(screen.getByRole('img', { name: /未来 5 日 · 训练 逐日 IC，2025-03-03 至 2025-03-06/ })).toBeInTheDocument();
    expect(screen.getByRole('img', { name: /多空累计曲线，终值 3.00%/ })).toBeInTheDocument();
    expect(screen.getByText(/IC 均值 0.0200/)).toBeInTheDocument();
    const ic = screen.getByRole('img', { name: /逐日 IC/ }).querySelector('path.ic')!;
    expect((ic.getAttribute('d') ?? '').match(/M/g)).toHaveLength(2);                          // the null IC on 03-04 breaks the line into two strokes
    expect(screen.getAllByText('没有 IC 日期').length).toBe(1);                                 // the empty validation slice says so
    expect(screen.getByText(/没有多空曲线/)).toBeInTheDocument();
  });

  it('says there is nothing to compute without a finished experiment', () => {
    render(<FactorSeriesView data={{ ...fx<FactorSeriesStatus>('factor-not-computed'), state: 'no_experiment', experiment_id: null, reason: 'no_completed_factor_experiment' }} />);
    expect(screen.getByText(/还没有已完成的因子实验/)).toBeInTheDocument();
  });
});

// ---- analog tab -------------------------------------------------------------------------------------------------------------------------
describe('analog tab', () => {
  it('offers 生成 when the study is not built and shows 排队中 after queueing', async () => {
    let status: AnalogStatus = fx('analog-not-built');
    const calls = route({
      'GET analog-study': () => ok(status),
      'POST analog-study': () => { status = fx('analog-queued'); return ok(fx('job-queued'), 202); },
    });
    renderAt(<AnalogTab instrumentId="88888888-8888-4888-8888-888888888888" releaseId="22222222-2222-4222-8222-222222222222" />);
    await userEvent.click(await screen.findByRole('button', { name: '生成相似情景' }));
    await waitFor(() => expect(screen.getByText('排队中')).toBeInTheDocument());
    const post = calls.find((c) => c.method === 'POST')!;
    expect(post.headers.get('Idempotency-Key')).toBeTruthy();
    expect(post.body).toMatchObject({ instrument_id: '88888888-8888-4888-8888-888888888888', release_id: '22222222-2222-4222-8222-222222222222', selection_policy_version: 'analog-knn-matured-v1:h20' });
    expect(screen.queryByRole('button', { name: '生成相似情景' })).toBeNull();                 // one job per study: no second button while queued
  });

  it('does not ask the server without a release', () => {
    const calls = route({});
    renderAt(<AnalogTab instrumentId="88888888-8888-4888-8888-888888888888" releaseId={null} />);
    expect(screen.getByText(/没有选定的发布/)).toBeInTheDocument();
    expect(calls).toHaveLength(0);
  });

  it('shows the failure code and lets the user retry', async () => {
    const onQueue = vi.fn();
    const failed: AnalogStatus = { ...fx<AnalogStatus>('analog-not-built'), state: 'failed', job: { job_run_id: 'x', run_state: 'failed', error_code: 'UNSUPPORTED_CONTEXT_SHAPE' } };
    render(<AnalogBody status={failed} h={20} onQueue={onQueue} />);
    expect(screen.getByText(/UNSUPPORTED_CONTEXT_SHAPE/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: '生成相似情景' }));
    expect(onQueue).toHaveBeenCalledTimes(1);
  });
});
