/**
 * Live data hooks. The SSE stream only announces "a new snapshot exists";
 * each mounted view re-asks for exactly what it shows. One model, no merging.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { api, isAbort, STREAM_URL } from './api';
import { useMarket } from './store';
import type { StreamEvent } from './contract';

const STALE_AFTER_MS = 15_000;

/** Owns the single EventSource and the overview that every view's chrome reads. */
export function useMarketStream() {
  useEffect(() => {
    let source: EventSource | null = null;
    let overview: AbortController | null = null;
    let closed = false;
    let retry = 0;
    let retryTimer = 0;

    const loadOverview = () => {
      overview?.abort();
      overview = new AbortController();
      api.overview(overview.signal)
        .then(data => useMarket.setState({ overview: data }))
        .catch(error => { if (!isAbort(error)) useMarket.setState({ connection: 'reconnecting' }); });
    };

    const connect = () => {
      source = new EventSource(STREAM_URL);
      source.addEventListener('snapshot', message => {
        retry = 0;
        const event = JSON.parse((message as MessageEvent).data) as StreamEvent;
        useMarket.setState({ sequence: event.sequence, asOf: event.as_of, mode: event.mode, connection: 'live', lastTickAt: Date.now() });
        if (!document.hidden) loadOverview();
      });
      source.onerror = () => {
        if (closed) return;
        useMarket.setState({ connection: 'reconnecting' });
        // The browser retries dropped streams itself, but gives up for good after an
        // HTTP error (e.g. a 502 while the API restarts). Reopen with backoff then.
        if (source?.readyState === EventSource.CLOSED) {
          source.close();
          retryTimer = window.setTimeout(connect, Math.min(15_000, 1000 * 2 ** retry++));
        }
      };
    };

    connect();
    loadOverview();
    api.watchlist().then(data => useMarket.setState({ watchlist: data.codes })).catch(() => undefined);

    const watchdog = window.setInterval(() => {
      const { connection, lastTickAt } = useMarket.getState();
      const silent = Date.now() - lastTickAt;
      if (connection === 'live' && silent > STALE_AFTER_MS) useMarket.setState({ connection: 'stale' });
      // A proxy can keep a dead stream "open" forever; after two silent windows, reopen it.
      if (connection === 'stale' && silent > STALE_AFTER_MS * 2 && !document.hidden) {
        source?.close();
        useMarket.setState({ lastTickAt: Date.now() });
        connect();
      }
    }, 3000);
    const onVisible = () => { if (!document.hidden) loadOverview(); };
    document.addEventListener('visibilitychange', onVisible);

    return () => {
      closed = true;
      window.clearTimeout(retryTimer);
      source?.close();
      overview?.abort();
      window.clearInterval(watchdog);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, []);
}

export interface LiveResult<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  refresh: () => void;
}

/**
 * Fetch on mount, on dependency change and (when `live`) on every new tick.
 * Previous data stays on screen while the next answer is in flight, so values
 * update in place instead of blinking through a loading state.
 */
export function useLive<T>(fetcher: (signal: AbortSignal) => Promise<T>, deps: unknown[], live = true): LiveResult<T> {
  const sequence = useMarket(state => (live ? state.sequence : 0));
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  useEffect(() => {
    setLoading(true);
  }, deps); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (document.hidden && data !== null) return;
    const controller = new AbortController();
    fetcherRef.current(controller.signal)
      .then(result => { setData(result); setError(null); })
      .catch(reason => { if (!isAbort(reason)) setError(reason instanceof Error ? reason.message : String(reason)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [...deps, sequence, nonce]); // eslint-disable-line react-hooks/exhaustive-deps

  const refresh = useCallback(() => setNonce(value => value + 1), []);
  return { data, error, loading, refresh };
}

/** Debounce a fast-changing value, e.g. slider drags, before it becomes a request. */
export function useDebounced<T>(value: T, delay = 180): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(value), delay);
    return () => window.clearTimeout(timer);
  }, [value, delay]);
  return settled;
}
