/** Pure display helpers of the fill-research lane: verdict / state names, number text, sparkline geometry, month grid. No IO; a missing value is never shown as 0. */
import type { AnalogState, BoardItem, FactorSeriesState, MonthReturn, RunState, Verdict } from './api';

export const VERDICT_TEXT: Record<Verdict, string> = { effective: '有效', regime_dependent: '依赖环境', weak: '偏弱', overfit: '疑似过拟合', insufficient: '样本不足' };
export const VERDICT_TONE: Record<Verdict, 'good' | 'warn' | 'neutral' | 'bad'> = { effective: 'good', regime_dependent: 'warn', weak: 'neutral', overfit: 'bad', insufficient: 'neutral' };
export const verdictLabel = (item: Pick<BoardItem, 'state' | 'evaluation'>): string => {
  if (item.state === 'unavailable') return '缺数据';
  if (item.state === 'failed') return '失败';
  if (item.state === 'not_run') return '未回测';
  const v = item.evaluation?.verdict;
  return v ? VERDICT_TEXT[v] : '仅样本内';
};

export const RUN_STATE_TEXT: Record<RunState, string> = { queued: '排队中', running: '运行中', succeeded: '已完成', failed: '失败', cancelled: '已取消' };
export const runStateText = (s: string): string => RUN_STATE_TEXT[s as RunState] ?? s;
export const FACTOR_STATE_TEXT: Record<FactorSeriesState, string> = {
  ready: '已生成', not_computed: '尚未生成', queued: '排队中', running: '运行中', failed: '失败', no_experiment: '没有已完成的实验',
};
export const ANALOG_STATE_TEXT: Record<AnalogState, string> = { ready: '已构建', not_built: '尚未构建', queued: '排队中', running: '运行中', failed: '构建失败' };

export const pctText = (v: string | null | undefined, d = 2): string => (v == null || v === '' || !Number.isFinite(Number(v)) ? '缺失' : `${(Number(v) * 100).toFixed(d)}%`);
export const numText = (v: string | null | undefined, d = 2): string => (v == null || v === '' || !Number.isFinite(Number(v)) ? '缺失' : Number(v).toFixed(d));
export const signClass = (v: string | null | undefined): string => (v == null || !Number.isFinite(Number(v)) || Number(v) === 0 ? '' : Number(v) > 0 ? 'pos' : 'neg');

/** Fitness ring geometry: the arc covers fitness / 100 of the circle; no fitness (null) draws no arc. */
export function ringDash(fitness: string | null | undefined, radius = 15): { circumference: number; filled: number } | null {
  if (fitness == null || !Number.isFinite(Number(fitness))) return null;
  const c = 2 * Math.PI * radius;
  return { circumference: c, filled: (Math.max(0, Math.min(100, Number(fitness))) / 100) * c };
}

/** SVG polyline points of a sparkline in a w x h box (ratio values of any scale); fewer than 2 points or a flat series return null. */
export function sparkPoints(values: string[], w = 72, h = 22, pad = 2): string | null {
  const xs = values.map(Number).filter((n) => Number.isFinite(n));
  if (xs.length < 2) return null;
  const lo = Math.min(...xs);
  const hi = Math.max(...xs);
  const span = hi - lo || 1;
  return xs.map((v, i) => `${(pad + (i / (xs.length - 1)) * (w - 2 * pad)).toFixed(1)},${(pad + (1 - (v - lo) / span) * (h - 2 * pad)).toFixed(1)}`).join(' ');
}

/** Month grid (rows = years descending, columns = 1..12): a month with no row is null, a row without a value keeps its reason. */
export interface MonthGridRow { year: number; cells: Array<MonthReturn | null> }
export function monthGrid(months: MonthReturn[]): MonthGridRow[] {
  const years = [...new Set(months.map((m) => m.year))].sort((a, b) => b - a);
  return years.map((year) => ({ year, cells: Array.from({ length: 12 }, (_, i) => months.find((m) => m.year === year && m.month === i + 1) ?? null) }));
}
/** Cell background alpha: a 10% month saturates. */
export const monthAlpha = (v: string | null): number => (v == null ? 0 : Math.min(Math.abs(Number(v)) / 0.1, 1) * 0.55);

/** Series -> SVG path of (date index, value) pairs; null values break the line. */
export function linePath(values: Array<number | null>, w: number, h: number, lo: number, hi: number, pad = 4): string {
  const span = hi - lo || 1;
  let d = '';
  let pen = false;
  values.forEach((v, i) => {
    if (v === null) { pen = false; return; }
    const x = pad + (values.length <= 1 ? 0 : (i / (values.length - 1)) * (w - 2 * pad));
    const y = pad + (1 - (v - lo) / span) * (h - 2 * pad);
    d += `${pen ? 'L' : 'M'}${x.toFixed(1)} ${y.toFixed(1)} `;
    pen = true;
  });
  return d.trim();
}

export const EXIT_TEXT: Record<string, string> = { stop_loss: '止损', take_profit: '止盈', trailing_stop: '追踪止损', holding_days: '持有到期', unknown: '未能判定' };
