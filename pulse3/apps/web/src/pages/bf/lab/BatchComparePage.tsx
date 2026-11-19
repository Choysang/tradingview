/**
 * 批量对比 (triage #36 #77): one playbook replayed on the top rows of a screen run, as a queued job. Each stock is replayed ALONE with the whole account,
 * through the same execution kernel and cost profile as the playbook experiments, on data visible at the screen run's cutoff. The result table shows the
 * tear-sheet columns; a stock that could not be replayed says why. The page warns that the stocks were picked with data up to the cutoff, so this is not
 * an out-of-sample test. ``?screen_run=<uuid>`` pre-fills the form; ``?run=<job_run_id>`` opens a finished run.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Badge, MissingText } from '../../../components/Badges';
import { EmptyState, ErrorState, Loading } from '../../../components/States';
import { useToast } from '../../../components/Toast';
import { fmtTime } from '../../../lib/format';
import { CATALOG } from '../../ext/b/helpers';
import type { BatchResult, BatchRow, BatchRunBrief, MetricName } from './api';
import { bfLab } from './api';
import { METRIC_META, metricText, RUN_STATE_TEXT, reasonText, signClass, skipText } from './format';
import { TearSheetView } from './TearSheetCard';
import './lab.css';

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
/** Columns of the result table: the tear-sheet figures that decide whether a rule is worth a second look. */
export const TABLE_COLUMNS: MetricName[] = ['total_return', 'max_drawdown', 'sharpe', 'sortino', 'calmar', 'profit_factor', 'win_rate', 'payoff_ratio', 'exposure', 'longest_drawdown_sessions'];
const PLAYBOOKS = CATALOG.playbooks.filter((p) => p.requires.length === 0);

export function BatchComparePage() {
  const [params, setParams] = useSearchParams();
  const runId = params.get('run');
  const open = (id: string | null) => { const n = new URLSearchParams(params); if (id) n.set('run', id); else n.delete('run'); setParams(n, { replace: false }); };
  return (
    <section className="bfl batch">
      <h2>批量对比</h2>
      <p className="muted small">选一次筛选运行和一个战法：把这个战法分别回放在筛选结果的前 10 只股票上（只取条件为「满足」的行）。回放只用筛选运行决策时点已可见的数据。</p>
      <BatchForm initialScreen={params.get('screen_run') ?? ''} onStarted={open} />
      {runId && <BatchRunView id={runId} />}
      <Archive current={runId} onOpen={open} />
    </section>
  );
}

function BatchForm({ initialScreen, onStarted }: { initialScreen: string; onStarted: (id: string) => void }) {
  const [screen, setScreen] = useState(initialScreen);
  const [playbook, setPlaybook] = useState(PLAYBOOKS[0]?.id ?? '');
  const [topN, setTopN] = useState(10);
  const toast = useToast();
  const qc = useQueryClient();
  const valid = UUID_RE.test(screen.trim()) && !!playbook && topN >= 1 && topN <= 10;
  const start = useMutation({
    mutationFn: () => bfLab.startBatch({ screen_run_id: screen.trim().toLowerCase(), playbook_id: playbook, top_n: topN }),
    onSuccess: (r) => { void qc.invalidateQueries({ queryKey: ['bf-lab', 'runs'] }); onStarted(r.data.receipt.job_run_id); },
    onError: (e) => toast.push('bad', e instanceof Error ? e.message : '提交失败'),
  });
  return (
    <form className="form" onSubmit={(e) => { e.preventDefault(); if (valid) start.mutate(); }} aria-label="批量对比表单">
      <label>筛选运行（screen_run_id）<input value={screen} onChange={(e) => setScreen(e.target.value)} placeholder="36 位 UUID" aria-invalid={screen !== '' && !UUID_RE.test(screen.trim())} /></label>
      <label>战法
        <select value={playbook} onChange={(e) => setPlaybook(e.target.value)}>{PLAYBOOKS.map((p) => <option key={p.id} value={p.id}>{p.name}（{p.id}）</option>)}</select>
      </label>
      <label>取前几只（最多 10）<input type="number" min={1} max={10} value={topN} onChange={(e) => setTopN(Math.max(1, Math.min(10, Number(e.target.value) || 1)))} /></label>
      <button className="btn btn-on" type="submit" disabled={!valid || start.isPending}>{start.isPending ? '提交中…' : '开始对比'}</button>
    </form>
  );
}

function BatchRunView({ id }: { id: string }) {
  const q = useQuery({
    queryKey: ['bf-lab', 'run', id], queryFn: ({ signal }) => bfLab.batchRun(id, signal), retry: false,
    refetchInterval: (query) => { const s = query.state.data?.data.run.run_state; return s === 'queued' || s === 'running' ? 2000 : false; },
  });
  if (q.isLoading) return <Loading label="读取对比结果…" />;
  if (q.isError) return <ErrorState error={q.error} what="对比结果" onRetry={() => q.refetch()} />;
  const { run, result } = q.data!.data;
  return (
    <div className="batch-run" aria-label="对比结果">
      <p className="small"><RunBadge run={run} /> 战法 {run.request.playbook_id} · 筛选 <code>{run.request.screen_run_id.slice(0, 8)}</code> · 前 {run.request.top_n} 只 · 排队于 {fmtTime(run.scheduled_for)}</p>
      {(run.run_state === 'queued' || run.run_state === 'running') && <Loading label="正在回放…" />}
      {run.run_state === 'failed' && <div className="state state-error" role="alert"><b>对比失败</b><span className="small">原因：{run.error_code ?? '未知'}。没有保存任何结果。</span></div>}
      {result && <ResultTable result={result} />}
    </div>
  );
}

export function RunBadge({ run }: { run: BatchRunBrief }) {
  const tone = run.run_state === 'succeeded' ? 'good' : run.run_state === 'failed' ? 'bad' : 'neutral';
  return <Badge tone={tone} title={`run_state = ${run.run_state}`}>{RUN_STATE_TEXT[run.run_state] ?? run.run_state}</Badge>;
}

export function ResultTable({ result }: { result: BatchResult }) {
  const [detail, setDetail] = useState<string | null>(null);
  const shown = result.rows.find((r) => r.instrument_id === detail && r.tearsheet);
  return (
    <div className="result">
      <p className="muted small">
        {result.playbook.name} · {result.window.start} ~ {result.window.end}（{result.window.sessions} 个交易日）· 决策时点 {fmtTime(result.screen.decision_as_of)} · 费用口径 {result.cost_policy_version} · 条件为真共 {result.screen.true_rows} 行，取 {result.screen.selected} 只
      </p>
      {!result.screen.panel_matches_screen && <Badge tone="warn" title="决策时点可见的研究快照与筛选运行当时用的不是同一份">研究快照与筛选时不同</Badge>}
      <div className="scroll">
        <table className="table dense batch-table">
          <thead><tr><th>#</th><th>股票</th><th>状态</th>{TABLE_COLUMNS.map((c) => <th key={c} title={METRIC_META[c].hint}>{METRIC_META[c].label}</th>)}<th /></tr></thead>
          <tbody>{result.rows.map((r) => <Row key={r.instrument_id} r={r} onDetail={setDetail} />)}</tbody>
        </table>
      </div>
      {shown?.tearsheet && (
        <TearSheetView data={{
          subject: { kind: 'replay', id: shown.instrument_id, label: `${shown.code ?? shown.instrument_id.slice(0, 8)} ${shown.name ?? ''}`.trim(), catalog_id: result.playbook.catalog_id, experiment_id: null },
          window: { start: result.window.start, end: result.window.end, sessions: result.window.sessions }, basis: { initial_nav: result.initial_cash, trade_pnl: '成交价之差 × 数量（毛）', nav_basis: `单只回放的账户净值，${result.price_basis}` },
          tearsheet: shown.tearsheet, caveats: [],
        }} />
      )}
      <ul className="muted small plain">{result.caveats.map((c) => <li key={c}>{c}</li>)}</ul>
    </div>
  );
}

function Row({ r, onDetail }: { r: BatchRow; onDetail: (id: string) => void }) {
  const status = r.status === 'ok' ? '有成交' : r.status === 'no_trades' ? '没有触发' : '未回放';
  return (
    <tr className={r.status === 'skipped' ? 'skipped' : undefined}>
      <td>{r.sort_position}</td>
      <td>{r.code ?? r.instrument_id.slice(0, 8)}{r.name && <div className="muted small">{r.name}</div>}</td>
      <td>{status}{r.skip_reason && <div className="small">{skipText(r.skip_reason)}</div>}</td>
      {TABLE_COLUMNS.map((c) => {
        const m = r.tearsheet?.metrics[c];
        return (
          <td key={c} className={`num ${m ? signClass(c, m) : ''}`} title={m?.value == null && m ? reasonText(m.reason) : undefined}>
            {m == null || m.value == null ? <MissingText>—</MissingText> : metricText(c, m)}
          </td>
        );
      })}
      <td>{r.tearsheet && <button className="link-btn" onClick={() => onDetail(r.instrument_id)}>详情</button>}</td>
    </tr>
  );
}

function Archive({ current, onOpen }: { current: string | null; onOpen: (id: string) => void }) {
  const q = useQuery({ queryKey: ['bf-lab', 'runs'], queryFn: ({ signal }) => bfLab.batchRuns(20, null, signal), retry: false, refetchInterval: 15_000 });
  return (
    <section className="archive" aria-label="对比档案">
      <h3>对比档案</h3>
      {q.isLoading && <Loading />}
      {q.isError && <ErrorState error={q.error} what="对比档案" onRetry={() => q.refetch()} />}
      {q.isSuccess && q.data.data.items.length === 0 && <EmptyState>还没有对比运行。</EmptyState>}
      {q.isSuccess && q.data.data.items.length > 0 && (
        <table className="table dense">
          <caption className="small muted">{q.data.data.order}</caption>
          <thead><tr><th>时间</th><th>战法</th><th>筛选</th><th>状态</th><th>结果</th><th /></tr></thead>
          <tbody>
            {q.data.data.items.map((r) => (
              <tr key={r.job_run_id} className={r.job_run_id === current ? 'current' : undefined}>
                <td>{fmtTime(r.scheduled_for)}</td><td>{r.request.playbook_id}</td><td><code>{r.request.screen_run_id.slice(0, 8)}</code></td>
                <td><RunBadge run={r} />{r.error_code && <div className="small">{r.error_code}</div>}</td>
                <td>{r.summary ? `${r.summary.instruments} 只，${r.summary.with_trades} 只有成交${r.summary.skipped ? `，${r.summary.skipped} 只未回放` : ''}` : '—'}</td>
                <td><button className="link-btn" onClick={() => onOpen(r.job_run_id)}>查看</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
