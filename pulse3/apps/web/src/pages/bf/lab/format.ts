/** Pure display helpers of the lab lane: verdict names, metric formatting, missing-reason text. No IO; a missing value is never shown as 0. */
import type { Metric, MetricName, Verdict } from './api';

export const VERDICT_TEXT: Record<Verdict, string> = { works: '有效', mixed: '分时期有效', inconclusive: '不确定', fails: '无效' };
export const VERDICT_TONE: Record<Verdict, 'good' | 'warn' | 'neutral' | 'bad'> = { works: 'good', mixed: 'warn', inconclusive: 'neutral', fails: 'bad' };

export const REASON_TEXT: Record<string, string> = {
  no_nav: '没有净值', single_nav_point: '只有一个净值点', base_nav_not_positive: '期初净值不为正', final_nav_not_positive: '期末净值不为正',
  fewer_than_2_returns: '日收益不足 2 个', no_returns: '没有日收益', zero_volatility: '波动为 0', no_downside: '没有下跌日', no_drawdown: '没有回撤',
  trade_list_unavailable: '没有成交明细', no_closed_trades: '没有已平仓交易', no_losses: '没有亏损交易', no_wins: '没有盈利交易', out_of_range: '数值超出范围', not_computable: '无法计算',
};
export const reasonText = (r: string | null): string => (r == null ? '缺失' : REASON_TEXT[r] ?? r);

export const RUN_STATE_TEXT: Record<string, string> = { queued: '排队中', running: '运行中', succeeded: '已完成', failed: '失败', cancelled: '已取消' };
export const SKIP_TEXT: Record<string, string> = { not_in_panel: '决策时点没有这只股票的数据', no_instrument_spec: '缺少板块/交易所规则，无法回放' };
export const skipText = (r: string | null): string => (r == null ? '' : r.startsWith('replay_error:') ? `回放出错（${r.slice(13)}）` : SKIP_TEXT[r] ?? r);

type Kind = 'pct' | 'ratio' | 'money' | 'sessions';
export const METRIC_META: Record<MetricName, { label: string; kind: Kind; hint: string }> = {
  total_return: { label: '总收益', kind: 'pct', hint: '期末净值 / 期初 - 1' },
  annualised_return: { label: '年化收益', kind: 'pct', hint: '按 252 个交易日复利年化' },
  annualised_volatility: { label: '年化波动', kind: 'pct', hint: '日收益样本标准差 × √252' },
  sharpe: { label: '夏普', kind: 'ratio', hint: '无风险利率 0；日收益均值 / 样本标准差 × √252' },
  sortino: { label: '索提诺', kind: 'ratio', hint: '下行偏差（低于 0 的日收益，总体口径）代替标准差' },
  max_drawdown: { label: '最大回撤', kind: 'pct', hint: '相对历史最高净值' },
  calmar: { label: '卡玛', kind: 'ratio', hint: '年化收益 / |最大回撤|' },
  longest_drawdown_sessions: { label: '最长回撤(交易日)', kind: 'sessions', hint: '从高点到收复（未收复则到最后一个交易日）' },
  profit_factor: { label: '盈亏因子', kind: 'ratio', hint: '盈利总额 / |亏损总额|（毛）' },
  win_rate: { label: '胜率', kind: 'pct', hint: '盈利笔数 / 已平仓笔数（平手计入分母）' },
  avg_win: { label: '平均盈利', kind: 'money', hint: '盈利交易的平均盈亏额（毛）' },
  avg_loss: { label: '平均亏损', kind: 'money', hint: '亏损交易的平均盈亏额（毛）' },
  payoff_ratio: { label: '盈亏比', kind: 'ratio', hint: '平均盈利 / |平均亏损|' },
  avg_win_return: { label: '平均盈利幅度', kind: 'pct', hint: '盈利交易的平均收益率' },
  avg_loss_return: { label: '平均亏损幅度', kind: 'pct', hint: '亏损交易的平均收益率' },
  exposure: { label: '持仓占比', kind: 'pct', hint: '收盘时有持仓的交易日占比' },
};

export function metricText(name: MetricName, m: Metric): string {
  if (m.value == null) return '缺失';
  const n = Number(m.value);
  if (!Number.isFinite(n)) return '缺失';
  switch (METRIC_META[name].kind) {
    case 'pct': return `${(n * 100).toFixed(2)}%`;
    case 'ratio': return n.toFixed(2);
    case 'sessions': return String(Math.round(n));
    case 'money': return n.toLocaleString('zh-CN', { maximumFractionDigits: 0 });
  }
}

/** Tone for a signed percent: red/green follow the shell's pos / neg classes; ratios and counts stay neutral. */
export const signClass = (name: MetricName, m: Metric): string => {
  if (m.value == null || !['total_return', 'annualised_return', 'avg_win', 'avg_loss', 'avg_win_return', 'avg_loss_return'].includes(name)) return '';
  return Number(m.value) >= 0 ? 'pos' : 'neg';
};
