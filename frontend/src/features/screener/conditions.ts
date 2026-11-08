/**
 * Describe the active filter as removable chips. Whatever produced the filter
 * — a typed question, a preset, the rail, an overview tile — the researcher sees
 * one list of conditions in plain words and can drop any of them.
 */
import type { FilterInput } from '../../core/api';
import { DEFAULT_FILTER } from '../../core/contract';
import type { ScreenerFilterRequest } from '../../core/contract';

export interface Condition {
  key: string;
  label: string;
  clear: FilterInput;
}

const POOL_LABEL: Record<string, string> = {
  limit_up: '涨停梯队',
  n_days_m_boards: 'N 天 M 板',
  broken_attack: '炸板 / 冲板',
  growth_streak: '连阳池',
};

const REGIME_LABEL: Record<string, string> = { shrink: '缩量加速', normal: '平量换手', expand: '放量分歧' };

const SORT_LABEL: Record<ScreenerFilterRequest['sort_by'], string> = {
  composite_score: '综合分',
  pct_chg: '涨幅',
  price: '股价',
  limit_streak: '连板',
  growth_streak: '连阳',
  amount_today: '成交额',
  turnover_today: '换手',
  free_float_mkt_cap: '自由流通',
  turnover_ratio_yd: '昨换比',
  seal_to_amount_ratio: '封成比',
};

function range(label: string, low: number | null | undefined, high: number | null | undefined, lowDefault: number | null, highDefault: number | null, unit: string) {
  const hasLow = low != null && low !== lowDefault;
  const hasHigh = high != null && high !== highDefault;
  if (!hasLow && !hasHigh) return null;
  if (hasLow && hasHigh) return `${label} ${low}–${high}${unit}`;
  return hasLow ? `${label} ≥ ${low}${unit}` : `${label} ≤ ${high}${unit}`;
}

function streaks(values: number[]) {
  const sorted = [...values].sort((a, b) => a - b);
  if (sorted.length === 1) return `${sorted[0]} 连板`;
  const contiguous = sorted.every((value, index) => index === 0 || value === sorted[index - 1] + 1);
  if (contiguous && sorted[sorted.length - 1] >= 12) return `连板 ≥ ${sorted[0]}`;
  return contiguous ? `${sorted[0]}–${sorted[sorted.length - 1]} 连板` : `${sorted.join('/')} 连板`;
}

export function conditions(filters: FilterInput): Condition[] {
  const f = { ...DEFAULT_FILTER, ...filters };
  const out: Condition[] = [];
  const push = (key: string, label: string | null, clear: FilterInput) => { if (label) out.push({ key, label, clear }); };

  if (f.pool_type !== 'all') push('pool', POOL_LABEL[f.pool_type], { pool_type: 'all', min_growth_streak: null });
  if (f.limit_streak_exact?.length) push('streak', streaks(f.limit_streak_exact), { limit_streak_exact: null });
  if (f.min_growth_streak != null) push('growth', `连阳 ≥ ${f.min_growth_streak} 天`, { min_growth_streak: null });
  if (f.allow_doji_tolerance) push('doji', '假阴容错', { allow_doji_tolerance: false });
  if (f.boards.length && f.boards.length < 3) push('boards', f.boards.join(' + '), { boards: DEFAULT_FILTER.boards });
  if (!f.exclude_st) push('st', '含 ST', { exclude_st: true });
  if (!f.exclude_new) push('new', '含次新', { exclude_new: true });
  push('cap', range('自由流通', f.free_cap_min, f.free_cap_max, 0, DEFAULT_FILTER.free_cap_max, ' 亿'), { free_cap_min: 0, free_cap_max: DEFAULT_FILTER.free_cap_max });
  push('price', range('股价', f.price_min, f.price_max, 0, DEFAULT_FILTER.price_max, ' 元'), { price_min: 0, price_max: DEFAULT_FILTER.price_max });
  push('pct', range('涨幅', f.pct_chg_min, f.pct_chg_max, null, null, '%'), { pct_chg_min: null, pct_chg_max: null });
  push('turnover', range('换手', f.turnover_today_min, f.turnover_today_max, 0, 100, '%'), { turnover_today_min: 0, turnover_today_max: 100 });
  push('turnover_yd', range('昨日换手', f.turnover_yd_min, f.turnover_yd_max, 0, 100, '%'), { turnover_yd_min: 0, turnover_yd_max: 100 });
  if (f.turnover_regime !== 'all') push('regime', REGIME_LABEL[f.turnover_regime], { turnover_regime: 'all' });
  push('ratio', range('昨换比', f.turnover_ratio_min, f.turnover_ratio_max, null, null, ''), { turnover_ratio_min: null, turnover_ratio_max: null });
  if (f.min_amount_today > 0) push('amount', `成交额 ≥ ${f.min_amount_today} 亿`, { min_amount_today: 0 });
  if (f.max_first_seal_time) push('seal_time', `${f.max_first_seal_time.slice(0, 5)} 前首封`, { max_first_seal_time: null });
  if (f.max_open_board_count != null) push('open', f.max_open_board_count === 0 ? '硬板（未开板）' : `炸板 ≤ ${f.max_open_board_count} 次`, { max_open_board_count: null });
  if (f.min_seal_to_amount_ratio > 0) push('seal_ratio', `封成比 ≥ ${f.min_seal_to_amount_ratio}%`, { min_seal_to_amount_ratio: 0 });
  if (f.only_early_seal) push('early', '09:35 前早封', { only_early_seal: false });
  if (f.min_seal_span_minutes != null) push('span', `封板时长 ≥ ${f.min_seal_span_minutes} 分`, { min_seal_span_minutes: null });
  if (f.min_streak_cum_return != null) push('cum', `区间累计 ≥ ${f.min_streak_cum_return}%`, { min_streak_cum_return: null });
  if (f.min_zt_count_1y > 0) push('zt', `一年涨停 ≥ ${f.min_zt_count_1y} 次`, { min_zt_count_1y: 0 });
  if (f.min_seal_success_rate_1y > 0) push('rate', `封板率 ≥ ${f.min_seal_success_rate_1y}%`, { min_seal_success_rate_1y: 0 });
  if (f.only_near_60d_high) push('high', '临近 60 日新高', { only_near_60d_high: false });
  f.selected_themes.forEach(theme => push(`theme:${theme}`, `#${theme}`, { selected_themes: f.selected_themes.filter(item => item !== theme) }));
  if (f.search.trim()) push('search', `“${f.search.trim()}”`, { search: '' });
  if (f.sort_by !== DEFAULT_FILTER.sort_by) push('sort', `按${SORT_LABEL[f.sort_by]}排序`, { sort_by: DEFAULT_FILTER.sort_by, sort_desc: true });
  return out;
}

export const SORT_OPTIONS = Object.entries(SORT_LABEL) as [ScreenerFilterRequest['sort_by'], string][];
