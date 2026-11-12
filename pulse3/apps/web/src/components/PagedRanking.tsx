import type { Envelope, Ranking } from '../api/types';
import type { PinnedPages } from '../hooks/usePinnedPages';
import { Badge } from './Badges';
import { RankingRows } from './Lists';
import { MetaStrip } from './MetaStrip';
import { EmptyState, ErrorState, Loading } from './States';

type RankingPage = Envelope<Ranking>;

/** A ranking pinned to its ranking_id/release_id; "加载更多" continues the same list. */
export function PagedRanking({ pages, horizonKey, title, expectedReleaseId }: { pages: PinnedPages<RankingPage, Ranking['entries'][number]>; horizonKey?: string; title: string; expectedReleaseId: string | null }) {
  if (pages.isLoading) return <Loading label="读取榜单…" />;
  if (pages.error && pages.paging.entries.length === 0) return <ErrorState error={pages.error} what="榜单" onRetry={pages.reload} />;
  const first = pages.firstPage;
  if (!first) return <EmptyState>暂无榜单。</EmptyState>;
  const shownRelease = first.data.release_id;
  return (
    <div className={pages.isPlaceholder ? 'is-updating' : ''}>
      <MetaStrip
        meta={first.meta}
        title={title}
        extra={
          <>
            <Badge tone="neutral" title="本榜固定的榜单编号，翻页不会换榜">榜单 {first.data.ranking_id.slice(0, 8)}</Badge>
            {pages.isPlaceholder && <Badge tone="warn">正在更新，显示旧版本</Badge>}
          </>
        }
        rows={[
          ['ranking_id', first.data.ranking_id], ['release_id', first.data.release_id], ['forecast_target', first.data.forecast_target],
          ['horizon_id', first.data.horizon_id], ['ranking_policy_version', first.data.ranking_policy_version], ['scope', `${first.data.ranking_scope}:${first.data.scope_key}`],
        ]}
      />
      {pages.paging.entries.length === 0 ? <EmptyState>这个榜单没有可显示的股票。</EmptyState> : (
        <RankingRows entries={pages.paging.entries} releaseId={shownRelease ?? expectedReleaseId} horizonKey={horizonKey} />
      )}
      {pages.paging.rejected && <ErrorState error={new Error(pages.paging.rejected.reason)} what="翻页" onRetry={pages.reload} />}
      {pages.error && pages.paging.entries.length > 0 && <ErrorState error={pages.error} what="下一页" onRetry={pages.loadMore} />}
      <div className="pager">
        <span className="muted small">已显示 {pages.paging.entries.length} 只，顺序即服务端排名顺序，翻页不会重新排序。</span>
        {pages.hasMore && <button className="btn" onClick={pages.loadMore} disabled={pages.isFetchingMore}>{pages.isFetchingMore ? '加载中…' : '加载更多'}</button>}
      </div>
    </div>
  );
}
