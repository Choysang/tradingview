/**
 * Stock statistics additions (legacy row #43): volume ratio, circulating cap and the seal success rate, rendered inside the stats card.
 * The server decides the value and whether it is exact, estimated or unavailable; this file only formats it. The seal success rate
 * always shows how many sessions it covers; a partial window is never presented as a 1-year figure. Nothing is defaulted to 0.
 */
import { useQuery } from '@tanstack/react-query';
import { Badge, MissingText } from '../../../components/Badges';
import { fillm, type PlusCoverage, type StatPlusField } from './api';

export interface PlusDisplay { text: string | null; estimated: boolean; missing: boolean; note: string | null; coverageText: string | null }

/** `covered 3 of 250 sessions, 2026-11-04 to 2026-11-10`; null when the field has no coverage block. */
export function coverageText(c: PlusCoverage | null): string | null {
  if (!c) return null;
  if (c.sessions_covered === 0) return `封板事实覆盖 0 个交易日（窗口 ${c.window_sessions} 个）`;
  const span = c.first_session && c.last_session ? `，${c.first_session} 至 ${c.last_session}` : '';
  const partial = c.sessions_covered < c.window_sessions ? `，不足 ${c.window_sessions} 个交易日，不是1年的值` : '';
  return `覆盖 ${c.sessions_covered} 个交易日${span}${partial}；触及涨停 ${c.touched} 次，收盘封住 ${c.sealed_at_close} 次`;
}

export function formatPlus(f: StatPlusField): PlusDisplay {
  const cov = coverageText(f.coverage);
  if (f.status === 'unavailable' || f.value === null) return { text: null, estimated: false, missing: true, note: f.reason ?? '没有这项数据', coverageText: cov };
  const n = Number(f.value);
  if (!Number.isFinite(n)) return { text: null, estimated: false, missing: true, note: '数值格式无法识别，不显示', coverageText: cov };
  const text = f.key === 'seal_success_rate_1y' ? `${n.toFixed(2)}%` : f.key === 'float_cap' ? `${n.toFixed(2)} 亿元` : `${n.toFixed(2)} 倍`;
  return { text, estimated: f.status === 'estimated', missing: false, note: f.reason, coverageText: cov };
}

export function StatsPlusRows({ instrumentId, releaseId }: { instrumentId: string; releaseId: string | null }) {
  const q = useQuery({
    queryKey: ['bf-fillm', 'stats-plus', instrumentId, releaseId],
    queryFn: ({ signal }) => fillm.statsPlus({ instrument_id: instrumentId, release_id: releaseId as string }, signal),
    enabled: !!releaseId,
    staleTime: 60_000,
  });
  const d = q.data?.data;
  if (q.error && !d) return <p className="muted small" role="alert">量比、流通市值和封板成功率读取失败。</p>;
  if (!d) return null;
  return (
    <dl className="facts" aria-label="补充统计字段">
      {d.fields.map((f) => {
        const s = formatPlus(f);
        return (
          <div key={f.key}>
            <dt>{f.key === 'seal_success_rate_1y' && f.coverage ? `${f.label}（${f.coverage.sessions_covered} 个交易日）` : f.label}</dt>
            <dd>
              {s.missing ? <MissingText>暂无数据</MissingText> : s.text}
              {s.estimated && <> <Badge tone="warn" title={s.note ?? '估算值'}>估算</Badge></>}
            </dd>
            {s.coverageText && <dd className="muted small fm-plus-note">{s.coverageText}</dd>}
            {(s.missing || s.estimated) && s.note && <dd className="muted small fm-plus-note">{s.note}</dd>}
          </div>
        );
      })}
    </dl>
  );
}
