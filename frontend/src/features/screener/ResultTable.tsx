import { useEffect, useMemo, useRef, useState } from 'react';
import type { KeyboardEvent, ReactNode } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { ArrowDown, ArrowUp, Star } from 'lucide-react';
import type { ScreenerFilterRequest, StockTickRecord } from '../../core/contract';
import { hm, money, num, pct } from '../../core/format';
import { useMarket, useWorkspace } from '../../core/store';
import { Spark } from '../../kit/charts';
import { Num, useSize } from '../../kit/primitives';

type SortKey = ScreenerFilterRequest['sort_by'];

interface Column {
  key: string;
  label: string;
  width: string;
  /** minimum pixels the column needs; lower priority columns drop first */
  min: number;
  priority: number;
  align?: 'right';
  sort?: SortKey;
  render: (record: StockTickRecord) => ReactNode;
}

export function Identity({ record }: { record: StockTickRecord }) {
  if (record.is_limit_up && record.limit_streak) return <span className={`badge ${record.limit_streak >= 4 ? 'gold' : 'up'}`}>{record.limit_streak} 连板</span>;
  if (record.ever_limit_up_today) return <span className="badge down">炸板{record.open_board_count ? ` ${record.open_board_count}` : ''}</span>;
  if (record.n_days_m_boards_label) return <span className="badge gold">{record.n_days_m_boards_label}</span>;
  if ((record.growth_streak ?? 0) >= 3) return <span className="badge outline">{record.growth_streak} 连阳</span>;
  return <span className="faint">—</span>;
}

function Score({ value }: { value: number | null }) {
  if (value == null) return <span className="faint">—</span>;
  return (
    <span className="score">
      <i style={{ width: `${Math.max(2, Math.min(100, value))}%` }} />
      <b className="num">{value.toFixed(0)}</b>
    </span>
  );
}

function WatchButton({ code, name }: { code: string; name: string }) {
  const watched = useMarket(state => state.watchlist.includes(code));
  return (
    <button
      className={`watch ${watched ? 'on' : ''}`}
      aria-label={watched ? `从自选移除${name}` : `加入自选${name}`}
      aria-pressed={watched}
      onClick={event => {
        event.stopPropagation();
        useMarket.getState().toggleWatch(code)
          .then(added => useWorkspace.getState().notify(added ? `已加入自选：${name}` : `已移出自选：${name}`, 'ok'))
          .catch(error => useWorkspace.getState().notify(`自选保存失败：${error.message}`, 'error'));
      }}
    >
      <Star size={14} fill={watched ? 'currentColor' : 'none'} />
    </button>
  );
}

const COLUMNS: Column[] = [
  { key: 'watch', label: '', width: '34px', min: 34, priority: 1, render: record => <WatchButton code={record.code} name={record.name} /> },
  {
    key: 'name', label: '名称', width: 'minmax(140px, 1.3fr)', min: 140, priority: 1,
    render: record => (
      <span className="cell-name">
        <strong className="truncate">{record.name}</strong>
        <span className="cell-code num">
          {record.code}
          {record.board !== '10cm' && <em className="tag cyan">{record.board}</em>}
          {record.is_st && <em className="tag">ST</em>}
          {record.is_suspended && <em className="tag">停牌</em>}
        </span>
      </span>
    ),
  },
  { key: 'identity', label: '身份', width: '92px', min: 92, priority: 2, sort: 'limit_streak', render: record => <Identity record={record} /> },
  { key: 'spark', label: '分时', width: '96px', min: 96, priority: 4, render: record => <Spark values={record.intraday_sparkline} width={84} height={24} baseline={0} label={`${record.name}分时`} /> },
  { key: 'price', label: '现价', width: '74px', min: 74, priority: 3, align: 'right', sort: 'price', render: record => <Num value={record.price} format={value => num(value, 2)} toneOf={record.pct_chg} signed /> },
  { key: 'pct', label: '涨幅', width: '78px', min: 78, priority: 1, align: 'right', sort: 'pct_chg', render: record => <Num value={record.pct_chg} format={value => pct(value)} signed /> },
  { key: 'score', label: '综合分', width: '96px', min: 96, priority: 2, sort: 'composite_score', render: record => <Score value={record.composite_score} /> },
  { key: 'amount', label: '成交额', width: '82px', min: 82, priority: 3, align: 'right', sort: 'amount_today', render: record => <span className="num">{money(record.amount_today)}</span> },
  { key: 'turnover', label: '换手', width: '66px', min: 66, priority: 5, align: 'right', sort: 'turnover_today', render: record => <span className="num">{pct(record.turnover_today, 1, false)}</span> },
  { key: 'ratio', label: '昨换比', width: '66px', min: 66, priority: 7, align: 'right', sort: 'turnover_ratio_yd', render: record => <span className={`num ${(record.turnover_ratio_yd ?? 1) < 0.75 ? 'down' : (record.turnover_ratio_yd ?? 1) > 1.35 ? 'up' : ''}`}>{num(record.turnover_ratio_yd, 2)}</span> },
  { key: 'cap', label: '自由流通', width: '84px', min: 84, priority: 6, align: 'right', sort: 'free_float_mkt_cap', render: record => <span className="num">{money(record.free_float_mkt_cap)}</span> },
  { key: 'seal', label: '首封', width: '60px', min: 60, priority: 6, align: 'right', render: record => <span className="num muted">{hm(record.first_seal_time)}</span> },
  { key: 'themes', label: '题材', width: 'minmax(110px, 1fr)', min: 110, priority: 8, render: record => <span className="cell-themes truncate">{record.theme_tags.slice(0, 2).join(' · ') || '—'}</span> },
];

function visibleColumns(width: number) {
  if (!width) return COLUMNS.filter(column => column.priority <= 3);
  const chosen = new Set<string>();
  let used = 24;
  for (const column of [...COLUMNS].sort((a, b) => a.priority - b.priority)) {
    if (used + column.min + 12 > width && column.priority > 1) continue;
    chosen.add(column.key);
    used += column.min + 12;
  }
  return COLUMNS.filter(column => chosen.has(column.key));
}

interface ResultTableProps {
  records: StockTickRecord[];
  sortBy: SortKey;
  sortDesc: boolean;
  onSort: (key: SortKey) => void;
  loading: boolean;
}

export default function ResultTable({ records, sortBy, sortDesc, onSort, loading }: ResultTableProps) {
  const [frame, { width }] = useSize<HTMLDivElement>();
  const scroller = useRef<HTMLDivElement>(null);
  const density = useWorkspace(state => state.density);
  const [cursor, setCursor] = useState(0);
  const columns = useMemo(() => visibleColumns(width), [width]);
  const template = columns.map(column => column.width).join(' ');
  const rowHeight = density === 'compact' ? 40 : 52;

  const virtualizer = useVirtualizer({
    count: records.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => rowHeight,
    overscan: 12,
  });

  useEffect(() => { virtualizer.measure(); }, [rowHeight, virtualizer]);
  useEffect(() => { setCursor(current => Math.min(current, Math.max(0, records.length - 1))); }, [records.length]);

  const move = (next: number) => {
    const clamped = Math.max(0, Math.min(records.length - 1, next));
    setCursor(clamped);
    virtualizer.scrollToIndex(clamped, { align: 'auto' });
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const page = Math.max(1, Math.floor((scroller.current?.clientHeight ?? 400) / rowHeight) - 1);
    const actions: Record<string, () => void> = {
      ArrowDown: () => move(cursor + 1),
      ArrowUp: () => move(cursor - 1),
      PageDown: () => move(cursor + page),
      PageUp: () => move(cursor - page),
      Home: () => move(0),
      End: () => move(records.length - 1),
      Enter: () => records[cursor] && useWorkspace.getState().openStock(records[cursor].code),
    };
    const action = actions[event.key];
    if (action) {
      event.preventDefault();
      action();
    }
  };

  return (
    <div className={`grid-table ${loading ? 'is-loading' : ''}`} ref={frame} role="grid" aria-rowcount={records.length + 1} aria-label="筛选结果">
      <div className="grid-head" role="row" style={{ gridTemplateColumns: template }}>
        {columns.map(column => (
          <div key={column.key} role="columnheader" className={column.align === 'right' ? 'right' : ''} aria-sort={column.sort && column.sort === sortBy ? (sortDesc ? 'descending' : 'ascending') : undefined}>
            {column.sort ? (
              <button className={column.sort === sortBy ? 'active' : ''} onClick={() => onSort(column.sort as SortKey)}>
                {column.label}
                {column.sort === sortBy && (sortDesc ? <ArrowDown size={11} /> : <ArrowUp size={11} />)}
              </button>
            ) : column.label}
          </div>
        ))}
      </div>
      <div className="grid-body" ref={scroller} tabIndex={0} onKeyDown={onKeyDown} aria-activedescendant={records[cursor] ? `row-${records[cursor].code}` : undefined}>
        <div style={{ height: virtualizer.getTotalSize(), position: 'relative' }}>
          {virtualizer.getVirtualItems().map(item => {
            const record = records[item.index];
            return (
              <div
                key={record.code}
                id={`row-${record.code}`}
                role="row"
                aria-rowindex={item.index + 2}
                aria-selected={item.index === cursor}
                className={`grid-row ${item.index === cursor ? 'is-cursor' : ''} ${record.is_limit_up ? 'is-sealed' : ''}`}
                style={{ gridTemplateColumns: template, height: rowHeight, transform: `translateY(${item.start}px)` }}
                onClick={() => { setCursor(item.index); useWorkspace.getState().openStock(record.code); }}
              >
                {columns.map(column => (
                  <div key={column.key} role="gridcell" className={column.align === 'right' ? 'right' : ''}>{column.render(record)}</div>
                ))}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

export function toCsv(records: StockTickRecord[]) {
  const header = ['代码', '名称', '制式', '现价', '涨幅%', '连板', '连阳', '综合分', '成交额(亿)', '换手%', '昨换比', '自由流通(亿)', '首封', '题材'];
  const escape = (value: unknown) => {
    const text = value == null ? '' : String(value);
    return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  const rows = records.map(record => [
    record.code, record.name, record.board, record.price, record.pct_chg, record.limit_streak, record.growth_streak,
    record.composite_score, record.amount_today, record.turnover_today, record.turnover_ratio_yd, record.free_float_mkt_cap,
    record.first_seal_time, record.theme_tags.join('|'),
  ].map(escape).join(','));
  return '﻿' + [header.join(','), ...rows].join('\n');
}
