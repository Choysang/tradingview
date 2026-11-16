/**
 * Stock statistics card (docs/LEGACY_TRIAGE.md row 43): turnover, distance to the 60-day high, 1-year limit-up count, streaks,
 * listing date, and the seal fields. Values come from `/v1/x/bf/stock/stats` (point-in-time at the release cutoff).
 * An estimated value carries a 估算 badge and its reason; a value pulse3 does not hold (seal times / seal amount: there is no
 * intraday data) shows 暂无数据 with the reason. Nothing is invented here.
 */
import { useQuery } from '@tanstack/react-query';
import { Badge, MissingText } from '../../../components/Badges';
import { DetailsButton } from '../../../components/Diagnostics';
import { EmptyState, ErrorState, Loading, SectionTitle } from '../../../components/States';
import { fmtTime } from '../../../lib/format';
import { bfStock } from './api';
import { formatStat } from './statsFormat';

const BASIS_LABEL = { raw: '原始价', forward_adjusted: '前复权价', backward_adjusted: '后复权价' } as const;

export function StockStatsCard({ instrumentId, releaseId }: { instrumentId: string; releaseId: string | null }) {
  const q = useQuery({
    queryKey: ['bf-stock', 'stats', instrumentId, releaseId],
    queryFn: ({ signal }) => bfStock.stats(instrumentId, releaseId, signal),
    enabled: !!releaseId,
    staleTime: 60_000,
  });
  const d = q.data?.data;
  return (
    <section className="card" aria-label="个股统计">
      <SectionTitle title="个股统计" hint="按本次发布的数据截点计算；估算值有标注，没有的数据不会补。" right={d ? <DetailsButton title="个股统计来源" meta={q.data?.meta} rows={[['price_basis', d.price_basis], ['bars_snapshot_id', d.bars_snapshot_id ?? '无'], ['last_trade_date', d.last_trade_date ?? '无'], ['decision_as_of', fmtTime(d.decision_as_of)]]} /> : undefined} />
      {q.isLoading && <Loading label="读取个股统计…" />}
      {q.error && !d && <ErrorState error={q.error} what="个股统计" onRetry={() => void q.refetch()} />}
      {!releaseId && <EmptyState>没有发布版本，无法计算统计。</EmptyState>}
      {d && (
        <>
          <p className="muted small">{d.last_trade_date ? `最新日线 ${d.last_trade_date}，价格口径 ${BASIS_LABEL[d.price_basis]}。` : '截点前没有可见的日线。'}</p>
          <dl className="facts" aria-label="统计字段">
            {d.fields.map((f) => {
              const s = formatStat(f);
              return (
                <div key={f.key}>
                  <dt>{f.label}</dt>
                  <dd>
                    {s.missing ? <MissingText>暂无数据</MissingText> : s.text}
                    {s.estimated && <> <Badge tone="warn" title={s.note ?? '估算值'}>估算</Badge></>}
                  </dd>
                  {(s.missing || s.estimated) && s.note && <dd className="muted small" style={{ fontWeight: 400 }}>{s.note}</dd>}
                </div>
              );
            })}
          </dl>
        </>
      )}
    </section>
  );
}
