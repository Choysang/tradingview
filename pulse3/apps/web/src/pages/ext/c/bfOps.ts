/** Types and helpers of the ops backfill lane (contract backfill_ops.schema.json): forward monitor, test push, job labels. */

export type MonitorStatus = 'resolved' | 'pending';

export interface ForwardMonitorRow {
  release_id: string; ranking_id: string; horizon_id: string; horizon_sessions: number; decision_as_of: string; decision_session: string | null;
  status: MonitorStatus; pending_reason: 'HORIZON_NOT_REACHED' | 'AWAITING_MONITOR_RUN' | 'DECISION_SESSION_NOT_OBSERVED' | null;
  entry_session: string | null; exit_session: string | null; sessions_after: number | null;
  n_ranked: number | null; n_evaluable: number | null; top_n: number | null; top_evaluable: number | null;
  rank_ic: string | null; top_decile_mean_gross: string | null; universe_median_gross: string | null; top_decile_excess: string | null;
  missing_reason: string | null; return_basis: 'gross'; method_version: string; record_kind: 'live_forward' | 'reconstruction' | null;
  evaluated_at: string | null; snapshot_id: string | null;
}

export interface ForwardMonitorData {
  slot_key: string; method_version: string; return_basis: 'gross'; note: string; resolved_count: number; pending_count: number; rows: ForwardMonitorRow[];
}

export interface TestPushData {
  alert_id: string; destination_id: string; channel: 'in_app' | 'webhook'; status: 'confirmed' | 'accepted' | 'failed' | 'unknown';
  error_code: string | null; recorded_at: string; is_test: true;
}

export const MONITOR_PENDING_LABEL: Record<string, string> = {
  HORIZON_NOT_REACHED: '期限未到',
  AWAITING_MONITOR_RUN: '期限已到，等待监控任务计算',
  DECISION_SESSION_NOT_OBSERVED: '决策日行情尚未入库',
};

export const MONITOR_MISSING_LABEL: Record<string, string> = {
  TOO_FEW_EVALUABLE: '可评估股票太少',
  NOT_COMPUTABLE: '无法计算',
  NO_RANKED_INSTRUMENT: '榜单为空',
};

export const PUSH_STATUS_LABEL: Record<TestPushData['status'], string> = {
  confirmed: '已写入站内收件箱', accepted: '服务商已接收（手机是否收到无法确认）', failed: '发送失败', unknown: '结果未知（超时或连接中断，可能已送达）',
};

/** Display a decimal ratio string (e.g. "0.031250") as a percentage with two decimals; missing stays missing (null). */
export function pctText(v: string | null): string | null {
  if (v === null) return null;
  const n = Number(v);
  return Number.isFinite(n) ? `${(n * 100).toFixed(2)}%` : null;
}

/** Plain-language name of a registered job that the run form shows next to (not instead of) its operation id. */
export const JOB_LABELS: Record<string, { label: string; hint: string; params: string }> = {
  'ReleaseService.build_release': {
    label: '重新生成分析发布',
    hint: '按当前入库数据重新构建并发布一版分析（主发布位）。保留全部构建守卫：原始行情覆盖不足、研究窗口过短、同一输入正在构建时会拒绝并如实报失败；输入没变时返回已发布的同一版（幂等）。参数：as_of（latest / 日期 / 带时区时间）、top_n（1-50）、availability（observed / reconstructed，后者必须给明确 as_of）。',
    params: '{"as_of":"latest","top_n":10}',
  },
};
