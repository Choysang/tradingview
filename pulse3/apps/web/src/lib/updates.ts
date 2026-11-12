/**
 * Update-log consumption (spec 11, 12; M4-E04): cursor handling, duplicate and
 * out-of-order events, expired cursor => full refetch.
 * Why: pollUpdates is the only update transport the contract fixes (SSE would reuse
 * the same events). Invariants: the cursor always advances to resume_cursor; an
 * event is applied at most once (event_id) and never after a newer aggregate
 * version of the same (event_type, aggregate_id); reset_required or a changed
 * stream_epoch discards local event memory and requests a full refetch.
 * Not covered: transport/timers (see hooks/useUpdates).
 */
import type { UpdateNotification, UpdatePage } from '../api/types';

export type UpdateScope = 'release' | 'account' | 'sources' | 'actionability';

export type UpdateEffect =
  | { type: 'invalidate'; scope: UpdateScope; aggregateId: string; version: number }
  | { type: 'full_refetch'; reason: 'reset_required' | 'epoch_changed' | 'cursor_expired' };

export interface UpdateState {
  epoch: string | null;
  cursor: string | null;
  /** most recent event ids (bounded) */
  seen: string[];
  /** highest aggregate_version applied per `${event_type}:${aggregate_id}` */
  versions: Record<string, number>;
  /** counters for the diagnostics drawer */
  stats: { applied: number; duplicates: number; outOfOrder: number; resets: number };
}

const SEEN_LIMIT = 500;

export function initialUpdateState(): UpdateState {
  return { epoch: null, cursor: null, seen: [], versions: {}, stats: { applied: 0, duplicates: 0, outOfOrder: 0, resets: 0 } };
}

const SCOPE_BY_TYPE: Record<UpdateNotification['event_type'], UpdateScope> = {
  'decision.published': 'release',
  'account.updated': 'account',
  'source.health.changed': 'sources',
  'actionability.ready': 'actionability',
};

export interface ApplyResult {
  state: UpdateState;
  effects: UpdateEffect[];
}

/** Called when the server answers that the cursor is no longer retained (e.g. HTTP 409). */
export function applyCursorExpired(state: UpdateState): ApplyResult {
  return {
    state: { ...initialUpdateState(), stats: { ...state.stats, resets: state.stats.resets + 1 } },
    effects: [{ type: 'full_refetch', reason: 'cursor_expired' }],
  };
}

export function applyUpdatePage(state: UpdateState, page: UpdatePage): ApplyResult {
  const epochChanged = state.epoch !== null && state.epoch !== page.stream_epoch;
  if (page.reset_required || epochChanged) {
    return {
      state: {
        ...initialUpdateState(),
        epoch: page.stream_epoch,
        cursor: page.resume_cursor,
        stats: { ...state.stats, resets: state.stats.resets + 1 },
      },
      effects: [{ type: 'full_refetch', reason: page.reset_required ? 'reset_required' : 'epoch_changed' }],
    };
  }

  const seen = new Set(state.seen);
  const order = [...state.seen];
  const versions = { ...state.versions };
  const stats = { ...state.stats };
  const effects: UpdateEffect[] = [];
  const emitted = new Set<string>();

  for (const ev of page.events) {
    if (seen.has(ev.event_id)) {
      stats.duplicates += 1;
      continue;
    }
    seen.add(ev.event_id);
    order.push(ev.event_id);
    const key = `${ev.event_type}:${ev.aggregate_id}`;
    const known = versions[key];
    if (known !== undefined && ev.aggregate_version <= known) {
      stats.outOfOrder += 1;
      continue;
    }
    versions[key] = ev.aggregate_version;
    stats.applied += 1;
    const scope = SCOPE_BY_TYPE[ev.event_type];
    const dedupe = `${scope}:${ev.aggregate_id}`;
    // a newer version of the same aggregate replaces an earlier effect in this page
    if (emitted.has(dedupe)) {
      const idx = effects.findIndex((e) => e.type === 'invalidate' && `${e.scope}:${e.aggregateId}` === dedupe);
      if (idx >= 0) effects.splice(idx, 1);
    }
    emitted.add(dedupe);
    effects.push({ type: 'invalidate', scope, aggregateId: ev.aggregate_id, version: ev.aggregate_version });
  }

  return {
    state: {
      epoch: page.stream_epoch,
      cursor: page.resume_cursor,
      seen: order.slice(-SEEN_LIMIT),
      versions,
      stats,
    },
    effects,
  };
}
