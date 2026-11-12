import { describe, expect, it } from 'vitest';
import type { UpdateNotification, UpdatePage } from '../src/api/types';
import { applyCursorExpired, applyUpdatePage, initialUpdateState } from '../src/lib/updates';

let seq = 0;
function ev(type: UpdateNotification['event_type'], aggregate: string, version: number, id?: string): UpdateNotification {
  seq += 1;
  return { event_id: id ?? `e${seq}`, event_type: type, schema_version: '3.0.0', aggregate_id: aggregate, aggregate_version: version, recorded_at: '2026-11-11T00:00:00Z', payload: {} as never };
}
const pg = (events: UpdateNotification[], cursor: string, epoch = 'ep1', reset = false): UpdatePage => ({ stream_epoch: epoch, events, resume_cursor: cursor, reset_required: reset });

describe('update cursor handling', () => {
  it('advances the cursor to resume_cursor even without events', () => {
    const { state, effects } = applyUpdatePage(initialUpdateState(), pg([], 'ep1:0'));
    expect(state.cursor).toBe('ep1:0');
    expect(state.epoch).toBe('ep1');
    expect(effects).toEqual([]);
  });

  it('turns a decision.published event into a release invalidation', () => {
    const { effects } = applyUpdatePage(initialUpdateState(), pg([ev('decision.published', 'slot', 2)], 'ep1:1'));
    expect(effects).toEqual([{ type: 'invalidate', scope: 'release', aggregateId: 'slot', version: 2 }]);
  });

  it('ignores a duplicated event (same event_id) across and within pages', () => {
    const e = ev('decision.published', 'slot', 2, 'dup');
    const a = applyUpdatePage(initialUpdateState(), pg([e, { ...e }], 'ep1:2'));
    expect(a.effects).toHaveLength(1);
    expect(a.state.stats.duplicates).toBe(1);
    const b = applyUpdatePage(a.state, pg([{ ...e }], 'ep1:3'));
    expect(b.effects).toEqual([]);
    expect(b.state.stats.duplicates).toBe(2);
  });

  it('ignores an out-of-order older aggregate version', () => {
    const a = applyUpdatePage(initialUpdateState(), pg([ev('decision.published', 'slot', 5)], 'ep1:1'));
    const b = applyUpdatePage(a.state, pg([ev('decision.published', 'slot', 4), ev('decision.published', 'slot', 5)], 'ep1:3'));
    expect(b.effects).toEqual([]);
    expect(b.state.stats.outOfOrder).toBe(2);
    expect(b.state.versions['decision.published:slot']).toBe(5);
  });

  it('within one page [v5, v4] applies v5 once and drops v4', () => {
    const { effects, state } = applyUpdatePage(initialUpdateState(), pg([ev('decision.published', 'slot', 5), ev('decision.published', 'slot', 4)], 'ep1:2'));
    expect(effects).toHaveLength(1);
    expect(state.stats.outOfOrder).toBe(1);
  });

  it('coalesces effects for the same aggregate inside a page', () => {
    const { effects } = applyUpdatePage(initialUpdateState(), pg([ev('account.updated', 'acc', 1), ev('account.updated', 'acc', 2)], 'ep1:2'));
    expect(effects).toEqual([{ type: 'invalidate', scope: 'account', aggregateId: 'acc', version: 2 }]);
  });

  it('reset_required clears memory and asks for a full refetch', () => {
    const a = applyUpdatePage(initialUpdateState(), pg([ev('decision.published', 'slot', 3)], 'ep1:1'));
    const b = applyUpdatePage(a.state, pg([], 'ep1:9', 'ep1', true));
    expect(b.effects).toEqual([{ type: 'full_refetch', reason: 'reset_required' }]);
    expect(b.state.cursor).toBe('ep1:9');
    expect(b.state.seen).toEqual([]);
    expect(b.state.versions).toEqual({});
    expect(b.state.stats.resets).toBe(1);
  });

  it('a changed stream_epoch (cursor from an old stream) is a full refetch, events are not trusted', () => {
    const a = applyUpdatePage(initialUpdateState(), pg([], 'ep1:4'));
    const b = applyUpdatePage(a.state, pg([ev('decision.published', 'slot', 1)], 'ep2:0', 'ep2'));
    expect(b.effects).toEqual([{ type: 'full_refetch', reason: 'epoch_changed' }]);
    expect(b.state.epoch).toBe('ep2');
  });

  it('after a reset the same event ids / versions are accepted again', () => {
    const a = applyUpdatePage(initialUpdateState(), pg([ev('decision.published', 'slot', 7, 'same')], 'ep1:1'));
    const reset = applyUpdatePage(a.state, pg([], 'ep2:0', 'ep2'));
    const c = applyUpdatePage(reset.state, pg([ev('decision.published', 'slot', 1, 'same')], 'ep2:1', 'ep2'));
    expect(c.effects).toHaveLength(1);
  });

  it('server-side cursor expiry (HTTP 409) drops the cursor and refetches everything', () => {
    const a = applyUpdatePage(initialUpdateState(), pg([], 'ep1:4'));
    const { state, effects } = applyCursorExpired(a.state);
    expect(state.cursor).toBeNull();
    expect(effects).toEqual([{ type: 'full_refetch', reason: 'cursor_expired' }]);
  });
});
