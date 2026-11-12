/**
 * Trade plan card: state, validity, entry zone (raw prices), sizing, horizon, trigger
 * policy in plain words, and the three condition trees (entry / exit / invalidation)
 * with TRUE / FALSE / UNKNOWN. The tree values are a page preview from server-supplied
 * confirmed bars/quote; the server ConditionRunner remains the only trigger.
 */
import { useMemo } from 'react';
import type { BarWindow, Envelope, QuoteSnapshot, Strategy, StrategyInstance, TradePlan } from '../api/types';
import { evaluateCondition, makeLookupResolver } from '../lib/condition';
import { PLAN_STATE_LABEL, VALIDATION_LABEL, fmtPrice, fmtTime } from '../lib/format';
import { toPercent } from '../lib/decimal';
import { gradeOfInstance } from '../lib/grades';
import { PLAN_KIND_LABEL, type PlanKind } from '../lib/planKind';
import { checkVersionConsistency } from '../lib/version';
import { Badge, FreshnessBadge, GradeBadge, MissingText } from './Badges';
import { ConditionTree, TriBadge } from './ConditionTree';
import { DetailsButton } from './Diagnostics';

const STATE_TONE = { watching: 'neutral', actionable: 'good', holding: 'good', superseded: 'warn', expired: 'bad' } as const;

function describeTrigger(t: TradePlan['trigger_policy']): string {
  const fire = t.fire_mode === 'on_transition_to_true' ? '条件由「不成立」变为「成立」时提示一次' : '条件持续成立期间可重复提示';
  const rearm = t.rearm.mode === 'never' ? '触发后不再重新开启' : t.rearm.mode === 'after_false' ? '条件回到不成立后可再次开启' : `冷却 ${Math.round(t.rearm.cooldown_ms / 60000)} 分钟后可再次开启`;
  return `${fire}；最多 ${t.max_fires} 次；${rearm}；${t.require_final_bar ? '只在K线收盘确认后判断' : '可在盘中判断'}；无法判断（UNKNOWN）时不会触发；有未完成委托时${t.pending_order_policy === 'block_retrigger' ? '不重复触发' : '按目标差额净额处理'}。`;
}

function describeSizing(s: TradePlan['sizing']): string {
  return s.kind === 'fixed_shares' ? `固定 ${s.quantity_shares} 股` : `组合资金的 ${toPercent(s.fraction, 1) ?? '缺失'}${s.maximum_quantity_shares ? `，最多 ${s.maximum_quantity_shares} 股` : ''}`;
}

export interface PlanCardProps {
  planEnv: Envelope<TradePlan>;
  kind: PlanKind;
  workspaceReleaseId: string;
  bars: BarWindow | null;
  quote: QuoteSnapshot | null;
  byInstance: Map<string, Strategy>;
  instanceById: Map<string, StrategyInstance>;
}

export function PlanCard({ planEnv, kind, workspaceReleaseId, bars, quote, byInstance, instanceById }: PlanCardProps) {
  const plan = planEnv.data;
  const resolver = useMemo(() => makeLookupResolver({ bars, quote }), [bars, quote]);
  const entry = useMemo(() => evaluateCondition(plan.entry_conditions, resolver), [plan, resolver]);
  const exit = useMemo(() => evaluateCondition(plan.exit_conditions, resolver), [plan, resolver]);
  const invalid = useMemo(() => evaluateCondition(plan.invalidation, resolver), [plan, resolver]);
  const consistent = checkVersionConsistency(workspaceReleaseId, { release_id: plan.based_on_release_id });
  if (!consistent.ok) {
    return (
      <section className="card plan-card">
        <h3>{PLAN_KIND_LABEL[kind]}</h3>
        <div className="notice notice-warn">这份计划基于另一个发布版本（{plan.based_on_release_id.slice(0, 8)}），与当前页面（{workspaceReleaseId.slice(0, 8)}）不一致，已隐藏，避免新旧内容混在一起。</div>
      </section>
    );
  }

  return (
    <section className="card plan-card" aria-label={PLAN_KIND_LABEL[kind]}>
      <div className="plan-head">
        <h3>{PLAN_KIND_LABEL[kind]}</h3>
        <Badge tone={STATE_TONE[plan.state]}>{PLAN_STATE_LABEL[plan.state]}</Badge>
        <Badge tone={plan.validation.status === 'passed' ? 'good' : plan.validation.status === 'failed' ? 'bad' : 'warn'} title={`验证政策 ${plan.validation.policy_version}`}>{VALIDATION_LABEL[plan.validation.status]}</Badge>
        <FreshnessBadge state={planEnv.meta.freshness_state} />
        <span className="grow" />
        <DetailsButton title={`${PLAN_KIND_LABEL[kind]}详情`} meta={planEnv.meta} rows={[
          ['plan', `${plan.plan_id} v${plan.plan_version}`], ['based_on_release_id', plan.based_on_release_id], ['supersedes', plan.supersedes ? `v${plan.supersedes.plan_version}` : '无'],
          ['model_run_id', plan.model_run_id ?? '无'], ['evidence_revision_ids', plan.evidence_revision_ids.join('、') || '无'], ['account', plan.account_id ?? '不绑定账户'],
          ['replace_policy', plan.replace_policy], ['trigger_id', plan.trigger_policy.trigger_id], ['available_at', fmtTime(plan.available_at)],
          ['validation.result_ref', plan.validation.result_ref ?? '无'],
        ]} />
      </div>
      <dl className="kv compact">
        <dt>依据的策略</dt>
        <dd className="chips">
          {plan.strategy_refs.map((r) => {
            const s = byInstance.get(r.instance_id);
            const inst = instanceById.get(r.instance_id);
            return <span key={r.instance_id} className="chip">{s?.display_name ?? `策略 ${r.strategy_id.slice(0, 8)}`} <GradeBadge grade={inst ? gradeOfInstance(inst) : 'unregistered'} /></span>;
          })}
        </dd>
        <dt>有效期</dt><dd>{fmtTime(plan.valid_from)} 至 {fmtTime(plan.expires_at)}</dd>
        <dt>买入价区间</dt>
        <dd>
          {plan.entry_price_zone ? <>{fmtPrice(plan.entry_price_zone.lower_price_raw)} ~ {fmtPrice(plan.entry_price_zone.upper_price_raw)} 元（原始价，只是触发区间，不保证成交价）</> : <MissingText>无价格区间</MissingText>}
        </dd>
        <dt>仓位</dt><dd>{describeSizing(plan.sizing)}</dd>
        <dt>计划持有</dt><dd>{plan.holding_horizon.value} {plan.holding_horizon.unit === 'trading_days' ? '个交易日' : plan.holding_horizon.unit === 'sessions' ? '个交易时段' : '分钟'}</dd>
        <dt>提示规则</dt><dd className="small">{describeTrigger(plan.trigger_policy)}</dd>
      </dl>
      <div className="cond-section">
        <div className="cond-title"><b>买入条件</b> <TriBadge tri={entry.tri} /></div>
        <ConditionTree node={entry} />
      </div>
      <div className="cond-section">
        <div className="cond-title"><b>卖出条件</b> <TriBadge tri={exit.tri} /></div>
        <ConditionTree node={exit} />
      </div>
      <div className="cond-section">
        <div className="cond-title"><b>计划失效条件</b> <TriBadge tri={invalid.tri} /></div>
        <ConditionTree node={invalid} />
      </div>
      <p className="muted small">状态为页面预览：只用服务器已确认的K线/报价逐项对照，缺任何一项就显示「无法判断」，不会按 0 处理。真正的提示与触发由服务端判断。</p>
    </section>
  );
}
