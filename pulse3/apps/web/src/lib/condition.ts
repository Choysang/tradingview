/**
 * Three-valued (TRUE / FALSE / UNKNOWN) display of a plan condition tree
 * (spec 7: all/any/not, compare, crosses, within_session, event_state).
 * Why: the plan card must show which parts hold, which do not, and which cannot be
 * judged. Invariants: Kleene logic; a missing operand, a unit mismatch or a missing
 * previous observation yields UNKNOWN, never 0/false; UNKNOWN never "fires".
 * This is a PAGE PREVIEW over server-supplied values (confirmed bars / quote). It
 * does not trigger anything and does not replace the server ConditionRunner; the
 * contract has no evaluation-trace field (see docs/web/M4_evidence.md gap G3).
 * Not covered: indicator computation (values are only looked up, never derived).
 */
import type { BarWindow, Condition, Operand, QuoteSnapshot } from '../api/types';
import { compareDecimal } from './decimal';
import { unitLabel } from './format';

export type Tri = 'TRUE' | 'FALSE' | 'UNKNOWN';

export interface OperandValue {
  value: string;
  unit: string;
}

export interface OperandResolver {
  /** Latest confirmed value, or null when the server did not supply it. */
  current(op: Operand, clockKind: string): OperandValue | null;
  /** Previous confirmed observation (needed by crosses), or null. */
  previous(op: Operand, clockKind: string): OperandValue | null;
  /** null = cannot tell */
  inSession(kinds: string[]): boolean | null;
  eventState(eventId: string, minimumRevision: number, states: string[]): boolean | null;
}

export interface EvalNode {
  tri: Tri;
  kind: Condition['node'];
  label: string;
  /** why the node is UNKNOWN / what was compared */
  detail: string;
  children: EvalNode[];
}

export function triAnd(values: Tri[]): Tri {
  if (values.includes('FALSE')) return 'FALSE';
  if (values.length > 0 && values.every((v) => v === 'TRUE')) return 'TRUE';
  return 'UNKNOWN';
}

export function triOr(values: Tri[]): Tri {
  if (values.includes('TRUE')) return 'TRUE';
  if (values.length > 0 && values.every((v) => v === 'FALSE')) return 'FALSE';
  return 'UNKNOWN';
}

export function triNot(v: Tri): Tri {
  return v === 'TRUE' ? 'FALSE' : v === 'FALSE' ? 'TRUE' : 'UNKNOWN';
}

const FIELD_LABEL: Record<string, string> = {
  close_price_raw: '收盘价(原始价)',
  open_price_raw: '开盘价(原始价)',
  high_price_raw: '最高价(原始价)',
  low_price_raw: '最低价(原始价)',
  volume_shares: '成交量(股)',
  last_price_raw: '最新价(原始价)',
};

const OP_LABEL: Record<string, string> = { lt: '低于', lte: '不高于', gt: '高于', gte: '不低于', eq: '等于' };

export function describeOperand(op: Operand): string {
  if (op.kind === 'constant') return `${op.value} ${unitLabel(op.unit)}`;
  return FIELD_LABEL[op.field_path] ?? `${op.feature_id}.${op.field_path}`;
}

export function describeClock(c: Condition['clock']): string {
  switch (c.kind) {
    case 'quote': return '按报价判断';
    case 'bar_close': return `${c.timeframe} K线收盘后判断`;
    case 'session_open': return '开盘时判断';
    case 'event_available': return '事件出现时判断';
    default: return '定时判断';
  }
}

function cmp(op: string, c: -1 | 0 | 1): boolean {
  switch (op) {
    case 'lt': return c < 0;
    case 'lte': return c <= 0;
    case 'gt': return c > 0;
    case 'gte': return c >= 0;
    default: return c === 0;
  }
}

function unknown(kind: Condition['node'], label: string, detail: string, children: EvalNode[] = []): EvalNode {
  return { tri: 'UNKNOWN', kind, label, detail, children };
}

function operandValue(op: Operand, clockKind: string, r: OperandResolver, which: 'current' | 'previous'): OperandValue | null {
  if (op.kind === 'constant') return { value: op.value, unit: op.unit };
  return which === 'current' ? r.current(op, clockKind) : r.previous(op, clockKind);
}

export function evaluateCondition(cond: Condition, r: OperandResolver): EvalNode {
  switch (cond.node) {
    case 'all':
    case 'any': {
      const children = cond.children.map((c) => evaluateCondition(c, r));
      const tri = (cond.node === 'all' ? triAnd : triOr)(children.map((c) => c.tri));
      const label = cond.node === 'all' ? '以下条件需全部满足' : '以下条件满足任一即可';
      return { tri, kind: cond.node, label, detail: describeClock(cond.clock), children };
    }
    case 'not': {
      const child = evaluateCondition(cond.child, r);
      return { tri: triNot(child.tri), kind: 'not', label: '以下条件不成立', detail: describeClock(cond.clock), children: [child] };
    }
    case 'compare': {
      const label = `${describeOperand(cond.left)} ${OP_LABEL[cond.operator] ?? cond.operator} ${describeOperand(cond.right)}`;
      const l = operandValue(cond.left, cond.clock.kind, r, 'current');
      const rr = operandValue(cond.right, cond.clock.kind, r, 'current');
      if (!l || !rr) {
        return unknown('compare', label, `缺少${!l ? describeOperand(cond.left) : describeOperand(cond.right)}的确认值，按 UNKNOWN 处理（不当作 0）`);
      }
      if (l.unit !== rr.unit) return unknown('compare', label, `单位不一致（${unitLabel(l.unit)} 与 ${unitLabel(rr.unit)}），无法比较`);
      const c = compareDecimal(l.value, rr.value);
      if (c === null) return unknown('compare', label, '数值格式无法识别');
      return { tri: cmp(cond.operator, c) ? 'TRUE' : 'FALSE', kind: 'compare', label, detail: `当前值 ${l.value} ${unitLabel(l.unit)}，${describeClock(cond.clock)}`, children: [] };
    }
    case 'crosses': {
      const label = `${describeOperand(cond.left)} 向${cond.direction === 'above' ? '上' : '下'}穿越 ${describeOperand(cond.right)}`;
      const cl = operandValue(cond.left, cond.clock.kind, r, 'current');
      const cr = operandValue(cond.right, cond.clock.kind, r, 'current');
      const pl = operandValue(cond.left, cond.clock.kind, r, 'previous');
      const pr = operandValue(cond.right, cond.clock.kind, r, 'previous');
      if (!cl || !cr || !pl || !pr) return unknown('crosses', label, '穿越判断需要前后两个已确认时点，缺任一个则为 UNKNOWN');
      if (cl.unit !== cr.unit || pl.unit !== pr.unit) return unknown('crosses', label, '单位不一致，无法比较');
      const prev = compareDecimal(pl.value, pr.value);
      const cur = compareDecimal(cl.value, cr.value);
      if (prev === null || cur === null) return unknown('crosses', label, '数值格式无法识别');
      const hit = cond.direction === 'above' ? prev <= 0 && cur > 0 : prev >= 0 && cur < 0;
      return { tri: hit ? 'TRUE' : 'FALSE', kind: 'crosses', label, detail: describeClock(cond.clock), children: [] };
    }
    case 'within_session': {
      const label = `处于交易时段：${cond.session_kinds.join('、')}`;
      const v = r.inSession(cond.session_kinds);
      if (v === null) return unknown('within_session', label, '页面没有服务器提供的时段状态，无法判断');
      return { tri: v ? 'TRUE' : 'FALSE', kind: 'within_session', label, detail: describeClock(cond.clock), children: [] };
    }
    case 'event_state': {
      const label = `事件 ${cond.event_id.slice(0, 8)} 状态属于 ${cond.states.join('、')}（修订号不低于 ${cond.minimum_revision}）`;
      const v = r.eventState(cond.event_id, cond.minimum_revision, cond.states);
      if (v === null) return unknown('event_state', label, '页面没有该事件的当前修订，无法判断');
      return { tri: v ? 'TRUE' : 'FALSE', kind: 'event_state', label, detail: describeClock(cond.clock), children: [] };
    }
  }
}

type AnyBar = BarWindow['bars'][number];

function barValue(bar: AnyBar | undefined, field: string): string | null {
  if (!bar) return null;
  const v = (bar as Record<string, unknown>)[field];
  return typeof v === 'string' ? v : null;
}

/**
 * Resolver that only LOOKS UP server-supplied fields: the last / previous final bar of
 * the matching timeframe, and the quote snapshot for quote-clock operands.
 */
export function makeLookupResolver(opts: {
  bars?: BarWindow | null;
  quote?: QuoteSnapshot | null;
  eventStates?: Record<string, { revision: number; state: string }>;
}): OperandResolver {
  const finals = (opts.bars?.bars ?? []).filter((b) => b.is_final && b.quality_status !== 'invalid');
  const last = finals[finals.length - 1];
  const prev = finals[finals.length - 2];
  const barsRaw = opts.bars?.adjustment_mode === 'raw';
  const look = (op: Operand, clockKind: string, which: 'current' | 'previous'): OperandValue | null => {
    if (op.kind !== 'feature') return null;
    if (clockKind === 'quote') {
      if (which === 'previous') return null;
      const v = (opts.quote?.quote as Record<string, unknown> | undefined)?.[op.field_path];
      return typeof v === 'string' ? { value: v, unit: op.unit } : null;
    }
    if (!opts.bars || !barsRaw || opts.bars.timeframe !== op.timeframe) return null;
    const v = barValue(which === 'current' ? last : prev, op.field_path);
    return v === null ? null : { value: v, unit: op.unit };
  };
  return {
    current: (op, c) => look(op, c, 'current'),
    previous: (op, c) => look(op, c, 'previous'),
    inSession: () => null,
    eventState: (id, minRev, states) => {
      const e = opts.eventStates?.[id];
      if (!e) return null;
      return e.revision >= minRev && states.includes(e.state);
    },
  };
}

export const TRI_LABEL: Record<Tri, string> = { TRUE: '成立', FALSE: '不成立', UNKNOWN: '无法判断' };
