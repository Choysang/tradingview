/**
 * Pure helpers of the ext-B pages: catalog lookups, AST building / describing, artifact-ref values, percentiles.
 * Why: the browser never computes scores or returns (spec 4.2); it only builds the whitelisted AST the user asked for and
 * reads values the server stored. Invariants: a missing value stays null (shown as 缺失, never 0); units of constants always equal the
 * field's catalog unit; the node shapes are exactly what `ScreenAst` accepts (all / any / not / compare).
 */
import raw from './catalog.json';
import type { CompareCond, Cond, ScreenAst, SortSpec, Truth } from './api';

export interface FieldDef { id: string; label: string; unit: string; description: string; group: string; needs: string }
export interface PlaybookDef {
  id: string; name: string; family: string; style: string; horizon: string; rule: string; source: string; requires: string[];
  unavailable_reason: string | null; holding_days: number; stop_loss_pct: number | null; trailing_stop_pct: number | null; max_positions: number;
  params: Array<{ key: string; label: string; value: number }>;
}
export interface Catalog {
  catalog_version: string; field_version: string; subject_id: string; fields: FieldDef[]; unsupported_terms: string[];
  presets: Array<{ id: string; name: string; subtitle: string; text: string; note: string }>;
  patterns: Array<{ id: string; name: string; family: string; rule: string; omitted: string; min_bars: number }>;
  factors: Array<{ id: string; name: string; description: string }>;
  families: Record<string, string>; playbooks: PlaybookDef[]; exit_variants: Array<{ key: string; label: string }>;
}
export const CATALOG = raw as unknown as Catalog;
export const FIELD_BY_ID: Record<string, FieldDef> = Object.fromEntries(CATALOG.fields.map((f) => [f.id, f]));

export const CLOCK = { kind: 'bar_close', timeframe: '1d', session_policy_version: 'regular_continuous_v1' } as const;
export const UNIT_TEXT: Record<string, string> = { pct: '%', CNY: '元', ratio: '', count: '', index: '', flag: '', '100m_cny': '亿' };
export const OP_TEXT: Record<CompareCond['operator'], string> = { lt: '<', lte: '≤', gt: '>', gte: '≥', eq: '=' };
export const TRUTH_TEXT: Record<Truth, string> = { true: '满足', unknown: '未知', false: '不满足' };

// ---- AST building ------------------------------------------------------------------------------------------------
export function compare(field: string, operator: CompareCond['operator'], value: number | string): CompareCond {
  const f = FIELD_BY_ID[field];
  if (!f) throw new Error(`字段不在目录：${field}`);
  return {
    node: 'compare', clock: CLOCK, operator,
    left: { kind: 'feature', feature_id: f.id, definition_version: CATALOG.field_version, instrument_id: CATALOG.subject_id, timeframe: '1d', field_path: 'value', unit: f.unit, resolution_policy_version: 'latest-completed-bar-v1', initial_feature_key_hash: null },
    right: { kind: 'constant', value: String(value), unit: f.unit },
  };
}
export const allOf = (...c: Cond[]): Cond => (c.length === 1 ? (c[0] as Cond) : { node: 'all', clock: CLOCK, children: c });
export const anyOf = (...c: Cond[]): Cond => (c.length === 1 ? (c[0] as Cond) : { node: 'any', clock: CLOCK, children: c });
export const DEFAULT_SORT: SortSpec[] = [{ field_id: 'pct_chg', direction: 'desc', nulls: 'last' }];
export function makeAst(condition: Cond, sort: SortSpec[] = DEFAULT_SORT): ScreenAst {
  return { condition, allowed_field_catalog_version: CATALOG.catalog_version, sort };
}

/** Filter-rail state: optional bounds per numeric field plus switches. Empty strings mean "not set". */
export interface RailState {
  bounds: Record<string, { lo: string; hi: string }>;
  excludeSt: boolean; excludeNew: boolean; boards: string[]; patterns: string[];
}
export const RAIL_FIELDS = ['pct_chg', 'ret_5d', 'ret_20d', 'ret_60d', 'volume_ratio_5', 'close', 'limit_streak', 'growth_streak', 'limit_count_250', 'dist_60d_high', 'ma_gap_20', 'rsi_14', 'kdj_j', 'atr_ratio_14', 'turnover_proxy_20', 'float_cap'] as const;
export const emptyRail = (): RailState => ({ bounds: {}, excludeSt: true, excludeNew: false, boards: [], patterns: [] });
const isNum = (s: string | undefined): s is string => s !== undefined && s.trim() !== '' && Number.isFinite(Number(s));

/** Rail -> condition (null when the rail sets nothing). */
export function railToCond(r: RailState): Cond | null {
  const nodes: Cond[] = [];
  for (const [field, b] of Object.entries(r.bounds)) {
    if (isNum(b.lo)) nodes.push(compare(field, 'gte', Number(b.lo)));
    if (isNum(b.hi)) nodes.push(compare(field, 'lte', Number(b.hi)));
  }
  if (r.excludeSt) nodes.push(compare('is_st', 'eq', 0));
  if (r.excludeNew) nodes.push(compare('is_new', 'eq', 0));
  if (r.boards.length) nodes.push(anyOf(...r.boards.map((b) => compare(`board_${b}`, 'eq', 1))));
  for (const p of r.patterns) nodes.push(compare(`pattern.${p}`, 'eq', 1));
  return nodes.length ? allOf(...nodes) : null;
}
/** Merge a parsed sentence condition with the rail (AND). */
export function mergeConds(a: Cond | null, b: Cond | null): Cond | null {
  if (a && b) return allOf(a, b);
  return a ?? b;
}

// ---- describing --------------------------------------------------------------------------------------------------
export function describeCompare(c: CompareCond): string {
  const f = c.left.kind === 'feature' ? FIELD_BY_ID[c.left.feature_id] : undefined;
  const label = f?.label ?? (c.left.kind === 'feature' ? c.left.feature_id : '常量');
  const value = c.right.kind === 'constant' ? c.right.value : '?';
  if (f?.unit === 'flag') return value === '1' ? label : `非${label}`;
  return `${label} ${OP_TEXT[c.operator]} ${value}${UNIT_TEXT[f?.unit ?? ''] ?? ''}`;
}
/** Flatten a condition into removable chips: top-level `all` children are separate chips; `any` stays one chip. */
export function chipsOf(c: Cond): Array<{ key: string; text: string; cond: Cond }> {
  if (c.node === 'all') return c.children.flatMap(chipsOf);
  return [{ key: JSON.stringify(c), text: describeCond(c), cond: c }];
}
export function describeCond(c: Cond): string {
  switch (c.node) {
    case 'compare': return describeCompare(c);
    case 'not': return `非（${describeCond(c.child)}）`;
    case 'any': return c.children.map(describeCond).join(' 或 ');
    case 'all': return c.children.map(describeCond).join(' 且 ');
  }
}
export function condFromChips(chips: Array<{ cond: Cond }>): Cond | null {
  return chips.length ? allOf(...chips.map((c) => c.cond)) : null;
}

// ---- stored feature values (value rides in the artifact ref) ------------------------------------------------------
export interface CellValue { value: string | null; unit: string | null; missing: string | null }
export function cellOf(ref: string | null | undefined): CellValue {
  if (!ref || !ref.includes('?')) return { value: null, unit: null, missing: 'no_ref' };
  const q = new URLSearchParams(ref.slice(ref.indexOf('?') + 1));
  return { value: q.get('value'), unit: q.get('unit'), missing: q.get('missing') };
}
export function showValue(c: CellValue, digits = 2): string {
  if (c.value === null) return '缺失';
  const n = Number(c.value);
  if (!Number.isFinite(n)) return c.value;
  return n.toFixed(Math.abs(n) >= 1000 ? 0 : digits);
}

// ---- display statistics of known outcomes (empirical, never a calibrated probability) -------------------------------------
export function percentile(sorted: number[], p: number): number | null {
  if (!sorted.length) return null;
  const x = p * (sorted.length - 1);
  const lo = Math.floor(x);
  const hi = Math.ceil(x);
  const a = sorted[lo] as number;
  const b = sorted[hi] as number;
  return a + (b - a) * (x - lo);
}
export function outcomeSummary(values: Array<string | null>): { known: number; immature: number; upShare: number | null; q: Record<number, number | null> } {
  const nums = values.filter((v): v is string => v !== null).map(Number).filter(Number.isFinite).sort((a, b) => a - b);
  const q: Record<number, number | null> = {};
  for (const p of [10, 25, 50, 75, 90]) q[p] = percentile(nums, p / 100);
  return { known: nums.length, immature: values.length - nums.length, upShare: nums.length ? nums.filter((x) => x > 0).length / nums.length : null, q };
}
export const pct = (x: number | null, d = 1): string => (x === null ? '缺失' : `${(x * 100).toFixed(d)}%`);
export const metricPct = (v: string | null | undefined, d = 2): string => (v == null ? '缺失' : pct(Number(v), d));

/** Date (YYYY-MM-DD) -> contract instant at 00:00:00Z (the server reads it as the Asia/Shanghai session date). */
export const dayInstant = (d: string): string => `${d}T00:00:00Z`;

export function newUuid(): string {
  return crypto.randomUUID();
}
