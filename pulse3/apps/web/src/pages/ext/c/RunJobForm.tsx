/** 运行已登记任务 (runRegisteredJob): pick a manually-triggerable registered job, build the inline parameter artifact, confirm, POST, then poll the run. */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useRef, useState } from 'react';
import { Badge } from '../../../components/Badges';
import { Loading } from '../../../components/States';
import { useToast } from '../../../components/Toast';
import { RUN_STATE_LABEL } from '../../../lib/format';
import { buildParameterArtifact, errorCode, explainError, extGet, extPost, getJobRun } from './api';
import { isoSeconds } from './time';
import type { JobDefinition, JobDefinitionsData, RunJobData } from './types';

export interface JobFormValues { definition: JobDefinition | null; params: string; scheduledFor: string }

/** Returns an error message per field, or an empty object when valid. */
export function validateJobForm(v: JobFormValues): Record<string, string> {
  const e: Record<string, string> = {};
  if (!v.definition) e.definition = '请选择要运行的任务';
  else if (!v.definition.enabled) e.definition = '该任务已停用';
  try { JSON.parse(v.params); } catch { e.params = '参数不是合法的 JSON'; }
  if (Number.isNaN(Date.parse(v.scheduledFor))) e.scheduledFor = '请选择计划时间';
  return e;
}

const toLocalInput = (ms: number): string => new Date(ms - new Date(ms).getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
const TERMINAL = new Set(['succeeded', 'failed', 'cancelled']);
const defKey = (d: JobDefinition): string => `${d.job_id}@${d.job_version}`;

function RunStatus({ runId }: { runId: string }) {
  const q = useQuery({
    queryKey: ['extc', 'job-run', runId],
    queryFn: ({ signal }) => getJobRun(runId, signal),
    refetchInterval: (query) => (query.state.data && TERMINAL.has(query.state.data.data.run_state) ? false : 3000),
  });
  const r = q.data?.data;
  return (
    <div className="notice notice-info" role="status">
      已提交：运行编号 <span className="mono">{runId.slice(0, 8)}</span>
      {r ? <Badge tone={r.run_state === 'failed' ? 'bad' : r.run_state === 'succeeded' ? 'good' : 'neutral'}>{RUN_STATE_LABEL[r.run_state] ?? r.run_state}</Badge> : q.error ? <span className="small">状态读取失败：{explainError(q.error)}</span> : <span className="small">读取状态…</span>}
      {r?.error_code && <span className="mono small">{r.error_code}</span>}
    </div>
  );
}

export function RunJobForm() {
  const toast = useToast();
  const qc = useQueryClient();
  const defs = useQuery({ queryKey: ['extc', 'job-definitions'], queryFn: ({ signal }) => extGet<JobDefinitionsData>('c/job-definitions', undefined, signal), retry: false });
  const [sel, setSel] = useState('');
  const [params, setParams] = useState('{}');
  const [scheduledFor, setScheduledFor] = useState(() => toLocalInput(Date.now()));
  const [errs, setErrs] = useState<Record<string, string>>({});
  const [confirming, setConfirming] = useState(false);
  const key = useRef(crypto.randomUUID());
  const manual = (defs.data?.data.definitions ?? []).filter((d) => d.manual_trigger);
  const definition = manual.find((d) => defKey(d) === sel) ?? null;
  const values: JobFormValues = { definition, params, scheduledFor };

  const run = useMutation({
    mutationFn: async () => {
      const d = definition as JobDefinition;
      const artifact = await buildParameterArtifact(JSON.parse(params), d.parameter_contract_id, d.parameter_schema_version);
      return extPost<RunJobData>('run-registered-job', {
        job_id: d.job_id, job_version: d.job_version, expected_definition_hash: d.definition_hash,
        parameter_artifact: artifact, input_snapshot_refs: [], scheduled_for: isoSeconds(Date.parse(scheduledFor)),
      }, key.current);
    },
    onSuccess: () => { key.current = crypto.randomUUID(); void qc.invalidateQueries({ queryKey: ['extc', 'query-job-runs'] }); },
  });
  const touch = (): void => { key.current = crypto.randomUUID(); };
  const submit = (): void => { const e = validateJobForm(values); setErrs(e); if (Object.keys(e).length === 0) setConfirming(true); };
  const go = (): void => { setConfirming(false); run.mutate(undefined, { onError: (er) => toast.push('bad', errorCode(er) === 'FORBIDDEN' ? '无运行权限' : `任务提交失败：${explainError(er)}`) }); };
  const receipt = run.data?.data.receipt;
  const denied = defs.error && errorCode(defs.error) === 'FORBIDDEN';

  return (
    <section className="card" aria-label="运行已登记任务">
      <h3 style={{ marginTop: 0 }}>运行已登记任务</h3>
      <p className="muted small">只能运行已在服务端登记且允许手动触发的任务；参数以内联 JSON 提交并带内容哈希。需要运行权限，提交前会再次确认。</p>
      {defs.isLoading ? <Loading /> : denied ? <p className="callout" role="alert">无查看任务定义权限</p> : defs.error ? <p className="field-error" role="alert">任务定义读取失败：{explainError(defs.error)}</p> : manual.length === 0 ? <p className="muted">没有允许手动触发的已登记任务。</p> : (
        <form className="form" onSubmit={(e) => { e.preventDefault(); submit(); }} aria-label="运行任务表单">
          <label>已登记任务
            <select value={sel} onChange={(e) => { setSel(e.target.value); touch(); }} aria-invalid={!!errs.definition}>
              <option value="">请选择…</option>
              {manual.map((d) => <option key={defKey(d)} value={defKey(d)} disabled={!d.enabled}>{d.application_operation} v{d.job_version}{d.enabled ? '' : '（已停用）'}</option>)}
            </select>
            {errs.definition && <span className="field-error">{errs.definition}</span>}
          </label>
          <label>计划运行时间<input type="datetime-local" value={scheduledFor} onChange={(e) => { setScheduledFor(e.target.value); touch(); }} />{errs.scheduledFor && <span className="field-error">{errs.scheduledFor}</span>}</label>
          {definition && (
            <dl className="kv compact" style={{ gridColumn: '1 / -1' }}>
              <dt>任务编号</dt><dd className="mono small">{definition.job_id}</dd>
              <dt>版本 / 定义哈希</dt><dd className="mono small">v{definition.job_version} · {definition.definition_hash.slice(0, 16)}…</dd>
              <dt>参数契约</dt><dd className="mono small">{definition.parameter_contract_id ?? '默认'} {definition.parameter_schema_version ?? ''}</dd>
            </dl>
          )}
          <label style={{ gridColumn: '1 / -1' }}>任务参数（JSON）<textarea value={params} onChange={(e) => { setParams(e.target.value); touch(); }} aria-invalid={!!errs.params} spellCheck={false} />{errs.params && <span className="field-error">{errs.params}</span>}</label>
          <button className="btn btn-on" disabled={run.isPending}>{run.isPending ? '提交中…' : '运行'}</button>
        </form>
      )}
      {run.error && <p className="field-error" role="alert">{errorCode(run.error) === 'FORBIDDEN' ? '无运行权限' : explainError(run.error)}</p>}
      {receipt && <RunStatus runId={receipt.job_run_id} />}
      {confirming && definition && (
        <div className="dialog-scrim">
          <div className="dialog" role="alertdialog" aria-modal="true" aria-labelledby="job-confirm-title">
            <h3 id="job-confirm-title" style={{ margin: 0 }}>确认运行任务？</h3>
            <p>将运行 <b>{definition.application_operation}</b> v{definition.job_version}，计划时间 {scheduledFor.replace('T', ' ')}。任务可能写入数据，提交后不能撤回。</p>
            <div className="dialog-actions">
              <button className="btn" autoFocus onClick={() => setConfirming(false)}>取消</button>
              <button className="btn btn-on" onClick={go}>确认运行</button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
