import { useEffect, useRef, useState } from 'react';
import { Star } from 'lucide-react';
import { api, isAbort } from '../../core/api';
import type { MarketEvent } from '../../core/contract';
import { hm, pct, tone } from '../../core/format';
import { useMarket, useWorkspace } from '../../core/store';
import { Panel, Seg } from '../../kit/primitives';

export const KIND_LABEL: Record<MarketEvent['kind'], string> = {
  seal: '封',
  reseal: '回封',
  break: '炸',
  surge: '拉升',
  plunge: '跳水',
  new_high: '新高',
};

type Filter = 'all' | 'seal' | 'break' | 'move';

const MATCH: Record<Filter, (event: MarketEvent) => boolean> = {
  all: () => true,
  seal: event => event.kind === 'seal' || event.kind === 'reseal' || event.kind === 'new_high',
  break: event => event.kind === 'break',
  move: event => event.kind === 'surge' || event.kind === 'plunge',
};

const KEEP = 160;

/** Keeps a rolling tape: first a full page, then only events newer than what we hold. */
export function useEventTape() {
  const sequence = useMarket(state => state.sequence);
  const mode = useMarket(state => state.mode);
  const [events, setEvents] = useState<MarketEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const newest = useRef<number | null>(null);
  const fresh = useRef(new Set<string>());

  // A mode switch clears the server tape; a restart resets its sequence. Either way the
  // local rows belong to another stream: drop them instead of mixing SIM with LIVE.
  useEffect(() => {
    newest.current = null;
    setEvents([]);
  }, [mode]);
  useEffect(() => {
    if (newest.current != null && sequence > 0 && sequence < newest.current) {
      newest.current = null;
      setEvents([]);
    }
    const controller = new AbortController();
    api.events(newest.current, controller.signal)
      .then(result => {
        setError(null);
        if (!result.events.length) return;
        newest.current = Math.max(newest.current ?? 0, ...result.events.map(event => event.sequence));
        setEvents(current => {
          const known = new Set(current.map(event => event.id));
          const incoming = result.events.filter(event => !known.has(event.id));
          fresh.current = current.length ? new Set(incoming.map(event => event.id)) : new Set();
          return [...incoming, ...current].slice(0, KEEP);
        });
      })
      .catch(reason => { if (!isAbort(reason)) setError(reason instanceof Error ? reason.message : String(reason)); });
    return () => controller.abort();
  }, [sequence, mode]);

  return { events, error, fresh: fresh.current };
}

export function EventRow({ event, fresh }: { event: MarketEvent; fresh: boolean }) {
  const watched = useMarket(state => state.watchlist.includes(event.code));
  return (
    <li className={`tape-row kind-${event.kind} ${fresh ? 'is-fresh' : ''}`}>
      <button onClick={() => useWorkspace.getState().openStock(event.code)}>
        <span className="tape-time num">{hm(event.time)}</span>
        <span className="tape-kind">{KIND_LABEL[event.kind]}</span>
        <span className="tape-name">
          {watched && <Star size={11} className="gold" fill="currentColor" aria-label="自选" />}
          <strong className="truncate">{event.name}</strong>
        </span>
        <span className="tape-detail truncate">{event.detail}</span>
        <span className={`num tape-pct ${tone(event.pct_chg)}`}>{pct(event.pct_chg)}</span>
      </button>
    </li>
  );
}

export default function EventTape() {
  const { events, error, fresh } = useEventTape();
  const [filter, setFilter] = useState<Filter>('all');
  const visible = events.filter(MATCH[filter]).slice(0, 60);
  return (
    <Panel
      eyebrow="TAPE"
      title="盘口异动"
      subtitle="快照之间的封板、炸板与急速拉升"
      className="tape-panel"
      actions={
        <Seg<Filter>
          label="异动类型"
          value={filter}
          onChange={setFilter}
          options={[{ value: 'all', label: '全部' }, { value: 'seal', label: '封板' }, { value: 'break', label: '炸板' }, { value: 'move', label: '异动' }]}
        />
      }
    >
      {error && !events.length ? (
        <p className="muted">异动流暂不可用：{error}</p>
      ) : visible.length ? (
        <ol className="tape" aria-live="off">
          {visible.map(event => <EventRow key={event.id} event={event} fresh={fresh.has(event.id)} />)}
        </ol>
      ) : (
        <p className="muted tape-empty">等待下一笔异动…… 每个快照都会与上一个比对。</p>
      )}
    </Panel>
  );
}
