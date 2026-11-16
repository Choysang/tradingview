/** Regime attribution cells shared by the per-playbook table and the lab heatmap. A cell below the sample gate is faded and flagged 样本不足. */
import type { CSSProperties } from 'react';
import type { AttributionCell, AttributionDimension } from './api';
import { cellState, heat, pctText } from './format';

export function RegimeCell({ cell, gate }: { cell: AttributionCell; gate: number }) {
  const st = cellState(cell);
  const h = heat(cell);
  const style: CSSProperties | undefined = h.tone === 'none' ? undefined : { background: `color-mix(in srgb, var(${h.tone === 'pos' ? '--good' : '--bad'}, ${h.tone === 'pos' ? '#1a7f37' : '#c0392b'}) ${Math.round(h.alpha * 100)}%, transparent)` };
  return (
    <td className={`rcell ${st}`} style={style} data-state={st} title={st === 'thin' ? `只有 ${cell.trade_count} 笔平仓交易，少于 ${gate} 笔，不能当作证据` : undefined}>
      {st === 'empty' ? <span className="muted">—</span> : (
        <>
          <b className="num">{pctText(cell.avg_return, 1)}</b>
          <span className="small"> · {cell.trade_count} 笔</span>
          {st === 'thin' && <div className="thin-flag small">样本不足</div>}
        </>
      )}
    </td>
  );
}

const REASON_TEXT: Record<string, string> = {
  insufficient_history: '历史不足', missing_index: '缺指数', no_regime: '无市场环境', unmeasured_label: '标签未测得', missing: '缺失',
};
export const unclassifiedText = (d: AttributionDimension): string =>
  d.unclassified.count === 0 ? '' : `未归类 ${d.unclassified.count} 笔（${Object.entries(d.unclassified.reasons).map(([k, v]) => `${REASON_TEXT[k] ?? k} ${v}`).join('，')}）`;
