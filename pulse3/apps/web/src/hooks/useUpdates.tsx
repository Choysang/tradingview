/**
 * Polls pollUpdates with a cursor and turns events into cache invalidations.
 * Rules live in lib/updates.ts (duplicates, out-of-order, reset => full refetch).
 * Connection loss is surfaced (stale), never hidden: a page whose update channel
 * is down says so instead of implying it is live.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { ApiError } from '../api/client';
import { ops } from '../api/ops';
import { SLOT_KEY } from '../config';
import {
  applyCursorExpired, applyUpdatePage, initialUpdateState, type UpdateEffect, type UpdateState,
} from '../lib/updates';

export const POLL_INTERVAL_MS = 5_000;

export interface UpdatesStatus {
  connected: boolean;
  lastOkAt: number | null;
  lastError: string | null;
  stats: UpdateState['stats'];
  lastEffects: UpdateEffect[];
  pollNow: () => void;
}

const Ctx = createContext<UpdatesStatus | null>(null);

export function useUpdatesStatus(): UpdatesStatus {
  const v = useContext(Ctx);
  if (!v) throw new Error('UpdatesProvider missing');
  return v;
}

export function UpdatesProvider({ children, intervalMs = POLL_INTERVAL_MS }: { children: ReactNode; intervalMs?: number }) {
  const qc = useQueryClient();
  const stateRef = useRef<UpdateState>(initialUpdateState());
  const [status, setStatus] = useState<Omit<UpdatesStatus, 'pollNow'>>({
    connected: true, lastOkAt: null, lastError: null, stats: stateRef.current.stats, lastEffects: [],
  });
  const busy = useRef(false);

  const runEffects = useCallback((effects: UpdateEffect[]) => {
    for (const e of effects) {
      if (e.type === 'full_refetch') {
        void qc.invalidateQueries();
        continue;
      }
      switch (e.scope) {
        case 'release': void qc.invalidateQueries({ queryKey: ['release', 'current'] }); break;
        case 'account': void qc.invalidateQueries({ queryKey: ['account'] }); void qc.invalidateQueries({ queryKey: ['actionability'] }); break;
        case 'sources': void qc.invalidateQueries({ queryKey: ['dataStatus'] }); void qc.invalidateQueries({ queryKey: ['quote'] }); break;
        case 'actionability': void qc.invalidateQueries({ queryKey: ['actionability'] }); break;
      }
    }
  }, [qc]);

  const poll = useCallback(async () => {
    if (busy.current) return;
    busy.current = true;
    try {
      const res = await ops.pollUpdates(SLOT_KEY, stateRef.current.cursor ?? undefined);
      const { state, effects } = applyUpdatePage(stateRef.current, res.data);
      stateRef.current = state;
      runEffects(effects);
      setStatus({ connected: true, lastOkAt: Date.now(), lastError: null, stats: state.stats, lastEffects: effects });
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        // explicit "cursor no longer retained": drop it and refetch everything
        const { state, effects } = applyCursorExpired(stateRef.current);
        stateRef.current = state;
        runEffects(effects);
        setStatus((s) => ({ ...s, connected: true, lastOkAt: Date.now(), lastError: null, stats: state.stats, lastEffects: effects }));
      } else {
        const msg = err instanceof Error ? err.message : '更新通道不可用';
        setStatus((s) => ({ ...s, connected: false, lastError: msg }));
      }
    } finally {
      busy.current = false;
    }
  }, [runEffects]);

  useEffect(() => {
    void poll();
    const t = setInterval(() => void poll(), intervalMs);
    return () => clearInterval(t);
  }, [poll, intervalMs]);

  const value = useMemo<UpdatesStatus>(() => ({ ...status, pollNow: () => void poll() }), [status, poll]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
