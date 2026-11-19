/**
 * Custom run (triage #21 #87): change a playbook's parameters, exits and sizing, register the variant as a DRAFT strategy and queue its replay.
 * Why: the old site ran a throw-away backtest on the spot; pulse3 records the variant (immutable draft, lineage to the base playbook, status 未检验) and
 * replays it through the same evaluator and sealed context as every experiment, in a background job. The page therefore shows 排队中 honestly until the
 * worker `pulse.workers.fillr run` ran, then the result: rules vs base, the board figures (verdict / fitness, out-of-sample), net return and trades.
 * The result is EXPLORATORY: picking the best of many tries overstates the real edge, and it never changes a playbook's evidence status.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { Badge, MissingText } from '../../../components/Badges';
import { EmptyState, ErrorState, Loading } from '../../../components/States';
import { useToast } from '../../../components/Toast';
import { explain } from '../../ext/b/api';
import { CATALOG, type PlaybookDef } from '../../ext/b/helpers';
import type { CustomRun, CustomRunBrief, CustomTrade, Overrides } from './api';
import { bfFillr } from './api';
import { EvaluationDetail, Sparkline } from './BoardCells';
import { EXIT_TEXT, numText, pctText, runStateText, signClass } from './format';
import './fillr.css';

type Form = { playbook: string; label: string; holding: string; stop: string; take: string; trail: string; maxPos: string; maxNew: string; params: Record<string, string> };

const str = (v: number | null | undefined): string => (v == null ? '' : String(v));
export function formFor(p: PlaybookDef): Form {
  return { playbook: p.id, label: '', holding: str(p.holding_days), stop: str(p.stop_loss_pct), take: '', trail: str(p.trailing_stop_pct), maxPos: str(p.max_positions), maxNew: '',
    params: Object.fromEntries(p.params.map((x) => [x.key, String(x.value)])) };
}

/** Only what differs from the base playbook is sent; a cleared stop / target becomes null (switched off), an empty sizing field is left to the base. */
export function overridesOf(p: PlaybookDef, f: Form): Overrides {
  const base = formFor(p);
  const ov: Overrides = {};
  const num = (s: string): number => Number(s);
  if (f.holding !== base.holding && f.holding.trim() !== '') ov.holding_days = num(f.holding);
  for (const [key, field, original] of [['stop_loss_pct', f.stop, base.stop], ['take_profit_pct', f.take, base.take], ['trailing_stop_pct', f.trail, base.trail]] as const) {
    if (field === original) continue;
    ov[key] = field.trim() === '' ? null : num(field);
  }
  if (f.maxPos !== base.maxPos && f.maxPos.trim() !== '') ov.max_positions = num(f.maxPos);
  if (f.maxNew.trim() !== '') ov.max_new_per_day = num(f.maxNew);
  const params: Record<string, number> = {};
  for (const [k, v] of Object.entries(f.params)) if (v !== base.params[k] && v.trim() !== '') params[k] = num(v);
  if (Object.keys(params).length) ov.params = params;
  return ov;
}

export function CustomRunPanel() {
  const toast = useToast();
  const qc = useQueryClient();
  const usable = useMemo(() => CATALOG.playbooks.filter((p) => p.requires.every((r) => r === 'float_cap')), []);
  const [form, setForm] = useState<Form>(() => formFor(usable[0] as PlaybookDef));
  const [active, setActive] = useState<string | null>(null);
  const pb = (usable.find((p) => p.id === form.playbook) ?? usable[0]) as PlaybookDef;
  const ov = overridesOf(pb, form);
  const submit = useMutation({
    mutationFn: () => bfFillr.startCustomRun({ playbook_id: pb.id, overrides: ov, ...(form.label.trim() ? { label: form.label.trim() } : {}) }),
    onSuccess: (r) => { setActive(r.data.receipt.job_run_id); void qc.invalidateQueries({ queryKey: ['bffr', 'runs'] }); toast.push('good', '已登记草案并排队，等待后台任务运行'); },
    onError: (e) => toast.push('bad', `提交失败：${explain(e)}`),
  });
  const set = (patch: Partial<Form>): void => setForm({ ...form, ...patch });
  return (
    <section className="bffr custom-run" aria-label="自定义回测">
      <p className="muted small">选一个战法，改它的参数、卖法或仓位：服务器把这个变体登记为「草案」（不可改的定义，状态「未检验」，记录来源战法），再按与实验相同的评价上下文和用户费用口径排队回放。结果是探索性的，不计入研究联赛，也不会改变战法的证据状态；多次调参后挑最好的一次会高估真实表现。</p>
      <div className="form-grid">
        <label>战法<select value={pb.id} onChange={(e) => { const p = usable.find((x) => x.id === e.target.value); if (p) setForm(formFor(p)); }}>
          {usable.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
        <label>草案名称（可空）<input value={form.label} maxLength={80} onChange={(e) => set({ label: e.target.value })} placeholder={`${pb.name}（自定义）`} /></label>
        <label>持有天数<input inputMode="numeric" value={form.holding} onChange={(e) => set({ holding: e.target.value })} /></label>
        <label>止损 %（空 = 关闭）<input inputMode="decimal" value={form.stop} onChange={(e) => set({ stop: e.target.value })} /></label>
        <label>止盈 %（空 = 关闭）<input inputMode="decimal" value={form.take} onChange={(e) => set({ take: e.target.value })} /></label>
        <label>追踪止损 %（空 = 关闭）<input inputMode="decimal" value={form.trail} onChange={(e) => set({ trail: e.target.value })} /></label>
        <label>最多持仓只数<input inputMode="numeric" value={form.maxPos} onChange={(e) => set({ maxPos: e.target.value })} /></label>
        <label>每日最多新开（空 = 战法默认）<input inputMode="numeric" value={form.maxNew} onChange={(e) => set({ maxNew: e.target.value })} /></label>
        {pb.params.map((x) => (
          <label key={x.key}>{x.label}<input inputMode="decimal" value={form.params[x.key] ?? ''} onChange={(e) => set({ params: { ...form.params, [x.key]: e.target.value } })} /></label>
        ))}
      </div>
      <p className="muted small">将改动：{Object.keys(ov).length === 0 ? '无（与战法默认一致，结果应与实验相同）' : JSON.stringify(ov)}</p>
      <button className="btn btn-on" disabled={submit.isPending} onClick={() => submit.mutate()}>登记草案并排队回放</button>
      {active && <RunView id={active} />}
      <RunList active={active} onPick={setActive} />
    </section>
  );
}

function RunList({ active, onPick }: { active: string | null; onPick: (id: string) => void }) {
  const q = useQuery({ queryKey: ['bffr', 'runs'], queryFn: ({ signal }) => bfFillr.customRuns(20, signal), refetchInterval: 15_000, retry: false });
  if (q.isLoading) return <Loading label="读取我的自定义回测…" />;
  if (q.isError) return <ErrorState error={q.error} what="自定义回测列表" onRetry={() => q.refetch()} />;
  const items = q.data!.data.items;
  if (items.length === 0) return <EmptyState>还没有自定义回测。</EmptyState>;
  return (
    <div className="run-list">
      <h4>我的自定义回测</h4>
      <table className="table dense">
        <thead><tr><th>时间</th><th>战法</th><th>改动</th><th>状态</th><th>判定</th><th>净收益</th><th /></tr></thead>
        <tbody>
          {items.map((r: CustomRunBrief) => (
            <tr key={r.job_run_id} className={active === r.job_run_id ? 'current' : ''}>
              <td>{r.scheduled_for.slice(0, 16).replace('T', ' ')}</td><td>{r.label ?? r.playbook_id}</td><td className="small">{JSON.stringify(r.request)}</td>
              <td>{runStateText(r.run_state)}{r.error_code ? <span className="muted small"> {r.error_code}</span> : null}</td>
              <td>{r.summary?.verdict ?? <MissingText>—</MissingText>}</td><td className={`num ${signClass(r.summary?.net_return)}`}>{r.summary ? pctText(r.summary.net_return) : <MissingText>—</MissingText>}</td>
              <td><button className="linklike" onClick={() => onPick(r.job_run_id)}>查看</button></td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted small">{q.data!.data.order}</p>
    </div>
  );
}

export function RunView({ id }: { id: string }) {
  const q = useQuery({
    queryKey: ['bffr', 'run', id], queryFn: ({ signal }) => bfFillr.customRun(id, signal), retry: false,
    refetchInterval: (query) => (['succeeded', 'failed', 'cancelled'].includes(query.state.data?.data.run.run_state ?? '') ? false : 5000),
  });
  if (q.isLoading) return <Loading label="读取运行状态…" />;
  if (q.isError) return <ErrorState error={q.error} what="自定义回测结果" onRetry={() => q.refetch()} />;
  const d = q.data!.data;
  return <RunResult data={d} />;
}

export function RunResult({ data }: { data: CustomRun }) {
  const { run, result } = data;
  if (run.run_state === 'failed') return <div className="state state-error" role="alert">自定义回测失败：{run.error_code}（草案已登记，失败也被记录）</div>;
  if (run.run_state !== 'succeeded' || !result) {
    return (
      <div className="queued-note" role="status">
        <Badge tone="warn">{runStateText(run.run_state)}</Badge> 草案已登记，回放还没有运行：由后台任务执行（<code>{data.worker_hint}</code>，由调度器定时运行）。这个页面每几秒检查一次，结果出来后自动显示。
      </div>
    );
  }
  const m = (name: string, slice: string) => result.metrics.find((x) => x.name === name && x.slice === slice);
  const rows: Array<[string, string, string]> = [['净收益·全程', 'net_return', 'overall'], ['净收益·训练', 'net_return', 'train'], ['净收益·验证', 'net_return', 'validation'],
    ['2倍费用·验证', 'net_return_cost_x2', 'validation'], ['3倍费用·验证', 'net_return_cost_x3', 'validation'], ['超额(沪深300)·验证', 'excess_return_vs_csi300', 'validation'],
    ['最大回撤', 'max_drawdown', 'overall'], ['胜率(毛)', 'win_rate_gross', 'overall']];
  const rule = (k: 'effective' | 'base'): string => {
    const r = result[k];
    return `持有 ${r.holding_days} 日 · 止损 ${r.stop_loss_pct ?? '关'} · 止盈 ${r.take_profit_pct ?? '关'} · 追踪 ${r.trailing_stop_pct ?? '关'} · 最多 ${r.max_positions} 只 · 每日新开 ${r.max_new_per_day}`;
  };
  return (
    <section className="run-result" aria-label="自定义回测结果">
      <div className="counts">
        <Badge tone="warn" title="参数由您事后选择，不计入研究联赛">探索性</Badge>
        <b>{result.playbook.name}</b> <span className="muted small">{result.window.start} ~ {result.window.end}，{result.window.sessions} 个交易日 · 费用口径 {result.cost_policy_version}</span>
      </div>
      <p className="small">改后：{rule('effective')}{Object.keys(result.effective.params).length ? ` · 参数 ${JSON.stringify(result.effective.params)}` : ''}</p>
      <p className="muted small">原版：{rule('base')}</p>
      <table className="table dense"><tbody>
        <tr>{rows.map(([l]) => <th key={l}>{l}</th>)}</tr>
        <tr>{rows.map(([l, n, s]) => { const x = m(n, s); return <td key={l} className={`num ${signClass(x?.value)}`} title={x?.missing_reason ?? undefined}>{x?.value == null ? <MissingText>缺失</MissingText> : pctText(x.value)}</td>; })}</tr>
      </tbody></table>
      <div><span className="muted small">净值 </span><Sparkline values={result.equity.filter((p) => p.nav != null).map((p) => p.nav as string)} label="自定义回测净值" /></div>
      <EvaluationDetail ev={result.evaluation} />
      <TradeTable items={result.trades.items} total={result.trades.total} />
      <ul className="muted small plain">{result.caveats.map((c) => <li key={c}>{c}</li>)}</ul>
    </section>
  );
}

function TradeTable({ items, total }: { items: CustomTrade[]; total: number }) {
  if (items.length === 0) return <EmptyState>这次回放没有成交。</EmptyState>;
  return (
    <>
      <table className="table dense">
        <thead><tr><th>代码</th><th>买入日</th><th>卖出日</th><th>买入价</th><th>卖出价</th><th>毛收益</th><th>卖出原因</th></tr></thead>
        <tbody>{items.slice(0, 30).map((t) => (
          <tr key={`${t.code}${t.entry_day}`}><td>{t.code ?? <MissingText />}</td><td>{t.entry_day}</td><td>{t.exit_day ?? '持仓中'}</td><td className="num">{numText(t.entry_price)}</td><td className="num">{numText(t.exit_price)}</td>
            <td className={`num ${signClass(t.gross_return)}`}>{pctText(t.gross_return)}</td><td>{t.status === 'open' ? '持仓中' : EXIT_TEXT[t.exit_reason ?? ''] ?? t.exit_reason ?? '缺失'}</td></tr>
        ))}</tbody>
      </table>
      <p className="muted small">最近 {Math.min(30, items.length)} 笔，共 {total} 笔；单笔收益为成交均价之比（毛），费用体现在净值里。</p>
    </>
  );
}
