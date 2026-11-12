/**
 * Sector page: the sector's own opportunity ranking, its in-sector stock list and the
 * fast-heating pool restricted to this sector, with each surge component (缺失 shown).
 */
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { ops } from '../api/ops';
import { PagedRanking } from '../components/PagedRanking';
import { PoolRows, SurgeComponents } from '../components/Lists';
import { MetaStrip } from '../components/MetaStrip';
import { ReleaseBar } from '../components/ReleaseBar';
import { EmptyState, ErrorState, Loading, SectionTitle } from '../components/States';
import { TAXONOMIES, horizonByKey } from '../config';
import { usePinnedPages } from '../hooks/usePinnedPages';
import { useReleaseSelection } from '../hooks/useRelease';
import { poolIdOf } from './HomePage';

export function SectorPage() {
  const { nodeId = '' } = useParams();
  const node = decodeURIComponent(nodeId);
  const rel = useReleaseSelection();
  const [sp] = useSearchParams();
  const h = horizonByKey(sp.get('h'));
  const tax = TAXONOMIES.find((t) => t.id === sp.get('tax')) ?? (TAXONOMIES[0] as (typeof TAXONOMIES)[number]);
  const rid = rel.effectiveReleaseId;
  const release = rel.release.data?.data;

  const ranking = usePinnedPages({
    queryKey: ['sectorRanking', rid, node, tax.id, tax.version, h.horizonId, h.forecastTarget],
    enabled: !!rid,
    fetchPage: (cursor, signal) => ops.getSectorRanking(node, { release_id: rid as string, taxonomy_id: tax.id, taxonomy_version: tax.version, horizon_id: h.horizonId, forecast_target: h.forecastTarget, cursor }, { signal }),
    pinOf: (p) => ({ releaseId: p.data.release_id, rankingId: p.data.ranking_id, actionabilityId: null }),
    entriesOf: (p) => p.data.entries,
    keyOf: (e) => e.instrument_id,
  });
  const poolId = poolIdOf(release);
  const pool = usePinnedPages({
    queryKey: ['pool', rid, poolId],
    enabled: !!rid && !!poolId,
    fetchPage: (cursor, signal) => ops.getCandidatePool(poolId as string, { release_id: rid as string, cursor }, { signal }),
    pinOf: (p) => ({ releaseId: p.data.release_id, rankingId: p.data.pool_id, actionabilityId: null }),
    entriesOf: (p) => p.data.items,
    keyOf: (i) => i.pool_event_id,
  });
  const inSector = pool.paging.entries.filter((i) => i.taxonomy_node.node_id === node);

  return (
    <div className="page">
      <ReleaseBar rel={rel} displayedReleaseId={ranking.firstPage?.data.release_id} />
      <p><Link to={`/?entry=sector&h=${h.key}${rel.pinnedReleaseId ? `&release=${rel.pinnedReleaseId}` : ''}`}>← 返回板块机会</Link></p>
      <SectionTitle title={node} hint={`${tax.label}分类 · ${h.label}（${h.hint}）`} />
      {ranking.firstPage && (
        <p className="muted small">
          成分股名单固定在成分快照 <span className="mono">{ranking.firstPage.data.membership_snapshot_id?.slice(0, 8) ?? '无'}</span>（同一次发布内不变）。同一股票可同时属于其他板块，组合层会单独计算集中度。
        </p>
      )}
      <h3>板块内股票排名</h3>
      {!rid ? <Loading /> : <PagedRanking pages={ranking} horizonKey={h.key} title={`${node} 板块内排名`} expectedReleaseId={rid} />}
      <h3>板块升温情况</h3>
      <p className="muted small">每个组成项都来自本次发布；缺失、过期、失败的项明确标出，不按 0 计入。</p>
      <SurgeComponents release={release} />
      <h3>本板块的升温候选</h3>
      {pool.isLoading && <Loading />}
      {pool.error && !pool.firstPage && <ErrorState error={pool.error} what="候选池" onRetry={pool.reload} />}
      {pool.firstPage && (
        <>
          <MetaStrip meta={pool.firstPage.meta} title="升温候选池" />
          {inSector.length === 0 ? <EmptyState>本板块暂无升温候选。</EmptyState> : <PoolRows items={inSector} releaseId={pool.firstPage.data.release_id} />}
        </>
      )}
    </div>
  );
}
