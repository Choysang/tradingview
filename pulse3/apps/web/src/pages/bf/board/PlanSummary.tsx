/**
 * Plan summary under a ranking row (triage #10/#14). Reads the plan through the existing getTradePlan operation (cached per
 * plan version, shared with the stock page). A plan from another release than the row's is hidden, as the plan card does.
 */
import { useQuery } from '@tanstack/react-query';
import { planQuery } from '../../../api/keys';
import type { PlanRef } from '../../../api/types';
import { MissingText } from '../../../components/Badges';
import { fmtPrice } from '../../../lib/format';
import { summarizePlan } from './planText';
import './board.css';

export function PlanSummary({ planRef, releaseId }: { planRef: PlanRef; releaseId: string | null }) {
  const q = useQuery(planQuery(planRef.plan_id, planRef.plan_version));
  if (q.isLoading) return <div className="bd-plan muted small">计划读取中…</div>;
  if (q.error || !q.data) return <div className="bd-plan muted small">计划暂时读取失败，进入个股页查看。</div>;
  const plan = q.data.data;
  if (releaseId && plan.based_on_release_id !== releaseId) {
    return <div className="bd-plan muted small">这份计划基于另一个发布版本，已隐藏。</div>;
  }
  const s = summarizePlan(plan);
  return (
    <dl className="bd-plan small" aria-label="交易计划摘要">
      <div><dt>买入区间</dt><dd className="mono">{s.entryZone ? `${fmtPrice(s.entryZone.lower)} ~ ${fmtPrice(s.entryZone.upper)} 元` : <MissingText>无价格区间</MissingText>}</dd></div>
      <div>
        <dt>失效/止损</dt>
        <dd title={s.stopText}>{s.stopPrice ? <span className="mono">{fmtPrice(s.stopPrice)} 元以下 </span> : null}<span className="muted">{s.stopText}</span></dd>
      </div>
      <div><dt>目标</dt><dd>{s.targetPrice ? <span className="mono">{fmtPrice(s.targetPrice)} 元</span> : <span className="muted">计划未给出目标价</span>}</dd></div>
      <div><dt>计划持有</dt><dd>{s.horizon}</dd></div>
      <div className="bd-plan-wide"><dt>卖出计划</dt><dd title={s.exitText}>{s.exitText}</dd></div>
    </dl>
  );
}
