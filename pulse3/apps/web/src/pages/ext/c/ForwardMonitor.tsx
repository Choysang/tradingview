/** 前向监控: rank IC and top-decile excess of each published ranking once its horizon is observed. All returns are gross; pending is not zero. */
import { useQuery } from '@tanstack/react-query';
import { Badge, MissingText } from '../../../components/Badges';
import { EmptyState, Loading, SectionTitle } from '../../../components/States';
import { fmtDate, fmtTime } from '../../../lib/format';
import { extGet } from './api';
import { MONITOR_MISSING_LABEL, MONITOR_PENDING_LABEL, pctText, type ForwardMonitorData, type ForwardMonitorRow } from './bfOps';
import { ExtError, Strip } from './common';

function Value({ v, reason }: { v: string | null; reason: string | null }) {
  return v === null ? <MissingText>{reason ? (MONITOR_MISSING_LABEL[reason] ?? reason) : '缺失'}</MissingText> : <>{v}</>;
}

function Row({ r }: { r: ForwardMonitorRow }) {
  if (r.status === 'pending') {
    const why = MONITOR_PENDING_LABEL[r.pending_reason ?? ''] ?? r.pending_reason ?? '待定';
    return (
      <tr>
        <td>{fmtDate(r.decision_session ?? r.decision_as_of)}</td><td>{r.horizon_id}</td>
        <td colSpan={5}><Badge tone="warn">待定</Badge> <span className="small muted">{why}{r.sessions_after !== null ? `（已观察 ${r.sessions_after}/${r.horizon_sessions} 个交易日）` : ''}</span></td>
      </tr>
    );
  }
  const excess = pctText(r.top_decile_excess);
  return (
    <tr>
      <td>{fmtDate(r.decision_session ?? r.decision_as_of)}</td><td>{r.horizon_id}</td>
      <td className="num mono"><Value v={r.rank_ic} reason={r.missing_reason} /></td>
      <td className="num mono">{excess === null ? <MissingText>{r.missing_reason ? (MONITOR_MISSING_LABEL[r.missing_reason] ?? r.missing_reason) : '缺失'}</MissingText> : excess}</td>
      <td className="num mono">{pctText(r.top_decile_mean_gross) ?? <MissingText />}</td>
      <td className="num mono">{pctText(r.universe_median_gross) ?? <MissingText />}</td>
      <td className="small muted" title={r.evaluated_at ? `计算于 ${fmtTime(r.evaluated_at)}` : undefined}>
        {r.n_evaluable ?? '—'}/{r.n_ranked ?? '—'} 只可评估 · {fmtDate(r.entry_session)} → {fmtDate(r.exit_session)}{r.record_kind === 'reconstruction' ? ' · 重建' : ''}
      </td>
    </tr>
  );
}

export function ForwardMonitor() {
  const q = useQuery({ queryKey: ['extc', 'bf-forward-monitor'], queryFn: ({ signal }) => extGet<ForwardMonitorData>('bf/ops/forward-monitor', undefined, signal), retry: false });
  const d = q.data?.data;
  return (
    <section aria-label="前向监控">
      <SectionTitle title="前向监控" hint="每个已发布排名在期限观察完毕后：排名与实际前向收益的秩相关（IC），以及前 10% 相对全体中位数的超额收益。收益均为毛收益，未扣任何成本；期限未到只显示待定，不当 0。" />
      <Strip meta={q.data?.meta} title="前向监控" />
      {q.isLoading ? <Loading /> : q.error ? <ExtError error={q.error} onRetry={() => { void q.refetch(); }} what="前向监控" /> : !d || d.rows.length === 0 ? <EmptyState>还没有已发布的排名可监控。</EmptyState> : (
        <div className="table-wrap">
          <table className="table">
            <caption className="muted small">已结算 {d.resolved_count} · 待定 {d.pending_count} · 方法 {d.method_version} · 收益口径：毛收益</caption>
            <thead><tr><th scope="col">决策日</th><th scope="col">期限</th><th scope="col" className="num">排名 IC</th><th scope="col" className="num">前 10% 超额（毛）</th><th scope="col" className="num">前 10% 均值（毛）</th><th scope="col" className="num">全体中位数（毛）</th><th scope="col">样本与区间</th></tr></thead>
            <tbody>{d.rows.map((r) => <Row key={r.ranking_id} r={r} />)}</tbody>
          </table>
        </div>
      )}
    </section>
  );
}
