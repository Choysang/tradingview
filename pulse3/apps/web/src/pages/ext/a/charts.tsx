/**
 * Small SVG charts for the market pages (no chart library: they only draw numbers the server already computed).
 * Colours follow the app convention: red = up, green = down; unknown values are drawn as gaps, never as zero.
 */
import { useMemo, useState } from 'react';
import type { RegimeDim, RegimePoint } from './types';

export const num = (v: string | number | null | undefined): number | null => (v == null || v === '' ? null : Number(v));

export function pctText(v: string | null | undefined, digits = 2, sign = true): string {
  const n = num(v);
  if (n == null || Number.isNaN(n)) return '—';
  return `${sign && n > 0 ? '+' : ''}${n.toFixed(digits)}%`;
}

export function Sparkline({ values, height = 56, label }: { values: Array<number | null>; height?: number; label: string }) {
  const w = 320;
  const pts = values.map((v, i) => [i, v] as const).filter((p): p is readonly [number, number] => p[1] != null);
  if (pts.length < 2) return <p className="muted small">历史曲线样本不足。</p>;
  const lo = Math.min(...pts.map((p) => p[1]));
  const hi = Math.max(...pts.map((p) => p[1]));
  const x = (i: number) => (i / Math.max(values.length - 1, 1)) * (w - 4) + 2;
  const y = (v: number) => height - 4 - ((v - lo) / Math.max(hi - lo, 1e-9)) * (height - 8);
  let d = '';
  let open = false;
  values.forEach((v, i) => {
    if (v == null) { open = false; return; }
    d += `${open ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)} `;
    open = true;
  });
  return (
    <svg className="ea-spark" viewBox={`0 0 ${w} ${height}`} role="img" aria-label={label} preserveAspectRatio="none">
      <path d={d} fill="none" stroke="currentColor" strokeWidth="1.6" />
    </svg>
  );
}

export function Gauge({ score, edges, words }: { score: number | null; edges: number[]; words: string[] }) {
  return (
    <div className="ea-gauge" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={score ?? undefined} aria-label="情绪温度（未校准）">
      <div className="ea-gauge-track">
        {edges.slice(0, -1).map((e) => <i key={e} style={{ left: `${e}%` }} />)}
        {score != null && <span className="ea-gauge-marker" style={{ left: `${Math.max(0, Math.min(100, score))}%` }} />}
      </div>
      <div className="ea-gauge-labels">
        {words.map((w, i) => {
          const lo = i === 0 ? 0 : (edges[i - 1] ?? 0);
          const hi = Math.min(edges[i] ?? 100, 100);
          return <span key={w} style={{ left: `${(lo + hi) / 2}%` }}>{w}</span>;
        })}
      </div>
    </div>
  );
}

export function Bars({ bins, label }: { bins: Array<{ label: string; count: number }>; label: string }) {
  const max = Math.max(...bins.map((b) => b.count), 1);
  const mid = 5;
  return (
    <div className="ea-bars" role="img" aria-label={label}>
      {bins.map((b, i) => (
        <div key={b.label} className="ea-bar-col" title={`${b.label}%：${b.count} 家`}>
          <span className="ea-bar-n mono">{b.count}</span>
          <div className={`ea-bar ${i < mid ? 'down' : i > mid ? 'up' : 'flat'}`} style={{ height: `${(b.count / max) * 100}%` }} />
          <span className="ea-bar-l small">{b.label}</span>
        </div>
      ))}
    </div>
  );
}

const DIMS: Array<{ key: RegimeDim; title: string; colour: Record<string, string> }> = [
  { key: 'trend', title: '趋势', colour: { bull: 'var(--up)', bear: 'var(--down)', range: 'var(--muted)' } },
  { key: 'liquidity', title: '流动性', colour: { expanding: 'var(--up)', contracting: 'var(--down)', steady: 'var(--muted)' } },
  { key: 'sentiment', title: '情绪', colour: { climax: 'var(--up)', repair: 'var(--warn)', ebb: 'var(--muted)', freezing: 'var(--down)' } },
  { key: 'style', title: '风格', colour: { small: 'var(--accent)', large: 'var(--warn)', balanced: 'var(--muted)', unknown: 'var(--border)' } },
];

/** Index line plus four label ribbons sharing one crosshair; a ribbon cell is faded when its `basis` is not ok. */
export function RegimeTimeline({ points, labels }: { points: RegimePoint[]; labels: Record<string, Record<string, string>> }) {
  const [hover, setHover] = useState<number | null>(null);
  const w = 900;
  const lineH = 150;
  const ribbon = 16;
  const h = lineH + DIMS.length * (ribbon + 4) + 8;
  const closes = points.map((p) => num(p.index_close));
  const shown = useMemo(() => {
    const vals = closes.filter((v): v is number => v != null);
    return { lo: Math.min(...vals), hi: Math.max(...vals) };
  }, [closes]);
  if (points.length < 2) return <p className="muted">样本不足，无法绘制时间线。</p>;
  const x = (i: number) => (i / (points.length - 1)) * (w - 8) + 4;
  const y = (v: number) => lineH - 6 - ((v - shown.lo) / Math.max(shown.hi - shown.lo, 1e-9)) * (lineH - 16);
  let d = '';
  closes.forEach((v, i) => { if (v != null) d += `${d ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)} `; });
  const cell = Math.max((w - 8) / points.length, 1);
  const hp = hover == null ? null : points[hover];
  return (
    <div className="ea-regime">
      <svg viewBox={`0 0 ${w} ${h}`} role="img" aria-label="市场周期时间线" onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const r = e.currentTarget.getBoundingClientRect();
          setHover(Math.max(0, Math.min(points.length - 1, Math.round(((e.clientX - r.left) / r.width * w - 4) / (w - 8) * (points.length - 1)))));
        }}>
        <path d={d} fill="none" stroke="var(--text)" strokeWidth="1.5" />
        {DIMS.map((dim, r) => (
          <g key={dim.key} transform={`translate(0 ${lineH + r * (ribbon + 4)})`}>
            {points.map((p, i) => (
              <rect key={p.date} x={x(i) - cell / 2} y={0} width={cell + 0.5} height={ribbon} fill={dim.colour[p[dim.key]] ?? 'var(--border)'}
                opacity={p.basis[dim.key] === 'ok' ? 0.95 : 0.3} />
            ))}
            <text x={6} y={ribbon - 4} fontSize="10" fill="var(--bg)">{dim.title}</text>
          </g>
        ))}
        {hover != null && <line x1={x(hover)} x2={x(hover)} y1={0} y2={h} stroke="var(--accent)" strokeDasharray="3 3" />}
      </svg>
      <div className="ea-regime-readout small" aria-live="polite">
        {hp ? (
          <>
            <b>{hp.date}</b> 指数 <span className="mono">{hp.index_close ?? '—'}</span>
            {DIMS.map((dim) => (
              <span key={dim.key}> · {dim.title}：{labels[dim.key]?.[hp[dim.key]] ?? hp[dim.key]}{hp.basis[dim.key] === 'ok' ? '' : '（默认值）'}</span>
            ))}
          </>
        ) : <span className="muted">移动光标查看某日的四个维度；浅色格子表示历史样本不足，标签只是默认值。</span>}
      </div>
    </div>
  );
}
