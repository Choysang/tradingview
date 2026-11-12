/**
 * TanStack Query keys and query options per operation.
 * Why: TanStack cache is a DISPLAY layer only (spec 4.2) - keys carry every scope
 * element that changes the answer (release_id, horizon, taxonomy, adjustment...), so
 * two versions can never share an entry. Persistent user state is read/written
 * only through the API operations (see hooks/useUserState).
 */
import { queryOptions } from '@tanstack/react-query';
import { ops } from './ops';
import { SLOT_KEY } from '../config';

export const keys = {
  currentRelease: ['release', 'current', SLOT_KEY] as const,
  release: (id: string) => ['release', id] as const,
  workspace: (id: string, releaseId: string, view: string, account?: string | null) => ['workspace', releaseId, id, view, account ?? null] as const,
  plan: (planId: string, version: number) => ['plan', planId, version] as const,
  quote: (id: string, policy: string) => ['quote', id, policy] as const,
  bars: (id: string, tf: string, mode: string, adjV: string | undefined, snapshot: string, start: string, end: string) => ['bars', id, tf, mode, adjV ?? '', snapshot, start, end] as const,
  strategies: (f: Record<string, string | undefined>) => ['strategies', f] as const,
  dataStatus: ['dataStatus'] as const,
  account: (id: string, v: number, snap: string) => ['account', id, v, snap] as const,
  layout: (id: string) => ['layout', id] as const,
  watchlists: ['watchlists'] as const,
  annotations: (instrumentId: string) => ['annotations', instrumentId] as const,
};

export const currentReleaseQuery = () =>
  queryOptions({
    queryKey: keys.currentRelease,
    queryFn: ({ signal }) => ops.getCurrentRelease(SLOT_KEY, { signal }),
    refetchInterval: 30_000,
    staleTime: 5_000,
  });

export const releaseQuery = (id: string | null) =>
  queryOptions({
    queryKey: keys.release(id ?? 'none'),
    queryFn: ({ signal }) => ops.getRelease(id as string, { signal }),
    enabled: !!id,
    staleTime: 60_000,
  });

export const workspaceQuery = (id: string, releaseId: string | null, view: 'public_research' | 'account_holding' = 'public_research', accountId?: string | null) =>
  queryOptions({
    queryKey: keys.workspace(id, releaseId ?? 'none', view, accountId),
    queryFn: ({ signal }) =>
      ops.getStockWorkspace(id, { release_id: releaseId as string, view_kind: view, ...(view === 'account_holding' && accountId ? { account_id: accountId } : {}) }, { signal }),
    enabled: !!releaseId,
    staleTime: 60_000,
  });

export const planQuery = (planId: string | null, version: number | null) =>
  queryOptions({
    queryKey: keys.plan(planId ?? 'none', version ?? 0),
    queryFn: ({ signal }) => ops.getTradePlan(planId as string, version as number, { signal }),
    enabled: !!planId && version != null,
    staleTime: 60_000,
  });

export const dataStatusQuery = () =>
  queryOptions({
    queryKey: keys.dataStatus,
    queryFn: ({ signal }) => ops.getDataStatus({ signal }),
    refetchInterval: 30_000,
    staleTime: 10_000,
  });
