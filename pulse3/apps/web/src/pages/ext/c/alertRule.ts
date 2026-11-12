/** Alert rule body construction (saveAlertRule). Pure, so it can be unit-tested. */
import type { AlertRuleBody, Condition } from './types';

export const ALERT_POLICY_VERSION = 'alert-rule-v1';

/**
 * v3 Condition for a rule whose trigger is fully described by its scope (news tier / instrument / sector).
 * The contract requires a Condition on every rule; the simplest valid one is a CompareCondition that is always
 * TRUE (constant 1 == constant 1, evaluated when a source event becomes available). The selection logic lives in
 * `scope`, not here. If the backend later grows a real news-tier condition, only this function changes.
 */
export function alwaysTrueCondition(): Condition {
  const one = { kind: 'constant', value: '1', unit: 'count' } as const;
  return { node: 'compare', clock: { kind: 'event_available' }, operator: 'eq', left: one, right: one } as Condition;
}

export type ScopeKind = 'news_tier' | 'instrument' | 'sector';
export const SCOPE_KIND_LABEL: Record<ScopeKind, string> = { news_tier: '新闻分级', instrument: '个股', sector: '行业' };

export interface RuleDraft { scopeKind: ScopeKind; scopeKey: string; destinationIds: string[]; enabled: boolean }

export function buildRuleBody(d: RuleDraft): AlertRuleBody {
  return {
    scope: { scope_kind: d.scopeKind, scope_key: d.scopeKey.trim(), membership_snapshot_id: null },
    condition: alwaysTrueCondition(),
    policy_version: ALERT_POLICY_VERSION,
    annotation_id: null,
    annotation_version: null,
    destination_ids: d.destinationIds,
    enabled: d.enabled,
  };
}

export function validateDraft(d: RuleDraft): string | null {
  if (d.scopeKey.trim() === '') return d.scopeKind === 'news_tier' ? '请选择分级' : '请填写范围标识';
  if (d.scopeKind === 'news_tier' && !['P0', 'P1', 'P2'].includes(d.scopeKey)) return '分级只能是 P0 / P1 / P2';
  if (d.destinationIds.length === 0) return '请至少选择一个通知去向';
  return null;
}

/** Same rule, flipped on/off; everything else (including the condition) is carried over unchanged. */
export const withEnabled = (body: AlertRuleBody, enabled: boolean): AlertRuleBody => ({ ...body, enabled });
