/** 数据同步: queue a bounded data sync (daily bars, legacy import, reconcile) and read the run log. A run stays 排队中 until the worker ran it. */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useRef, useState } from 'react';
import { Badge } from '../../../components/Badges';
import { Loading } from '../../../components/States';
import { useToast } from '../../../components/Toast';
import { RUN_STATE_LABEL, fmtTime } from '../../../lib/format';
import type { Tone } from '../../../lib/version';
import { errorCode, explainError, extGet, extPost } from './api';
import {
  EMPTY_SYNC_FORM, SYNC_KIND_HINT, SYNC_KIND_LABEL, buildSyncParameters, describeSyncParameters, isActive,
  type SyncForm, type SyncJob, type SyncJobList, type SyncKind, type SyncReceipt,
} from './syncJobs';

const RUN_TONE: Record<string, Tone> = { succeeded: 'good', running: 'neutral', queued: 'neutral', failed: 'bad', cancelled: 'warn' };
const ERROR_LABEL: Record<string, string> = { SOURCE_BLOCKED: '数据源封禁，已立即停止', SOURCE_UNREACHABLE: '数据源无法连接' };
const LIST_KEY = ['extc', 'sync-jobs'] as const;

function explain(e: unknown): string {
  return errorCode(e) === 'FORBIDDEN' ? '无同步权限（需要运行权限）' : explainError(e);
}

function JobRow({ j }: { j: SyncJob }) {
  return (
    <tr>
      <td className="small">{fmtTime(j.requested_at)}</td>
      <td>{SYNC_KIND_LABEL[j.kind]}<div className="muted small">{describeSyncParameters(j.kind, j.parameters)}</div></td>
      <td><Badge tone={RUN_TONE[j.run_state] ?? 'neutral'}>{RUN_STATE_LABEL[j.run_state] ?? j.run_state}</Badge></td>
      <td className="small">{j.started_at ? fmtTime(j.started_at) : '—'}<div className="muted">{j.finished_at ? `结束 ${fmtTime(j.finished_at)}` : ''}</div></td>
      <td className="small"><span className="mono">{j.exit_status ?? '—'}</span>{j.error_code && <div><Badge tone="bad" title={j.error_code}>{ERROR_LABEL[j.error_code] ?? j.error_code}</Badge></div>}</td>
      <td className="small">{j.summary ? <details><summary>查看输出</summary><pre className="mono small" style={{ whiteSpace: 'pre-wrap', maxHeight: 240, overflow: 'auto' }}>{j.summary}</pre></details> : <span className="muted">{isActive(j) ? '等待执行' : '无输出'}</span>}</td>
    </tr>
  );
}

export function SyncPanel() {
  const toast = useToast();
  const qc = useQueryClient();
  const list = useQuery({
    queryKey: LIST_KEY,
    queryFn: ({ signal }) => extGet<SyncJobList>('bf/sync/jobs', { limit: 30 }, signal),
    retry: false,
    refetchInterval: (q) => (q.state.data?.data.items.some(isActive) ? 5000 : false),
  });
  const [form, setForm] = useState<SyncForm>(EMPTY_SYNC_FORM);
  const [errs, setErrs] = useState<Record<string, string>>({});
  const [pending, setPending] = useState<Record<string, unknown> | null>(null);
  const key = useRef(crypto.randomUUID());
  const set = (patch: Partial<SyncForm>): void => { setForm((f) => ({ ...f, ...patch })); key.current = crypto.randomUUID(); };

  const data = list.data?.data;
  const busy = data?.items.find((j) => j.kind === form.kind && isActive(j));
  const denied = list.error ? errorCode(list.error) === 'FORBIDDEN' : false;

  const queue = useMutation({
    mutationFn: (parameters: Record<string, unknown>) => extPost<SyncReceipt>('bf/sync/jobs', { kind: form.kind, parameters }, key.current),
    onSuccess: () => { key.current = crypto.randomUUID(); void qc.invalidateQueries({ queryKey: LIST_KEY }); },
    onError: (e) => { toast.push('bad', `同步提交失败：${explain(e)}`); void qc.invalidateQueries({ queryKey: LIST_KEY }); },
  });

  const submit = (): void => {
    if (!data) return;
    const r = buildSyncParameters(form, data.bounds);
    if (!r.ok) { setErrs(r.errors); return; }
    setErrs({});
    setPending(r.parameters);
  };
  const go = (): void => { const p = pending; setPending(null); if (p) queue.mutate(p); };
  const receipt = queue.data?.data;

  return (
    <section className="card" aria-label="数据同步">
      <h3 style={{ marginTop: 0 }}>数据同步</h3>
      <p className="muted small">从这里排队一次数据同步；提交只会排队，由后台任务执行器（{data?.worker_hint ?? 'sync_jobs run'}）取走后才会真正抓取或导入。同一类同步同时只能有一个在排队或运行。需要运行权限。</p>
      {list.isLoading ? <Loading /> : denied ? <p className="callout" role="alert">无同步权限（需要运行权限）</p> : list.error ? <p className="field-error" role="alert">同步记录读取失败：{explainError(list.error)}</p> : data && (
        <>
          <form className="form" onSubmit={(e) => { e.preventDefault(); submit(); }} aria-label="数据同步表单">
            <label>同步类型
              <select value={form.kind} onChange={(e) => set({ kind: e.target.value as SyncKind })}>
                {data.kinds.map((k) => <option key={k} value={k}>{SYNC_KIND_LABEL[k] ?? k}</option>)}
              </select>
            </label>
            <p className="muted small" style={{ gridColumn: '1 / -1', margin: 0 }} role="note">{SYNC_KIND_HINT[form.kind]}</p>
            {form.kind === 'sync_daily_bars' && (
              <>
                <label>范围
                  <select value={form.mode} onChange={(e) => set({ mode: e.target.value as SyncForm['mode'] })}>
                    <option value="codes">指定代码</option><option value="universe">整个研究股票池（耗时长）</option>
                  </select>
                </label>
                <label>数据源
                  <select value={form.source} onChange={(e) => set({ source: e.target.value as SyncForm['source'] })}>
                    <option value="sina">新浪</option><option value="tencent">腾讯</option><option value="both">两者都抓</option>
                  </select>
                </label>
                <label>每只股票的日线根数（1–{data.bounds.max_count}）
                  <input inputMode="numeric" value={form.count} onChange={(e) => set({ count: e.target.value })} aria-invalid={!!errs.count} />
                  {errs.count && <span className="field-error">{errs.count}</span>}
                </label>
              </>
            )}
            {form.kind === 'sync_legacy_import' && (
              <>
                <label>导入最近几天（1–{data.bounds.max_since_days}）
                  <input inputMode="numeric" value={form.sinceDays} onChange={(e) => set({ sinceDays: e.target.value })} aria-invalid={!!errs.sinceDays} />
                  {errs.sinceDays && <span className="field-error">{errs.sinceDays}</span>}
                </label>
                <label>板块
                  <select value={form.boards} onChange={(e) => set({ boards: e.target.value as SyncForm['boards'] })}>
                    <option value="main">仅主板</option><option value="all">全部板块</option>
                  </select>
                </label>
              </>
            )}
            {form.kind === 'sync_reconcile' && (
              <label>对比最近几个日期（1–{data.bounds.max_last}）
                <input inputMode="numeric" value={form.last} onChange={(e) => set({ last: e.target.value })} aria-invalid={!!errs.last} />
                {errs.last && <span className="field-error">{errs.last}</span>}
              </label>
            )}
            {!(form.kind === 'sync_daily_bars' && form.mode === 'universe') && (
              <label style={{ gridColumn: '1 / -1' }}>股票代码（6 位数字，空格或逗号分隔，最多 {data.bounds.max_codes} 个{form.kind === 'sync_legacy_import' ? '；留空表示全部' : ''}）
                <textarea value={form.codes} onChange={(e) => set({ codes: e.target.value })} aria-invalid={!!errs.codes} spellCheck={false} />
                {errs.codes && <span className="field-error">{errs.codes}</span>}
              </label>
            )}
            <button className="btn btn-on" disabled={queue.isPending || !!busy || !data.executor_enabled}>{queue.isPending ? '提交中…' : '提交同步'}</button>
          </form>
          {!data.executor_enabled && <p className="callout" role="status">切换后才会执行：当前环境没有启用同步执行器，现在不接受提交；下面只显示历史记录。</p>}
          {busy && <p className="callout" role="status">{SYNC_KIND_LABEL[form.kind]}已有一个{RUN_STATE_LABEL[busy.run_state]}的任务（{busy.job_run_id.slice(0, 8)}），请等它结束后再提交。</p>}
          {receipt && <div className="notice notice-info" role="status">已排队：运行编号 <span className="mono">{receipt.receipt.job_run_id.slice(0, 8)}</span>，等待后台执行器取走。</div>}
          {data.items.length === 0 ? <p className="muted">还没有同步记录。</p> : (
            <div className="table-wrap"><table className="table"><caption className="sr-only">数据同步记录</caption>
              <thead><tr><th scope="col">提交时间</th><th scope="col">类型与参数</th><th scope="col">状态</th><th scope="col">开始 / 结束</th><th scope="col">退出码</th><th scope="col">输出</th></tr></thead>
              <tbody>{data.items.map((j) => <JobRow key={j.job_run_id} j={j} />)}</tbody></table></div>
          )}
        </>
      )}
      {pending && (
        <div className="dialog-scrim">
          <div className="dialog" role="alertdialog" aria-modal="true" aria-labelledby="sync-confirm-title">
            <h3 id="sync-confirm-title" style={{ margin: 0 }}>确认排队同步？</h3>
            <p>将排队 <b>{SYNC_KIND_LABEL[form.kind]}</b>：{describeSyncParameters(form.kind, pending)}。后台执行器取走后会访问数据源或写入数据，不能撤回。</p>
            <div className="dialog-actions">
              <button className="btn" autoFocus onClick={() => setPending(null)}>取消</button>
              <button className="btn btn-on" onClick={go}>确认排队</button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
