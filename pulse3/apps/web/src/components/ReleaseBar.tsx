/**
 * Release header: which published version the page shows, when it was released, the
 * "follow latest / pinned" switch, and the version-change notices.
 */
import type { ReleaseCtx } from '../hooks/useRelease';
import { fmtAge, fmtTime, shortId } from '../lib/format';
import { isDisplayStale } from '../lib/version';
import { Badge, FreshnessBadge } from './Badges';
import { DetailsButton } from './Diagnostics';

export function ReleaseBar({ rel, displayedReleaseId }: { rel: ReleaseCtx; displayedReleaseId?: string | null }) {
  const r = rel.release.data;
  const meta = rel.release.data?.meta;
  const staleDisplay = isDisplayStale(displayedReleaseId, rel.effectiveReleaseId);
  return (
    <section className="release-bar" aria-label="发布版本">
      <div className="release-main">
        <div>
          <div className="eyebrow">分析发布版本</div>
          <div className="release-title">
            <span className="mono">{shortId(rel.effectiveReleaseId)}</span>
            {r ? (
              <span className="muted"> · 发布于 {fmtTime(r.data.published_at)}（{fmtAge(r.data.published_at)}）· 数据截至 {fmtTime(r.data.decision_as_of)}</span>
            ) : <span className="muted"> · 读取中…</span>}
          </div>
        </div>
        <div className="release-flags">
          {meta && <FreshnessBadge state={meta.freshness_state} label={meta.freshness_state === 'stale' ? '这是旧版本（只读）' : undefined} />}
          {staleDisplay && <Badge tone="warn" title="下方内容属于上一个版本，新版本读取完成后会整体替换">正在更新，当前显示旧版本</Badge>}
          <div className="segmented" role="group" aria-label="版本跟随方式">
            <button className={rel.mode === 'follow' ? 'on' : ''} aria-pressed={rel.mode === 'follow'} onClick={rel.follow}>跟随最新</button>
            <button className={rel.mode === 'pinned' ? 'on' : ''} aria-pressed={rel.mode === 'pinned'} onClick={rel.pinCurrent} disabled={!rel.effectiveReleaseId}>固定此版本</button>
          </div>
          {r && <DetailsButton title="发布版本" meta={r.meta} rows={[
            ['release_id', r.data.release_id],
            ['release_hash', r.data.release_hash ?? '无'],
            ['universe_policy_version', r.data.universe_policy_version],
            ['market_rules_version', r.data.market_rules_version],
            ['required_components', r.data.required_components.join('、')],
            ...r.data.components.map((c): [string, string] => [c.component_key, `${c.component_state}，计算于 ${fmtTime(c.computed_at)}`]),
          ]} />}
        </div>
      </div>
      {rel.mode === 'pinned' && rel.newerAvailable && (
        <div className="notice notice-warn" role="status">
          你正在查看固定的旧版本 <span className="mono">{shortId(rel.effectiveReleaseId)}</span>；已有新版本 <span className="mono">{shortId(rel.currentReleaseId)}</span>。
          旧版本的榜单、计划和理由保持一致、不会与新版本混用。
          <button className="btn btn-small" onClick={rel.switchToLatest}>切换到最新版本</button>
        </div>
      )}
      {rel.notice && (
        <div className="notice notice-info" role="status">
          已自动更新到新版本 <span className="mono">{shortId(rel.notice.to)}</span>（原 <span className="mono">{shortId(rel.notice.from)}</span>）。
          原来的排名、买卖计划可能已失效，请以当前页面为准；新旧内容不会混在同一屏。
          <button className="btn btn-small btn-ghost" onClick={rel.dismissNotice}>知道了</button>
        </div>
      )}
    </section>
  );
}
