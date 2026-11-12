/**
 * Typed client of the extension-A operations (paths /v1/x/..., query parameters = the <op>Request fields; object and
 * array fields travel as JSON text). Non-2xx answers become the same ApiError as the core client.
 * Also the SSE helpers: an EventSource resumes with Last-Event-ID by itself; a `reset` event means "refetch everything".
 */
import { useQuery, type UseQueryOptions } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { ApiError } from '../../../api/client';
import { API_ORIGIN } from '../../../config';
import type {
  ArtifactBody, QueryMarketSentimentData, QueryMarketTodayData, QueryRankingHistoryData, QueryRankingTrackRecordData,
  QuerySectorDetailsData, Resp, Scope, TapePage,
} from './types';

/** Stable id of the one supported outcome method (uuid5 of `ranking-outcome-context|ranking-forward-outcome-v1`; asserted by the backend test). */
export const OUTCOME_CONTEXT_ID = '3dffbb0a-c3ae-5dac-a527-c8f81b80ef54';
export const MARKET_SCOPE: Scope = { scope_kind: 'market', scope_key: 'main-board', membership_snapshot_id: null };

/** Request fields each wrapper sends, checked against extension.schema.json by tests/ext_a.test.tsx (drift guard). */
export const REQUEST_FIELDS = {
  queryMarketToday: ['release_id', 'account_id'],
  queryMarketSentiment: ['release_id', 'scope'],
  querySectorDetails: ['sector', 'release_id', 'horizon_id', 'forecast_target'],
  queryRankingHistory: ['scope', 'window', 'horizon_id', 'forecast_target', 'page'],
  queryRankingTrackRecord: ['ranking_ids', 'evaluation_context_id', 'outcome_as_of'],
  streamRegisteredUpdates: ['cursor', 'scope', 'accepted_schema_versions'],
} as const;

type Params = Record<string, unknown>;

export function toQuery(params: Params): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null) continue;
    sp.set(k, typeof v === 'object' ? JSON.stringify(v) : String(v));
  }
  return sp.toString();
}

export async function extGet<T>(path: string, params: Params = {}, signal?: AbortSignal): Promise<Resp<T>> {
  let res: Response;
  try {
    res = await fetch(`${API_ORIGIN}${path}?${toQuery(params)}`, { signal, headers: { Accept: 'application/json' } });
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw e;
    throw new ApiError(0, null, '网络不可用或服务无响应');
  }
  const body = (await res.json().catch(() => null)) as (Resp<T> & { error?: { code: string; message: string; retryable: boolean; field_errors: [] } }) | null;
  if (!res.ok || !body || body.error) throw new ApiError(res.status, body as never);
  return body;
}

export const extOps = {
  queryMarketToday: (release_id: string, account_id?: string | null, signal?: AbortSignal) =>
    extGet<QueryMarketTodayData>('/v1/x/query-market-today', { release_id, account_id }, signal),
  queryMarketSentiment: (release_id: string, scope: Scope, signal?: AbortSignal) =>
    extGet<QueryMarketSentimentData>('/v1/x/query-market-sentiment', { release_id, scope }, signal),
  querySectorDetails: (q: { sector: { taxonomy_id: string; taxonomy_version: number; node_id: string }; release_id: string; horizon_id: string; forecast_target: string }, signal?: AbortSignal) =>
    extGet<QuerySectorDetailsData>('/v1/x/query-sector-details', q, signal),
  queryRankingHistory: (q: { scope: Scope; window: { start: string; end: string }; horizon_id: string; forecast_target: string; page: { limit: number; cursor: string | null } }, signal?: AbortSignal) =>
    extGet<QueryRankingHistoryData>('/v1/x/query-ranking-history', q, signal),
  queryRankingTrackRecord: (ranking_ids: string[], outcome_as_of: string, signal?: AbortSignal) =>
    extGet<QueryRankingTrackRecordData>('/v1/x/query-ranking-track-record', { ranking_ids, evaluation_context_id: OUTCOME_CONTEXT_ID, outcome_as_of }, signal),
  readArtifact: async <T,>(ref: string, signal?: AbortSignal): Promise<ArtifactBody<T>> => (await extGet<ArtifactBody<T>>('/v1/x/a/artifact', { ref }, signal)).data,
  marketTape: (q: { cursor?: string | null; recent?: number }, signal?: AbortSignal) => extGet<TapePage>('/v1/x/a/market-tape', q, signal),
};

/** Artifact bodies are immutable (content-addressed): cache forever. */
export function artifactQuery<T>(ref: string | null | undefined): UseQueryOptions<ArtifactBody<T>> {
  return { queryKey: ['ext-a', 'artifact', ref], queryFn: ({ signal }) => extOps.readArtifact<T>(ref as string, signal), enabled: !!ref, staleTime: Infinity };
}

export function useArtifact<T>(ref: string | null | undefined) {
  return useQuery(artifactQuery<T>(ref));
}

export type StreamState = 'connecting' | 'open' | 'retrying' | 'closed';

/**
 * EventSource with state. `onEvent(name, data, lastId)` receives every named event; the browser resends Last-Event-ID on
 * reconnect, so the server resumes exactly after the last delivered id (or sends `reset`).
 */
export function useEventStream(url: string | null, names: string[], onEvent: (name: string, data: unknown, id: string) => void): StreamState {
  const [state, setState] = useState<StreamState>('connecting');
  const handler = useRef(onEvent);
  handler.current = onEvent;
  const key = names.join(',');
  useEffect(() => {
    if (!url || typeof EventSource === 'undefined') { setState('closed'); return undefined; }
    const es = new EventSource(url);
    setState('connecting');
    es.onopen = () => setState('open');
    es.onerror = () => setState(es.readyState === EventSource.CLOSED ? 'closed' : 'retrying');
    for (const name of key.split(',')) {
      es.addEventListener(name, (ev) => {
        const m = ev as MessageEvent<string>;
        try { handler.current(name, JSON.parse(m.data), m.lastEventId); } catch { /* a malformed frame is ignored, never fatal */ }
      });
    }
    return () => es.close();
  }, [url, key]);
  return state;
}

export function updatesUrl(slotKey: string): string {
  return `${API_ORIGIN}/v1/x/stream-registered-updates?${toQuery({ scope: { scope_kind: 'market', scope_key: slotKey, membership_snapshot_id: null }, accepted_schema_versions: ['3.0.0'] })}`;
}

export function tapeStreamUrl(): string {
  return `${API_ORIGIN}/v1/x/a/market-tape/stream`;
}
