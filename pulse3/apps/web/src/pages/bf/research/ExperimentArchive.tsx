/**
 * Experiment archive (triage #76): every past experiment, newest first, filterable by strategy instance, purpose and state, paged by the server.
 * A failed run shows its error code (a failure is a recorded trial, not an empty success); a finished run is not shown as verified profit.
 */
import { useInfiniteQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { Badge, MissingText } from '../../../components/Badges';
import { EmptyState, ErrorState, Loading } from '../../../components/States';
import { RUN_PURPOSE_LABEL, RUN_STATE_LABEL, fmtTime } from '../../../lib/format';
import type { ExperimentItem, RunPurpose, RunState } from './api';
import { bfResearch } from './api';
import { pctText } from './format';
import './research.css';

const KIND_TEXT: Record<ExperimentItem['kind'], string> = { playbook: '战法', factor: '因子', exit_rule: '卖法', intraday_t: '做T', other: '其他' };
const PURPOSES: RunPurpose[] = ['screening', 'validation', 'shadow', 'blind_final'];
const STATES: RunState[] = ['queued', 'running', 'completed', 'failed', 'cancelled'];

export function ExperimentArchive() {
  const [instance, setInstance] = useState('');
  const [purpose, setPurpose] = useState<RunPurpose | ''>('');
  const [status, setStatus] = useState<RunState | ''>('');
  const f = { instance_id: instance.trim() || undefined, purpose, status };
  const validInstance = !f.instance_id || /^[0-9a-f-]{36}$/i.test(f.instance_id);
  const q = useInfiniteQuery({
    queryKey: ['bfr', 'archive', f], enabled: validInstance, retry: false, initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => bfResearch.experiments(f, 30, pageParam, signal),
    getNextPageParam: (p) => p.data.page.next_cursor,
  });
  const items = q.data?.pages.flatMap((p) => p.data.items) ?? [];
  return (
    <section className="bfr archive">
      <div className="filters">
        <label className="select-label">策略实例（UUID）<input value={instance} onChange={(e) => setInstance(e.target.value)} placeholder="全部" aria-invalid={!validInstance} /></label>
        <label className="select-label">目的<select value={purpose} onChange={(e) => setPurpose(e.target.value as RunPurpose | '')}><option value="">全部</option>{PURPOSES.map((p) => <option key={p} value={p}>{RUN_PURPOSE_LABEL[p]}</option>)}</select></label>
        <label className="select-label">状态<select value={status} onChange={(e) => setStatus(e.target.value as RunState | '')}><option value="">全部</option>{STATES.map((s) => <option key={s} value={s}>{RUN_STATE_LABEL[s]}</option>)}</select></label>
      </div>
      {!validInstance && <div className="state state-error" role="alert">策略实例需要是 36 位 UUID。</div>}
      {q.isLoading && <Loading />}
      {q.isError && <ErrorState error={q.error} what="实验档案" onRetry={() => q.refetch()} />}
      {q.isSuccess && items.length === 0 && <EmptyState>没有符合条件的实验。</EmptyState>}
      {items.length > 0 && (
        <div className="scroll">
          <table className="table dense">
            <caption className="small muted">{q.data?.pages[0]?.data.order}</caption>
            <thead><tr><th>时间</th><th>实验</th><th>类型</th><th>目的</th><th>状态</th><th>验证期净收益</th><th>最大回撤</th><th>成交笔数</th><th /></tr></thead>
            <tbody>
              {items.map((e) => (
                <tr key={e.experiment_id}>
                  <td>{fmtTime(e.completed_at ?? e.started_at)}</td>
                  <td title={e.experiment_id}>{e.display_name}<div className="muted small">{e.catalog_id ?? '—'}</div></td>
                  <td>{KIND_TEXT[e.kind]}</td><td>{RUN_PURPOSE_LABEL[e.run_purpose]}</td>
                  <td><Badge tone={e.run_state === 'completed' ? 'good' : e.run_state === 'failed' ? 'bad' : 'neutral'}>{RUN_STATE_LABEL[e.run_state]}</Badge>{e.error_code && <div className="small">{e.error_code}</div>}</td>
                  <td className="num">{e.run_state === 'completed' ? pctText(e.net_return_validation) : '—'}</td>
                  <td className="num">{e.run_state === 'completed' ? pctText(e.max_drawdown) : '—'}</td>
                  <td className="num">{e.trade_count == null ? <MissingText>—</MissingText> : Number(e.trade_count)}</td>
                  <td>{e.has_replay && e.kind === 'playbook' && <Link to="/playbooks" className="small">净值与成交</Link>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {q.hasNextPage && <button className="btn" disabled={q.isFetchingNextPage} onClick={() => q.fetchNextPage()}>{q.isFetchingNextPage ? '加载中…' : '加载更多'}</button>}
      <p className="muted small">净值与回撤来自该实验的指标表（前复权研究序列，已含用户费用口径），不代表已验证的盈利能力；失败的试验同样保留。</p>
    </section>
  );
}
