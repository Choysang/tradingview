import { useMemo, useState } from 'react';
import type { MacroDashboardSummary, SealPoint } from '../../core/contract';
import { hm, sessionMinute } from '../../core/format';
import { useWorkspace } from '../../core/store';
import { Panel, useSize } from '../../kit/primitives';

const BIN = 5;
const START = -15;
const END = 240;
const TICKS: [number, string][] = [[0, '09:30'], [30, '10:00'], [60, '10:30'], [90, '11:00'], [120, '11:30 / 13:00'], [150, '13:30'], [180, '14:00'], [210, '14:30'], [240, '15:00']];

interface Placed {
  point: SealPoint;
  x: number;
  y: number;
  r: number;
}

/**
 * 封板时钟: every limit-up placed on the session clock by its first seal.
 * Early clusters mean conviction; an afternoon drizzle means a weak tape.
 */
export default function SealClock({ overview }: { overview: MacroDashboardSummary }) {
  const [ref, { width }] = useSize<HTMLDivElement>();
  const [hover, setHover] = useState<Placed | null>(null);
  const points = overview.seal_timeline;
  const height = 168;
  const base = height - 26;
  const maxStreak = overview.max_limit_height;

  const placed = useMemo(() => {
    if (!width) return [] as Placed[];
    const x = (minute: number) => ((minute - START) / (END - START)) * (width - 16) + 8;
    const bins = new Map<number, number>();
    return points.map(point => {
      const minute = Math.max(START, Math.min(END, sessionMinute(point.first_seal_time)));
      const bin = Math.floor((minute - START) / BIN);
      const stack = bins.get(bin) ?? 0;
      bins.set(bin, stack + 1);
      const r = (point.limit_streak ?? 1) >= 3 ? 5.5 : 4.2;
      return { point, x: x(START + bin * BIN + BIN / 2), y: base - 6 - stack * 10.5, r };
    });
  }, [points, width, base]);

  const sealed = points.filter(point => point.is_limit_up);
  const early = sealed.filter(point => point.first_seal_time <= '09:35:00').length;
  const morning = sealed.filter(point => point.first_seal_time <= '10:00:00').length;
  const x = (minute: number) => ((minute - START) / (END - START)) * (width - 16) + 8;

  // Running count of sealed names across the session: how fast the tape filled up.
  const cumulative = useMemo(() => {
    if (!width || !sealed.length) return '';
    const minutes = sealed.map(point => Math.max(START, Math.min(END, sessionMinute(point.first_seal_time)))).sort((a, b) => a - b);
    const top = 14;
    const y = (count: number) => base - (count / minutes.length) * (base - top);
    let path = `M${x(START)},${base}`;
    minutes.forEach((minute, index) => { path += `H${x(minute).toFixed(1)}V${y(index + 1).toFixed(1)}`; });
    return `${path}H${x(END)}`;
  }, [sealed, width, base]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <Panel
      eyebrow="SEAL CLOCK"
      title="封板时钟"
      subtitle="按首次封板时间排布；越早越密，承接越强"
      actions={
        <div className="clock-stats">
          <span><b className="num up">{early}</b> 秒板/早封</span>
          <span><b className="num">{sealed.length ? Math.round((morning / sealed.length) * 100) : 0}%</b> 十点前</span>
        </div>
      }
    >
      <div className="seal-clock" ref={ref} style={{ height }}>
        {width > 0 && (
          <svg width={width} height={height} role="img" aria-label={`封板时钟：${sealed.length} 家封板，${points.length - sealed.length} 家开板`}>
            <rect className="clock-lunch" x={x(118)} y={8} width={x(122) - x(118)} height={base - 8} />
            <line className="chart-grid" x1={8} x2={width - 8} y1={base} y2={base} />
            {cumulative && (
              <>
                <path className="clock-area" d={`${cumulative}V${base}Z`} />
                <path className="clock-curve" d={cumulative} />
                <text className="chart-axis" x={width - 8} y={18} textAnchor="end">累计 {sealed.length} 家</text>
              </>
            )}
            {TICKS.map(([minute, label]) => (
              <g key={label}>
                <line className="chart-grid" x1={x(minute)} x2={x(minute)} y1={base} y2={base + 4} />
                <text className="chart-axis" x={x(minute)} y={height - 6} textAnchor={minute === 0 ? 'start' : minute === 240 ? 'end' : 'middle'}>{label}</text>
              </g>
            ))}
            {placed.map((item, index) => (
              <circle
                key={item.point.code}
                className={`seal-dot ${item.point.is_limit_up ? 'sealed' : 'broken'} ${item.point.limit_streak === maxStreak && maxStreak > 1 ? 'apex' : ''}`}
                cx={item.x}
                cy={Math.max(item.r + 2, item.y)}
                r={item.r}
                style={{ animationDelay: `${Math.min(index, 60) * 12}ms` }}
                onPointerEnter={() => setHover(item)}
                onPointerLeave={() => setHover(null)}
                onClick={() => useWorkspace.getState().openStock(item.point.code)}
              />
            ))}
          </svg>
        )}
        {hover && (
          <div className="chart-tip" style={{ left: Math.min(Math.max(0, hover.x + 10), width - 160), top: Math.max(0, hover.y - 46) }}>
            <b>{hover.point.name}</b>
            <div className="muted mono">
              {hm(hover.point.first_seal_time)} 首封 · {hover.point.limit_streak ? `${hover.point.limit_streak} 板` : '—'}
              {hover.point.is_limit_up ? '' : ` · 已开板${hover.point.open_board_count ? ` ${hover.point.open_board_count} 次` : ''}`}
            </div>
          </div>
        )}
      </div>
      {!points.length && <p className="muted">暂无封板记录（实时源未提供首封时间时此处为空）。</p>}
    </Panel>
  );
}
