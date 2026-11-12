/**
 * 板块详情 (PEX03): one sector at one release - its members, the in-sector ranking and the frozen heating pool all use the
 * same membership snapshot. A sector without a ranking says why (coverage) instead of widening the member set.
 */
import { useQuery } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import { Badge } from '../../../components/Badges';
import { MetaStrip } from '../../../components/MetaStrip';
import { ReleaseBar } from '../../../components/ReleaseBar';
import { EmptyState, ErrorState, Loading, SectionTitle } from '../../../components/States';
import { TAXONOMIES, horizonByKey } from '../../../config';
import { useInstrumentLabel } from '../../../hooks/useInstrumentLabel';
import { useReleaseSelection } from '../../../hooks/useRelease';
import { shortId } from '../../../lib/format';
import './ext-a.css';
import { extOps } from './api';
import { useMarketToday, usePanel } from './hooks';
import type { Themes } from './types';

const SHOW_MEMBERS = 40;

function Name({ id, releaseId }: { id: string; releaseId: string | null }) {
  const l = useInstrumentLabel(id, releaseId);
  return <Link to={`/stock/${id}${releaseId ? `?release=${releaseId}` : ''}`}>{l.name}{l.symbol ? <span className="mono small muted"> {l.symbol}</span> : null}</Link>;
}

export function SectorDetailPage() {
  const rel = useReleaseSelection();
  const rid = rel.effectiveReleaseId;
  const [sp, setSp] = useSearchParams();
  const h = horizonByKey(sp.get('h'));
  const tax = TAXONOMIES.find((t) => t.id === sp.get('tax')) ?? (TAXONOMIES[0] as (typeof TAXONOMIES)[number]);
  const node = sp.get('node') ?? '';
  const today = useMarketToday(rid);
  const themes = usePanel<Themes>(today.data?.data, 'themes');
  const q = useQuery({
    queryKey: ['ext-a', 'sector', rid, tax.id, tax.version, node, h.horizonId, h.forecastTarget],
    queryFn: ({ signal }) => extOps.querySectorDetails({ sector: { taxonomy_id: tax.id, taxonomy_version: tax.version, node_id: node }, release_id: rid as string, horizon_id: h.horizonId, forecast_target: h.forecastTarget }, signal),
    enabled: !!rid && !!node,
  });
  const d = q.data?.data;
  return (
    <div className="page ea-page">
      <ReleaseBar rel={rel} displayedReleaseId={q.data?.meta.release_id} />
      <SectionTitle title={`板块详情${node ? ` · ${themes.body?.themes.find((t) => t.node_id === node)?.name ?? node}` : ''}`} hint={`${tax.label}分类 · ${h.label}（${h.hint}）`} />
      {!node && (
        <section className="card">
          <p>选择一个板块（来自当日题材共振）：</p>
          {themes.body?.themes.length ? (
            <ul className="ea-themes">{themes.body.themes.map((t) => <li key={t.node_id}><button className="btn btn-small" onClick={() => setSp((p) => { const n = new URLSearchParams(p); n.set('node', t.node_id); return n; })}>{t.name}</button></li>)}</ul>
          ) : <EmptyState>当前发布包没有题材共振榜；也可以在地址里加 ?node=板块编号。</EmptyState>}
        </section>
      )}
      {q.isLoading && <Loading />}
      {q.error && <ErrorState error={q.error} what="板块详情" onRetry={() => void q.refetch()} />}
      {q.data && <MetaStrip meta={q.data.meta} title="板块详情" />}
      {d && (
        <>
          <p className="muted small">成分、榜单、升温候选使用同一份成分快照 <span className="mono">{d.ranking.membership_snapshot_id?.slice(0, 8) ?? '无'}</span>；同一股票可同时属于其他板块。成员龙头/中军角色没有算法输出，保持未知。</p>
          <section className="card">
            <div className="ea-head"><h3>成分股（{d.members.length}）</h3></div>
            <ul className="ea-members">
              {d.members.slice(0, SHOW_MEMBERS).map((m) => <li key={m.instrument_id}><Name id={m.instrument_id} releaseId={rid} /></li>)}
            </ul>
            {d.members.length > SHOW_MEMBERS && <p className="muted small">另有 {d.members.length - SHOW_MEMBERS} 只未展开。</p>}
          </section>
          <section className="card">
            <div className="ea-head"><h3>板块内排名</h3><Badge tone="neutral">{d.ranking.ranking_policy_version}</Badge></div>
            {d.ranking.entries.length === 0 ? <EmptyState>本板块在这个发布包里没有排名：{q.data?.meta.coverage.unverifiable_reasons.join('；') || '未登记原因'}。不会扩大母集去凑榜单。</EmptyState> : (
              <table className="table">
                <thead><tr><th>排名</th><th>证券</th><th>相对得分</th><th>状态</th></tr></thead>
                <tbody>{d.ranking.entries.slice(0, 30).map((e) => (
                  <tr key={e.instrument_id}><td className="mono">{e.alpha_rank}</td><td><Name id={e.instrument_id} releaseId={rid} /></td><td className="mono">{e.relative_rank_score}</td><td>{e.action_status}</td></tr>
                ))}</tbody>
              </table>
            )}
          </section>
          <section className="card">
            <h3>升温候选池（冻结）</h3>
            {d.candidate_pools.every((p) => p.items.length === 0) ? <EmptyState>本板块暂无升温候选。</EmptyState> : d.candidate_pools.map((p) => (
              <div key={p.pool_id}>
                <p className="muted small">池 {shortId(p.pool_id)} · 政策 {p.policy_version}</p>
                <ul>{p.items.map((i) => <li key={i.pool_event_id}><Name id={i.instrument_id} releaseId={rid} /> <Badge>{i.current_state}</Badge> <span className="small muted">{i.trigger_code}</span></li>)}</ul>
              </div>
            ))}
          </section>
        </>
      )}
    </div>
  );
}
