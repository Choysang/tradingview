/**
 * 榜单战绩 (PEX02): every published ranking next to what happened afterwards, with no backfilled hindsight.
 * The ranking list comes from queryRankingHistory (stable pages pinned to a watermark); the outcomes are separate artifacts
 * (queryRankingTrackRecord) that exist only once the horizon has been observed. Unresolved rankings stay "待到期".
 * The avoid group is shown as a descriptive long-only return of the names that were NOT picked - never as a short return.
 */
import { useInfiniteQuery, useQueries, useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { Badge } from '../../../components/Badges';
import { EmptyState, ErrorState, Loading, SectionTitle } from '../../../components/States';
import { HORIZONS, horizonByKey } from '../../../config';
import { fmtTime, shortId } from '../../../lib/format';
import './ext-a.css';
import { artifactQuery, extOps, MARKET_SCOPE } from './api';
import { pctText } from './charts';
import type { OutcomeBody, Metric } from './types';

const PAGE = 20;
const MIN_SAMPLE = 20;

export function metricMap(metrics: Metric[]): Record<string, Metric> {
  return Object.fromEntries(metrics.map((m) => [m.name, m]));
}

/** Plain-language state of one ranking row (pure: unit-tested). */
export function outcomeState(body: OutcomeBody | undefined): 'resolved' | 'pending' {
  return body && body.status === 'resolved' ? 'resolved' : 'pending';
}

export function TrackRecordPage() {
  const [hk, setHk] = useState(horizonByKey('short').key);
  const h = horizonByKey(hk);
  const window = useMemo(() => {
    const end = new Date();
    end.setUTCHours(0, 0, 0, 0);
    end.setUTCDate(end.getUTCDate() + 2);
    const start = new Date(end.getTime() - 400 * 86_400_000);
    return { start: start.toISOString().replace('.000', ''), end: end.toISOString().replace('.000', '') };
  }, []);
  const asOf = useMemo(() => new Date().toISOString().replace(/\.\d+Z$/, 'Z'), []);

  const hist = useInfiniteQuery({
    queryKey: ['ext-a', 'history', h.horizonId, h.forecastTarget],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => extOps.queryRankingHistory({ scope: { ...MARKET_SCOPE, scope_key: 'market' }, window, horizon_id: h.horizonId, forecast_target: h.forecastTarget, page: { limit: PAGE, cursor: pageParam } }, signal),
    getNextPageParam: (last) => last.data.page.next_cursor,
  });
  const entries = hist.data?.pages.flatMap((p) => p.data.entries) ?? [];
  const ids = entries.map((e) => e.ranking_id);
  const track = useQuery({
    queryKey: ['ext-a', 'track', ids.join(',')],
    queryFn: ({ signal }) => extOps.queryRankingTrackRecord(ids, asOf, signal),
    enabled: ids.length > 0 && ids.length <= 200,
  });
  const bodies = useQueries({ queries: (track.data?.data.outcomes ?? []).map((o) => artifactQuery<OutcomeBody>(o.ref)) });
  const byRanking = new Map<string, OutcomeBody>();
  for (const b of bodies) if (b.data) byRanking.set(b.data.body.ranking_id, b.data.body);

  const resolved = entries.filter((e) => outcomeState(byRanking.get(e.ranking_id)) === 'resolved');
  const live = resolved.map((e) => byRanking.get(e.ranking_id) as OutcomeBody).filter((b) => b.record_kind === 'live_forward');
  const excess = live.map((b) => Number(metricMap(b.metrics)['top_mean_excess_return']?.value)).filter((n) => Number.isFinite(n));
  const mean = excess.length ? excess.reduce((a, b) => a + b, 0) / excess.length : null;

  return (
    <div className="page ea-page">
      <SectionTitle title="榜单战绩" hint="每一期发布的榜单是不可变的；后来的真实走势单独记成“结果”，到期前一律显示待到期，不提前、不回填。" />
      <div className="segmented" role="group" aria-label="期限">
        {HORIZONS.map((x) => <button key={x.key} className={x.key === hk ? 'btn-on' : ''} onClick={() => setHk(x.key)} title={x.hint}>{x.label}</button>)}
      </div>
      <p className="muted small">过滤条件是真实的期限与预测目标（{h.horizonId} · {h.forecastTarget}），“短线/波段/长期”只是显示名称。</p>
      {hist.isLoading && <Loading />}
      {hist.error && <ErrorState error={hist.error} what="榜单历史" onRetry={() => void hist.refetch()} />}
      {track.error && <ErrorState error={track.error} what="榜单结果" />}
      {hist.data && entries.length === 0 && <EmptyState>这个期限还没有已发布的榜单。</EmptyState>}
      {entries.length > 0 && (
        <>
          <div className="cards">
            <div className="card stat"><div className="muted small">已发布榜单</div><div className="stat-value mono">{entries.length}{hist.hasNextPage ? '+' : ''}</div></div>
            <div className="card stat"><div className="muted small">已有结果</div><div className="stat-value mono">{resolved.length}</div></div>
            <div className="card stat"><div className="muted small">待到期</div><div className="stat-value mono">{entries.length - resolved.length}</div></div>
            <div className="card stat">
              <div className="muted small">实盘前向：前 N 平均超额</div>
              <div className="stat-value mono">{live.length >= MIN_SAMPLE && mean != null ? pctText(String(mean * 100)) : '样本不足'}</div>
              <div className="small muted">{live.length} 期；少于 {MIN_SAMPLE} 期不下结论</div>
            </div>
          </div>
          <table className="table ea-track">
            <thead><tr><th>决策时间</th><th>发布版本</th><th>状态</th><th>前 N 平均超额（扣成本）</th><th>命中率（按机会数）</th><th>机会 / 可评估</th><th>未入选组描述收益</th><th>记录类型</th></tr></thead>
            <tbody>
              {entries.map((e) => {
                const b = byRanking.get(e.ranking_id);
                const m = b ? metricMap(b.metrics) : {};
                const done = outcomeState(b) === 'resolved';
                return (
                  <tr key={e.ranking_id} className={done ? '' : 'muted'}>
                    <td>{fmtTime(e.decision_as_of)}</td>
                    <td className="mono">{shortId(e.release_id)}</td>
                    <td>{done ? <Badge tone="good">已结算 {b?.exit_session}</Badge> : <Badge tone="neutral">待到期</Badge>}</td>
                    <td className="mono">{done ? pctText(m['top_mean_excess_return']?.value == null ? null : String(Number(m['top_mean_excess_return'].value) * 100)) : '—'}</td>
                    <td className="mono">{done ? pctText(m['top_hit_rate_over_opportunities']?.value == null ? null : String(Number(m['top_hit_rate_over_opportunities'].value) * 100), 0, false) : '—'}</td>
                    <td className="mono">{done ? `${m['top_opportunity_count']?.value ?? '—'} / ${m['top_evaluable_count']?.value ?? '—'}` : '—'}</td>
                    <td className="mono">{done ? pctText(m['avoid_descriptive_long_only_mean_excess_return']?.value == null ? null : String(Number(m['avoid_descriptive_long_only_mean_excess_return'].value) * 100)) : '—'}</td>
                    <td>{b?.record_kind === 'reconstruction' ? <Badge tone="warn" title="历史重建，不计入实盘前向统计">历史重建</Badge> : done ? <Badge tone="neutral">实盘前向</Badge> : '—'}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {hist.hasNextPage && <button className="btn" disabled={hist.isFetchingNextPage} onClick={() => void hist.fetchNextPage()}>加载更早的榜单</button>}
          <details className="small card">
            <summary>评价口径</summary>
            <ul>
              <li>榜单决策日收盘后形成；入场按下一交易日开盘价，持有到期限最后一个交易日收盘价；用未复权价，路径中出现不可能的涨跌幅（除权等）则该股“不可评估”并单独计数。</li>
              <li>超额 = 个股毛收益 − 往返成本 − 全部可评估入榜股票的中位数收益。成本口径见各期记录。</li>
              <li>命中率分母是预先登记的机会数（前 N），未下单、无法评估的也计入分母；不把回避组反转成未经验证的做空收益。</li>
              <li>“历史重建”是用事后规则重新生成的榜单，只作对照，不与实盘前向混算。</li>
            </ul>
          </details>
        </>
      )}
    </div>
  );
}
