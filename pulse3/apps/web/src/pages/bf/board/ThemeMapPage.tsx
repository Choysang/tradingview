/**
 * 题材图谱 (triage #30): treemap of the release's sectors. Area = sector amount (estimate), colour = average change.
 * The sector list comes from the existing sector overview read of the release; amount and change come from the board lane
 * theme-map read pinned to the same membership snapshot. A sector without an amount is listed under the map, not drawn.
 * Nothing is ranked or recomputed here; a click opens the sector page of the same release.
 */
import { useQuery } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import { ops } from '../../../api/ops';
import { Badge } from '../../../components/Badges';
import { MetaStrip } from '../../../components/MetaStrip';
import { ReleaseBar } from '../../../components/ReleaseBar';
import { EmptyState, ErrorState, Loading, SectionTitle } from '../../../components/States';
import { TAXONOMIES, horizonByKey } from '../../../config';
import { useReleaseSelection } from '../../../hooks/useRelease';
import { boardOps, type ThemeMapNode } from './api';
import { fmtPct, fmtYi } from './fmt';
import { COLOR_FULL_PCT, changeColor, squarify } from './treemap';
import './board.css';

export interface MapTile { node: ThemeMapNode; x: number; y: number; w: number; h: number }

/** Join overview nodes with their metrics; returns the placed tiles and the nodes that cannot be drawn. */
export function buildTiles(overviewIds: readonly string[], metrics: readonly ThemeMapNode[]): { tiles: MapTile[]; undrawn: Array<{ id: string; node: ThemeMapNode | null }> } {
  const by = new Map(metrics.map((m) => [m.node_id, m]));
  const items = overviewIds.flatMap((id) => {
    const n = by.get(id);
    const v = n?.amount_estimate_cny == null ? NaN : Number(n.amount_estimate_cny);
    return n && v > 0 ? [{ key: id, value: v }] : [];
  });
  const rects = squarify(items);
  const placed = new Set(rects.map((r) => r.key));
  return {
    tiles: rects.map((r) => ({ node: by.get(r.key) as ThemeMapNode, x: r.x, y: r.y, w: r.w, h: r.h })),
    undrawn: overviewIds.filter((id) => !placed.has(id)).map((id) => ({ id, node: by.get(id) ?? null })),
  };
}

export function ThemeMapPage() {
  const rel = useReleaseSelection();
  const rid = rel.effectiveReleaseId;
  const [sp, setSp] = useSearchParams();
  const tax = TAXONOMIES.find((t) => t.id === sp.get('tax')) ?? (TAXONOMIES[0] as (typeof TAXONOMIES)[number]);
  const h = horizonByKey(null);
  const overview = useQuery({
    queryKey: ['sectorOverview', rid, tax.id, tax.version, 'sector_opportunity_rank', h.horizonId, h.forecastTarget],
    queryFn: ({ signal }) => ops.getSectorOverview({ release_id: rid as string, taxonomy_id: tax.id, taxonomy_version: tax.version, rank_kind: 'sector_opportunity_rank', horizon_id: h.horizonId, forecast_target: h.forecastTarget }, { signal }),
    enabled: !!rid,
  });
  const map = useQuery({
    queryKey: ['bf-board', 'theme-map', rid, tax.id, tax.version],
    queryFn: ({ signal }) => boardOps.themeMap({ release_id: rid as string, taxonomy_id: tax.id, taxonomy_version: tax.version }, signal),
    enabled: !!rid,
    staleTime: 60_000,
  });
  const ids = overview.data?.data.entries.map((e) => e.taxonomy_node.node_id) ?? [];
  const built = map.data ? buildTiles(ids, map.data.data.nodes) : null;
  const sectorLink = (id: string): string => `/sector/${encodeURIComponent(id)}?${new URLSearchParams({ release: rid as string, tax: tax.id, h: h.key }).toString()}`;
  const err = overview.error ?? map.error;
  return (
    <div className="page">
      <ReleaseBar rel={rel} displayedReleaseId={map.data?.meta.release_id} />
      <SectionTitle title="题材图谱" hint="面积 = 板块成交额（估算：股数×收盘价），颜色 = 板块成员平均涨跌幅（红涨绿跌）。点击方块进入板块页。" />
      <div className="segmented" role="tablist" aria-label="板块分类">
        {TAXONOMIES.map((t) => (
          <button key={t.id} role="tab" aria-selected={tax.id === t.id} className={tax.id === t.id ? 'on' : ''}
            onClick={() => setSp((p) => { const n = new URLSearchParams(p); n.set('tax', t.id); return n; }, { replace: true })}>{t.label}</button>
        ))}
      </div>
      {!rid && <Loading label="读取当前发布版本…" />}
      {(overview.isLoading || map.isLoading) && <Loading />}
      {err && <ErrorState error={err} what="题材图谱" onRetry={() => { void overview.refetch(); void map.refetch(); }} />}
      {map.data && <MetaStrip meta={map.data.meta} title="题材图谱" extra={<Badge tone="warn" title={map.data.data.method}>成交额为估算</Badge>} rows={[['membership_snapshot_id', map.data.data.membership_snapshot_id], ['session', map.data.data.session ?? '缺失']]} />}
      {built && (
        <>
          {built.tiles.length === 0 ? <EmptyState>这个分类没有可画的板块：缺少成交额数据。</EmptyState> : (
            <div className="bd-map" role="list" aria-label="题材图谱">
              {built.tiles.map((t) => {
                const pct = t.node.avg_pct == null ? null : Number(t.node.avg_pct);
                return (
                  <Link key={t.node.node_id} role="listitem" className="bd-tile" to={sectorLink(t.node.node_id)}
                    style={{ left: `${t.x}%`, top: `${t.y}%`, width: `${t.w}%`, height: `${t.h}%`, background: changeColor(pct) }}
                    title={`${t.node.name}\n成交额（估算） ${fmtYi(t.node.amount_estimate_cny)}\n平均涨跌幅 ${fmtPct(t.node.avg_pct)}（${t.node.pct_samples}/${t.node.members} 只有涨跌幅）`}>
                    {t.w > 7 && t.h > 7 && <><b>{t.node.name}</b><span className="mono">{fmtPct(t.node.avg_pct)}</span></>}
                  </Link>
                );
              })}
            </div>
          )}
          <div className="bd-legend" aria-label="颜色图例">
            <i style={{ background: changeColor(-COLOR_FULL_PCT) }} />-{COLOR_FULL_PCT}%
            <i style={{ background: changeColor(0) }} />0
            <i style={{ background: changeColor(COLOR_FULL_PCT) }} />+{COLOR_FULL_PCT}%
            <i style={{ background: changeColor(null) }} />缺失
          </div>
          {built.undrawn.length > 0 && (
            <details className="small">
              <summary>未画出的板块（{built.undrawn.length}）：缺少成交额，不按 0 画</summary>
              <ul>{built.undrawn.map((u) => <li key={u.id}><Link to={sectorLink(u.id)}>{u.node?.name ?? u.id}</Link>{u.node ? <span className="muted">（有行情 {u.node.observed}/{u.node.members} 只）</span> : <span className="muted">（图谱数据里没有这个板块）</span>}</li>)}</ul>
            </details>
          )}
        </>
      )}
    </div>
  );
}
