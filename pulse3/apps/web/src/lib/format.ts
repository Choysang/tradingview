/**
 * Display formatting and Chinese labels for contract enums.
 * Why: plain-language UI for a retail investor (spec 1.1); all instants are UTC Z
 * on the wire and shown in Asia/Shanghai. Not covered: any business computation.
 */
import { toFixed } from './decimal';

const TZ = 'Asia/Shanghai';

const dtf = new Intl.DateTimeFormat('zh-CN', {
  timeZone: TZ, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
});
const dtfDate = new Intl.DateTimeFormat('zh-CN', { timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit' });

export function fmtTime(ts: string | null | undefined): string {
  if (!ts) return '未知';
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return '未知';
  return dtf.format(d).replace(/\//g, '-');
}

export function fmtDate(ts: string | null | undefined): string {
  if (!ts) return '未知';
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return '未知';
  return dtfDate.format(d).replace(/\//g, '-');
}

export function ageSeconds(ts: string | null | undefined, now = Date.now()): number | null {
  if (!ts) return null;
  const t = Date.parse(ts);
  return Number.isNaN(t) ? null : Math.max(0, Math.round((now - t) / 1000));
}

export function fmtAge(ts: string | null | undefined, now = Date.now()): string {
  const s = ageSeconds(ts, now);
  if (s === null) return '未知';
  if (s < 60) return `${s} 秒前`;
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  return `${Math.floor(s / 86400)} 天前`;
}

export function fmtPrice(v: string | null | undefined): string {
  return toFixed(v, 2) ?? '缺失';
}

export function fmtInt(v: number | null | undefined): string {
  return v == null ? '缺失' : v.toLocaleString('zh-CN');
}

export function fmtMoney(v: string | null | undefined): string {
  const s = toFixed(v, 2);
  if (s === null) return '缺失';
  const neg = s.startsWith('-');
  const [i = '0', f = '00'] = (neg ? s.slice(1) : s).split('.');
  return `${neg ? '-' : ''}${i.replace(/\B(?=(\d{3})+(?!\d))/g, ',')}.${f}`;
}

export function shortId(id: string | null | undefined): string {
  return id ? id.slice(0, 8) : '—';
}

export const UNIT_LABELS: Record<string, string> = {
  'CNY/share': '元/股', CNY: '元', shares: '股', ratio: '比例', percent: '%',
};
export const unitLabel = (u: string): string => UNIT_LABELS[u] ?? u;

export const ACTION_STATUS_LABEL: Record<string, string> = {
  watching: '观察中', actionable: '条件已触发', blocked: '暂不可操作', missing_data: '数据缺失',
};
export const PLAN_STATE_LABEL: Record<string, string> = {
  watching: '观察中', actionable: '可操作', holding: '持有中', superseded: '已被新版替代', expired: '已过期',
};
export const ACTIONABILITY_LABEL: Record<string, string> = {
  eligible: '可执行', not_triggered: '条件未触发', blocked: '被限制', unknown: '无法判断',
};
export const SIDE_LABEL: Record<string, string> = { buy: '买入', sell: '卖出' };
export const POOL_STATE_LABEL: Record<string, string> = {
  observing: '观察', qualified: '入选', actionable: '可操作', cooling: '降温', removed: '已移出',
};
export const RANK_KIND_LABEL: Record<string, string> = {
  sector_opportunity_rank: '机会榜', sector_attention_rank: '关注度榜', sector_rising_rank: '升温榜',
};
export const RUN_STATE_LABEL: Record<string, string> = {
  queued: '排队中', running: '运行中', completed: '已完成', succeeded: '已完成', failed: '失败', cancelled: '已取消', scheduled: '已排期',
};
export const LIFECYCLE_LABEL: Record<string, string> = {
  discovered: '已发现', specified: '已定义', data_ready: '数据就绪', screened: '已初筛', validated: '已验证',
  shadow: '影子运行', active: '已启用', paused: '已暂停', retired: '已退役',
};
export const ROLE_LABEL: Record<string, string> = {
  selection: '选股', allocation: '配置', entry: '入场', holding: '持有', execution: '执行', exit: '退出',
};
export const RUNTIME_STATE_LABEL: Record<string, string> = {
  healthy: '运行正常', degraded: '降级', unavailable: '不可用', auth_required: '需要授权', paused: '已暂停', unconfigured: '未配置',
};
export const FRESHNESS_LABEL: Record<string, string> = {
  current: '数据新鲜', stale: '数据已过期', unknown: '新鲜度未知', not_expected: '当前无需更新',
};
export const QUALITY_LABEL: Record<string, string> = {
  valid: '数据完整', partial: '部分缺失', invalid: '数据无效', disputed: '来源有争议',
};
export const CLAIM_STATUS_LABEL: Record<string, string> = {
  supported: '来源支持', contradicted: '被反驳', disputed: '有争议', unverified: '未证实', not_falsifiable_yet: '暂无法证伪',
};
export const ORIGIN_LABEL: Record<string, string> = {
  confirmed_original: '原始来源', confirmed_independent_report: '独立报道', republication: '转载', unknown: '来源未确认',
};
export const DOC_STATUS_LABEL: Record<string, string> = {
  published: '已发布', corrected: '已更正', withdrawn: '已撤回', deleted_at_source: '源站已删除', unknown: '状态未知',
};
export const ASSERTION_LABEL: Record<string, string> = {
  official_disclosure: '官方披露', independently_reported: '独立报道', attributed_statement: '转述',
  opinion: '观点', forecast: '预测', rumor: '传闻',
};
export const MEASUREMENT_CLASS_LABEL: Record<string, string> = {
  disclosed_observation: '交易所/机构披露', provider_derived: '平台推算', research_inference: '研究推断',
};
export const RESOLUTION_LABEL: Record<string, string> = {
  resolved: '已确认席位', ambiguous: '可能对应多个席位', unresolved: '未能确认', report_local_label: '仅为报告内标签',
};
export const PUB_STATUS_LABEL: Record<string, string> = {
  preprint: '预印本', submitted: '已投稿', accepted: '已录用', published: '已发表', withdrawn: '已撤回', unknown: '状态未知',
};
export const READ_DEPTH_LABEL: Record<string, string> = {
  metadata: '仅元数据', abstract: '读到摘要', full_text_sections: '读过部分正文', full_text: '读过全文', code_review: '审阅过代码', reproduced: '已复现',
};
export const OUTCOME_LABEL: Record<string, string> = {
  pending: '待定', left: '左方胜', right: '右方胜', tie: '平局', bye: '轮空', incomparable: '不可比较',
};
export const VALIDATION_LABEL: Record<string, string> = { pending: '验证中', passed: '已通过验证', failed: '未通过验证' };
export const VIEW_KIND_LABEL: Record<string, string> = { public_research: '公开研究视角', account_holding: '我的持仓视角' };
export const RUN_PURPOSE_LABEL: Record<string, string> = {
  screening: '初筛', validation: '验证', shadow: '影子运行', blind_final: '盲测终评',
};
