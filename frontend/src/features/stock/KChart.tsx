/**
 * Daily K-line with overlays and one oscillator pane. Uses TradingView
 * Lightweight Charts (Apache-2.0); its attribution logo stays on as the
 * licence NOTICE requires.
 */
import { useEffect, useRef } from 'react';
import { CandlestickSeries, ColorType, createChart, HistogramSeries, LineSeries } from 'lightweight-charts';
import type { IChartApi, Time } from 'lightweight-charts';
import type { IndicatorSeries, OHLCBar } from '../../core/contract';
import { cssRgb } from '../../kit/charts';

export type Oscillator = 'macd' | 'kdj' | 'rsi';
export type Overlay = 'ma' | 'boll' | 'none';

const LINE_COLOURS: Record<string, string> = {
  ma5: '--text-0',
  ma10: '--gold',
  ma20: '--accent',
  ma60: '--cyan',
  boll_upper: '--text-3',
  boll_lower: '--text-3',
  macd_dif: '--text-0',
  macd_dea: '--gold',
  kdj_k: '--text-0',
  kdj_d: '--gold',
  kdj_j: '--accent',
  rsi6: '--gold',
  rsi14: '--accent',
};

interface KChartProps {
  bars: OHLCBar[];
  indicators: IndicatorSeries[];
  overlay: Overlay;
  oscillator: Oscillator;
  height?: number;
}

export default function KChart({ bars, indicators, overlay, oscillator, height = 420 }: KChartProps) {
  const host = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);

  useEffect(() => {
    const element = host.current;
    if (!element || !bars.length) return;
    const up = cssRgb('--up');
    const down = cssRgb('--down');
    const upVolume = cssRgb('--up', 0.32);
    const downVolume = cssRgb('--down', 0.3);
    const chart = createChart(element, {
      height,
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: cssRgb('--text-2'),
        fontFamily: 'Geist Mono, JetBrains Mono, monospace',
        fontSize: 10,
        attributionLogo: true,
        panes: { separatorColor: cssRgb('--line-2'), separatorHoverColor: cssRgb('--line-3') },
      },
      grid: { vertLines: { color: 'rgba(255, 255, 255, 0.035)' }, horzLines: { color: 'rgba(255, 255, 255, 0.035)' } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, rightOffset: 4, barSpacing: 7 },
      crosshair: { mode: 1 },
    });
    chartRef.current = chart;
    const time = (bar: OHLCBar) => bar.date as Time;

    const candles = chart.addSeries(CandlestickSeries, {
      upColor: up,
      downColor: down,
      borderUpColor: up,
      borderDownColor: down,
      wickUpColor: up,
      wickDownColor: down,
      priceLineVisible: false,
    });
    candles.setData(bars.map(bar => ({ time: time(bar), open: bar.open, high: bar.high, low: bar.low, close: bar.close })));

    const volume = chart.addSeries(HistogramSeries, { priceScaleId: 'volume', priceFormat: { type: 'volume' }, priceLineVisible: false, lastValueVisible: false });
    chart.priceScale('volume').applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
    volume.setData(bars.map(bar => ({ time: time(bar), value: bar.volume, color: bar.close >= bar.open ? upVolume : downVolume })));

    const line = (series: IndicatorSeries, pane: number) => {
      const added = chart.addSeries(LineSeries, {
        color: cssRgb(LINE_COLOURS[series.key] ?? '--text-2'),
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
        lineStyle: series.key.startsWith('boll') ? 2 : 0,
      }, pane);
      added.setData(series.values.flatMap((value, index) => (value == null || !bars[index] ? [] : [{ time: time(bars[index]), value }])));
    };

    const keys = overlay === 'ma' ? ['ma5', 'ma10', 'ma20', 'ma60'] : overlay === 'boll' ? ['ma20', 'boll_upper', 'boll_lower'] : [];
    indicators.filter(series => keys.includes(series.key)).forEach(series => line(series, 0));

    const paneSeries = indicators.filter(series => series.pane === oscillator);
    paneSeries.forEach(series => {
      if (series.style === 'histogram') {
        const hist = chart.addSeries(HistogramSeries, { priceLineVisible: false, lastValueVisible: false }, 1);
        hist.setData(series.values.flatMap((value, index) => (value == null || !bars[index] ? [] : [{ time: time(bars[index]), value, color: value >= 0 ? up : down }])));
      } else {
        line(series, 1);
      }
    });
    const panes = chart.panes();
    if (panes[1]) panes[1].setHeight(Math.round(height * 0.26));

    const visible = Math.min(bars.length, 120);
    chart.timeScale().setVisibleLogicalRange({ from: bars.length - visible, to: bars.length + 3 });
    return () => {
      chart.remove();
      chartRef.current = null;
    };
  }, [bars, indicators, overlay, oscillator, height]);

  return <div className="kchart" ref={host} style={{ height }} role="img" aria-label="日 K 线与技术指标" />;
}
