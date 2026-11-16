/**
 * Display text of one stock statistic (docs/LEGACY_TRIAGE.md row 43).
 * Why: the server decides the value and whether it is exact, estimated or unavailable; the page only formats it.
 * Invariants: an unavailable field never produces a number (text is null, the caller shows 暂无数据 and the reason); an
 * estimated field is always flagged; nothing is defaulted to 0. Not covered: any computation beyond unit scaling of the display.
 */
import { toChartNumber, toFixed } from '../../../lib/decimal';
import type { StatField } from './api';

export interface StatDisplay { text: string | null; estimated: boolean; missing: boolean; note: string | null }

const CNY_UNIT = (n: number): string => (Math.abs(n) >= 1e8 ? `${(n / 1e8).toFixed(2)} 亿元` : Math.abs(n) >= 1e4 ? `${(n / 1e4).toFixed(2)} 万元` : `${n.toFixed(2)} 元`);

export function formatStat(f: StatField): StatDisplay {
  if (f.status === 'unavailable' || f.value === null) {
    return { text: null, estimated: false, missing: true, note: f.reason ?? '没有这项数据' };
  }
  const estimated = f.status === 'estimated';
  const note = f.reason ?? null;
  let text: string | null;
  if (f.value_kind === 'date') text = f.value;
  else if (f.value_kind === 'integer') text = `${f.value}${f.unit ? ` ${f.unit}` : ''}`;
  else if (f.unit === '元') {
    const n = toChartNumber(f.value);
    text = n === null ? null : CNY_UNIT(n);
  } else if (f.unit === '%') {
    const fixed = toFixed(f.value, 2);
    text = fixed === null ? null : `${f.key === 'dist_to_60d_high' && !fixed.startsWith('-') && fixed !== '0.00' ? '+' : ''}${fixed}%`;
  } else {
    text = `${f.value}${f.unit ? ` ${f.unit}` : ''}`;
  }
  if (text === null) return { text: null, estimated: false, missing: true, note: '数值格式无法识别，不显示' };
  return { text, estimated, missing: false, note };
}
