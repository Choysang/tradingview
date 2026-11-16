/** Pure display helpers of the research lane: percent / money text, exit-reason names, regime cell tone, table sorting. No IO. */
import type { AttributionCell, ExitReason } from './api';

export const EXIT_REASON_TEXT: Record<ExitReason, string> = {
  stop_loss: '止损', take_profit: '止盈', trailing_stop: '追踪止损', chandelier: '吊灯止损', below_ma5: '跌破5日线', stall: '放量滞涨', bias: '乖离过大',
  shadow: '长上影', scale_out: '分批卖出', holding_days: '持有到期', unknown: '未能判定',
};
export const exitText = (r: ExitReason | null, status: 'closed' | 'open'): string => (status === 'open' ? '持仓中' : r == null ? '缺失' : EXIT_REASON_TEXT[r] ?? r);

export const pctText = (v: string | null | undefined, d = 2): string => (v == null || v === '' || !Number.isFinite(Number(v)) ? '缺失' : `${(Number(v) * 100).toFixed(d)}%`);
export const moneyText = (v: string | null | undefined): string => (v == null || !Number.isFinite(Number(v)) ? '缺失' : Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 0 }));
export const priceText = (v: string | null | undefined): string => (v == null || !Number.isFinite(Number(v)) ? '缺失' : Number(v).toFixed(2));

/** Cell state for the faded / flagged rendering: empty (no trades), thin (below the gate: 样本不足) or ok. */
export type CellState = 'empty' | 'thin' | 'ok';
export const cellState = (c: AttributionCell): CellState => (c.trade_count === 0 ? 'empty' : c.insufficient_sample ? 'thin' : 'ok');
/** Heat colour of a cell: sign of the average return and a strength 0..0.5 (a 20% average saturates). */
export function heat(c: AttributionCell): { tone: 'pos' | 'neg' | 'none'; alpha: number } {
  if (c.avg_return == null) return { tone: 'none', alpha: 0 };
  const r = Number(c.avg_return);
  if (!Number.isFinite(r) || r === 0) return { tone: 'none', alpha: 0 };
  return { tone: r > 0 ? 'pos' : 'neg', alpha: Math.min(Math.abs(r) / 0.2, 1) * 0.5 };
}

/** Stable sort of already loaded rows by a column value; null values always last whatever the direction. */
export function sortByColumn<T>(rows: readonly T[], value: (row: T) => number | string | null, dir: 'asc' | 'desc'): T[] {
  const sign = dir === 'asc' ? 1 : -1;
  return rows.map((r, i) => ({ r, i, v: value(r) })).sort((a, b) => {
    if (a.v === null && b.v === null) return a.i - b.i;
    if (a.v === null) return 1;
    if (b.v === null) return -1;
    const c = typeof a.v === 'number' && typeof b.v === 'number' ? a.v - b.v : String(a.v).localeCompare(String(b.v), 'zh-CN', { numeric: true });
    return c === 0 ? a.i - b.i : sign * c;
  }).map((x) => x.r);
}
