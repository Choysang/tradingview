/**
 * Home: four entries (spec 1.1) x horizon tabs. Every list is pinned to the release
 * (and ranking) it started from; "follow latest / pinned" lives in ReleaseBar.
 */
import { useQuery } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import { ops } from '../api/ops';
import type { AnalysisRelease } from '../api/types';
import { Badge } from '../components/Badges';
import { ActionabilityList } from '../components/Account';
import { PagedRanking } from '../components/PagedRanking';
import { PoolRows, SurgeComponents, InstrumentName, WatchStar, stockHref } from '../components/Lists';
import { MetaStrip } from '../components/MetaStrip';
import { ReleaseBar } from '../components/ReleaseBar';
import { EmptyState, ErrorState, Loading, SectionTitle } from '../components/States';
import { HORIZONS, TAXONOMIES, horizonByKey } from '../config';
import { useAccountContext } from '../hooks/useAccount';
import { usePinnedPages } from '../hooks/usePinnedPages';
import { useReleaseSelection, type ReleaseCtx } from '../hooks/useRelease';
import { useWatchlists } from '../hooks/useUserState';
import { RANK_KIND_LABEL, fmtTime } from '../lib/format';

type EntryKey = 'market' | 'sector' | 'heating' | 'mine';
const ENTRIES: Array<{ key: EntryKey; title: string; hint: string }> = [
  { key: 'market', title: '全市场精选', hint: '全市场按期限排好的研究机会顺序' },
  { key: 'sector', title: '板块机会', hint: '哪些板块更值得先看' },
  { key: 'heating', title: '快速升温候选', hint: '刚开始升温的板块与股票，提前关注' },
  { key: 'mine', title: '自选与持仓', hint: '你关注和持有的股票' },
];

export function poolIdOf(release: AnalysisRelease | undefined): string | null {
  const c = release?.components.find((x) => x.component_key.startsWith('candidate_pool:'));
  return c ? c.component_key.slice('candidate_pool:'.length) : null;
}

export function HomePage() {
  const rel = useReleaseSelection();
  const [sp, setSp] = useSearchParams();
  const entry = (ENTRIES.find((e) => e.key === sp.get('entry'))?.key ?? 'market') as EntryKey;
  const horizon = horizonByKey(sp.get('h'));
  const setParam = (k: string, v: string): void => setSp((prev) => { const n = new URLSearchParams(prev); n.set(k, v); return n; }, { replace: true });
  const usesHorizon = entry === 'market' || entry === 'sector';

  return (
    <div className="page">
      <ReleaseBar rel={rel} displayedReleaseId={rel.release.data?.data.release_id} />
      <nav className="entry-tabs" aria-label="首页入口">
        {ENTRIES.map((e) => (
          <button key={e.key} className={`entry-tab ${entry === e.key ? 'on' : ''}`} aria-pressed={entry === e.key} onClick={() => setParam('entry', e.key)}>
            <b>{e.title}</b><span className="muted small">{e.hint}</span>
          </button>
        ))}
      </nav>
      {usesHorizon && (
        <div className="horizon-bar">
          <div className="segmented" role="tablist" aria-label="期限">
            {HORIZONS.map((h) => (
              <button key={h.key} role="tab" aria-selected={horizon.key === h.key} className={horizon.key === h.key ? 'on' : ''} onClick={() => setParam('h', h.key)}>{h.label}</button>
            ))}
          </div>
          <span className="muted small">{horizon.label}：{horizon.hint}。同一只股票在不同期限可能有不同结论。</span>
        </div>
      )}
      {!rel.effectiveReleaseId && (rel.pointer.isLoading ? <Loading label="读取当前发布版本…" /> : <ErrorState error={rel.pointer.error ?? new Error('当前没有已发布的版本')} what="发布版本" onRetry={() => void rel.pointer.refetch()} />)}
      {rel.effectiveReleaseId && entry === 'market' && <MarketEntry rel={rel} horizonKey={horizon.key} />}
      {rel.effectiveReleaseId && entry === 'sector' && <SectorEntry rel={rel} horizonKey={horizon.key} />}
      {rel.effectiveReleaseId && entry === 'heating' && <HeatingEntry rel={rel} />}
      {entry === 'mine' && <MineEntry rel={rel} />}
    </div>
  );
}

function MarketEntry({ rel, horizonKey }: { rel: ReleaseCtx; horizonKey: string }) {
  const h = horizonByKey(horizonKey);
  const rid = rel.effectiveReleaseId as string;
  const pages = usePinnedPages({
    queryKey: ['ranking', rid, h.horizonId, h.forecastTarget],
    fetchPage: (cursor, signal) => ops.getMarketRanking({ release_id: rid, horizon_id: h.horizonId, forecast_target: h.forecastTarget, cursor }, { signal }),
    pinOf: (p) => ({ releaseId: p.data.release_id, rankingId: p.data.ranking_id, actionabilityId: p.meta.actionability_id }),
    entriesOf: (p) => p.data.entries,
    keyOf: (e) => e.instrument_id,
  });
  return (
    <section>
      <SectionTitle title={`全市场精选 · ${h.label}`} hint="名次只表示同一目标、同一股票池内的相对顺序；带「校准概率」标签的才是经过校准的概率。" />
      <PagedRanking pages={pages} horizonKey={h.key} title={`全市场精选 ${h.label}`} expectedReleaseId={rid} />
    </section>
  );
}

function SectorEntry({ rel, horizonKey }: { rel: ReleaseCtx; horizonKey: string }) {
  const h = horizonByKey(horizonKey);
  const rid = rel.effectiveReleaseId as string;
  const [sp, setSp] = useSearchParams();
  const tax = TAXONOMIES.find((t) => t.id === sp.get('tax')) ?? (TAXONOMIES[0] as (typeof TAXONOMIES)[number]);
  const kind = (['sector_opportunity_rank', 'sector_attention_rank', 'sector_rising_rank'] as const).find((k) => k === sp.get('kind')) ?? 'sector_opportunity_rank';
  const set = (k: string, v: string): void => setSp((p) => { const n = new URLSearchParams(p); n.set(k, v); return n; }, { replace: true });
  const q = useQuery({
    queryKey: ['sectorOverview', rid, tax.id, tax.version, kind, h.horizonId, h.forecastTarget],
    queryFn: ({ signal }) => ops.getSectorOverview({ release_id: rid, taxonomy_id: tax.id, taxonomy_version: tax.version, rank_kind: kind, horizon_id: h.horizonId, forecast_target: h.forecastTarget }, { signal }),
    placeholderData: (prev) => prev,
  });
  return (
    <section>
      <SectionTitle title={`板块机会 · ${h.label}`} hint="不同分类口径（行业/概念）各自独立排名，不强行合并；同一股票可属于多个板块。" />
      <div className="horizon-bar">
        <div className="segmented" role="tablist" aria-label="板块分类">
          {TAXONOMIES.map((t) => <button key={t.id} role="tab" aria-selected={tax.id === t.id} className={tax.id === t.id ? 'on' : ''} onClick={() => set('tax', t.id)}>{t.label}</button>)}
        </div>
        <div className="segmented" role="tablist" aria-label="榜单类型">
          {(['sector_opportunity_rank', 'sector_attention_rank', 'sector_rising_rank'] as const).map((k) => <button key={k} role="tab" aria-selected={kind === k} className={kind === k ? 'on' : ''} onClick={() => set('kind', k)}>{RANK_KIND_LABEL[k]}</button>)}
        </div>
      </div>
      {q.isLoading && <Loading />}
      {q.error && !q.data && <ErrorState error={q.error} what="板块榜" onRetry={() => void q.refetch()} />}
      {q.data && (
        <div className={q.isPlaceholderData ? 'is-updating' : ''}>
          <MetaStrip meta={q.data.meta} title="板块榜" extra={q.isPlaceholderData ? <Badge tone="warn">正在更新，显示旧版本</Badge> : undefined}
            rows={[['ranking_id', q.data.data.ranking_id], ['release_id', q.data.data.release_id], ['taxonomy', `${q.data.data.taxonomy_id} v${q.data.data.taxonomy_version}`], ['policy', q.data.data.ranking_policy_version]]} />
          {q.data.data.entries.length === 0 ? <EmptyState>这个分类下没有板块。</EmptyState> : (
            <ol className="rows" aria-label="板块排名">
              {q.data.data.entries.map((s) => (
                <li key={s.taxonomy_node.node_id} className="row">
                  <span className="rank">{s.rank}</span>
                  <div className="row-main">
                    <Link className="row-link" to={`/sector/${encodeURIComponent(s.taxonomy_node.node_id)}?${new URLSearchParams({ release: q.data.data.release_id, tax: tax.id, h: h.key }).toString()}`}><b>{s.taxonomy_node.node_id}</b></Link>
                    <div className="row-sub muted small">
                      成分 {s.observed_member_count}/{s.member_count} 只有数据 · 得分 {s.score}（相对分，不是概率）
                      {s.observed_member_count < s.member_count && <Badge tone="warn">部分成分缺失</Badge>}
                    </div>
                  </div>
                  <div className="row-side"><Badge tone={s.quality_status === 'valid' ? 'good' : 'warn'}>{s.quality_status === 'valid' ? '数据完整' : '部分缺失'}</Badge></div>
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
    </section>
  );
}

function HeatingEntry({ rel }: { rel: ReleaseCtx }) {
  const rid = rel.effectiveReleaseId as string;
  const release = rel.release.data?.data;
  const poolId = poolIdOf(release);
  const pages = usePinnedPages({
    queryKey: ['pool', rid, poolId],
    enabled: !!poolId,
    fetchPage: (cursor, signal) => ops.getCandidatePool(poolId as string, { release_id: rid, cursor }, { signal }),
    pinOf: (p) => ({ releaseId: p.data.release_id, rankingId: p.data.pool_id, actionabilityId: null }),
    entriesOf: (p) => p.data.items,
    keyOf: (i) => i.pool_event_id,
  });
  const tax = TAXONOMIES[0] as (typeof TAXONOMIES)[number];
  const h = horizonByKey(null);
  const rising = useQuery({
    queryKey: ['sectorOverview', rid, tax.id, tax.version, 'sector_rising_rank', h.horizonId, h.forecastTarget],
    queryFn: ({ signal }) => ops.getSectorOverview({ release_id: rid, taxonomy_id: tax.id, taxonomy_version: tax.version, rank_kind: 'sector_rising_rank', horizon_id: h.horizonId, forecast_target: h.forecastTarget }, { signal }),
  });
  return (
    <section>
      <SectionTitle title="快速升温候选" hint="升温是「提前关注」的线索，数据不足时只放入观察，不会直接出现在买入榜。" />
      <h3>升温中的板块</h3>
      {rising.isLoading && <Loading />}
      {rising.data && (
        <ol className="rows">
          {rising.data.data.entries.slice(0, 3).map((s) => (
            <li key={s.taxonomy_node.node_id} className="row">
              <span className="rank">{s.rank}</span>
              <div className="row-main"><Link className="row-link" to={`/sector/${encodeURIComponent(s.taxonomy_node.node_id)}?${new URLSearchParams({ release: rid, tax: tax.id, h: h.key }).toString()}`}><b>{s.taxonomy_node.node_id}</b></Link></div>
              <div className="row-side muted small">升温得分 {s.score}</div>
            </li>
          ))}
        </ol>
      )}
      <h3>升温由哪些部分组成</h3>
      <p className="muted small">每一项都来自本次发布；缺失、过期或计算失败的项明确标出，不会按 0 计入。</p>
      <SurgeComponents release={release} />
      <h3>候选股票</h3>
      {!poolId && (rel.release.isLoading ? <Loading /> : <EmptyState>本次发布没有声明升温候选池。</EmptyState>)}
      {poolId && pages.isLoading && <Loading />}
      {pages.error && !pages.firstPage && <ErrorState error={pages.error} what="升温候选池" onRetry={pages.reload} />}
      {pages.firstPage && (
        <div className={pages.isPlaceholder ? 'is-updating' : ''}>
          <MetaStrip meta={pages.firstPage.meta} title="升温候选池" rows={[['pool_id', pages.firstPage.data.pool_id], ['policy', `${pages.firstPage.data.policy_id} ${pages.firstPage.data.policy_version}`]]} />
          {pages.paging.entries.length === 0 ? <EmptyState>候选池为空。</EmptyState> : <PoolRows items={pages.paging.entries} releaseId={pages.firstPage.data.release_id} />}
          {pages.hasMore && <button className="btn" onClick={pages.loadMore}>加载更多</button>}
        </div>
      )}
    </section>
  );
}

function MineEntry({ rel }: { rel: ReleaseCtx }) {
  const wl = useWatchlists();
  const acct = useAccountContext();
  const rid = rel.effectiveReleaseId;
  const members = wl.primary?.members ?? [];
  return (
    <section>
      <SectionTitle title="自选与持仓" hint="自选保存在服务端，换设备也在；清空浏览器缓存不会丢失。" />
      <h3>我的自选{wl.primary ? `：${wl.primary.display_name}` : ''}</h3>
      {wl.isLoading && <Loading />}
      {wl.error && <ErrorState error={wl.error} what="自选" />}
      {!wl.isLoading && members.length === 0 && <EmptyState>还没有自选股票。在榜单或个股页点击 ☆ 加入。</EmptyState>}
      {members.length > 0 && (
        <ul className="rows">
          {members.map((m) => (
            <li key={m.instrument_id} className="row">
              <span className="rank">{m.sort_position}</span>
              <div className="row-main">
                <Link className="row-link" to={stockHref(m.instrument_id, rid)}><InstrumentName instrumentId={m.instrument_id} releaseId={rid} /></Link>
                <div className="row-sub muted small">加入于 {fmtTime(m.added_at)}{m.note ? ` · ${m.note}` : ''}</div>
              </div>
              <WatchStar instrumentId={m.instrument_id} releaseId={rid} />
            </li>
          ))}
        </ul>
      )}
      <h3>账户可行动</h3>
      {!acct.complete ? (
        <EmptyState>还没有选择账户。请到 <Link to="/portfolio">账户与可行动</Link> 页填写账户信息。</EmptyState>
      ) : (
        <ActionabilityList ctx={acct.ctx} releaseId={rid} candidateInstrumentIds={members.map((m) => m.instrument_id)} />
      )}
    </section>
  );
}
