/** Extension group A web checks: wire helpers, drift guard against extension.schema.json, pure view logic and chart rendering. */
import { render, screen } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { OUTCOME_CONTEXT_ID, REQUEST_FIELDS, toQuery, updatesUrl } from '../src/pages/ext/a/api';
import { Bars, Gauge, RegimeTimeline, pctText } from '../src/pages/ext/a/charts';
import { mergeTape } from '../src/pages/ext/a/TapePanel';
import { metricMap, outcomeState } from '../src/pages/ext/a/TrackRecordPage';
import type { OutcomeBody, RegimePoint, TapeItem } from '../src/pages/ext/a/types';

const schema = JSON.parse(readFileSync(resolve(__dirname, '../../../src/pulse/contracts/data/extension.schema.json'), 'utf-8')) as {
  $defs: Record<string, { properties: Record<string, unknown> }>;
};

describe('wire helpers', () => {
  it('sends the request fields of every wrapper exactly as the extension contract declares them', () => {
    for (const [op, fields] of Object.entries(REQUEST_FIELDS)) {
      const declared = Object.keys(schema.$defs[`${op}Request`]?.properties ?? {}).sort();
      expect([...fields].sort(), op).toEqual(declared);
    }
  });
  it('encodes objects and arrays as JSON text and skips null / undefined', () => {
    const q = new URLSearchParams(toQuery({ a: 'x', b: { k: 1 }, c: ['u1', 'u2'], d: null, e: undefined, f: 3 }));
    expect(q.get('b')).toBe('{"k":1}');
    expect(q.get('c')).toBe('["u1","u2"]');
    expect([...q.keys()].sort()).toEqual(['a', 'b', 'c', 'f']);
  });
  it('subscribes to the update stream with the 3.0.0 schema only', () => {
    const u = new URL(updatesUrl('main'), 'http://x');
    expect(u.pathname).toBe('/v1/x/stream-registered-updates');
    expect(JSON.parse(u.searchParams.get('accepted_schema_versions') ?? '[]')).toEqual(['3.0.0']);
    expect(JSON.parse(u.searchParams.get('scope') ?? '{}').scope_key).toBe('main');
  });
  it('uses a uuid as the outcome context id (the backend test pins the value)', () => {
    expect(OUTCOME_CONTEXT_ID).toMatch(/^[0-9a-f-]{36}$/);
  });
});

const ev = (id: string): TapeItem => ({
  event_id: id, recorded_at: '2026-11-11T02:00:00Z', event_kind: 'seal', instrument_id: 'i', code: '000001', name: '平安银行', price: '11', pct_chg: '10',
  limit_streak: 1, detail: '封涨停', themes: [], trade_date: '2026-11-11', event_time: null, from_snapshot_id: 'a', to_snapshot_id: 'b', tape_version: 'tape-v1',
});

describe('tape merge', () => {
  it('drops duplicates (reconnect replay), keeps log order and caps the list', () => {
    const a = mergeTape([], [ev('1'), ev('2')]);
    expect(mergeTape(a, [ev('2'), ev('3')]).map((e) => e.event_id)).toEqual(['1', '2', '3']);
    expect(mergeTape(a, [ev('3'), ev('4')], 3).map((e) => e.event_id)).toEqual(['2', '3', '4']);
  });
});

describe('track record logic', () => {
  const metric = (name: string, value: string | null) => ({ name, definition_version: 'v', value, unit: 'ratio', sample_count: 1, missing_reason: value ? null : 'X' });
  it('maps metrics by name and treats anything unresolved as pending', () => {
    expect(metricMap([metric('a', '1'), metric('b', null)]).a?.value).toBe('1');
    expect(outcomeState(undefined)).toBe('pending');
    expect(outcomeState({ status: 'pending' } as OutcomeBody)).toBe('pending');
    expect(outcomeState({ status: 'resolved' } as OutcomeBody)).toBe('resolved');
  });
});

describe('formatting and charts', () => {
  const words = ['冰点', '低迷', '中性', '升温', '亢奋', '高潮'];
  const edges = [25, 40, 55, 70, 85, 100];
  it('shows unknown as a dash, never as zero', () => {
    expect(pctText(null)).toBe('—');
    expect(pctText('1.5')).toBe('+1.50%');
    expect(pctText('-0.5', 1, false)).toBe('-0.5%');
  });
  it('draws the gauge marker only when the score is known', () => {
    const { container, rerender } = render(<Gauge score={null} edges={edges} words={words} />);
    expect(container.querySelector('.ea-gauge-marker')).toBeNull();
    rerender(<Gauge score={50.2} edges={edges} words={words} />);
    expect(container.querySelector('.ea-gauge-marker')).not.toBeNull();
    expect(screen.getByRole('meter').getAttribute('aria-valuenow')).toBe('50.2');
  });
  it('renders 11 histogram columns', () => {
    const bins = Array.from({ length: 11 }, (_, i) => ({ label: String(i), count: i }));
    const { container } = render(<Bars bins={bins} label="x" />);
    expect(container.querySelectorAll('.ea-bar-col')).toHaveLength(11);
  });
  it('fades regime cells whose dimension had no basis', () => {
    const pt = (d: string, ok: boolean): RegimePoint => ({
      date: d, index_close: '4000', trend: 'range', liquidity: 'steady', sentiment: 'ebb', style: 'unknown', breadth_ma20: null, limit_up_count: null, broken_rate: null,
      max_height: null, amount_ratio: null, premium: null, lu_percentile: null, index_ma60: null, ma60_slope: null, style_spread: null,
      basis: { trend: ok ? 'ok' : 'insufficient_history', liquidity: 'ok', sentiment: 'ok', style: 'missing_index' },
    });
    const labels = { trend: { range: '震荡' }, liquidity: { steady: '存量' }, sentiment: { ebb: '退潮' }, style: { unknown: '风格未知' } };
    const { container } = render(<RegimeTimeline points={[pt('2026-11-09', false), pt('2026-11-10', true)]} labels={labels} />);
    const opacities = [...container.querySelectorAll('rect')].map((r) => r.getAttribute('opacity'));
    expect(opacities).toContain('0.3');
    expect(opacities).toContain('0.95');
  });
});
