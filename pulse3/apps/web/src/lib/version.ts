/**
 * Version consistency and freshness rules (spec 4.2, 11, 12; M4-E02).
 * Why: a page must never mix an old plan with new reasons; stale data is shown as
 * stale, not silently replaced. Invariants: a block is accepted only when its
 * meta.release_id equals the page release (or the response is release-less
 * operational data). Not covered: fetching.
 */
import type { FreshnessState, ResponseMeta } from '../api/types';

export interface ConsistencyResult {
  ok: boolean;
  reason?: string;
}

export function checkVersionConsistency(
  expectedReleaseId: string | null | undefined,
  meta: Pick<ResponseMeta, 'release_id'> | null | undefined,
): ConsistencyResult {
  if (!meta) return { ok: false, reason: '缺少响应元数据' };
  if (meta.release_id == null) return { ok: true };
  if (!expectedReleaseId) return { ok: true };
  if (meta.release_id === expectedReleaseId) return { ok: true };
  return { ok: false, reason: `版本不一致：页面为 ${expectedReleaseId.slice(0, 8)}，该部分为 ${meta.release_id.slice(0, 8)}` };
}

export type Tone = 'good' | 'warn' | 'bad' | 'neutral';

export function freshnessTone(f: FreshnessState): Tone {
  switch (f) {
    case 'current': return 'good';
    case 'stale': return 'bad';
    case 'unknown': return 'warn';
    default: return 'neutral';
  }
}

/**
 * "Is the page showing something older than what it should show?"
 * displayedReleaseId: release of the data actually rendered; intendedReleaseId: target.
 */
export function isDisplayStale(displayedReleaseId: string | null | undefined, intendedReleaseId: string | null | undefined): boolean {
  return !!displayedReleaseId && !!intendedReleaseId && displayedReleaseId !== intendedReleaseId;
}

export type ReleaseMode = 'follow' | 'pinned';

export interface ReleaseSelection {
  mode: ReleaseMode;
  /** release to request; null while the current pointer is still unknown. */
  effectiveReleaseId: string | null;
  currentReleaseId: string | null;
  /** pinned and a newer release exists */
  newerAvailable: boolean;
}

export function resolveReleaseSelection(pinned: string | null, current: string | null): ReleaseSelection {
  if (pinned) {
    return { mode: 'pinned', effectiveReleaseId: pinned, currentReleaseId: current, newerAvailable: !!current && current !== pinned };
  }
  return { mode: 'follow', effectiveReleaseId: current, currentReleaseId: current, newerAvailable: false };
}
