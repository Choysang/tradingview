import { useMemo, useState } from 'react';
import { ArrowRight } from 'lucide-react';
import { navigate } from '../../app/router';
import { api } from '../../core/api';
import type { ThemeResonanceStat } from '../../core/contract';
import { money, num, pct, tone } from '../../core/format';
import { useLive } from '../../core/live';
import { NONE, useMarket, useWorkspace } from '../../core/store';
import { Spark } from '../../kit/charts';
import { Empty, Num, Panel, Skeleton, useSize } from '../../kit/primitives';
import { Identity } from '../screener/ResultTable';
import { PageHead } from '../shell/Shell';
import Crowd from './Crowd';
import './themes.css';

interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
  item: ThemeResonanceStat;
}

/** Squarified treemap (Bruls et al.): keeps tiles close to square so labels stay legible. */
function squarify(items: readonly ThemeResonanceStat[], width: number, height: number): Rect[] {
  const total = items.reduce((sum, item) => sum + Math.max(item.amount, 0.01), 0);
  const scale = (width * height) / (total || 1);
  const nodes = items.map(item => ({ item, area: Math.max(item.amount, 0.01) * scale })).sort((a, b) => b.area - a.area);
  const out: Rect[] = [];
  let box = { x: 0, y: 0, w: width, h: height };
  let row: typeof nodes = [];

  const worst = (candidate: typeof nodes, side: number) => {
    const sum = candidate.reduce((acc, node) => acc + node.area, 0);
    const max = Math.max(...candidate.map(node => node.area));
    const min = Math.min(...candidate.map(node => node.area));
    return Math.max((side * side * max) / (sum * sum), (sum * sum) / (side * side * min));
  };

  const layout = (rowNodes: typeof nodes) => {
    const sum = rowNodes.reduce((acc, node) => acc + node.area, 0);
    const horizontal = box.w >= box.h;
    const thickness = sum / (horizontal ? box.h : box.w);
    let offset = 0;
    rowNodes.forEach(node => {
      const length = node.area / thickness;
      out.push(horizontal
        ? { x: box.x, y: box.y + offset, w: thickness, h: length, item: node.item }
        : { x: box.x + offset, y: box.y, w: length, h: thickness, item: node.item });
      offset += length;
    });
    box = horizontal
      ? { x: box.x + thickness, y: box.y, w: box.w - thickness, h: box.h }
      : { x: box.x, y: box.y + thickness, w: box.w, h: box.h - thickness };
  };

  nodes.forEach(node => {
    const side = Math.min(box.w, box.h);
    if (!row.length || worst([...row, node], side) <= worst(row, side)) {
      row.push(node);
    } else {
      layout(row);
      row = [node];
    }
  });
  if (row.length) layout(row);
  return out;
}

function heat(value: number) {
  const clamped = Math.max(-6, Math.min(6, value)) / 6;
  const alpha = 0.12 + Math.abs(clamped) * 0.55;
  return clamped >= 0 ? `oklch(0.62 0.19 19 / ${alpha.toFixed(2)})` : `oklch(0.66 0.13 166 / ${alpha.toFixed(2)})`;
}

function Treemap({ themes, selected, onSelect }: { themes: readonly ThemeResonanceStat[]; selected: string | null; onSelect: (name: string) => void }) {
  const [ref, { width, height }] = useSize<HTMLDivElement>();
  const rects = useMemo(() => (width && height ? squarify(themes, width, height) : []), [themes, width, height]);
  return (
    <div className="treemap" ref={ref} role="list" aria-label="题材热力图：面积为成交额，颜色为平均涨幅">
      {rects.map((rect, index) => (
        <button
          key={rect.item.theme_name}
          role="listitem"
          className={`tile-theme ${selected === rect.item.theme_name ? 'is-selected' : ''} ${rect.w < 120 || rect.h < 64 ? 'is-small' : ''}`}
          style={{ left: rect.x, top: rect.y, width: rect.w, height: rect.h, background: heat(rect.item.avg_pct_chg), animationDelay: `${index * 30}ms` }}
          onClick={() => onSelect(rect.item.theme_name)}
        >
          <strong className="truncate">{rect.item.theme_name}</strong>
          <span className={`num ${tone(rect.item.avg_pct_chg)}`}>{pct(rect.item.avg_pct_chg)}</span>
          <small className="num">{money(rect.item.amount)} · 涨停 {rect.item.limit_up_count}</small>
        </button>
      ))}
    </div>
  );
}

function Members({ theme }: { theme: ThemeResonanceStat }) {
  const members = useLive(signal => api.query({ selected_themes: [theme.theme_name], sort_by: 'pct_chg', limit: 40, exclude_new: false }, signal), [theme.theme_name]);
  return (
    <Panel
      eyebrow="CONSTITUENTS"
      title={<>#{theme.theme_name}</>}
      subtitle={`${theme.stock_count} 只 · 涨停 ${theme.limit_up_count} · 连阳 ${theme.growth_count} · 强度 ${num(theme.strength, 1)}`}
      actions={<button className="btn sm" onClick={() => { useWorkspace.getState().replaceFilters({ selected_themes: [theme.theme_name] }, `#${theme.theme_name}`); navigate('screener'); }}>进入选股<ArrowRight size={13} /></button>}
      className="members-panel"
    >
      <div className="theme-summary">
        <div><span className="faint">平均涨幅</span><b className={`num ${tone(theme.avg_pct_chg)}`}>{pct(theme.avg_pct_chg)}</b></div>
        <div><span className="faint">成交额</span><b className="num">{money(theme.amount)}</b></div>
        <div className="theme-summary-spark"><Spark values={theme.sparkline} width={140} height={36} baseline={0} label="题材均值分时" /></div>
      </div>
      {members.data ? (
        <ol className="members">
          {members.data.records.map(record => (
            <li key={record.code}>
              <button onClick={() => useWorkspace.getState().openStock(record.code)}>
                <span className="truncate"><strong>{record.name}</strong><small className="num faint"> {record.code}</small></span>
                <Identity record={record} />
                <Spark values={record.intraday_sparkline} width={64} height={20} baseline={0} />
                <Num value={record.pct_chg} format={value => pct(value)} signed />
              </button>
            </li>
          ))}
        </ol>
      ) : <Skeleton height={240} />}
    </Panel>
  );
}

export default function ThemesView() {
  const themes = useMarket(state => state.overview?.hot_themes ?? NONE);
  const [selected, setSelected] = useState<string | null>(null);
  const current = themes.find(theme => theme.theme_name === selected) ?? themes[0];
  return (
    <div className="page-inner">
      <PageHead title="题材图谱" lead="面积是成交额，颜色是成分股平均涨幅。点选题材查看成分与龙头。下方是收盘后的抱团快照：哪些行业、哪些同名股、哪些席位在一起动。" />
      {!themes.length ? (
        <Empty title="暂无题材数据">实时源未提供题材标签时，这里保持为空。</Empty>
      ) : (
        <div className="themes-layout">
          <Panel eyebrow="HEATMAP" title="题材热力" subtitle={`前 ${themes.length} 个题材 · 按强度入选`} className="treemap-panel">
            <Treemap themes={themes} selected={current?.theme_name ?? null} onSelect={setSelected} />
            <div className="heat-legend" aria-hidden="true">
              <span>−6%</span><i /><span>0</span><i className="hot" /><span>+6%</span>
            </div>
          </Panel>
          {current && <Members theme={current} />}
        </div>
      )}
      <Crowd />
    </div>
  );
}
