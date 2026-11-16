/** Equity curve (net asset value) with the drawdown below it, drawn as inline SVG from server-computed points. A missing NAV breaks the line. */
import type { EquityPoint } from './api';
import { pctText } from './format';

const W = 640;
const H_NAV = 150;
const H_DD = 60;
const PAD = 6;

function path(points: Array<{ x: number; y: number | null }>): string {
  let d = '';
  let pen = false;
  for (const p of points) {
    if (p.y === null) { pen = false; continue; }
    d += `${pen ? 'L' : 'M'}${p.x.toFixed(1)} ${p.y.toFixed(1)} `;
    pen = true;
  }
  return d.trim();
}

export function EquityChart({ points, initial }: { points: EquityPoint[]; initial: string }) {
  const navs = points.map((p) => (p.nav === null ? null : Number(p.nav)));
  const known = navs.filter((n): n is number => n !== null);
  if (known.length === 0) return <p className="muted small">没有可画的净值点。</p>;
  const lo = Math.min(...known, Number(initial));
  const hi = Math.max(...known, Number(initial));
  const span = hi - lo || 1;
  const x = (i: number): number => PAD + (points.length <= 1 ? 0 : (i / (points.length - 1)) * (W - 2 * PAD));
  const yNav = (v: number): number => PAD + (1 - (v - lo) / span) * (H_NAV - 2 * PAD);
  const dds = points.map((p) => (p.drawdown === null ? null : Number(p.drawdown)));
  const worst = Math.min(0, ...dds.filter((v): v is number => v !== null));
  const yDd = (v: number): number => PAD + (worst === 0 ? 0 : (v / worst) * (H_DD - 2 * PAD));
  const navPath = path(navs.map((n, i) => ({ x: x(i), y: n === null ? null : yNav(n) })));
  const ddPath = path(dds.map((v, i) => ({ x: x(i), y: v === null ? null : yDd(v) })));
  const first = points[0]?.d ?? '';
  const last = points[points.length - 1]?.d ?? '';
  return (
    <figure className="equity" aria-label="净值曲线与回撤">
      <svg viewBox={`0 0 ${W} ${H_NAV + H_DD + 14}`} role="img" aria-label={`净值 ${first} 至 ${last}，最大回撤 ${pctText(String(worst))}`} preserveAspectRatio="none">
        <line x1={PAD} x2={W - PAD} y1={yNav(Number(initial))} y2={yNav(Number(initial))} className="eq-base" />
        <path d={navPath} className="eq-nav" fill="none" />
        <g transform={`translate(0 ${H_NAV + 14})`}>
          <line x1={PAD} x2={W - PAD} y1={PAD} y2={PAD} className="eq-base" />
          <path d={ddPath} className="eq-dd" fill="none" />
        </g>
      </svg>
      <figcaption className="muted small">
        <span>{first}</span> — <span>{last}</span> · 上图：净值（虚线为期初资金）；下图：相对历史高点的回撤，最深 {pctText(String(worst))}
      </figcaption>
    </figure>
  );
}
