/**
 * Event tape (盘口异动): seal / reseal / break / surge / plunge / new-high derived from consecutive quote snapshots.
 * Loads the newest events once, then follows the SSE log; the browser resumes with Last-Event-ID after a drop and a
 * `reset` event reloads the list. Coverage is the quoted set only - the panel says so.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Badge } from '../../../components/Badges';
import { EmptyState } from '../../../components/States';
import { fmtTime } from '../../../lib/format';
import { extOps, tapeStreamUrl, useEventStream } from './api';
import { STREAM_LABEL } from './hooks';
import type { TapeItem } from './types';

export const KIND_LABEL: Record<TapeItem['event_kind'], string> = {
  seal: '封板', reseal: '回封', break: '炸板', surge: '拉升', plunge: '跳水', new_high: '新高度',
};
const KIND_TONE: Record<TapeItem['event_kind'], 'bad' | 'good' | 'warn' | 'neutral'> = {
  seal: 'bad', reseal: 'bad', break: 'good', surge: 'warn', plunge: 'good', new_high: 'warn',
};
const MAX_ITEMS = 200;

/** Merge by event id, keep log order, drop duplicates and cap the list (pure: unit-tested). */
export function mergeTape(prev: TapeItem[], incoming: TapeItem[], cap = MAX_ITEMS): TapeItem[] {
  const seen = new Set(prev.map((e) => e.event_id));
  const fresh = incoming.filter((e) => !seen.has(e.event_id));
  return [...prev, ...fresh].slice(-cap);
}

export function TapePanel() {
  const [items, setItems] = useState<TapeItem[]>([]);
  const [filter, setFilter] = useState<TapeItem['event_kind'] | 'all'>('all');
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(() => {
    extOps.marketTape({ recent: 100 }).then((r) => { setItems(r.data.events); setError(null); }).catch((e: unknown) => setError(e instanceof Error ? e.message : '读取失败'));
  }, []);
  useEffect(reload, [reload]);

  const state = useEventStream(tapeStreamUrl(), ['tape', 'reset'], (name, data) => {
    if (name === 'reset') { reload(); return; }
    setItems((prev) => mergeTape(prev, [data as TapeItem]));
  });

  const shown = useMemo(() => [...items].reverse().filter((e) => filter === 'all' || e.event_kind === filter), [items, filter]);
  return (
    <section className="card ea-tape" aria-label="盘口异动">
      <div className="ea-head">
        <h3>盘口异动</h3>
        <Badge tone={state === 'open' ? 'good' : state === 'retrying' ? 'warn' : 'neutral'}>{STREAM_LABEL[state]}</Badge>
        <span className="grow" />
        <div className="segmented small-seg" role="group" aria-label="异动类型">
          <button className={filter === 'all' ? 'btn-on' : ''} onClick={() => setFilter('all')}>全部</button>
          {(Object.keys(KIND_LABEL) as Array<TapeItem['event_kind']>).map((k) => (
            <button key={k} className={filter === k ? 'btn-on' : ''} onClick={() => setFilter(k)}>{KIND_LABEL[k]}</button>
          ))}
        </div>
      </div>
      <p className="muted small">由相邻两次报价快照比较得出，只覆盖被报价的关注股票（不是全市场）；快照之间发生的事不会被推断。</p>
      {error && <p className="flag flag-warn">{error}</p>}
      {shown.length === 0 ? <EmptyState>暂无异动。需要至少两次同一交易日的报价快照。</EmptyState> : (
        <ul className="ea-tape-list">
          {shown.map((e) => (
            <li key={e.event_id}>
              <span className="mono muted small">{fmtTime(e.event_time ?? e.recorded_at)}</span>
              <Badge tone={KIND_TONE[e.event_kind]}>{KIND_LABEL[e.event_kind]}</Badge>
              <b>{e.name}</b> <span className="mono small">{e.code ?? ''}</span>
              <span className="mono">{e.price}</span>
              <span className={`mono ${Number(e.pct_chg) >= 0 ? 'up' : 'down'}`}>{Number(e.pct_chg) > 0 ? '+' : ''}{e.pct_chg}%</span>
              <span className="small">{e.detail}</span>
              {e.themes.map((t) => <Badge key={t}>{t}</Badge>)}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
