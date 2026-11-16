/**
 * Pure drawing logic for the server-computed indicator series (docs/LEGACY_TRIAGE.md rows 41, 159).
 * Why: the browser only draws. This file maps the `StockIndicators` payload to chart points and decides whether the payload may be
 * drawn on a given candle window; it never computes an indicator value, never fills a missing value and never interpolates.
 * Invariants: a null value is a gap (no point); a payload is drawn only when its window_hash equals the hash of the candles
 * on screen (same bars); time keys follow the candle mapping exactly (daily: trade date, intraday: open time as CST wall clock).
 * Not covered: rendering (PriceChart owns the canvas).
 */
import type { Time, UTCTimestamp } from 'lightweight-charts';
import type { BarWindow } from '../../../api/types';
import type { IndicatorBar, IndicatorSeries, StockIndicators, SubPaneId } from './api';
import { toChartNumber } from '../../../lib/decimal';

const CST_OFFSET_S = 8 * 3600;

export interface LinePoint { time: Time; value: number }
export interface DrawnSeries { key: string; label: string; kind: 'line' | 'histogram'; points: LinePoint[]; color: string }
export interface OverlayToggles { ma: boolean; ema: boolean; boll: boolean }

export const isIntradayFrame = (tf: string): boolean => /^[0-9]+(s|m|h)$/.test(tf);

/** Chart time of a bar: the same mapping as the candles (PriceChart.plotBars). */
export function barTime(bar: Pick<IndicatorBar, 'bar_open_time' | 'trade_date'>, intraday: boolean): Time {
  return intraday ? ((Math.floor(Date.parse(bar.bar_open_time) / 1000) + CST_OFFSET_S) as UTCTimestamp) : (bar.trade_date as Time);
}

/** Fixed colour per series key, so a line keeps its colour across toggles. */
const COLORS: Record<string, string> = {
  ma5: '#f59e0b', ma10: '#3b82f6', ma20: '#a855f7', ma60: '#14b8a6', ema12: '#ec4899', ema26: '#64748b',
  boll_mid: '#a855f7', boll_upper: '#94a3b8', boll_lower: '#94a3b8',
  macd: '#3b82f6', macd_signal: '#f59e0b', macd_hist: '#94a3b8',
  kdj_k: '#3b82f6', kdj_d: '#f59e0b', kdj_j: '#a855f7', rsi6: '#3b82f6', rsi14: '#f59e0b',
};
export const colorOf = (key: string): string => COLORS[key] ?? '#888888';

/** Points of one series: nulls are gaps, a repeated time keeps the first value (candles do the same). */
export function seriesPoints(ind: Pick<StockIndicators, 'bars' | 'timeframe'>, s: Pick<IndicatorSeries, 'values'>): LinePoint[] {
  const intraday = isIntradayFrame(ind.timeframe);
  const out: LinePoint[] = [];
  const seen = new Set<string>();
  const n = Math.min(ind.bars.length, s.values.length);
  for (let i = 0; i < n; i += 1) {
    const v = toChartNumber(s.values[i]);
    if (v === null) continue;
    const time = barTime(ind.bars[i] as IndicatorBar, intraday);
    const k = String(time);
    if (seen.has(k)) continue;
    seen.add(k);
    out.push({ time, value: v });
  }
  return out;
}

export type Alignment = { ok: true } | { ok: false; reason: string };

/**
 * May this payload be drawn on these candles? Only when it was computed from the very window on screen.
 * A mismatch (a newer snapshot, another release, a different window) is reported, never papered over.
 */
export function checkAlignment(ind: StockIndicators | null | undefined, win: BarWindow | null | undefined): Alignment {
  if (!ind || !win) return { ok: false, reason: '指标或K线尚未读取' };
  if (ind.snapshot_id !== win.snapshot_id) return { ok: false, reason: '指标所依据的行情快照与图上K线不是同一个' };
  if (ind.window_hash !== win.window_hash) return { ok: false, reason: '指标与图上K线的窗口不一致（window_hash 不同），为避免错位不绘制' };
  if (ind.bars.length !== win.bars.length) return { ok: false, reason: '指标与K线的根数不同，为避免错位不绘制' };
  return { ok: true };
}

const pick = (ind: StockIndicators, test: (s: IndicatorSeries) => boolean): IndicatorSeries[] => ind.series.filter(test);
const draw = (ind: StockIndicators, s: IndicatorSeries, kind: DrawnSeries['kind'] = 'line'): DrawnSeries =>
  ({ key: s.key, label: s.label, kind, points: seriesPoints(ind, s), color: colorOf(s.key) });

/** Lines on the price pane for the enabled overlays (MA / EMA / BOLL); series the payload does not carry are simply absent. */
export function overlaySeries(ind: StockIndicators, on: OverlayToggles): DrawnSeries[] {
  const wanted = (s: IndicatorSeries): boolean => s.pane === 'price' && ((s.indicator === 'ma' && on.ma) || (s.indicator === 'ema' && on.ema) || (s.indicator === 'boll' && on.boll));
  return pick(ind, wanted).map((s) => draw(ind, s));
}

/** Series of the selected sub-pane; the MACD histogram is drawn as bars. */
export function subPaneSeries(ind: StockIndicators, pane: SubPaneId): DrawnSeries[] {
  return pick(ind, (s) => s.pane === 'sub' && s.indicator === pane).map((s) => draw(ind, s, s.output === 'hist' ? 'histogram' : 'line'));
}

/** Which indicator sets must be requested for the current toggles. */
export function requestedSets(on: OverlayToggles, sub: SubPaneId | 'none'): Array<'ma' | 'ema' | 'boll' | SubPaneId> {
  const sets: Array<'ma' | 'ema' | 'boll' | SubPaneId> = [];
  if (on.ma) sets.push('ma');
  if (on.ema) sets.push('ema');
  if (on.boll) sets.push('boll');
  if (sub !== 'none') sets.push(sub);
  return sets;
}

/** Explains the warm-up gap of the drawn series: where their first value appears (empty when nothing is missing at the start). */
export function warmupNote(ind: StockIndicators, drawn: DrawnSeries[]): string | null {
  const gaps = drawn.map((d) => ind.series.find((s) => s.key === d.key)).filter((s): s is IndicatorSeries => !!s)
    .filter((s) => s.first_valid_index === null || s.first_valid_index > 0);
  if (gaps.length === 0) return null;
  const empty = gaps.filter((s) => s.first_valid_index === null).map((s) => s.label);
  const longest = Math.max(...gaps.map((s) => s.first_valid_index ?? s.warmup_bars));
  const parts = [`窗口开头的K线不足以计算部分指标（最长 ${longest} 根），缺失处留空，没有补 0`];
  if (empty.length) parts.push(`${empty.join('、')} 在这个窗口里完全无值（K线不足）`);
  return parts.join('；');
}
