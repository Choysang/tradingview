/**
 * Monthly returns heatmap (triage #35): calendar month x year of a stored playbook replay, computed on the server from the daily NAV (net of the user cost
 * profile). A month without a NAV is hatched with its reason (never 0); the first / last month may be partial (the session count is in the tooltip).
 * Mounted by the tear sheet card when the source is a playbook (catalog id); a book has no stored replay NAV series here.
 */
import { useQuery } from '@tanstack/react-query';
import { ApiError } from '../../../api/client';
import { Loading } from '../../../components/States';
import type { MonthlyReturns } from './api';
import { bfFillr } from './api';
import { monthAlpha, monthGrid, pctText } from './format';
import './fillr.css';

export function MonthlyHeatmapCard({ catalogId }: { catalogId: string }) {
  const q = useQuery({ queryKey: ['bffr', 'monthly', catalogId], queryFn: ({ signal }) => bfFillr.monthlyReturns(catalogId, signal), staleTime: 60_000, retry: false });
  if (q.isLoading) return <Loading label="读取月度收益…" />;
  if (q.isError) {
    if (q.error instanceof ApiError && q.error.status === 404) return null;                  // the tear sheet card already says there is no replay
    return <p className="muted small" role="alert">月度收益读取失败：{q.error instanceof Error ? q.error.message : '未知错误'}</p>;
  }
  return <MonthlyHeatmapView data={q.data!.data} />;
}

export function MonthlyHeatmapView({ data }: { data: MonthlyReturns }) {
  const grid = monthGrid(data.months);
  return (
    <section className="bffr monthly" aria-label={`月度收益 ${data.subject.label}`}>
      <h4>月度收益</h4>
      <table className="month-grid">
        <thead><tr><th>年</th>{Array.from({ length: 12 }, (_, i) => <th key={i}>{i + 1}月</th>)}</tr></thead>
        <tbody>
          {grid.map((row) => (
            <tr key={row.year}>
              <th scope="row">{row.year}</th>
              {row.cells.map((c, i) => {
                if (!c) return <td key={i} className="none" aria-label={`${row.year}年${i + 1}月 不在回放区间`}>·</td>;
                if (c.value == null) return <td key={i} className="missing" title={`${c.reason ?? '缺失'}（${c.sessions} 个交易日）`}>缺失</td>;
                const pos = Number(c.value) >= 0;
                return (
                  <td key={i} className={pos ? 'pos' : 'neg'} title={`${c.sessions} 个交易日`}
                    style={{ background: `color-mix(in srgb, ${pos ? 'var(--good, #1a7f37)' : 'var(--bad, #c0392b)'} ${(monthAlpha(c.value) * 100).toFixed(0)}%, transparent)` }}>
                    {pctText(c.value, 1)}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted small">{data.basis}</p>
      <ul className="muted small plain">{data.caveats.map((c) => <li key={c}>{c}</li>)}</ul>
    </section>
  );
}
