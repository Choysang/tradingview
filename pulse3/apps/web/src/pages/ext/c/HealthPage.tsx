/** 数据健康: dataset manifests/watermarks, job runs, and the registered-job runner. */
import { useMemo, useState } from 'react';
import { Badge, MissingText } from '../../../components/Badges';
import { EmptyState, SectionTitle } from '../../../components/States';
import { RUN_STATE_LABEL, fmtAge, fmtInt, fmtTime } from '../../../lib/format';
import type { Tone } from '../../../lib/version';
import { LoadMore, Pending, Strip, useNowAnchor, usePaged } from './common';
import { RunJobForm } from './RunJobForm';
import { windowLastDays } from './time';
import type { DatasetStatus, DatasetStatusData, JobRunsData } from './types';

const MANIFEST_LABEL: Record<string, string> = { ready: '就绪', building: '构建中', pending: '待构建', failed: '失败', stale: '已过期', missing: '缺失' };
const MANIFEST_TONE: Record<string, Tone> = { ready: 'good', building: 'neutral', pending: 'warn', failed: 'bad', stale: 'warn', missing: 'bad' };
const RUN_TONE: Record<string, Tone> = { succeeded: 'good', running: 'neutral', queued: 'neutral', failed: 'bad', cancelled: 'warn' };

function DatasetRow({ d }: { d: DatasetStatus }) {
  const noData = d.available_watermark === null || d.row_count === null || d.row_count === 0;
  const c = d.coverage;
  return (
    <tr>
      <td className="mono">{d.dataset_id}</td>
      <td>
        <Badge tone={MANIFEST_TONE[d.manifest_state] ?? 'neutral'} title={`manifest_state = ${d.manifest_state}`}>{MANIFEST_LABEL[d.manifest_state] ?? d.manifest_state}</Badge>
        {d.manifest_version != null && <span className="muted small"> v{d.manifest_version}</span>}
        {noData && <div><Badge tone="warn" title="清单已登记不等于已收到数据">尚未收到数据</Badge></div>}
      </td>
      <td>{d.available_watermark ? <>{fmtTime(d.available_watermark)}<div className="muted small">{fmtAge(d.available_watermark)}</div></> : <MissingText>无水位</MissingText>}</td>
      <td className="num">{d.row_count === null ? <MissingText /> : fmtInt(d.row_count)}</td>
      <td className="small">{c.expected_count != null ? `${c.observed_count}/${c.expected_count}` : `${c.observed_count}（总数未知）`}{(c.missing_count ?? 0) > 0 && <span className="missing"> 缺 {c.missing_count}</span>}{c.unverifiable_reasons.map((r) => <div key={r} className="muted">{r}</div>)}</td>
      <td className="small">{d.issues.length === 0 ? '无' : d.issues.map((i, k) => <div key={`${i.reason_code}${k}`}><Badge tone="warn">{i.reason_code}</Badge> {i.requirement}</div>)}</td>
    </tr>
  );
}

function Datasets() {
  const [text, setText] = useState('all');
  const [id, setId] = useState('all');
  const paged = usePaged<DatasetStatusData>('query-dataset-status', { dataset_id: id }, 100, id !== '');
  const rows = paged.pages.flatMap((p) => p.data.datasets);
  return (
    <section aria-label="数据集状态">
      <SectionTitle title="数据集状态" hint="填 all 列出全部数据集，或填具体数据集编号。「已启用」只表示被配置为启用，不等于已经收到数据：请看水位、行数和覆盖。" />
      <form className="filters" onSubmit={(e) => { e.preventDefault(); setId(text.trim()); }}>
        <label className="select-label">数据集编号 dataset_id<input value={text} onChange={(e) => setText(e.target.value)} /></label>
        <button className="btn btn-small">查询</button>
      </form>
      <Strip meta={paged.pages[0]?.meta} title="数据集状态" />
      <Pending loading={paged.isLoading} error={paged.error} onRetry={paged.refetch} what="数据集状态" />
      {!paged.isLoading && !paged.error && rows.length === 0 && <EmptyState>没有匹配的数据集。</EmptyState>}
      {rows.length > 0 && (
        <div className="table-wrap"><table className="table"><caption className="sr-only">数据集状态</caption>
          <thead><tr><th scope="col">数据集</th><th scope="col">清单状态</th><th scope="col">可用水位</th><th scope="col" className="num">行数</th><th scope="col">覆盖</th><th scope="col">问题</th></tr></thead>
          <tbody>{rows.map((d) => <DatasetRow key={`${d.dataset_id}-${d.snapshot_id ?? ''}`} d={d} />)}</tbody></table></div>
      )}
      <LoadMore paged={paged} />
    </section>
  );
}

function JobRuns() {
  const [now, refresh] = useNowAnchor();
  const [days, setDays] = useState(7);
  const win = useMemo(() => windowLastDays(days, now), [days, now]);
  const paged = usePaged<JobRunsData>('query-job-runs', { window: win }, 50);
  const jobs = paged.pages.flatMap((p) => p.data.jobs);
  return (
    <section aria-label="任务运行">
      <SectionTitle title="任务运行" right={<button className="btn btn-small" onClick={refresh}>刷新</button>} />
      <div className="filters">
        <label className="select-label">时间范围
          <select value={days} onChange={(e) => setDays(Number(e.target.value))}>{[1, 7, 30].map((d) => <option key={d} value={d}>最近 {d} 天</option>)}</select>
        </label>
      </div>
      <Strip meta={paged.pages[0]?.meta} title="任务运行" />
      <Pending loading={paged.isLoading} error={paged.error} onRetry={paged.refetch} what="任务运行" />
      {!paged.isLoading && !paged.error && jobs.length === 0 && <EmptyState>这段时间没有任务运行记录。</EmptyState>}
      {jobs.length > 0 && (
        <div className="table-wrap"><table className="table"><caption className="sr-only">任务运行</caption>
          <thead><tr><th scope="col">运行</th><th scope="col">任务</th><th scope="col">状态</th><th scope="col">计划时间</th><th scope="col">心跳</th><th scope="col">完成</th><th scope="col" className="num">尝试</th><th scope="col">错误码</th></tr></thead>
          <tbody>{jobs.map((j) => (
            <tr key={j.job_run_id}>
              <td className="mono">{j.job_run_id.slice(0, 8)}</td><td className="mono">{j.job_id.slice(0, 8)} v{j.job_version}</td>
              <td><Badge tone={RUN_TONE[j.run_state] ?? 'neutral'}>{RUN_STATE_LABEL[j.run_state] ?? j.run_state}</Badge></td>
              <td className="small">{fmtTime(j.scheduled_for)}</td><td className="small">{j.heartbeat_at ? fmtAge(j.heartbeat_at) : '—'}</td><td className="small">{j.completed_at ? fmtTime(j.completed_at) : '—'}</td>
              <td className="num">{j.attempt_count}</td><td className="mono small">{j.error_code ?? ''}</td>
            </tr>))}</tbody></table></div>
      )}
      <LoadMore paged={paged} />
    </section>
  );
}

export function HealthPage() {
  return (
    <div className="page">
      <Datasets />
      <JobRuns />
      <RunJobForm />
    </div>
  );
}
