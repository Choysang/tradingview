/**
 * Account context + actionability snapshot id discovery.
 * The contract has no "list accounts" operation (extension PEX ListAccountsQuery is
 * pending), so the account pointer comes from config/URL (?account=&av=&vs=). The
 * actionability_id is read from an account_holding workspace response, never invented.
 */
import { useQuery } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import { keys, workspaceQuery } from '../api/keys';
import { ops } from '../api/ops';
import { ACCOUNT_DEFAULTS, type AccountContext } from '../config';

export function useAccountContext(): { ctx: AccountContext; complete: boolean; set: (p: Partial<{ account: string; av: string; vs: string }>) => void } {
  const [sp, setSp] = useSearchParams();
  const av = sp.get('av');
  const ctx: AccountContext = {
    accountId: sp.get('account') ?? ACCOUNT_DEFAULTS.accountId,
    accountVersion: av ? Number(av) : ACCOUNT_DEFAULTS.accountVersion,
    valuationSnapshotId: sp.get('vs') ?? ACCOUNT_DEFAULTS.valuationSnapshotId,
  };
  const complete = !!ctx.accountId && ctx.accountVersion != null && Number.isFinite(ctx.accountVersion) && !!ctx.valuationSnapshotId;
  return {
    ctx,
    complete,
    set: (p) => setSp((prev) => { const n = new URLSearchParams(prev); for (const [k, v] of Object.entries(p)) { if (v) n.set(k, v); else n.delete(k); } return n; }, { replace: true }),
  };
}

export function useAccountProjection(ctx: AccountContext, enabled: boolean) {
  return useQuery({
    queryKey: keys.account(ctx.accountId ?? 'none', ctx.accountVersion ?? 0, ctx.valuationSnapshotId ?? 'none'),
    queryFn: ({ signal }) => ops.getAccountProjection(ctx.accountId as string, { account_version: ctx.accountVersion as number, valuation_snapshot_id: ctx.valuationSnapshotId as string }, { signal }),
    enabled,
    staleTime: 10_000,
  });
}

/** Any instrument can carry the account-view workspace; prefer holdings, then the given fallback ids. */
export function useActionabilityId(ctx: AccountContext, releaseId: string | null, candidateInstrumentIds: string[]) {
  const first = candidateInstrumentIds[0] ?? null;
  const q = useQuery({
    ...workspaceQuery(first ?? 'none', releaseId, 'account_holding', ctx.accountId),
    enabled: !!first && !!releaseId && !!ctx.accountId,
    staleTime: 15_000,
  });
  return { actionabilityId: q.data?.data.actionability_id ?? null, workspace: q };
}
