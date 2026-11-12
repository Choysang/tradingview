/** Pure display helpers for extension C. Money/ratios stay decimal strings; null renders as 缺失/—, never 0. */
import { parseDecimal, toFixed, toPercent, toChartNumber } from '../../../lib/decimal';
import { fmtDate, fmtMoney, fmtTime, unitLabel } from '../../../lib/format';
import type { ForwardBook, ForwardBookRule, Measurement, Metric } from './types';

const SCALE = 10n ** 12n;
const WAN = 10_000n * SCALE;
const YI = 100_000_000n * SCALE;

/** scaled-by-1e12 bigint -> plain decimal string */
function scaledToString(s: bigint): string {
  const neg = s < 0n;
  const abs = neg ? -s : s;
  const int = abs / SCALE;
  const frac = (abs % SCALE).toString().padStart(12, '0');
  return `${neg ? '-' : ''}${int.toString()}.${frac}`;
}

/** 元 -> 万元 / 亿元 for large amounts; null/invalid -> 缺失. */
export function fmtCnyAmount(v: string | null | undefined): string {
  const s = parseDecimal(v);
  if (s === null) return '缺失';
  const abs = s < 0n ? -s : s;
  if (abs >= YI) return `${toFixed(scaledToString(s / 10_000n / 10_000n), 2)} 亿元`;
  if (abs >= WAN) return `${toFixed(scaledToString(s / 10_000n), 2)} 万元`;
  return `${fmtMoney(v)} 元`;
}

export function fmtMetricValue(value: string | null, unit: string, nullLabel = '缺失'): string {
  if (value === null) return nullLabel;
  if (unit === 'ratio') return toPercent(value, 2) ?? value;
  if (unit === 'percent') return `${value}%`;
  if (unit === 'CNY') return fmtCnyAmount(value);
  return unit ? `${value} ${unitLabel(unit)}` : value;
}

export const metricLabel = (m: Metric): string => m.name;

/** Expected/actual calendar value. A missing value is shown as — (not yet published), never 0. */
export function fmtMeasurement(m: Measurement | null): string {
  if (m === null) return '—';
  const unit = m.unit ? ` ${unitLabel(m.unit)}` : '';
  if (m.value !== null) return `${m.value}${unit}`;
  if (m.text_value) return m.text_value;
  return '缺失';
}

/* ---------- calendar ---------- */
export interface DatedEvent { scheduled_at: string | null; scheduled_date: string | null; time_precision: string }

/** Day key YYYY-MM-DD (Shanghai) used for grouping; null when the event has no date at all. */
export function eventDayKey(e: DatedEvent): string | null {
  if (e.scheduled_date) return e.scheduled_date;
  if (e.scheduled_at) {
    const d = fmtDate(e.scheduled_at);
    return d === '未知' ? null : d;
  }
  return null;
}

const DATE_ONLY_PRECISIONS = new Set(['date', 'day', 'month', 'week', 'quarter', 'year']);
/** A fake clock time must never be shown for date-only events. */
export function eventTimeLabel(e: DatedEvent): string {
  const day = eventDayKey(e);
  if (day === null) return '时间未定';
  const hasClock = e.scheduled_at !== null && !DATE_ONLY_PRECISIONS.has(e.time_precision);
  return hasClock ? fmtTime(e.scheduled_at) : `${day}（仅日期）`;
}

export function groupByMonth<T extends DatedEvent>(events: readonly T[]): Array<{ month: string; items: T[] }> {
  const sorted = [...events].sort((a, b) => {
    const ka = eventDayKey(a) ?? '9999-99-99';
    const kb = eventDayKey(b) ?? '9999-99-99';
    return ka === kb ? (a.scheduled_at ?? '').localeCompare(b.scheduled_at ?? '') : ka.localeCompare(kb);
  });
  const out: Array<{ month: string; items: T[] }> = [];
  for (const e of sorted) {
    const k = eventDayKey(e);
    const month = k ? `${k.slice(0, 4)} 年 ${k.slice(5, 7)} 月` : '日期未定';
    const last = out[out.length - 1];
    if (last && last.month === month) last.items.push(e); else out.push({ month, items: [e] });
  }
  return out;
}

export const CALENDAR_STATUS_LABEL: Record<string, string> = {
  scheduled: '已排期', postponed: '已延期', cancelled: '已取消', occurred: '已发生', corrected: '已更正',
};

/* ---------- forward books ---------- */
export function ruleSummary(r: ForwardBookRule): string {
  const per = typeof r.per_name_cny === 'number' ? String(r.per_name_cny) : r.per_name_cny;
  return `${r.hold_days}日持有 / ${r.slots}只 / 每日${r.new_per_day}只 / 前${r.offer_top} / 每只${fmtCnyAmount(per)}`;
}

/** Sparkline polyline segments: null NAV points split the line (gaps). Chart-only numbers; nothing is computed from them. */
export function sparkSegments(history: ForwardBook['history'], width: number, height: number): string[] {
  const vals = history.map((h) => toChartNumber(h.nav_cny));
  const nums = vals.filter((v): v is number => v !== null);
  if (nums.length === 0) return [];
  const lo = Math.min(...nums);
  const hi = Math.max(...nums);
  const span = hi - lo || 1;
  const n = history.length;
  const segs: string[] = [];
  let cur: string[] = [];
  const flush = (): void => { if (cur.length > 0) { segs.push(cur.join(' ')); cur = []; } };
  vals.forEach((v, i) => {
    if (v === null) { flush(); return; }
    const x = n === 1 ? width / 2 : (i / (n - 1)) * width;
    const y = height - ((v - lo) / span) * (height - 4) - 2;
    cur.push(`${x.toFixed(1)},${y.toFixed(1)}`);
  });
  flush();
  return segs;
}
