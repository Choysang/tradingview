/**
 * Which plans on a workbench are the main plan and which are T (做T) / grid (网格)
 * sub-plans. The v3.0.0 TradePlan has no explicit kind field, so this classifies by
 * the registered strategy of the plan (method_family / role). Heuristic, display only
 * (docs/web/M4_evidence.md gap G4). Unknown => generic "子计划".
 */
import type { Strategy, TradePlan } from '../api/types';

export type PlanKind = 'main' | 't' | 'grid' | 'sub';

export const PLAN_KIND_LABEL: Record<PlanKind, string> = { main: '主计划', t: '做T 子计划', grid: '网格 子计划', sub: '子计划' };

export function classifyPlan(plan: Pick<TradePlan, 'strategy_refs'>, byInstance: Map<string, Strategy>): PlanKind {
  const families = plan.strategy_refs.map((r) => byInstance.get(r.instance_id)).filter((s): s is Strategy => !!s);
  if (families.some((s) => /grid/i.test(s.method_family))) return 'grid';
  if (families.some((s) => /(^|_)t(_|$)|t_trade|intraday_t|do_t/i.test(s.method_family))) return 't';
  if (families.some((s) => s.role === 'selection' || s.role === 'entry')) return 'main';
  return families.length === 0 ? 'main' : 'sub';
}

/** Main plan first, then T, grid, other sub-plans; stable for equal kinds. */
export function sortPlans<T extends { kind: PlanKind }>(items: T[]): T[] {
  const order: Record<PlanKind, number> = { main: 0, t: 1, grid: 2, sub: 3 };
  return items.map((x, i) => ({ x, i })).sort((a, b) => order[a.x.kind] - order[b.x.kind] || a.i - b.i).map(({ x }) => x);
}
