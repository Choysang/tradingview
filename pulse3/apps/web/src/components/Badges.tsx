import type { ReactNode } from 'react';
import type { FreshnessState, QualityStatus } from '../api/types';
import { FRESHNESS_LABEL, QUALITY_LABEL } from '../lib/format';
import { GRADE_LABEL, type EvidenceGrade } from '../lib/grades';
import { freshnessTone, type Tone } from '../lib/version';

export function Badge({ tone = 'neutral', children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return <span className={`badge badge-${tone}`} title={title}>{children}</span>;
}

export function FreshnessBadge({ state, label }: { state: FreshnessState; label?: string }) {
  return <Badge tone={freshnessTone(state)} title={`freshness_state = ${state}`}>{label ?? FRESHNESS_LABEL[state] ?? state}</Badge>;
}

export function QualityBadge({ status }: { status: QualityStatus }) {
  const tone: Tone = status === 'valid' ? 'good' : status === 'partial' ? 'warn' : 'bad';
  return <Badge tone={tone} title={`quality_status = ${status}`}>{QUALITY_LABEL[status] ?? status}</Badge>;
}

const GRADE_TONE: Record<EvidenceGrade, Tone> = { verified: 'good', testing: 'warn', untested: 'neutral', refuted: 'bad', unregistered: 'neutral' };
const GRADE_HINT: Record<EvidenceGrade, string> = {
  verified: '登记库中已复现并通过验证',
  testing: '正在影子运行或复现中，结论尚不稳定',
  untested: '尚未检验，只是一个想法或原型',
  refuted: '检验未通过或已退役',
  unregistered: '登记库中没有这个策略',
};

export function GradeBadge({ grade }: { grade: EvidenceGrade }) {
  return <Badge tone={GRADE_TONE[grade]} title={GRADE_HINT[grade]}>{GRADE_LABEL[grade]}</Badge>;
}

export function MissingText({ children = '缺失' }: { children?: ReactNode }) {
  return <span className="missing" title="数据缺失，没有用 0 或旧值替代">{children}</span>;
}
