/** Client of the live-lane reads (GET /v1/x/bf/live/*); query parameters are exactly the names below (drift-guarded by the tests). */
import { useQuery } from '@tanstack/react-query';
import { ApiError } from '../../../api/client';
import { extGet } from '../../ext/a/api';
import type { Resp, SealClockData, SealFactsData } from './types';

export const REQUEST_FIELDS = { sealClock: ['session_date', 'bucket_minutes'], sealFacts: ['instrument_id', 'session_date'] } as const;
const DATE = /^\d{4}-\d{2}-\d{2}$/;

/** A session date only when it looks like one; anything else means "the latest session". */
export const sessionParam = (s: string | null | undefined): string | null => (s && DATE.test(s) ? s : null);

export const liveOps = {
  sealClock: (sessionDate: string | null, releaseId: string | null, signal?: AbortSignal) =>
    extGet<SealClockData>('/v1/x/bf/live/seal-clock', { session_date: sessionDate, release_id: releaseId }, signal),
  sealFacts: (instrumentId: string, sessionDate: string | null, releaseId: string | null, signal?: AbortSignal) =>
    extGet<SealFactsData>('/v1/x/bf/live/seal-facts', { instrument_id: instrumentId, session_date: sessionDate, release_id: releaseId }, signal),
};

/** A client error (404 = no facts for the session) is an answer, not a failure: no retry. Server / network errors get one retry. */
const noRetryOn404 = (n: number, e: unknown) => !(e instanceof ApiError && e.status >= 400 && e.status < 500) && n < 1;

/** ``releaseId`` pins the read to that release's session and decision cutoff (nothing recorded later is shown). */
export function useSealClock(sessionDate: string | null | undefined, releaseId: string | null = null) {
  const d = sessionParam(sessionDate);
  return useQuery<Resp<SealClockData>>({ queryKey: ['bf-live', 'seal-clock', d, releaseId], queryFn: ({ signal }) => liveOps.sealClock(d, releaseId, signal), staleTime: 60_000, retry: noRetryOn404 });
}

export function useSealFacts(instrumentId: string | undefined, sessionDate?: string | null, releaseId: string | null = null) {
  const d = sessionParam(sessionDate);
  return useQuery<Resp<SealFactsData>>({
    queryKey: ['bf-live', 'seal-facts', instrumentId, d, releaseId], queryFn: ({ signal }) => liveOps.sealFacts(instrumentId as string, d, releaseId, signal),
    enabled: !!instrumentId, staleTime: 60_000, retry: noRetryOn404,
  });
}
