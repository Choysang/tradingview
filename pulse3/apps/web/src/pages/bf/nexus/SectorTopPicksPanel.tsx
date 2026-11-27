/**
 * Sector Top Buy Leaderboard & Rapidly Rising Watch Pool (Nexus 2026).
 * Displayed immediately upon entering any sector page (/sector/:nodeId) so the user sees
 * the top recommended stocks to buy in that sector, classified by sector role
 * (龙一 / 龙二 / 容量中军 / 异动先锋 / 低位补涨), with buy zone, stop loss, target price,
 * 7 surge components (ready vs missing), and watch pool entries.
 */
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { Badge, MissingText } from '../../../components/Badges';
import { stockHref } from '../../../components/Lists';
import { EmptyState, Loading, SectionTitle } from '../../../components/States';
import { fmtVal, nexusOps, SECTOR_ROLE_TEXT } from './api';
import './nexus.css';

export function SectorTopPicksPanel({
  nodeId,
  taxonomyId,
  horizonId,
  horizonKey,
  releaseId,
}: {
  nodeId: string;
  taxonomyId: string;
  horizonId: string;
  horizonKey: string;
  releaseId: string | null | undefined;
}) {
  const q = useQuery({
    queryKey: ['bf-nexus', 'sector-deep-dive', nodeId, taxonomyId, horizonId, releaseId],
    queryFn: ({ signal }) =>
      nexusOps.sectorDeepDive(nodeId, { taxonomy_id: taxonomyId, horizon_id: horizonId, release_id: releaseId ?? undefined }, signal),
    enabled: !!nodeId && !!releaseId,
    retry: false,
    staleTime: 45_000,
  });

  if (q.isLoading) return <Loading label="计算板块内龙头梯队与最推荐买入榜单…" />;
  // Graceful non-blocking degradation when running against minimal mock tests without /nexus mocked
  if (q.error || !q.data) return null;

  const d = q.data.data;

  return (
    <section className="card" aria-label="板块内最推荐买入榜单">
      <SectionTitle
        title={`${d.node_id} · 当前最推荐买入个股榜`}
        hint="按多策略正交合成得分排序，自动识别龙一/龙二/容量中军/异动先锋/低位补涨生态位，并给出原始可交易价买卖区间。"
        right={(
          <div className="chips">
            <Badge tone={d.is_rapidly_rising ? 'good' : 'neutral'}>
              {d.is_rapidly_rising ? '🔥 火速晋升板块' : '常规跟踪板块'}（升温分 {fmtVal(d.surge_score)}）
            </Badge>
            <Badge tone="neutral">{d.taxonomy_id} · {d.horizon_id}</Badge>
          </div>
        )}
      />

      {d.top_picks.length === 0 ? (
        <EmptyState>本板块当前暂无满足正交合成门槛的推荐标的。</EmptyState>
      ) : (
        <div className="nx-grid-2">
          {d.top_picks.slice(0, 6).map((p) => (
            <article key={p.instrument_id} className="nx-pick-card">
              <div className="nx-pick-head">
                <div className="chips">
                  <span className="nx-rank-pill">#{p.rank}</span>
                  <Link to={stockHref(p.instrument_id, d.release_id, horizonKey)}>
                    <b>{p.display_name}</b> <span className="mono muted">{p.symbol}</span>
                  </Link>
                  <Badge tone={p.sector_role === 'leader_1' || p.sector_role === 'core_capacity' ? 'good' : 'neutral'}>
                    {SECTOR_ROLE_TEXT[p.sector_role] ?? p.sector_role}
                  </Badge>
                  {p.risk_flags.map((rf) => <Badge key={rf} tone="warn">{rf}</Badge>)}
                </div>
                <Badge tone="good">综合分 {p.ensemble_score}</Badge>
              </div>

              <dl className="nx-price-strip">
                <div><dt>低吸买入区间</dt><dd>{fmtVal(p.entry_low)} ~ {fmtVal(p.entry_high)}</dd></div>
                <div><dt>纪律止损位</dt><dd className="nx-down">{fmtVal(p.stop_loss)}</dd></div>
                <div><dt>目标止盈位</dt><dd className="nx-up">{fmtVal(p.target_price)}</dd></div>
                <div><dt>建议持有周期</dt><dd>{p.holding_horizon}</dd></div>
              </dl>

              <div className="small muted">
                {p.catalyst_summary} · 共振投票 <span className="mono">{p.voter_support_count}/{p.total_active_voters}</span>
              </div>
            </article>
          ))}
        </div>
      )}

      {d.surge_components.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <h4>板块升温 7 大组成项（ready vs missing 诚实标缺失）</h4>
          <div className="surge" role="list" aria-label="板块升温7大组成项">
            {d.surge_components.map((c) => (
              <div key={c.key} role="listitem" className={`surge-item ${c.status === 'ready' ? '' : 'surge-missing'}`}>
                <div><b>{c.label}</b></div>
                <div className="mono small">
                  {c.status === 'ready' && c.value !== null ? c.value : <MissingText>缺失</MissingText>}
                </div>
                <div>
                  <Badge tone={c.status === 'ready' ? 'good' : 'bad'}>
                    {c.status === 'ready' ? 'ready · 已就绪' : 'missing · 缺失不计入'}
                  </Badge>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {d.watch_pool_members.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <h4>⚡ 火速晋升 · 提前锁定备选池（{d.watch_pool_members.length} 只）</h4>
          <div className="chips">
            {d.watch_pool_members.map((w) => (
              <Link key={w.instrument_id} className="chip" to={stockHref(w.instrument_id, d.release_id, horizonKey)}>
                <b>{w.display_name}</b>
                <span className="mono muted">{w.symbol}</span>
                <Badge tone={w.pool_state === 'actionable' ? 'good' : 'warn'}>
                  {w.pool_state === 'actionable' ? '已晋级可操作' : w.pool_state}
                </Badge>
                <Badge tone={w.plan_ready ? 'good' : 'neutral'}>
                  {w.plan_ready ? '计划已就绪' : '待生成计划'}
                </Badge>
              </Link>
            ))}
          </div>
        </div>
      )}

      {d.sector_news.length > 0 && (
        <div style={{ marginTop: 10 }}>
          <h4>板块关联实时新闻催化</h4>
          <ul className="plain small">
            {d.sector_news.map((headline, idx) => (
              <li key={idx}>• {headline}</li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
