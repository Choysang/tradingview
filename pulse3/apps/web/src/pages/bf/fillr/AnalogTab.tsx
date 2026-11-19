/**
 * Analog tab of the stock page (triage #42 #68 #158): the nearest historical setups of THIS stock at the selected release.
 * Why: the analog study used to exist only after `research analog-study --code X` was run by hand. Now the page asks the server: study ready -> show it
 * (the existing empirical distribution of what followed); not built -> a 生成 button that QUEUES the build (Idempotency-Key, one job per study); queued ->
 * 排队中 until the worker `pulse.workers.fillr run` ran. Neighbours are chosen by feature distance only; the distribution is empirical, not a calibrated probability.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { Badge, MissingText } from '../../../components/Badges';
import { EmptyState, ErrorState, Loading } from '../../../components/States';
import { useToast } from '../../../components/Toast';
import { extB, explain, type AnalogNeighbor } from '../../ext/b/api';
import { metricPct, outcomeSummary, pct } from '../../ext/b/helpers';
import type { AnalogStatus } from './api';
import { bfFillr } from './api';
import { ANALOG_STATE_TEXT, runStateText } from './format';
import './fillr.css';

const HORIZONS = [5, 10, 20, 40];

export function AnalogTab({ instrumentId, releaseId }: { instrumentId: string; releaseId: string | null }) {
  const [h, setH] = useState(20);
  const policy = `analog-knn-matured-v1:h${h}`;
  const toast = useToast();
  const qc = useQueryClient();
  const key = ['bffr', 'analog', instrumentId, releaseId, policy];
  const status = useQuery({
    queryKey: key, enabled: !!releaseId, retry: false,
    queryFn: ({ signal }) => bfFillr.analogStatus({ instrumentId, releaseId: releaseId as string, policy }, signal),
    refetchInterval: (query) => (['queued', 'running'].includes(query.state.data?.data.state ?? '') ? 5000 : false),
  });
  const queue = useMutation({
    mutationFn: () => bfFillr.queueAnalog({ instrumentId, releaseId: releaseId as string, policy }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: key }); toast.push('good', '已排队，等待后台任务运行'); },
    onError: (e) => toast.push('bad', `排队失败：${explain(e)}`),
  });
  return (
    <section className="bffr analog-tab" aria-label="相似情景">
      <div className="form-grid">
        <label>向后看（交易日）<select value={h} onChange={(e) => setH(Number(e.target.value))}>{HORIZONS.map((x) => <option key={x} value={x}>{x}</option>)}</select></label>
      </div>
      {!releaseId && <EmptyState>没有选定的发布，无法确定相似情景的决策时点。</EmptyState>}
      {status.isLoading && releaseId && <Loading label="检查相似情景研究…" />}
      {status.isError && <ErrorState error={status.error} what="相似情景状态" onRetry={() => status.refetch()} />}
      {status.data && <AnalogBody status={status.data.data} h={h} onQueue={() => queue.mutate()} queueing={queue.isPending} />}
    </section>
  );
}

export function AnalogBody({ status, h, onQueue, queueing = false }: { status: AnalogStatus; h: number; onQueue: () => void; queueing?: boolean }) {
  if (status.state === 'ready') return <AnalogResult status={status} h={h} />;
  return (
    <div className="queued-note" role="status">
      <Badge tone={status.state === 'failed' ? 'bad' : 'warn'}>{ANALOG_STATE_TEXT[status.state]}</Badge>{' '}
      {status.state === 'not_built' && '这只股票在该发布时点的相似情景研究还没有构建。'}
      {status.state === 'failed' && `上次构建失败（${status.job?.error_code ?? '未知'}），可以重新排队。`}
      {(status.state === 'queued' || status.state === 'running') && <>任务{runStateText(status.job?.run_state ?? status.state)}，由后台任务执行（<code>{status.worker_hint}</code>）；这个页面每几秒检查一次。</>}{' '}
      {(status.state === 'not_built' || status.state === 'failed') && <button className="btn btn-on" disabled={queueing} onClick={onQueue}>生成相似情景</button>}
    </div>
  );
}

function AnalogResult({ status, h }: { status: AnalogStatus; h: number }) {
  const q = useQuery({
    queryKey: ['bffr', 'analog-cases', status.instrument_id, status.release_id, status.selection_policy_version, status.study?.content_hash],
    retry: false,
    queryFn: () => extB.queryAnalogCases({ instrument_id: status.instrument_id, release_id: status.release_id, feature_key_hash: status.feature_key_hash, selection_policy_version: status.selection_policy_version }, 100),
  });
  if (q.isLoading) return <Loading label="读取相似情景…" />;
  if (q.isError) return <ErrorState error={q.error} what="相似情景" onRetry={() => q.refetch()} />;
  return <Neighbours neighbors={q.data!.data.neighbors} h={h} neighbours={status.study?.neighbors ?? 0} />;
}

function Neighbours({ neighbors, h, neighbours }: { neighbors: AnalogNeighbor[]; h: number; neighbours: number }) {
  const s = outcomeSummary(neighbors.map((n) => n.outcome?.value ?? null));
  const labels = useQuery({ queryKey: ['bffr', 'analog-labels', neighbors.length, neighbors[0]?.instrument_id], queryFn: () => extB.instrumentLabels({ ids: [...new Set(neighbors.map((n) => n.instrument_id))] }), enabled: neighbors.length > 0 });
  const by = new Map((labels.data?.data ?? []).map((l) => [l.instrument_id, l]));
  if (neighbors.length === 0) return <EmptyState>研究已构建，但没有邻居。</EmptyState>;
  return (
    <>
      <div className="counts">
        <Badge>邻居 {neighbours || neighbors.length}</Badge><Badge tone="good">已知结果 {s.known}</Badge>{s.immature ? <Badge tone="warn">未到期 {s.immature}</Badge> : null}
        <Badge title="相似情景中后 N 日上涨的经验频率，不是校准概率">上涨经验频率 {pct(s.upShare, 0)}</Badge>
      </div>
      <p>未来 {h} 个交易日收益的经验分布：{[10, 25, 50, 75, 90].map((p) => `P${p} ${pct(s.q[p] ?? null)}`).join(' · ')}</p>
      <p className="muted small">相似情景不是预测，分布宽说明不确定性高；邻居只按特征距离选取，结果不参与选取；价格为前复权研究序列。</p>
      <table className="table dense"><thead><tr><th>#</th><th>股票</th><th>情景日期</th><th>距离</th><th>结果到期日</th><th>后{h}日收益</th></tr></thead>
        <tbody>{neighbors.slice(0, 30).map((n, i) => { const l = by.get(n.instrument_id); return (
          <tr key={`${n.instrument_id}${n.case_as_of}`}><td>{i + 1}</td><td>{l ? `${l.code} ${l.name ?? ''}` : n.instrument_id.slice(0, 8)}</td><td>{n.case_as_of.slice(0, 10)}</td><td className="num">{n.distance.toFixed(3)}</td><td>{n.outcome_end.slice(0, 10)}</td>
            <td className="num">{n.outcome ? metricPct(n.outcome.value) : <MissingText>未到期</MissingText>}</td></tr>); })}</tbody></table>
    </>
  );
}
