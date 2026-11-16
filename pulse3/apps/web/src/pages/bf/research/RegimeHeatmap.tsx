/**
 * Playbook x market-regime heatmap (triage #24) for the lab page. One row per playbook that has a stored replay; one column per regime bucket of the
 * chosen dimension. Cell = average gross return of the closed trades entered in that regime, with the trade count; below the sample gate it is faded and
 * flagged 样本不足. Regime labels are uncalibrated descriptions, not predictions.
 */
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { EmptyState, ErrorState, Loading } from '../../../components/States';
import { CATALOG } from '../../ext/b/helpers';
import type { Dimension } from './api';
import { bfResearch } from './api';
import { RegimeCell } from './RegimeCells';
import './research.css';

export function RegimeHeatmapView() {
  const q = useQuery({ queryKey: ['bfr', 'heatmap'], queryFn: ({ signal }) => bfResearch.regimeHeatmap(signal), staleTime: 60_000, retry: false });
  const [dim, setDim] = useState<Dimension>('trend');
  if (q.isLoading) return <Loading />;
  if (q.isError) return <ErrorState error={q.error} what="战法 × 市场环境" onRetry={() => q.refetch()} />;
  const hm = q.data!.data;
  if (hm.rows.length === 0) return <EmptyState>还没有战法有回放结果：运行 <code>uv run python -m pulse.workers.research ext-b-run</code> 之后这里才有数据。</EmptyState>;
  const col = hm.columns.find((c) => c.dimension === dim) ?? hm.columns[0]!;
  const nameOf = (id: string): string => CATALOG.playbooks.find((p) => p.id === id)?.name ?? id;
  return (
    <section className="bfr" aria-label="战法与市场环境热力表">
      <div className="segmented" role="tablist" aria-label="环境维度">
        {hm.columns.map((c) => <button key={c.dimension} role="tab" aria-selected={dim === c.dimension} className={dim === c.dimension ? 'on' : ''} onClick={() => setDim(c.dimension)}>{c.label}</button>)}
      </div>
      <p className="muted small">{hm.note}</p>
      <div className="scroll">
        <table className="table dense heatmap">
          <thead><tr><th>战法</th><th>平仓笔数</th>{col.buckets.map((b) => <th key={b.bucket}>{b.label}</th>)}</tr></thead>
          <tbody>
            {hm.rows.map((r) => {
              const d = r.dimensions.find((x) => x.dimension === dim);
              return (
                <tr key={r.catalog_id}>
                  <th scope="row">{nameOf(r.playbook_id)}{!r.regime_available && <span className="muted small"> · 无环境数据</span>}</th>
                  <td className="num">{r.closed_trades}</td>
                  {col.buckets.map((b) => { const cell = d?.cells.find((c) => c.bucket === b.bucket); return cell ? <RegimeCell key={b.bucket} cell={cell} gate={hm.min_trades} /> : <td key={b.bucket} className="rcell empty">—</td>; })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
