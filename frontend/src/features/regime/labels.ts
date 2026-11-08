/** Display names and colour tokens for regime states, verdicts and horizons. */
import type { PlaybookSpec, PlaybookSummary, RegimePoint } from '../../core/contract';

export type Dimension = 'trend' | 'liquidity' | 'sentiment' | 'style';

interface StateMeta {
  label: string;
  short: string;
  colour: string;
}

/** Ordered along each dimension's spectrum, so a meter can place the active state. */
export const REGIME_STATES: Record<Dimension, Record<string, StateMeta>> = {
  trend: {
    bear: { label: '熊市', short: '熊', colour: '--down' },
    range: { label: '震荡', short: '震', colour: '--cyan' },
    bull: { label: '牛市', short: '牛', colour: '--up' },
  },
  liquidity: {
    contracting: { label: '缩量', short: '缩', colour: '--down' },
    steady: { label: '存量', short: '存', colour: '--gold' },
    expanding: { label: '增量', short: '增', colour: '--up' },
  },
  sentiment: {
    freezing: { label: '冰点', short: '冰', colour: '--cyan' },
    repair: { label: '修复', short: '修', colour: '--gold' },
    climax: { label: '高潮', short: '潮', colour: '--up' },
    ebb: { label: '退潮', short: '退', colour: '--down' },
  },
  style: {
    large: { label: '大盘占优', short: '大', colour: '--accent' },
    balanced: { label: '均衡', short: '均', colour: '--text-3' },
    small: { label: '小盘占优', short: '小', colour: '--gold' },
  },
};

export const DIMENSIONS: { key: Dimension; label: string }[] = [
  { key: 'trend', label: '趋势' },
  { key: 'liquidity', label: '资金' },
  { key: 'sentiment', label: '情绪' },
  { key: 'style', label: '风格' },
];

export function stateMeta(dimension: Dimension, state: string): StateMeta {
  return REGIME_STATES[dimension][state] ?? { label: state, short: state, colour: '--text-3' };
}

export function stateOf(point: RegimePoint, dimension: Dimension): string {
  return point[dimension];
}

export type Verdict = PlaybookSummary['verdict'];

export const VERDICT: Record<Verdict, { label: string; badge: string; hint: string }> = {
  effective: { label: '有效', badge: 'up', hint: '样本外仍为正收益，且统计显著' },
  regime_dependent: { label: '依赖周期', badge: 'gold', hint: '整体一般，但在特定周期里显著有效' },
  weak: { label: '偏弱', badge: 'outline', hint: '扣除成本后没有稳定优势' },
  overfit: { label: '过拟合', badge: 'down', hint: '样本内好看，样本外失效' },
  insufficient: { label: '样本不足', badge: 'outline', hint: '历史交易太少，结论不可靠' },
  unavailable: { label: '无法回测', badge: 'cyan', hint: '缺少时点数据，只用于今日排序' },
};

export const VERDICT_ORDER: Verdict[] = ['effective', 'regime_dependent', 'weak', 'overfit', 'insufficient', 'unavailable'];

export type Horizon = PlaybookSpec['horizon'];

export const HORIZON: Record<Horizon, string> = { short: '短线', swing: '波段', long: '长线' };

export const STYLE: Record<PlaybookSpec['style'], string> = {
  sentiment: '情绪',
  trend: '趋势',
  momentum: '动量',
  reversal: '反转',
  breakout: '突破',
  volume: '量能',
  volatility: '波动',
  value: '价值',
};
