/**
 * Short, readable summary of a TradePlan for ranking rows (triage #10/#14): entry zone, stop (invalidation), target,
 * holding horizon and the exit plan as one line of words.
 * Why: the contract has no stop or target price fields; the plan carries an `invalidation` and an `exit_conditions` tree.
 * A stop/target price is shown only when the tree says it plainly (a price compare against a CNY constant); otherwise the
 * condition is rendered as words and the price stays missing. Nothing is invented or computed (the browser only formats).
 */
import type { Condition, Operand, TradePlan } from '../../../api/types';
import { describeOperand } from '../../../lib/condition';
import { fmtPrice, unitLabel } from '../../../lib/format';

const OP_WORD: Record<string, string> = { lt: '低于', lte: '不高于', gt: '高于', gte: '不低于', eq: '等于' };
const FLIP = { lt: 'gt', lte: 'gte', gt: 'lt', gte: 'lte', eq: 'eq' } as const;
const PRICE_FIELDS = new Set(['close_price_raw', 'last_price_raw', 'high_price_raw', 'low_price_raw', 'open_price_raw']);
const PRICE_UNITS = new Set(['CNY/share', 'CNY']);
const MAX_DEPTH = 4;

function operandText(o: Operand): string {
  return o.kind === 'constant' ? `${fmtPrice(o.value)} ${unitLabel(o.unit)}` : describeOperand(o);
}

/** One line of words for a condition tree; deep trees are cut with an ellipsis, never silently dropped. */
export function conditionText(c: Condition, depth = 0): string {
  if (depth > MAX_DEPTH) return '…';
  switch (c.node) {
    case 'compare': return `${operandText(c.left)}${OP_WORD[c.operator] ?? c.operator}${operandText(c.right)}`;
    case 'crosses': return `${operandText(c.left)}${c.direction === 'above' ? '上穿' : '下穿'}${operandText(c.right)}`;
    case 'within_session': return '处于连续竞价时段';
    case 'event_state': return `事件状态为 ${c.states.join('/')}`;
    case 'not': return `不满足（${conditionText(c.child, depth + 1)}）`;
    case 'all':
    case 'any': {
      const parts = c.children.map((x) => conditionText(x, depth + 1));
      if (parts.length === 0) return '（空条件）';
      const joined = parts.join(c.node === 'all' ? ' 且 ' : ' 或 ');
      return parts.length > 1 && depth > 0 ? `（${joined}）` : joined;
    }
    default: return '…';
  }
}

interface PricePoint { price: string; op: string; text: string }

/** A plain price compare `price-field <op> CNY constant` (either side), or null. */
function pricePoint(c: Condition): PricePoint | null {
  if (c.node !== 'compare') return null;
  let feat: Operand = c.left;
  let konst: Operand = c.right;
  let op: string = c.operator;
  if (feat.kind === 'constant' && konst.kind === 'feature') {
    [feat, konst] = [konst, feat];
    op = FLIP[c.operator];
  }
  if (feat.kind !== 'feature' || konst.kind !== 'constant') return null;
  if (!PRICE_FIELDS.has(feat.field_path) || !PRICE_UNITS.has(konst.unit)) return null;
  return { price: konst.value, op, text: conditionText(c) };
}

/** A top-level compare, or the alternatives of a top-level `any` (each alone ends the position). */
function alternatives(c: Condition): Condition[] {
  return c.node === 'any' ? c.children : [c];
}

export interface PlanSummaryData {
  entryZone: { lower: string; upper: string } | null;
  /** price the invalidation falls to, when the tree says it as a price compare; null otherwise */
  stopPrice: string | null;
  stopText: string;
  targetPrice: string | null;
  targetText: string | null;
  horizon: string;
  exitText: string;
  state: TradePlan['state'];
}

export function summarizePlan(plan: TradePlan): PlanSummaryData {
  const stops = alternatives(plan.invalidation).map(pricePoint).filter((p): p is PricePoint => p !== null && (p.op === 'lt' || p.op === 'lte'));
  const targets = alternatives(plan.exit_conditions).map(pricePoint).filter((p): p is PricePoint => p !== null && (p.op === 'gt' || p.op === 'gte'));
  const h = plan.holding_horizon;
  const unit = h.unit === 'trading_days' ? '个交易日' : h.unit === 'sessions' ? '个交易时段' : '分钟';
  return {
    entryZone: plan.entry_price_zone ? { lower: plan.entry_price_zone.lower_price_raw, upper: plan.entry_price_zone.upper_price_raw } : null,
    stopPrice: stops.length === 1 ? (stops[0] as PricePoint).price : null,
    stopText: conditionText(plan.invalidation),
    targetPrice: targets.length === 1 ? (targets[0] as PricePoint).price : null,
    targetText: targets.length === 1 ? (targets[0] as PricePoint).text : null,
    horizon: `${h.value} ${unit}`,
    exitText: conditionText(plan.exit_conditions),
    state: plan.state,
  };
}
