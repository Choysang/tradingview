/**
 * Watchlist tape (legacy row #39): the market event tape (ext A, `queryMarketTape`) filtered to the user's watchlist.
 * The tape only covers the quoted set and only the newest events are loaded, so an empty answer says how many events were read and
 * that a quiet tape is not proof that nothing happened. Events are matched by instrument id, never by name.
 */
import { useQuery } from '@tanstack/react-query';
import { Badge } from '../../../components/Badges';
import { EmptyState } from '../../../components/States';
import { fmtTime } from '../../../lib/format';
import { extOps } from '../../ext/a/api';
import { KIND_LABEL } from '../../ext/a/TapePanel';
import type { TapeItem } from '../../ext/a/types';
import './fillm.css';

export const TAPE_RECENT = 300;

/** Events of watched instruments, newest first; an empty watchlist matches nothing (not everything). */
export function filterTapeToWatch(events: readonly TapeItem[], watchIds: readonly string[]): TapeItem[] {
  if (watchIds.length === 0) return [];
  const ids = new Set(watchIds);
  return events.filter((e) => ids.has(e.instrument_id)).slice().reverse();
}

export function WatchTape({ watchIds }: { watchIds: readonly string[] }) {
  const key = watchIds.join(',');
  const q = useQuery({
    queryKey: ['bf-fillm', 'watch-tape', key],
    queryFn: ({ signal }) => extOps.marketTape({ recent: TAPE_RECENT }, signal),
    enabled: watchIds.length > 0,
    refetchInterval: 30_000,
  });
  if (watchIds.length === 0) return null;
  const events = q.data?.data.events ?? [];
  const shown = filterTapeToWatch(events, watchIds);
  return (
    <section aria-label="自选异动" className="card">
      <div className="ea-head"><h3>自选异动</h3><span className="muted small">最近 {events.length} 条盘口异动中属于自选的 {shown.length} 条</span></div>
      {q.error && !q.data && <p className="flag flag-warn" role="alert">读取盘口异动失败。</p>}
      {shown.length === 0 && !q.error ? (
        <EmptyState>最近 {events.length} 条异动里没有自选股票的。异动只覆盖被报价的关注股票，需要同一交易日至少两次报价快照；这里没有异动不代表没有发生。</EmptyState>
      ) : (
        <ul className="fm-tape">
          {shown.map((e) => (
            <li key={e.event_id}>
              <span className="mono muted small">{fmtTime(e.event_time ?? e.recorded_at)}</span>
              <Badge>{KIND_LABEL[e.event_kind]}</Badge>
              <b>{e.name}</b> <span className="mono small">{e.code ?? ''}</span>
              <span className="mono">{e.price}</span>
              <span className={`mono ${Number(e.pct_chg) >= 0 ? 'fm-up' : 'fm-down'}`}>{Number(e.pct_chg) > 0 ? '+' : ''}{e.pct_chg}%</span>
              <span className="small">{e.detail}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
