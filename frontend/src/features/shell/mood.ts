/**
 * Presentation of the backend's sentiment score. The score itself is computed
 * server-side; this only names the band and picks the ambient hue.
 */
import type { MarketHistoryPoint } from '../../core/contract';

export interface MoodBand {
  word: string;
  hint: string;
}

const BANDS: [number, MoodBand][] = [
  [25, { word: '冰点', hint: '亏钱效应主导，空间被压缩' }],
  [40, { word: '低迷', hint: '赚钱效应稀薄，接力谨慎' }],
  [55, { word: '中性', hint: '分歧与修复并存' }],
  [70, { word: '升温', hint: '梯队完整，接力意愿回升' }],
  [85, { word: '亢奋', hint: '涨停扩散，注意高位分歧' }],
  [101, { word: '高潮', hint: '情绪顶点附近，风险收益比下降' }],
];

export function moodBand(score: number | null | undefined): MoodBand {
  if (score == null) return { word: '—', hint: '等待第一份快照' };
  return BANDS.find(([limit]) => score < limit)?.[1] ?? BANDS[BANDS.length - 1][1];
}

/** Hue in OKLCH degrees: cold mint (165) → neutral gold (82) → hot coral (19). */
export function moodHue(score: number | null | undefined) {
  if (score == null) return 82;
  const clamped = Math.max(0, Math.min(100, score));
  return clamped < 50 ? 165 - (clamped / 50) * (165 - 82) : 82 - ((clamped - 50) / 50) * (82 - 19);
}

/** Slope of the last points of the session curve, in score points. */
export function moodTrend(curve: MarketHistoryPoint[]) {
  const tail = curve.slice(-12);
  if (tail.length < 3) return { delta: 0, label: '持平' };
  const delta = tail[tail.length - 1].value - tail[0].value;
  return { delta, label: delta > 1.5 ? '走强' : delta < -1.5 ? '走弱' : '持平' };
}
