/**
 * Strategy catalogue, league and experiments (M4-E05). The page keeps four things apart:
 * evidence grade, implementation status, health and league outcome. League results are
 * never shown as probabilities, and a finished run is not shown as "verified profit".
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import { ApiError, newIdempotencyKey } from '../api/client';
import { ops } from '../api/ops';
import type { Evaluation, ExperimentRequest, JobReceipt, Strategy } from '../api/types';
import { Badge, GradeBadge, MissingText } from '../components/Badges';
import { MetaStrip } from '../components/MetaStrip';
import { EmptyState, ErrorState, Loading, SectionTitle } from '../components/States';
import { useToast } from '../components/Toast';
import { HORIZONS, LEAGUE_DEFAULT } from '../config';
import { usePinnedPages } from '../hooks/usePinnedPages';
import { useStrategyCatalog } from '../hooks/useStrategyCatalog';
import { toFixed, toPercent } from '../lib/decimal';
import { LIFECYCLE_LABEL, OUTCOME_LABEL, ROLE_LABEL, RUN_PURPOSE_LABEL, RUN_STATE_LABEL, fmtTime } from '../lib/format';
import { gradeOfInstance } from '../lib/grades';

type Tab = 'catalog' | 'league' | 'experiment';
const TABS: Array<[Tab, string]> = [['catalog', '策略目录'], ['league', '联赛'], ['experiment', '提交实验']];
const IMPL_LABEL: Record<string, string> = { not_implemented: '未实现', prototype: '原型', implemented: '已实现' };
const REPRO_LABEL: Record<string, string> = { not_started: '未开始复现', running: '复现中', reproduced: '已复现', failed: '复现失败' };
const HEALTH_LABEL: Record<string, string> = { healthy: '运行正常', degraded: '降级', unavailable: '不可用' };

export function StrategiesPage() {
  const [sp, setSp] = useSearchParams();
  const tab = (TABS.find(([k]) => k === sp.get('tab'))?.[0] ?? 'catalog') as Tab;
  const set = (k: string, v: string): void => setSp((p) => { const n = new URLSearchParams(p); n.set(k, v); return n; }, { replace: true });
  return (
    <div className="page">
      <SectionTitle title="策略与联赛" hint="证据等级、实现状态、运行健康和联赛名次是四件不同的事，分别显示。" />
      <div className="segmented" role="tablist">
        {TABS.map(([k, l]) => <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? 'on' : ''} onClick={() => set('tab', k)}>{l}</button>)}
      </div>
      {tab === 'catalog' && <Catalog />}
      {tab === 'league' && <LeagueView />}
      {tab === 'experiment' && <ExperimentForm />}
    </div>
  );
}

function Catalog() {
  const [role, setRole] = useState('');
  const [horizon, setHorizon] = useState('');
  const [lifecycle, setLifecycle] = useState('');
  const filters = { role, horizon, lifecycle };
  const pages = usePinnedPages({
    queryKey: ['strategies', filters],
    fetchPage: (cursor, signal) => ops.listStrategies({
      ...(role ? { role: role as Strategy['role'] } : {}), ...(horizon ? { horizon_id: horizon } : {}),
      ...(lifecycle ? { lifecycle: lifecycle as Strategy['instances'][number]['lifecycle'] } : {}), cursor,
    }, { signal }),
    pinOf: () => ({ releaseId: null, rankingId: JSON.stringify(filters), actionabilityId: null }),
    entriesOf: (p) => p.data.items,
    keyOf: (s) => `${s.strategy_id}:${s.strategy_version}`,
  });
  return (
    <section>
      <div className="filters">
        <label className="select-label">职责<select value={role} onChange={(e) => setRole(e.target.value)}><option value="">全部</option>{Object.entries(ROLE_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
        <label className="select-label">期限<select value={horizon} onChange={(e) => setHorizon(e.target.value)}><option value="">全部</option>{HORIZONS.map((h) => <option key={h.key} value={h.horizonId}>{h.label}</option>)}</select></label>
        <label className="select-label">状态<select value={lifecycle} onChange={(e) => setLifecycle(e.target.value)}><option value="">全部</option>{Object.entries(LIFECYCLE_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
      </div>
      {pages.isLoading && <Loading />}
      {pages.error && !pages.firstPage && <ErrorState error={pages.error} what="策略目录" onRetry={pages.reload} />}
      {pages.firstPage && <MetaStrip meta={pages.firstPage.meta} title="策略目录" />}
      {pages.firstPage && pages.paging.entries.length === 0 && <EmptyState>没有符合条件的策略。</EmptyState>}
      <ul className="cards-list">
        {pages.paging.entries.map((s) => (
          <li key={`${s.strategy_id}:${s.strategy_version}`} className="card item">
            <div className="item-head"><b>{s.display_name}</b><Badge>{ROLE_LABEL[s.role]}</Badge><span className="muted small">{s.method_family} · {s.horizon_id} · v{s.strategy_version}</span></div>
            {s.instances.length === 0 && <div className="muted small">没有运行实例。</div>}
            {s.instances.map((i) => (
              <div key={i.instance_id} className="inst-line">
                <GradeBadge grade={gradeOfInstance(i)} />
                <Badge title="生命周期">{LIFECYCLE_LABEL[i.lifecycle]}</Badge>
                <Badge tone={i.implementation_status === 'implemented' ? 'good' : 'neutral'}>{IMPL_LABEL[i.implementation_status]}</Badge>
                <Badge tone={i.reproduction_status === 'reproduced' ? 'good' : i.reproduction_status === 'failed' ? 'bad' : 'neutral'}>{REPRO_LABEL[i.reproduction_status]}</Badge>
                <Badge tone={i.health === 'healthy' ? 'good' : i.health === 'degraded' ? 'warn' : 'bad'}>{HEALTH_LABEL[i.health]}</Badge>
              </div>
            ))}
            <p className="muted small">预测目标：{s.forecast_target}。「已实现」「运行正常」只说明工程状态，不等于已验证能盈利。</p>
          </li>
        ))}
      </ul>
      {pages.hasMore && <button className="btn" onClick={pages.loadMore} disabled={pages.isFetchingMore}>加载更多</button>}
    </section>
  );
}

function LeagueView() {
  const [sp, setSp] = useSearchParams();
  const leagueId = sp.get('league') ?? LEAGUE_DEFAULT.id;
  const version = Number(sp.get('lv') ?? LEAGUE_DEFAULT.version);
  const [draft, setDraft] = useState(leagueId ?? '');
  const catalog = useStrategyCatalog();
  const q = useQuery({
    queryKey: ['league', leagueId, version],
    queryFn: ({ signal }) => ops.getLeague(leagueId as string, version, { signal }),
    enabled: !!leagueId,
  });
  const expId = sp.get('exp');
  const name = (instanceId: string | null): string => (instanceId ? catalog.data?.byInstance.get(instanceId)?.display_name ?? `实例 ${instanceId.slice(0, 8)}` : '—');
  return (
    <section>
      <form className="filters" onSubmit={(e) => { e.preventDefault(); setSp((p) => { const n = new URLSearchParams(p); n.set('league', draft.trim()); return n; }, { replace: true }); }}>
        <label className="select-label">联赛编号<input className="wide" value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="联赛 UUID（接口目前没有联赛列表）" /></label>
        <label className="select-label">版本<input type="number" min={1} value={version} onChange={(e) => setSp((p) => { const n = new URLSearchParams(p); n.set('lv', e.target.value); return n; }, { replace: true })} /></label>
        <button className="btn btn-small">查看</button>
      </form>
      {!leagueId && <EmptyState>填写联赛编号后查看。当前合同没有“联赛列表”接口，因此需要给出编号。</EmptyState>}
      {q.isLoading && <Loading />}
      {q.error && <ErrorState error={q.error} what="联赛" onRetry={() => void q.refetch()} />}
      {q.data && (
        <>
          <MetaStrip meta={q.data.meta} title="联赛" hideCoverage rows={[['league_id', q.data.data.league_id], ['evaluation_context_id', q.data.data.evaluation_context_id], ['pairing_policy_version', q.data.data.pairing_policy_version], ['promotion_policy_version', q.data.data.promotion_policy_version]]} />
          <h3>{q.data.data.display_name}</h3>
          <p className="muted small">联赛只比较在同一数据、同一成本口径下的两个候选；「胜」不是上涨概率，「不可比较」不等于输。</p>
          {q.data.data.rounds.map((r) => (
            <div key={r.round_no} className="card item">
              <div className="item-head"><b>第 {r.round_no} 轮</b><Badge tone={r.round_state === 'completed' ? 'good' : r.round_state === 'failed' ? 'bad' : 'neutral'}>{RUN_STATE_LABEL[r.round_state]}</Badge></div>
              <ul className="plain">
                {r.pairings.map((p) => (
                  <li key={p.pairing_id}>
                    {name(p.left_instance_id)} <span className="muted">对</span> {p.right_instance_id ? name(p.right_instance_id) : '（无对手）'}
                    {' '}<Badge tone={p.outcome === 'pending' ? 'neutral' : p.outcome === 'incomparable' ? 'warn' : 'good'}>{OUTCOME_LABEL[p.outcome]}</Badge>
                    {[p.left_experiment_id, p.right_experiment_id].map((id, i) => id && (
                      <button key={id} className="link-btn" onClick={() => setSp((s) => { const n = new URLSearchParams(s); n.set('exp', id); return n; }, { replace: true })}>{i === 0 ? '左方' : '右方'}实验</button>
                    ))}
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </>
      )}
      {expId && <EvaluationPanel experimentId={expId} />}
    </section>
  );
}

export function EvaluationPanel({ experimentId }: { experimentId: string }) {
  const q = useQuery({
    queryKey: ['evaluation', experimentId],
    queryFn: ({ signal }) => ops.getEvaluation(experimentId, { signal }),
    refetchInterval: (query) => { const st = query.state.data?.data.run_state; return st === 'queued' || st === 'running' ? 3_000 : false; },
  });
  if (q.isLoading) return <Loading label="读取实验结果…" />;
  if (q.error) return <ErrorState error={q.error} what="实验" onRetry={() => void q.refetch()} />;
  const e = q.data!.data as Evaluation;
  return (
    <div className="card item" aria-label="实验结果">
      <div className="item-head"><b>实验 {experimentId.slice(0, 8)}</b><Badge tone={e.run_state === 'completed' ? 'good' : e.run_state === 'failed' ? 'bad' : 'warn'}>{RUN_STATE_LABEL[e.run_state]}</Badge><Badge>{RUN_PURPOSE_LABEL[e.run_purpose]}</Badge></div>
      <MetaStrip meta={q.data!.meta} title="实验结果" hideCoverage rows={[['context_hash', e.context_hash], ['evaluation_context_id', e.evaluation_context_id], ['started_at', fmtTime(e.started_at)], ['completed_at', fmtTime(e.completed_at)], ...Object.entries(e.policy_versions).map(([k, v]): [string, string] => [k, v]), ['train', `${fmtTime(e.context.train_start)} ~ ${fmtTime(e.context.train_end)}`], ['validation', `${fmtTime(e.context.validation_start)} ~ ${fmtTime(e.context.validation_end)}`]]} />
      {e.error_code && <div className="notice notice-bad">失败原因代码：{e.error_code}</div>}
      {e.metrics.length === 0 ? <EmptyState>还没有指标。</EmptyState> : (
        <table className="table">
          <thead><tr><th>指标</th><th>数值</th><th>区间</th><th>样本数</th></tr></thead>
          <tbody>
            {e.metrics.map((m) => (
              <tr key={`${m.metric_name}${m.slice_key}`}>
                <td>{m.metric_name}</td>
                <td>{m.value === null ? <MissingText>{m.missing_reason ?? '缺失'}</MissingText> : m.unit === 'ratio' ? toPercent(m.value, 2) : `${toFixed(m.value, 4)} ${m.unit}`}</td>
                <td>{m.lower_bound !== null && m.upper_bound !== null ? `${m.unit === 'ratio' ? toPercent(m.lower_bound, 2) : m.lower_bound} ~ ${m.unit === 'ratio' ? toPercent(m.upper_bound, 2) : m.upper_bound}${m.confidence_level ? `（置信 ${toPercent(m.confidence_level, 0)}）` : ''}` : '—'}</td>
                <td>{m.sample_count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="muted small">这是历史回放/验证的统计结果，不是对未来的承诺；样本数少时区间很宽。</p>
    </div>
  );
}

function ExperimentForm() {
  const catalog = useStrategyCatalog();
  const toast = useToast();
  const instances = useMemo(() => (catalog.data?.strategies ?? []).flatMap((s) => s.instances.map((i) => ({ i, s }))), [catalog.data]);
  const [form, setForm] = useState({ instance_id: '', hypothesis_id: '', hypothesis_version: 1, evaluation_context_id: '', run_purpose: 'screening' as ExperimentRequest['run_purpose'], random_seed: 1 });
  const idem = useRef<{ payload: string; key: string } | null>(null);
  const [receipt, setReceipt] = useState<JobReceipt | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Array<{ path: string; message: string }>>([]);

  const submit = useMutation({
    mutationFn: () => {
      const body: ExperimentRequest = { ...form, instance_id: form.instance_id };
      const payload = JSON.stringify(body);
      if (idem.current?.payload !== payload) idem.current = { payload, key: newIdempotencyKey() };
      return ops.createExperiment(body, idem.current.key);
    },
    onSuccess: (res) => { setReceipt(res.data); setFieldErrors([]); toast.push('good', '实验已提交，正在排队。'); },
    onError: (e) => {
      if (e instanceof ApiError) setFieldErrors(e.fieldErrors.map((f) => ({ path: f.path, message: f.message })));
      toast.push('bad', `提交失败：${e.message}`);
    },
  });
  const job = useQuery({
    queryKey: ['job', receipt?.job_run_id],
    queryFn: ({ signal }) => ops.getJobRun(receipt!.job_run_id, { signal }),
    enabled: !!receipt,
    refetchInterval: (q) => { const s = q.state.data?.data.run_state; return s === 'succeeded' || s === 'failed' || s === 'cancelled' ? false : 2_000; },
  });
  useEffect(() => { if (job.data?.data.run_state === 'succeeded') toast.push('good', '实验运行完成，可查看结果。'); }, [job.data?.data.run_state, toast]);
  const err = (name: string): string | undefined => fieldErrors.find((f) => f.path.endsWith(name))?.message;
  const set = <K extends keyof typeof form>(k: K, v: (typeof form)[K]): void => setForm((f) => ({ ...f, [k]: v }));

  return (
    <section>
      <p className="muted small">提交的是一个可复算的研究任务：同样的参数和随机种子应得到同样的结果。提交后会得到回执，可随时回来查询；重复点击同一份内容不会重复创建。</p>
      <form className="form" onSubmit={(e) => { e.preventDefault(); submit.mutate(); }}>
        <label>策略实例
          <select value={form.instance_id} onChange={(e) => set('instance_id', e.target.value)} required>
            <option value="">请选择…</option>
            {instances.map(({ i, s }) => <option key={i.instance_id} value={i.instance_id}>{s.display_name}（{LIFECYCLE_LABEL[i.lifecycle]}）</option>)}
          </select>{err('instance_id') && <span className="field-error">{err('instance_id')}</span>}
        </label>
        <label>假设编号（UUID）<input value={form.hypothesis_id} onChange={(e) => set('hypothesis_id', e.target.value)} required pattern="[0-9a-f-]{36}" placeholder="例如 xxxxxxxx-xxxx-…" />{err('hypothesis_id') && <span className="field-error">{err('hypothesis_id')}</span>}</label>
        <label>假设版本<input type="number" min={1} value={form.hypothesis_version} onChange={(e) => set('hypothesis_version', Number(e.target.value))} required /></label>
        <label>评估上下文编号（UUID）<input value={form.evaluation_context_id} onChange={(e) => set('evaluation_context_id', e.target.value)} required pattern="[0-9a-f-]{36}" />{err('evaluation_context_id') && <span className="field-error">{err('evaluation_context_id')}</span>}</label>
        <label>运行目的
          <select value={form.run_purpose} onChange={(e) => set('run_purpose', e.target.value as ExperimentRequest['run_purpose'])}>
            {Object.entries(RUN_PURPOSE_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <label>随机种子<input type="number" min={0} value={form.random_seed} onChange={(e) => set('random_seed', Number(e.target.value))} required /></label>
        <button className="btn" disabled={submit.isPending}>{submit.isPending ? '提交中…' : '提交实验'}</button>
      </form>
      {receipt && (
        <div className="card item">
          <div className="item-head"><b>回执</b><Badge>{RUN_STATE_LABEL[job.data?.data.run_state ?? receipt.run_state]}</Badge></div>
          <div className="muted small mono">任务 {receipt.job_run_id}</div>
          <div className="muted small mono">实验 {receipt.resource_id}</div>
          {job.data?.data.run_state === 'succeeded' && <EvaluationPanel experimentId={receipt.resource_id} />}
        </div>
      )}
    </section>
  );
}
