import { useMemo, useState } from 'react';
import type { PointerEvent } from 'react';
import { navigate } from '../../app/router';
import { api } from '../../core/api';
import type { PlaybookSummary, RegimePoint, RegimeState } from '../../core/contract';
import { num, pct } from '../../core/format';
import { useLive } from '../../core/live';
import { useWorkspace } from '../../core/store';
import { ErrorNote, Panel, Seg, Skeleton, useSize } from '../../kit/primitives';
import { PageHead } from '../shell/Shell';
import { DIMENSIONS, HORIZON, REGIME_STATES, stateMeta, VERDICT } from './labels';
import type { Dimension } from './labels';
import { HistoryChip, RegimeBanner } from './RegimeBanner';
import './regime.css';

const WINDOWS = [{ value: '250', label: '1 年' }, { value: '500', label: '2 年' }, { value: '750', label: '3 年' }, { value: '1500', label: '6 年' }];

/** Index line on top, one ribbon per regime dimension below, one shared crosshair. */
function Timeline({ points, name }: { points: RegimePoint[]; name: string }) {
  const [ref, { width }] = useSize<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const lineHeight = 170;
  const ribbon = 14;
  const gap = 6;
  const labelWidth = 34;
  const height = lineHeight + DIMENSIONS.length * (ribbon + gap) + 18;
  const inner = Math.max(10, width - labelWidth - 52);
  const count = points.length;
  const x = (index: number) => labelWidth + (count <= 1 ? 0 : (index / (count - 1)) * inner);
  const closes = points.map(point => point.index_close).filter((value): value is number => value != null);
  const min = Math.min(...closes);
  const max = Math.max(...closes);
  const y = (value: number) => 8 + (1 - (value - min) / (max - min || 1)) * (lineHeight - 20);

  const line = useMemo(() => {
    let path = '';
    let started = false;
    points.forEach((point, index) => {
      if (point.index_close == null) { started = false; return; }
      path += `${started ? 'L' : 'M'}${x(index).toFixed(1)},${y(point.index_close).toFixed(1)}`;
      started = true;
    });
    return path;
  }, [points, width]); // eslint-disable-line react-hooks/exhaustive-deps

  // Collapse consecutive equal states into runs: a few dozen rects instead of one per day.
  const runs = useMemo(() => DIMENSIONS.map(({ key }) => {
    const out: { start: number; end: number; state: string }[] = [];
    points.forEach((point, index) => {
      const state = point[key];
      const last = out[out.length - 1];
      if (last && last.state === state) last.end = index;
      else out.push({ start: index, end: index, state });
    });
    return { key, runs: out };
  }), [points]);

  const onMove = (event: PointerEvent<SVGRectElement>) => {
    const bounds = event.currentTarget.getBoundingClientRect();
    const ratio = (event.clientX - bounds.left) / bounds.width;
    setHover(Math.max(0, Math.min(count - 1, Math.round(ratio * (count - 1)))));
  };
  const step = count > 1 ? inner / (count - 1) : inner;
  const point = hover == null ? null : points[hover];
  const trendRuns = runs[0].runs;

  return (
    <div className="timeline chart" ref={ref} style={{ height }}>
      {width > 0 && count > 1 && (
        <svg width={width} height={height} role="img" aria-label={`${name}与市场周期`}>
          {trendRuns.map(run => (
            <rect key={`bg-${run.start}`} x={x(run.start) - step / 2} y={0} width={Math.max(1, x(run.end) - x(run.start) + step)} height={lineHeight} fill={`var(${stateMeta('trend', run.state).colour})`} opacity={run.state === 'range' ? 0.03 : 0.07} />
          ))}
          <path d={line} fill="none" stroke="var(--text-0)" strokeWidth={1.5} strokeLinejoin="round" />
          <g className="chart-axis">
            <text x={width} y={16} textAnchor="end">{num(max, 0)}</text>
            <text x={width} y={lineHeight - 12} textAnchor="end">{num(min, 0)}</text>
          </g>
          {runs.map(({ key, runs: segments }, row) => {
            const top = lineHeight + row * (ribbon + gap);
            return (
              <g key={key}>
                <text className="timeline-label" x={0} y={top + ribbon - 3}>{DIMENSIONS[row].label}</text>
                {segments.map(run => (
                  <rect key={run.start} x={x(run.start) - step / 2} y={top} width={Math.max(1, x(run.end) - x(run.start) + step)} height={ribbon} fill={`var(${stateMeta(key, run.state).colour})`} opacity={0.78} />
                ))}
              </g>
            );
          })}
          <g className="chart-axis">
            {[0, Math.floor((count - 1) / 2), count - 1].map(index => (
              <text key={index} x={x(index)} y={height - 3} textAnchor={index === 0 ? 'start' : index === count - 1 ? 'end' : 'middle'}>{points[index].date}</text>
            ))}
          </g>
          {point && (
            <g>
              <line className="chart-cross" x1={x(hover!)} x2={x(hover!)} y1={0} y2={height - 16} />
              {point.index_close != null && <circle cx={x(hover!)} cy={y(point.index_close)} r={3.2} fill="var(--text-0)" stroke="var(--ink-1)" strokeWidth={2} />}
            </g>
          )}
          <rect x={labelWidth} y={0} width={inner} height={height} fill="transparent" onPointerMove={onMove} onPointerLeave={() => setHover(null)} />
        </svg>
      )}
      {point && width > 0 && (
        <div className="chart-tip timeline-tip" style={{ left: Math.min(Math.max(0, x(hover!) + 12), width - 200), top: 4 }}>
          <div className="faint mono">{point.date}</div>
          <div><span>{name}</span><b>{num(point.index_close, 2)}</b></div>
          <div className="tip-states">
            {DIMENSIONS.map(({ key, label }) => {
              const meta = stateMeta(key, point[key]);
              return [<span key={`${key}-l`}>{label}</span>, <b key={`${key}-v`} style={{ color: `var(${meta.colour})` }}>{meta.label}</b>];
            })}
            <span>站上 MA20</span><b>{point.breadth_ma20 == null ? '—' : `${num(point.breadth_ma20, 0)}%`}</b>
            <span>成交比</span><b>{num(point.amount_ratio, 2)}</b>
            <span>涨停/炸板</span><b>{point.limit_up_count ?? '—'} / {point.broken_rate == null ? '—' : `${num(point.broken_rate, 0)}%`}</b>
            <span>最高板</span><b>{point.max_height ?? '—'}</b>
            <span>涨停溢价</span><b>{pct(point.premium, 2)}</b>
          </div>
        </div>
      )}
    </div>
  );
}

function Legend() {
  return (
    <div className="timeline-legend">
      {DIMENSIONS.map(({ key, label }) => (
        <span key={key}>
          <strong>{label}</strong>
          {Object.entries(REGIME_STATES[key]).map(([state, meta]) => (
            <span key={state} style={{ '--c': `var(${meta.colour})` } as React.CSSProperties}><i />{meta.label}</span>
          ))}
        </span>
      ))}
    </div>
  );
}

/** Playbooks × regime buckets: average return per trade when entering in that regime. */
function FitMatrix({ playbooks, regime }: { playbooks: PlaybookSummary[]; regime: RegimeState }) {
  const columns = DIMENSIONS.flatMap(({ key }) => Object.entries(REGIME_STATES[key]).map(([state, meta]) => ({ dimension: key as Dimension, state, label: meta.label })));
  const rows = playbooks.filter(item => item.by_regime.length > 0).sort((a, b) => (b.fitness ?? -1) - (a.fitness ?? -1));
  const extreme = Math.max(0.5, ...rows.flatMap(item => item.by_regime.map(bucket => Math.abs(bucket.stats.avg_return ?? 0))));
  const cell = (value: number | null | undefined, trades: number) => {
    if (value == null || trades === 0) return { background: 'var(--ink-2)' };
    const strength = Math.min(1, Math.abs(value) / extreme);
    const colour = value >= 0 ? '--up' : '--down';
    const alpha = (trades < 10 ? 0.35 : 1) * (0.1 + strength * 0.62);
    return { background: `color-mix(in oklab, var(${colour}) ${Math.round(alpha * 100)}%, var(--ink-2))` };
  };
  if (!rows.length) return <p className="faint">战法回测尚未完成。</p>;
  return (
    <div className="fit-matrix">
      <table>
        <thead>
          <tr>
            <th />
            {DIMENSIONS.map(({ key, label }) => <th key={key} colSpan={Object.keys(REGIME_STATES[key]).length}>{label}</th>)}
          </tr>
          <tr>
            <th />
            {columns.map(column => <th key={`${column.dimension}-${column.state}`} className={regime[column.dimension] === column.state ? 'is-now' : ''}>{column.label}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.map(item => (
            <tr key={item.spec.id}>
              <th scope="row">
                <button onClick={() => { useWorkspace.getState().selectPlaybook(item.spec.id); navigate('playbooks'); }}>
                  {item.spec.name}
                  <small>{HORIZON[item.spec.horizon]} · {VERDICT[item.verdict].label}{item.fitness != null ? ` · ${item.fitness.toFixed(0)}` : ''}</small>
                </button>
              </th>
              {columns.map(column => {
                const bucket = item.by_regime.find(entry => entry.dimension === column.dimension && entry.regime === column.state);
                const trades = bucket?.stats.trades ?? 0;
                const value = bucket?.stats.avg_return;
                const now = regime[column.dimension] === column.state;
                return (
                  <td
                    key={`${column.dimension}-${column.state}`}
                    className={`fit-cell ${now ? 'is-now' : ''} ${trades < 10 ? 'is-thin' : ''}`}
                    style={cell(value, trades)}
                    title={`${item.spec.name} · ${column.label}：${trades} 笔，每笔 ${pct(value, 2)}，胜率 ${bucket?.stats.win_rate == null ? '—' : num(bucket.stats.win_rate, 0) + '%'}`}
                  >
                    {trades ? num(value, 1) : '·'}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function RegimeView() {
  const [days, setDays] = useState('500');
  const history = useLive(signal => api.regime(Number(days), signal), [days], false);
  const board = useLive(signal => api.playbooks(signal), [], false);
  const data = history.data;

  return (
    <div className="page-inner regime-page">
      <PageHead title="市场周期" lead="先认清行情，再选打法。趋势看指数与均线，资金看成交放缩，情绪看涨停、炸板与溢价，风格看大小盘强弱；每一天都被归入一个周期，战法的历史成绩按周期拆开看。">
        {data && <HistoryChip provenance={data.provenance} />}
      </PageHead>
      {history.error && !data && <ErrorNote>周期计算失败：{history.error}</ErrorNote>}
      {!data && !history.error && <><Skeleton height={220} radius={20} /><Skeleton height={300} radius={14} /></>}
      {data && (
        <>
          <RegimeBanner state={data.current} actions={<button className="btn sm ghost" onClick={() => navigate('picks')}>按此周期生成的今日推荐</button>} />
          <Panel
            eyebrow="REGIME HISTORY"
            title={`${data.index_name} · 周期史`}
            subtitle="背景色是趋势周期；下方四条色带依次是趋势、资金、情绪、风格"
            actions={<Seg label="时间窗" value={days} onChange={setDays} options={WINDOWS} />}
          >
            <Timeline points={data.points} name={data.index_name} />
            <Legend />
          </Panel>
          <div className="regime-split">
            <Panel eyebrow="WHAT WORKS WHEN" title="打法 × 周期" subtitle="入场当日处于该周期时的每笔平均收益（%），扣除成本；红色为正、绿色为负，淡色格子不足 10 笔">
              {board.data ? <FitMatrix playbooks={board.data.playbooks} regime={data.current} /> : board.error ? <ErrorNote>{board.error}</ErrorNote> : <Skeleton height={360} />}
              <p className="fit-note">读法：一列看一种行情里哪些打法赚钱，一行看一种打法怕什么行情。当前所处周期的列标题带圆点。</p>
            </Panel>
            <Panel eyebrow="RULES" title="周期判定规则" subtitle="全部是确定性规则，没有拟合参数">
              <ol className="regime-rules">
                {data.rules.map(rule => <li key={rule}>{rule}</li>)}
              </ol>
            </Panel>
          </div>
        </>
      )}
    </div>
  );
}
