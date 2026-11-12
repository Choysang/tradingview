import { describe, expect, it } from 'vitest';
import type { BarWindow, Envelope, Workspace } from '../src/api/types';
import { plotBars } from '../src/components/PriceChart';
import { compareDecimal, toFixed, toPercent } from '../src/lib/decimal';
import { deriveBarsParams, pickBarsSnapshot } from '../src/lib/barsParams';
import { gradeOfInstance } from '../src/lib/grades';
import { classifyPlan, sortPlans } from '../src/lib/planKind';
import { createMockState, currentRelease, handle } from '../mock/handler.ts';
import { INSTRUMENTS } from '../mock/market.ts';
import { uuid } from '../mock/util.ts';

describe('decimal helpers', () => {
  it('compares without float error and returns null for invalid input', () => {
    expect(compareDecimal('0.1', '0.10')).toBe(0);
    expect(compareDecimal('10.000000000001', '10')).toBe(1);
    expect(compareDecimal('-1', '0')).toBe(-1);
    expect(compareDecimal(null, '1')).toBeNull();
    expect(compareDecimal('1e3', '1')).toBeNull();
  });
  it('formats half-even and percent', () => {
    expect(toFixed('2.345', 2)).toBe('2.34');
    expect(toFixed('2.355', 2)).toBe('2.36');
    expect(toFixed('-0.004', 2)).toBe('0.00');
    expect(toPercent('0.5600', 1)).toBe('56.0%');
    expect(toPercent(null)).toBeNull();
  });
});

describe('evidence grade mapping is conservative', () => {
  const g = (lifecycle: string, repro: string, impl: string) => gradeOfInstance({ lifecycle, reproduction_status: repro, implementation_status: impl } as never);
  it('only reproduced + implemented + validated/active reads 已验证', () => {
    expect(g('active', 'reproduced', 'implemented')).toBe('verified');
    expect(g('active', 'running', 'implemented')).toBe('testing');
    expect(g('active', 'reproduced', 'prototype')).toBe('testing');
    expect(g('discovered', 'not_started', 'not_implemented')).toBe('untested');
    expect(g('retired', 'reproduced', 'implemented')).toBe('refuted');
    expect(g('screened', 'failed', 'implemented')).toBe('refuted');
  });
});

describe('plan classification', () => {
  const strat = (family: string, role: string) => ({ method_family: family, role }) as never;
  it('separates main plan from T and grid sub-plans via the registered strategy', () => {
    const by = new Map<string, never>([['m', strat('rule_based', 'selection')], ['t', strat('t_trade', 'execution')], ['g', strat('grid_trade', 'execution')]]);
    const plan = (id: string) => ({ strategy_refs: [{ instance_id: id }] }) as never;
    expect(classifyPlan(plan('m'), by)).toBe('main');
    expect(classifyPlan(plan('t'), by)).toBe('t');
    expect(classifyPlan(plan('g'), by)).toBe('grid');
    expect(sortPlans([{ kind: 'grid' as const }, { kind: 'main' as const }, { kind: 't' as const }]).map((x) => x.kind)).toEqual(['main', 't', 'grid']);
  });
});

function fetchWs(state = createMockState(Date.parse('2026-11-11T08:00:00Z'))) {
  const r = currentRelease(state);
  const res = handle(state, { method: 'GET', path: `/v1/stocks/${INSTRUMENTS[0]!.id}/workspace`, query: new URLSearchParams({ release_id: r.id, view_kind: 'public_research' }), headers: {}, body: undefined });
  return { state, r, ws: res.body as Envelope<Workspace> };
}

describe('bars: parameters and plotting', () => {
  it('derives getStockBars params from the frozen workspace (snapshot, decision_as_of, availability policy), not from now', () => {
    const { ws } = fetchWs();
    const d = deriveBarsParams(ws, '1d', 'raw');
    expect(d.ok).toBe(true);
    if (!d.ok) return;
    expect(d.params.decision_as_of).toBe(ws.meta.decision_as_of);
    expect(d.params.end).toBe(ws.meta.decision_as_of);
    expect(d.params.availability_policy_version).toBe(ws.meta.availability_policy_version);
    expect(ws.meta.input_snapshot_refs.map((s) => s.snapshot_id)).toContain(d.params.snapshot_id);
    expect(d.params.adjustment_version).toBeUndefined();
    // real mode never invents an adjustment version: the server fills in the snapshot's own version
    const f = deriveBarsParams(ws, '1w', 'forward_adjusted');
    expect(f.ok && f.params.adjustment_version).toBeUndefined();
    const declared = structuredClone(ws);
    declared.data.features[0]!.key.adjustment_mode = 'forward_adjusted';
    declared.data.features[0]!.key.adjustment_version = 'legacy-qfq-unversioned';
    const g = deriveBarsParams(declared, '1w', 'forward_adjusted');
    expect(g.ok && g.params.adjustment_version).toBe('legacy-qfq-unversioned');
  });

  it('picks the snapshot whose dataset can answer the request', () => {
    const ref = (dataset_id: string, n: number) => ({ snapshot_id: `00000000-0000-4000-8000-00000000000${n}`, dataset_id, manifest_version: 1, content_hash: 'a'.repeat(64) });
    const refs = [ref('quotes', 1), ref('bars_legacy_fwd_adjusted_1d', 2), ref('bars_raw_1d_tencent', 3), ref('bars_raw_1m', 4), ref('bars_raw_5m', 5)];
    expect(pickBarsSnapshot(refs, '1d', 'raw')?.dataset_id).toBe('bars_raw_1d_tencent');
    expect(pickBarsSnapshot(refs, '1w', 'raw')?.dataset_id).toBe('bars_raw_1d_tencent');
    expect(pickBarsSnapshot(refs, '5m', 'raw')?.dataset_id).toBe('bars_raw_5m');
    expect(pickBarsSnapshot(refs, '1m', 'raw')?.dataset_id).toBe('bars_raw_1m');
    expect(pickBarsSnapshot(refs, '1d', 'forward_adjusted')?.dataset_id).toBe('bars_legacy_fwd_adjusted_1d');
    expect(pickBarsSnapshot([ref('quotes', 1)], '1d', 'raw')?.dataset_id).toBe('quotes');
  });

  it('refuses to guess when the release does not declare a cut-off', () => {
    const { ws } = fetchWs();
    const bad = deriveBarsParams({ ...ws, meta: { ...ws.meta, decision_as_of: null } }, '1d', 'raw');
    expect(bad.ok).toBe(false);
  });

  it('plots server values only; source-gap bars stay gaps and unfinished bars are counted', () => {
    const { state, ws } = fetchWs();
    const d = deriveBarsParams(ws, '1m', 'raw');
    if (!d.ok) throw new Error('params');
    const res = handle(state, { method: 'GET', path: `/v1/stocks/${INSTRUMENTS[0]!.id}/bars`, query: new URLSearchParams(d.params as never), headers: {}, body: undefined });
    const win = (res.body as Envelope<BarWindow>).data;
    const colors = { up: 'u', down: 'd', upSoft: 'us', downSoft: 'ds' };
    const plotted = plotBars(win, colors);
    expect(win.window_complete).toBe(false);
    expect(plotted.gaps).toBe(1);
    expect(plotted.candles.length).toBe(win.bars.length - 1);
    const first = win.bars.find((b) => b.quality_status === 'valid') as { open_price_raw: string };
    expect(plotted.candles[0]?.open).toBe(Number(first.open_price_raw));
    const wp = deriveBarsParams(ws, '1w', 'raw');
    if (!wp.ok) throw new Error('params');
    const wk = (handle(state, { method: 'GET', path: `/v1/stocks/${INSTRUMENTS[0]!.id}/bars`, query: new URLSearchParams(wp.params as never), headers: {}, body: undefined }).body as Envelope<BarWindow>).data;
    expect(plotBars(wk, colors).unfinished).toBe(1);
  });
});

describe('mock server honours contract semantics', () => {
  it('binds cursors to the ranking: a cursor from another horizon is refused', () => {
    const state = createMockState(Date.parse('2026-11-11T02:00:00Z'));
    const r = currentRelease(state);
    const q = (h: string, c?: string) => new URLSearchParams({ release_id: r.id, horizon_id: h, forecast_target: 't', ...(c ? { cursor: c } : {}) });
    const p1 = handle(state, { method: 'GET', path: '/v1/rankings', query: q('short_term'), headers: {}, body: undefined }).body as Envelope<{ entries: unknown[] }>;
    expect(p1.meta.next_cursor).toBeTruthy();
    const ok = handle(state, { method: 'GET', path: '/v1/rankings', query: q('short_term', p1.meta.next_cursor!), headers: {}, body: undefined });
    expect(ok.status).toBe(200);
    const bad = handle(state, { method: 'GET', path: '/v1/rankings', query: q('swing', p1.meta.next_cursor!), headers: {}, body: undefined });
    expect(bad.status).toBe(409);
  });

  it('keeps old releases readable and serves a different ranking for the new one', () => {
    const state = createMockState(Date.parse('2026-11-11T02:00:00Z'));
    const r1 = currentRelease(state);
    handle(state, { method: 'POST', path: '/__mock/publish', query: new URLSearchParams(), headers: {}, body: undefined });
    const q = (id: string) => new URLSearchParams({ release_id: id, horizon_id: 'short_term', forecast_target: 't' });
    const a = handle(state, { method: 'GET', path: '/v1/rankings', query: q(r1.id), headers: {}, body: undefined }).body as Envelope<{ ranking_id: string }>;
    const b = handle(state, { method: 'GET', path: '/v1/rankings', query: q(currentRelease(state).id), headers: {}, body: undefined }).body as Envelope<{ ranking_id: string }>;
    expect(a.meta.freshness_state).toBe('stale');
    expect(b.meta.freshness_state).toBe('current');
    expect(a.data.ranking_id).not.toBe(b.data.ranking_id);
  });

  it('layout writes require the expected version and an idempotency key; replay is idempotent', () => {
    const state = createMockState(Date.parse('2026-11-11T02:00:00Z'));
    const id = uuid('layout:t');
    const put = (v: number, key?: string) => handle(state, { method: 'PUT', path: `/v1/workspace-layouts/${id}`, query: new URLSearchParams(), headers: key ? { 'idempotency-key': key } : {}, body: { expected_layout_version: v, display_name: 'x', layout_document: {} } });
    expect(put(0).status).toBe(400);
    const first = put(0, 'k1');
    expect(first.status).toBe(200);
    expect(put(0, 'k1').body).toEqual(first.body); // replay
    expect(put(0, 'k2').status).toBe(409); // stale expected version
    expect(put(1, 'k3').status).toBe(200);
  });

  it('pollUpdates: no cursor starts at the end; an unknown epoch resets', () => {
    const state = createMockState(Date.parse('2026-11-11T02:00:00Z'));
    const poll = (cursor?: string) => (handle(state, { method: 'GET', path: '/v1/updates', query: new URLSearchParams({ slot_key: 's', ...(cursor ? { cursor } : {}) }), headers: {}, body: undefined }).body as Envelope<{ events: unknown[]; reset_required: boolean; resume_cursor: string }>).data;
    const start = poll();
    handle(state, { method: 'POST', path: '/__mock/publish', query: new URLSearchParams(), headers: {}, body: undefined });
    const next = poll(start.resume_cursor);
    expect(next.events.length).toBeGreaterThan(0);
    expect(poll('old-epoch:3').reset_required).toBe(true);
  });
});
