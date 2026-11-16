/**
 * Candlestick + volume chart (lightweight-charts 5.3.0). Every plotted number is a
 * server value from getStockBars; the browser never aggregates K-lines or computes
 * indicators. Source gaps stay gaps (bars with null prices are not drawn or filled).
 * Plan zones / drawings are shown only when their price basis matches the chart.
 * Indicator overlays (MA/EMA/BOLL on the price pane) and one sub-pane (MACD/KDJ/RSI) are drawn from server series
 * (`DrawnSeries`, see pages/bf/stock/indicatorDraw.ts); null values are gaps, nothing is computed here.
 */
import { useEffect, useRef } from 'react';
import {
  CandlestickSeries, ColorType, CrosshairMode, HistogramSeries, LineSeries, LineStyle, createChart,
  type IChartApi, type IPriceLine, type ISeriesApi, type Time,
} from 'lightweight-charts';
import type { BarWindow } from '../api/types';
import { toChartNumber } from '../lib/decimal';
import { isIntraday } from '../lib/barsParams';
import { barTime, type DrawnSeries } from '../pages/bf/stock/indicatorDraw';

export interface ChartLine {
  id: string;
  price: number;
  title: string;
  tone: 'plan' | 'draw' | 'exit' | 'risk';
  dashed?: boolean;
}

export interface PlottedBars {
  candles: Array<{ time: Time; open: number; high: number; low: number; close: number; color?: string; borderColor?: string; wickColor?: string }>;
  volumes: Array<{ time: Time; value: number; color: string }>;
  gaps: number;
  unfinished: number;
}

type AnyBar = BarWindow['bars'][number];

function ohlc(b: AnyBar): { o: number | null; h: number | null; l: number | null; c: number | null } {
  if ('open_price_raw' in b) return { o: toChartNumber(b.open_price_raw), h: toChartNumber(b.high_price_raw), l: toChartNumber(b.low_price_raw), c: toChartNumber(b.close_price_raw) };
  return { o: toChartNumber(b.open_price), h: toChartNumber(b.high_price), l: toChartNumber(b.low_price), c: toChartNumber(b.close_price) };
}

/** Pure mapping from contract bars to chart points (tested without a canvas). */
export function plotBars(win: BarWindow, colors: { up: string; down: string; upSoft: string; downSoft: string }): PlottedBars {
  const intraday = isIntraday(win.timeframe);
  const candles: PlottedBars['candles'] = [];
  const volumes: PlottedBars['volumes'] = [];
  let gaps = 0;
  let unfinished = 0;
  const seen = new Set<string>();
  for (const b of win.bars) {
    const { o, h, l, c } = ohlc(b);
    if (o === null || h === null || l === null || c === null) { gaps += 1; continue; }
    const time: Time = barTime(b, intraday);
    const k = String(time);
    if (seen.has(k)) continue;
    seen.add(k);
    const up = c >= o;
    const soft = !b.is_final;
    if (soft) unfinished += 1;
    const col = up ? (soft ? colors.upSoft : colors.up) : (soft ? colors.downSoft : colors.down);
    candles.push({ time, open: o, high: h, low: l, close: c, color: col, borderColor: col, wickColor: col });
    if (b.volume_shares !== null) volumes.push({ time, value: b.volume_shares, color: col });
  }
  return { candles, volumes, gaps, unfinished };
}

function cssVar(name: string, fallback: string): string {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

export interface PriceChartProps {
  bars: BarWindow;
  showVolume: boolean;
  lines: ChartLine[];
  drawMode: boolean;
  onPickPrice?: (price: number) => void;
  themeVersion: number;
  /** Server indicator lines drawn on the price pane. */
  overlays?: DrawnSeries[];
  /** Server series of the selected sub-pane (own pane below price / volume). */
  subSeries?: DrawnSeries[];
}

export function PriceChart({ bars, showVolume, lines, drawMode, onPickPrice, themeVersion, overlays, subSeries }: PriceChartProps) {
  const host = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const volRef = useRef<ISeriesApi<'Histogram'> | null>(null);
  const priceLines = useRef<IPriceLine[]>([]);
  const extraRef = useRef<Array<ISeriesApi<'Line'> | ISeriesApi<'Histogram'>>>([]);
  const drawRef = useRef(drawMode);
  const pickRef = useRef(onPickPrice);
  drawRef.current = drawMode;
  pickRef.current = onPickPrice;

  // create once
  useEffect(() => {
    if (!host.current) return undefined;
    const chart = createChart(host.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: 'transparent' }, textColor: cssVar('--muted', '#888'), attributionLogo: true },
      grid: { vertLines: { color: cssVar('--grid', '#8883') }, horzLines: { color: cssVar('--grid', '#8883') } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, timeVisible: true, secondsVisible: false },
    });
    chartRef.current = chart;
    candleRef.current = chart.addSeries(CandlestickSeries, {});
    chart.subscribeClick((p) => {
      if (!drawRef.current || !p.point || !candleRef.current) return;
      const price = candleRef.current.coordinateToPrice(p.point.y);
      if (price !== null && Number.isFinite(price)) pickRef.current?.(price);
    });
    return () => { chart.remove(); chartRef.current = null; candleRef.current = null; volRef.current = null; priceLines.current = []; extraRef.current = []; };
  }, []);

  // data + theme
  useEffect(() => {
    const chart = chartRef.current;
    const candle = candleRef.current;
    if (!chart || !candle) return;
    const up = cssVar('--up', '#e5484d');
    const down = cssVar('--down', '#2fa36b');
    chart.applyOptions({
      layout: { textColor: cssVar('--muted', '#888') },
      grid: { vertLines: { color: cssVar('--grid', '#8883') }, horzLines: { color: cssVar('--grid', '#8883') } },
      timeScale: { timeVisible: isIntraday(bars.timeframe) },
    });
    const plotted = plotBars(bars, { up, down, upSoft: `${up}88`, downSoft: `${down}88` });
    candle.applyOptions({ upColor: up, downColor: down, borderUpColor: up, borderDownColor: down, wickUpColor: up, wickDownColor: down });
    candle.setData(plotted.candles);
    if (showVolume) {
      if (!volRef.current) {
        volRef.current = chart.addSeries(HistogramSeries, { priceFormat: { type: 'volume' }, priceLineVisible: false, lastValueVisible: false }, 1);
        const panes = chart.panes();
        panes[0]?.setStretchFactor(3);
        panes[1]?.setStretchFactor(1);
      }
      volRef.current.setData(plotted.volumes);
    } else if (volRef.current) {
      chart.removeSeries(volRef.current);
      volRef.current = null;
    }
    chart.timeScale().fitContent();
  }, [bars, showVolume, themeVersion]);

  // indicator overlays and sub-pane (after the data effect so the volume pane is settled)
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    for (const old of extraRef.current) chart.removeSeries(old);
    extraRef.current = [];
    const up = cssVar('--up', '#e5484d');
    const down = cssVar('--down', '#2fa36b');
    const add = (d: DrawnSeries, pane: number): void => {
      if (d.points.length === 0) return;
      if (d.kind === 'histogram') {
        const h = chart.addSeries(HistogramSeries, { priceLineVisible: false, lastValueVisible: false, title: d.label }, pane);
        h.setData(d.points.map((p) => ({ time: p.time, value: p.value, color: p.value >= 0 ? up : down })));
        extraRef.current.push(h);
        return;
      }
      const l = chart.addSeries(LineSeries, { color: d.color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, title: pane === 0 ? '' : d.label }, pane);
      l.setData(d.points);
      extraRef.current.push(l);
    };
    for (const o of overlays ?? []) add(o, 0);
    const sub = (subSeries ?? []).filter((s) => s.points.length > 0);
    if (sub.length > 0) {
      const pane = showVolume ? 2 : 1;
      for (const s of sub) add(s, pane);
      const panes = chart.panes();
      panes[0]?.setStretchFactor(3);
      panes[pane]?.setStretchFactor(1.5);
    }
  }, [overlays, subSeries, showVolume, bars, themeVersion]);

  // lines
  useEffect(() => {
    const candle = candleRef.current;
    if (!candle) return;
    for (const pl of priceLines.current) candle.removePriceLine(pl);
    priceLines.current = lines.map((l) => candle.createPriceLine({
      price: l.price,
      color: l.tone === 'plan' ? cssVar('--accent', '#2563eb') : l.tone === 'exit' ? cssVar('--up', '#e5484d') : l.tone === 'risk' ? cssVar('--down', '#2fa36b') : cssVar('--warn', '#d97706'),
      lineWidth: 1,
      lineStyle: l.dashed ? LineStyle.Dashed : LineStyle.Solid,
      axisLabelVisible: true,
      title: l.title,
    }));
  }, [lines, bars]);

  return <div ref={host} className={`chart-host ${drawMode ? 'drawing' : ''}`} role="img" aria-label="K线图" />;
}
