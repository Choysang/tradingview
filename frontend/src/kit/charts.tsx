/**
 * Small, dependency-free charts. Everything is drawn at measured pixel size so
 * text never stretches, and every chart exposes its values on hover.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import type { PointerEvent, ReactNode } from 'react';
import { useSize } from './primitives';

const colourCache = new Map<string, string>();

/** Resolve a CSS custom property (e.g. --up) to a colour canvas can paint. */
export function cssColour(token: string): string {
  if (!token.startsWith('--')) return token;
  const cached = colourCache.get(token);
  if (cached) return cached;
  const value = getComputedStyle(document.documentElement).getPropertyValue(token).trim() || '#888';
  colourCache.set(token, value);
  return value;
}

const rgbCache = new Map<string, string>();

/**
 * Same token as plain rgba(). Third-party canvases (the K-line library) do not
 * all parse OKLCH, so the browser rasterises one pixel and we read it back.
 */
export function cssRgb(token: string, alpha = 1): string {
  const key = `${token}/${alpha}`;
  const cached = rgbCache.get(key);
  if (cached) return cached;
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 1;
  const context = canvas.getContext('2d', { willReadFrequently: true });
  if (!context) return '#888888';
  context.fillStyle = cssColour(token);
  context.fillRect(0, 0, 1, 1);
  const [r, g, b] = context.getImageData(0, 0, 1, 1).data;
  const value = `rgba(${r}, ${g}, ${b}, ${alpha})`;
  rgbCache.set(key, value);
  return value;
}

interface SparkProps {
  values: number[];
  width?: number;
  height?: number;
  /** colour token; defaults to up/down by last value relative to first */
  colour?: string;
  baseline?: number | null;
  fill?: boolean;
  label?: string;
}

/** Canvas sparkline: cheap enough to draw hundreds of rows on every tick. */
export function Spark({ values, width = 84, height = 26, colour, baseline = 0, fill = true, label }: SparkProps) {
  const canvas = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const element = canvas.current;
    if (!element) return;
    const ratio = window.devicePixelRatio || 1;
    element.width = width * ratio;
    element.height = height * ratio;
    const context = element.getContext('2d');
    if (!context) return;
    context.scale(ratio, ratio);
    context.clearRect(0, 0, width, height);
    if (values.length < 2) return;
    const all = baseline == null ? values : [...values, baseline];
    const min = Math.min(...all);
    const max = Math.max(...all);
    const span = max - min || 1;
    const pad = 2;
    const x = (index: number) => (index / (values.length - 1)) * (width - pad * 2) + pad;
    const y = (value: number) => height - pad - ((value - min) / span) * (height - pad * 2);
    const last = values[values.length - 1];
    const stroke = cssColour(colour ?? (last > (baseline ?? values[0]) ? '--up' : last < (baseline ?? values[0]) ? '--down' : '--text-2'));
    if (baseline != null && baseline > min && baseline < max) {
      context.strokeStyle = cssColour('--line-2');
      context.setLineDash([2, 3]);
      context.beginPath();
      context.moveTo(0, y(baseline));
      context.lineTo(width, y(baseline));
      context.stroke();
      context.setLineDash([]);
    }
    context.beginPath();
    values.forEach((value, index) => (index ? context.lineTo(x(index), y(value)) : context.moveTo(x(index), y(value))));
    context.lineWidth = 1.4;
    context.lineJoin = 'round';
    context.strokeStyle = stroke;
    context.stroke();
    if (fill) {
      const gradient = context.createLinearGradient(0, 0, 0, height);
      gradient.addColorStop(0, withAlpha(stroke, 0.28));
      gradient.addColorStop(1, withAlpha(stroke, 0));
      context.lineTo(x(values.length - 1), height);
      context.lineTo(x(0), height);
      context.closePath();
      context.fillStyle = gradient;
      context.fill();
    }
    context.fillStyle = stroke;
    context.beginPath();
    context.arc(x(values.length - 1), y(last), 1.8, 0, Math.PI * 2);
    context.fill();
  }, [values, width, height, colour, baseline, fill]);
  return <canvas ref={canvas} style={{ width, height }} role="img" aria-label={label ?? '走势'} />;
}

/** Sparkline that fills its container's width (redrawn on resize, never stretched). */
export function FluidSpark(props: Omit<SparkProps, 'width'>) {
  const [ref, { width }] = useSize<HTMLDivElement>();
  return <div ref={ref} style={{ width: '100%', height: props.height ?? 26 }}>{width > 0 && <Spark {...props} width={width} />}</div>;
}

function withAlpha(colour: string, alpha: number) {
  if (colour.startsWith('oklch(')) {
    const inner = colour.slice(6, -1).split('/')[0].trim();
    return `oklch(${inner} / ${alpha})`;
  }
  return colour;
}

export interface LineSeries {
  key: string;
  label: string;
  values: (number | null)[];
  colour: string;
  area?: boolean;
  dashed?: boolean;
  width?: number;
}

interface LineChartProps {
  series: LineSeries[];
  labels: string[];
  height?: number;
  format?: (value: number) => string;
  zero?: number | null;
  /** show min / max / last on the right axis */
  axis?: boolean;
  footer?: ReactNode;
  /** vertical reference lines, e.g. the in-sample / out-of-sample split */
  markers?: { index: number; label: string }[];
}

/** Multi-series line/area chart with a crosshair readout. */
export function LineChart({ series, labels, height = 200, format = value => value.toFixed(2), zero = null, axis = true, footer, markers = [] }: LineChartProps) {
  const [ref, { width }] = useSize<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const gradientId = useMemo(() => `g${Math.random().toString(36).slice(2, 8)}`, []);
  const plotRight = axis ? 52 : 4;
  const count = labels.length;

  // The scale is padded so lines never touch the frame; the axis labels show the
  // data's own extremes, never a padded bound (a NAV must not read as negative).
  const { min, max, low, high } = useMemo(() => {
    const all = series.flatMap(item => item.values.filter((value): value is number => value != null));
    if (zero != null) all.push(zero);
    if (!all.length) return { min: 0, max: 1, low: 0, high: 1 };
    const low = Math.min(...all);
    const high = Math.max(...all);
    const pad = (high - low) * 0.08 || Math.abs(high) * 0.05 || 1;
    return { min: low - pad, max: high + pad, low, high };
  }, [series, zero]);

  const innerWidth = Math.max(10, width - plotRight);
  const top = 8;
  const bottom = height - 22;
  const x = (index: number) => (count <= 1 ? 0 : (index / (count - 1)) * innerWidth);
  const y = (value: number) => bottom - ((value - min) / (max - min || 1)) * (bottom - top);

  const paths = series.map(item => {
    let line = '';
    let started = false;
    item.values.forEach((value, index) => {
      if (value == null) { started = false; return; }
      line += `${started ? 'L' : 'M'}${x(index).toFixed(1)},${y(value).toFixed(1)}`;
      started = true;
    });
    const firstIndex = item.values.findIndex(value => value != null);
    const lastIndex = item.values.length - 1 - [...item.values].reverse().findIndex(value => value != null);
    const area = item.area && firstIndex >= 0
      ? `${line}L${x(lastIndex).toFixed(1)},${bottom}L${x(firstIndex).toFixed(1)},${bottom}Z`
      : '';
    return { item, line, area };
  });

  const onMove = (event: PointerEvent<SVGRectElement>) => {
    const bounds = event.currentTarget.getBoundingClientRect();
    const ratio = (event.clientX - bounds.left) / bounds.width;
    setHover(Math.max(0, Math.min(count - 1, Math.round(ratio * (count - 1)))));
  };

  const ticks = count > 1 ? [...new Set([0, Math.floor((count - 1) / 2), count - 1])] : [];
  const primary = series[0];
  const lastValue = primary ? [...primary.values].reverse().find((value): value is number => value != null) : undefined;

  return (
    <div className="chart" ref={ref} style={{ height }}>
      {width > 0 && count > 0 && (
        <svg width={width} height={height} role="img" aria-label={series.map(item => item.label).join('、')}>
          <defs>
            {series.map((item, index) => (
              <linearGradient key={item.key} id={`${gradientId}-${index}`} x1="0" x2="0" y1="0" y2="1">
                <stop offset="0%" stopColor={`var(${item.colour})`} stopOpacity="0.32" />
                <stop offset="100%" stopColor={`var(${item.colour})`} stopOpacity="0" />
              </linearGradient>
            ))}
          </defs>
          {[0.25, 0.5, 0.75].map(step => (
            <line key={step} className="chart-grid" x1={0} x2={innerWidth} y1={top + (bottom - top) * step} y2={top + (bottom - top) * step} />
          ))}
          {zero != null && zero > min && zero < max && <line className="chart-zero" x1={0} x2={innerWidth} y1={y(zero)} y2={y(zero)} />}
          {markers.filter(marker => marker.index >= 0 && marker.index < count).map(marker => (
            <g key={marker.label} className="chart-marker">
              <line x1={x(marker.index)} x2={x(marker.index)} y1={top} y2={bottom} />
              <text x={x(marker.index) + 4} y={top + 9}>{marker.label}</text>
            </g>
          ))}
          {paths.map(({ item, line, area }, index) => (
            <g key={item.key}>
              {area && <path d={area} fill={`url(#${gradientId}-${index})`} />}
              <path d={line} fill="none" stroke={`var(${item.colour})`} strokeWidth={item.width ?? 1.6} strokeLinejoin="round" strokeLinecap="round" strokeDasharray={item.dashed ? '4 4' : undefined} />
            </g>
          ))}
          {axis && (
            <g className="chart-axis">
              <text x={width} y={Math.max(top + 8, y(high) + 3)} textAnchor="end">{format(high)}</text>
              <text x={width} y={Math.min(bottom, y(low) + 3)} textAnchor="end">{format(low)}</text>
              {lastValue != null && (
                <text x={width} y={Math.min(bottom - 12, Math.max(top + 20, y(lastValue) + 3))} textAnchor="end" style={{ fill: `var(${primary.colour})` }}>{format(lastValue)}</text>
              )}
            </g>
          )}
          <g className="chart-axis">
            {ticks.map(index => (
              <text key={index} x={x(index)} y={height - 4} textAnchor={index === 0 ? 'start' : index === count - 1 ? 'end' : 'middle'}>{labels[index]}</text>
            ))}
          </g>
          {hover != null && (
            <g>
              <line className="chart-cross" x1={x(hover)} x2={x(hover)} y1={top} y2={bottom} />
              {series.map(item => item.values[hover] != null && (
                <circle key={item.key} cx={x(hover)} cy={y(item.values[hover] as number)} r={3.2} fill={`var(${item.colour})`} stroke="var(--ink-1)" strokeWidth={2} />
              ))}
            </g>
          )}
          <rect x={0} y={0} width={innerWidth} height={height} fill="transparent" onPointerMove={onMove} onPointerLeave={() => setHover(null)} />
        </svg>
      )}
      {hover != null && width > 0 && (
        <div className="chart-tip" style={{ left: Math.min(Math.max(0, x(hover) + 12), width - 150), top: 4 }}>
          <div className="faint mono" style={{ marginBottom: 2 }}>{labels[hover]}</div>
          {series.map(item => (
            <div key={item.key}>
              <span>{item.label}</span>
              <b style={{ color: `var(${item.colour})` }}>{item.values[hover] == null ? '—' : format(item.values[hover] as number)}</b>
            </div>
          ))}
        </div>
      )}
      {footer}
    </div>
  );
}

interface BarsProps {
  values: (number | null)[];
  labels: string[];
  height?: number;
  format?: (value: number) => string;
  /** colour bars by sign (A-share: positive red) */
  diverging?: boolean;
  colour?: string;
  showValues?: boolean;
  highlight?: number | null;
}

/** Vertical bars around zero, with hover readout. */
export function Bars({ values, labels, height = 160, format = value => value.toFixed(2), diverging = true, colour = '--accent', showValues = false, highlight = null }: BarsProps) {
  const [ref, { width }] = useSize<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const known = values.filter((value): value is number => value != null);
  const max = Math.max(0, ...known);
  const min = Math.min(0, ...known);
  const top = showValues ? 16 : 6;
  const bottom = height - 20;
  const span = max - min || 1;
  const y = (value: number) => top + ((max - value) / span) * (bottom - top);
  const slot = values.length ? width / values.length : 0;
  const barWidth = Math.max(1, Math.min(28, slot * 0.64));
  const labelEvery = Math.max(1, Math.ceil(values.length / Math.max(1, Math.floor(width / 44))));

  return (
    <div className="chart" ref={ref} style={{ height }}>
      {width > 0 && (
        <svg width={width} height={height} role="img" aria-label="柱状图">
          <line className="chart-zero" x1={0} x2={width} y1={y(0)} y2={y(0)} />
          {values.map((value, index) => {
            if (value == null) return null;
            const fill = diverging ? (value >= 0 ? 'var(--up)' : 'var(--down)') : `var(${colour})`;
            const yTop = Math.min(y(value), y(0));
            const barHeight = Math.max(1, Math.abs(y(value) - y(0)));
            const active = hover === index || highlight === index;
            return (
              <g key={index}>
                <rect
                  x={index * slot + (slot - barWidth) / 2}
                  y={yTop}
                  width={barWidth}
                  height={barHeight}
                  rx={Math.min(3, barWidth / 3)}
                  fill={fill}
                  opacity={hover == null || active ? 0.92 : 0.45}
                  style={{ transition: 'opacity 160ms, y 360ms var(--ease-out), height 360ms var(--ease-out)' }}
                />
                {showValues && (
                  <text className="chart-axis" x={index * slot + slot / 2} y={value >= 0 ? yTop - 4 : yTop + barHeight + 11} textAnchor="middle">{format(value)}</text>
                )}
              </g>
            );
          })}
          <g className="chart-axis">
            {labels.map((label, index) => index % labelEvery === 0 && (
              <text key={index} x={index * slot + slot / 2} y={height - 4} textAnchor="middle">{label}</text>
            ))}
          </g>
          {values.map((_, index) => (
            <rect key={index} x={index * slot} y={0} width={slot} height={height} fill="transparent" onPointerEnter={() => setHover(index)} onPointerLeave={() => setHover(null)} />
          ))}
        </svg>
      )}
      {hover != null && values[hover] != null && (
        <div className="chart-tip" style={{ left: Math.min(Math.max(0, hover * slot), width - 120) }}>
          <span>{labels[hover]}</span>
          <b>{format(values[hover] as number)}</b>
        </div>
      )}
    </div>
  );
}

interface RingProps {
  value: number | null | undefined;
  size?: number;
  stroke?: number;
  colour?: string;
  label?: string;
  children?: ReactNode;
}

/** 0–100 score as an arc; the number sits in the middle. */
export function ScoreRing({ value, size = 56, stroke = 4, colour = '--accent', label, children }: RingProps) {
  const radius = (size - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  const known = value != null && Number.isFinite(value);
  const share = known ? Math.max(0, Math.min(100, value)) / 100 : 0;
  return (
    <span className="ring" style={{ width: size, height: size }} role="img" aria-label={label ?? (known ? `${Math.round(value)} 分` : '无评分')}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden="true">
        <circle cx={size / 2} cy={size / 2} r={radius} fill="none" stroke="var(--line-2)" strokeWidth={stroke} />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke={`var(${colour})`}
          strokeWidth={stroke}
          strokeLinecap="round"
          strokeDasharray={`${circumference * share} ${circumference}`}
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
          style={{ transition: 'stroke-dasharray var(--t-4) var(--ease-out)' }}
        />
      </svg>
      <span className="ring-value">{children ?? (known ? Math.round(value) : '—')}</span>
    </span>
  );
}

export interface FanPoint {
  day: number;
  p10: number;
  p25: number;
  p50: number;
  p75: number;
  p90: number;
}

/** Forward-return distribution: outer band p10–p90, inner band p25–p75, median line. */
export function FanChart({ path, height = 200, format = value => `${value.toFixed(1)}%` }: { path: FanPoint[]; height?: number; format?: (value: number) => string }) {
  const [ref, { width }] = useSize<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const points = useMemo(() => [{ day: 0, p10: 0, p25: 0, p50: 0, p75: 0, p90: 0 }, ...path], [path]);
  const right = 56;
  const top = 10;
  const bottom = height - 22;
  const low = Math.min(0, ...points.map(point => point.p10));
  const high = Math.max(0, ...points.map(point => point.p90));
  const pad = (high - low) * 0.08 || 1;
  const min = low - pad;
  const max = high + pad;
  const last = points[points.length - 1];
  const inner = Math.max(10, width - right);
  const x = (day: number) => (last.day ? (day / last.day) * inner : 0);
  const y = (value: number) => bottom - ((value - min) / (max - min || 1)) * (bottom - top);
  const band = (upper: keyof FanPoint, lower: keyof FanPoint) =>
    points.map((point, index) => `${index ? 'L' : 'M'}${x(point.day).toFixed(1)},${y(point[upper]).toFixed(1)}`).join('') +
    [...points].reverse().map(point => `L${x(point.day).toFixed(1)},${y(point[lower]).toFixed(1)}`).join('') + 'Z';
  const median = points.map((point, index) => `${index ? 'L' : 'M'}${x(point.day).toFixed(1)},${y(point.p50).toFixed(1)}`).join('');
  const onMove = (event: PointerEvent<SVGRectElement>) => {
    const bounds = event.currentTarget.getBoundingClientRect();
    const day = Math.round(((event.clientX - bounds.left) / bounds.width) * last.day);
    setHover(Math.max(1, Math.min(last.day, day)));
  };
  const hovered = hover == null ? null : points.find(point => point.day === hover) ?? null;
  const ticks = [...new Set([0, Math.round(last.day / 2), last.day])];
  const tone = last.p50 >= 0 ? '--up' : '--down';

  return (
    <div className="chart fan" ref={ref} style={{ height }}>
      {width > 0 && points.length > 1 && (
        <svg width={width} height={height} role="img" aria-label="相似情景未来收益分布">
          <line className="chart-zero" x1={0} x2={inner} y1={y(0)} y2={y(0)} />
          <path d={band('p90', 'p10')} fill={`var(${tone})`} opacity={0.12} />
          <path d={band('p75', 'p25')} fill={`var(${tone})`} opacity={0.22} />
          <path d={median} fill="none" stroke={`var(${tone})`} strokeWidth={1.8} strokeLinejoin="round" />
          <g className="chart-axis">
            <text x={width} y={Math.max(top + 8, y(last.p90) + 3)} textAnchor="end">P90 {format(last.p90)}</text>
            <text x={width} y={Math.min(bottom - 14, Math.max(top + 20, y(last.p50) + 3))} textAnchor="end" style={{ fill: `var(${tone})` }}>P50 {format(last.p50)}</text>
            <text x={width} y={Math.min(bottom, y(last.p10) + 3)} textAnchor="end">P10 {format(last.p10)}</text>
            {ticks.map(day => (
              <text key={day} x={x(day)} y={height - 4} textAnchor={day === 0 ? 'start' : day === last.day ? 'end' : 'middle'}>{day === 0 ? '今日' : `T+${day}`}</text>
            ))}
          </g>
          {hovered && <line className="chart-cross" x1={x(hovered.day)} x2={x(hovered.day)} y1={top} y2={bottom} />}
          <rect x={0} y={0} width={inner} height={height} fill="transparent" onPointerMove={onMove} onPointerLeave={() => setHover(null)} />
        </svg>
      )}
      {hovered && width > 0 && (
        <div className="chart-tip" style={{ left: Math.min(Math.max(0, x(hovered.day) + 12), width - 150), top: 4 }}>
          <div className="faint mono" style={{ marginBottom: 2 }}>T+{hovered.day}</div>
          <div><span>P90</span><b>{format(hovered.p90)}</b></div>
          <div><span>P75</span><b>{format(hovered.p75)}</b></div>
          <div><span>中位</span><b style={{ color: `var(${tone})` }}>{format(hovered.p50)}</b></div>
          <div><span>P25</span><b>{format(hovered.p25)}</b></div>
          <div><span>P10</span><b>{format(hovered.p10)}</b></div>
        </div>
      )}
    </div>
  );
}
