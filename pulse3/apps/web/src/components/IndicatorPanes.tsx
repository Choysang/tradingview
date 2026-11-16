/**
 * Indicator controls and notes under the K-line chart (docs/LEGACY_TRIAGE.md rows 41, 159).
 * Why: replaces the old "缺失" placeholder. The series come from the server (`useStockIndicators`) and are drawn by PriceChart;
 * this component shows the BOLL toggle and the sub-pane selector, the line legend, the warm-up note and any reason a series
 * could not be drawn (request failed, window mismatch). The browser computes nothing.
 */
import type { IndicatorView } from '../pages/bf/stock/useStockIndicators';
import type { SubPaneId } from '../pages/bf/stock/api';
import { Loading, ErrorState } from './States';
import { MissingText } from './Badges';

export const SUB_PANE_LABEL: Record<SubPaneId | 'none', string> = { none: '无副图', macd: 'MACD', kdj: 'KDJ', rsi: 'RSI' };

export function IndicatorControls({ boll, onBoll, sub, onSub }: { boll: boolean; onBoll: (v: boolean) => void; sub: SubPaneId | 'none'; onSub: (v: SubPaneId | 'none') => void }) {
  return (
    <>
      <label className="check"><input type="checkbox" checked={boll} onChange={(e) => onBoll(e.target.checked)} />BOLL</label>
      <label className="select-label">副图
        <select value={sub} onChange={(e) => onSub(e.target.value as SubPaneId | 'none')} aria-label="副图指标">
          {(Object.keys(SUB_PANE_LABEL) as Array<SubPaneId | 'none'>).map((k) => <option key={k} value={k}>{SUB_PANE_LABEL[k]}</option>)}
        </select>
      </label>
    </>
  );
}

export function IndicatorNotes({ view }: { view: IndicatorView }) {
  if (!view.enabled) return null;
  const drawn = [...view.overlays, ...view.subSeries];
  return (
    <div className="indicator-panes" aria-label="指标说明">
      {view.isLoading && <Loading label="读取指标…" />}
      {view.error && !view.data ? <ErrorState error={view.error} what="指标" onRetry={view.refetch} /> : null}
      {view.data && !view.alignment.ok && (
        <div className="indicator-pane"><b>指标</b> <MissingText>未绘制</MissingText><span className="muted small"> {view.alignment.reason}。页面不会自己计算指标。</span></div>
      )}
      {drawn.length > 0 && (
        <div className="indicator-pane" aria-label="指标图例">
          {drawn.map((d) => (
            <span key={d.key} className="small" style={{ marginRight: 12 }}>
              <span aria-hidden style={{ display: 'inline-block', width: 10, height: 3, background: d.color, marginRight: 4, verticalAlign: 'middle' }} />{d.label}
            </span>
          ))}
          <span className="muted small">服务器按上图同一批K线计算（{view.data?.numeric_policy}）。</span>
          {view.warmup && <div className="muted small">{view.warmup}。</div>}
        </div>
      )}
    </div>
  );
}
