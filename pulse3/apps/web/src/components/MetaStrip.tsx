/**
 * One-line honesty strip under every data block: freshness, quality, coverage
 * (exclusions / missing data) and non-synthetic warnings, plus the details drawer.
 */
import type { ReactNode } from 'react';
import type { ResponseMeta } from '../api/types';
import { fmtTime } from '../lib/format';
import { Badge, FreshnessBadge, QualityBadge } from './Badges';
import { DetailsButton } from './Diagnostics';

export function MetaStrip({ meta, title, extra, rows, hideCoverage }: { meta: ResponseMeta; title: string; extra?: ReactNode; rows?: Array<[string, ReactNode]>; hideCoverage?: boolean }) {
  const c = meta.coverage;
  const warnings = meta.warnings.filter((w) => w.code !== 'SYNTHETIC_FIXTURE');
  const hasMissing = (c.missing_count ?? 0) > 0 || c.unverifiable_reasons.length > 0;
  return (
    <div className="meta-strip">
      <div className="meta-line">
        <FreshnessBadge state={meta.freshness_state} />
        <QualityBadge status={meta.quality_status} />
        {!hideCoverage && c.expected_count != null && (
          <Badge tone={hasMissing ? 'warn' : 'neutral'} title={`覆盖政策 ${c.policy_version}`}>
            覆盖 {c.observed_count}/{c.expected_count}{hasMissing ? `，缺失 ${c.missing_count ?? '未知'}` : ''}
          </Badge>
        )}
        <span className="muted small">数据截至 {fmtTime(meta.decision_as_of)}</span>
        {extra}
        <span className="grow" />
        <DetailsButton title={title} meta={meta} rows={rows} />
      </div>
      {(hasMissing || warnings.length > 0) && (
        <ul className="flags">
          {c.unverifiable_reasons.map((r) => <li key={r} className="flag flag-warn">排除/缺失：{r}</li>)}
          {warnings.map((w) => <li key={`${w.code}${w.affected_component ?? ''}`} className="flag flag-warn">{w.message}</li>)}
        </ul>
      )}
    </div>
  );
}
