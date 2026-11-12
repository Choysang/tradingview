/**
 * Evidence-grade display labels: 已验证 / 检验中 / 未检验 / 已证伪 (spec 6, product 02 sec.1).
 * Why: the grade must come from the strategy registry, not from page logic. The v3.0.0
 * contract has no explicit grade field, so this is a conservative display mapping of
 * registry fields (lifecycle, reproduction_status, health). It never upgrades:
 * only a reproduced + implemented + validated/active instance reads 已验证.
 * Implementation status, health and run permission stay separate badges.
 * Not covered: how the registry decides lifecycle.
 */
import type { StrategyInstance } from '../api/types';

export type EvidenceGrade = 'verified' | 'testing' | 'untested' | 'refuted' | 'unregistered';

export const GRADE_LABEL: Record<EvidenceGrade, string> = {
  verified: '已验证',
  testing: '检验中',
  untested: '未检验',
  refuted: '已证伪',
  unregistered: '未登记',
};

export function gradeOfInstance(i: Pick<StrategyInstance, 'lifecycle' | 'reproduction_status' | 'implementation_status'>): EvidenceGrade {
  if (i.lifecycle === 'retired' || i.reproduction_status === 'failed') return 'refuted';
  if (
    (i.lifecycle === 'validated' || i.lifecycle === 'active') &&
    i.reproduction_status === 'reproduced' &&
    i.implementation_status === 'implemented'
  ) {
    return 'verified';
  }
  if (i.lifecycle === 'screened' || i.lifecycle === 'shadow' || i.lifecycle === 'paused' || i.lifecycle === 'validated' || i.lifecycle === 'active' || i.reproduction_status === 'running') {
    return 'testing';
  }
  return 'untested';
}
