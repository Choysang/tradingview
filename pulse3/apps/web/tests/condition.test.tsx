import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { BarWindow, Condition, Operand } from '../src/api/types';
import { ConditionTree } from '../src/components/ConditionTree';
import { evaluateCondition, makeLookupResolver, triAnd, triNot, triOr, type OperandResolver } from '../src/lib/condition';

const clock = { kind: 'bar_close' as const, timeframe: '1d', session_policy_version: 's' };
const feat = (field: string, tf = '1d'): Operand => ({
  kind: 'feature', feature_id: 'f', definition_version: '1', instrument_id: 'i', timeframe: tf, field_path: field, unit: 'CNY/share', resolution_policy_version: 'p', initial_feature_key_hash: null,
});
const k = (v: string, unit = 'CNY/share'): Operand => ({ kind: 'constant', value: v, unit });
const cmp = (op: 'lt' | 'lte' | 'gt' | 'gte' | 'eq', l: Operand, r: Operand): Condition => ({ node: 'compare', clock, operator: op, left: l, right: r });

function bar(close: string | null, final = true, quality: 'valid' | 'partial' = 'valid'): BarWindow['bars'][number] {
  return {
    instrument_id: 'i', trade_date: '2026-11-10', timeframe: '1d', session_scope: 'regular', bar_open_time: '2026-11-10T01:30:00Z', bar_close_time: '2026-11-10T07:00:00Z',
    snapshot_id: 's', currency: 'CNY', adjustment_mode: 'raw', open_price_raw: close, high_price_raw: close, low_price_raw: close, close_price_raw: close, volume_shares: 1, turnover_amount_cny: '1',
    exchange_reference_price_raw: close, is_final: final, available_at: '2026-11-10T07:00:05Z', quality_status: quality, field_quality: {},
  };
}
const win = (bars: BarWindow['bars'], mode: 'raw' | 'forward_adjusted' = 'raw'): BarWindow => ({
  instrument_id: 'i', snapshot_id: 's', timeframe: '1d', session_policy_version: 's', adjustment_mode: mode, adjustment_version: null, start: 'a', end: 'b', window_complete: true, window_hash: 'h', bars,
});

describe('Kleene three-valued logic', () => {
  it('and: FALSE dominates, UNKNOWN otherwise unless all TRUE', () => {
    expect(triAnd(['TRUE', 'TRUE'])).toBe('TRUE');
    expect(triAnd(['TRUE', 'UNKNOWN'])).toBe('UNKNOWN');
    expect(triAnd(['FALSE', 'UNKNOWN'])).toBe('FALSE');
    expect(triAnd([])).toBe('UNKNOWN');
  });
  it('or: TRUE dominates, UNKNOWN unless all FALSE', () => {
    expect(triOr(['FALSE', 'FALSE'])).toBe('FALSE');
    expect(triOr(['FALSE', 'UNKNOWN'])).toBe('UNKNOWN');
    expect(triOr(['TRUE', 'UNKNOWN'])).toBe('TRUE');
  });
  it('not keeps UNKNOWN', () => {
    expect(triNot('TRUE')).toBe('FALSE');
    expect(triNot('FALSE')).toBe('TRUE');
    expect(triNot('UNKNOWN')).toBe('UNKNOWN');
  });
});

describe('condition evaluation over server values', () => {
  const r = makeLookupResolver({ bars: win([bar('9.80'), bar('10.20')]) });

  it('compares with decimal strings (no float drift)', () => {
    expect(evaluateCondition(cmp('gte', feat('close_price_raw'), k('10.20')), r).tri).toBe('TRUE');
    expect(evaluateCondition(cmp('gt', feat('close_price_raw'), k('10.20')), r).tri).toBe('FALSE');
    expect(evaluateCondition(cmp('lt', k('0.1'), k('0.10000000001')), r).tri).toBe('TRUE');
  });

  it('missing operand is UNKNOWN, never treated as 0', () => {
    const n = evaluateCondition(cmp('lte', feat('macd_hist'), k('0')), r);
    expect(n.tri).toBe('UNKNOWN');
    expect(n.detail).toContain('不当作 0');
    // if it were treated as 0, "lte 0" would be TRUE
  });

  it('only confirmed (final) bars count; an unfinished last bar is ignored', () => {
    const only = makeLookupResolver({ bars: win([bar('9.80'), bar('99.00', false)]) });
    expect(evaluateCondition(cmp('lt', feat('close_price_raw'), k('10')), only).tri).toBe('TRUE');
  });

  it('null price in a source-gap bar yields UNKNOWN', () => {
    const gap = makeLookupResolver({ bars: win([bar(null, true, 'partial')]) });
    expect(evaluateCondition(cmp('lt', feat('close_price_raw'), k('10')), gap).tri).toBe('UNKNOWN');
  });

  it('adjusted bars never resolve raw-price operands', () => {
    const adj = makeLookupResolver({ bars: win([bar('9.80')], 'forward_adjusted') });
    expect(evaluateCondition(cmp('lt', feat('close_price_raw'), k('10')), adj).tri).toBe('UNKNOWN');
  });

  it('unit mismatch is UNKNOWN', () => {
    expect(evaluateCondition(cmp('lt', feat('close_price_raw'), k('10', 'CNY')), r).tri).toBe('UNKNOWN');
  });

  it('crosses needs both previous and current confirmed observations', () => {
    const crosses: Condition = { node: 'crosses', clock, direction: 'above', left: feat('close_price_raw'), right: k('10'), previous_observation: { selection: 'previous_valid', max_age_ms: 1000 } };
    expect(evaluateCondition(crosses, r).tri).toBe('TRUE'); // 9.80 -> 10.20 crosses 10 upward
    const single = makeLookupResolver({ bars: win([bar('10.20')]) });
    expect(evaluateCondition(crosses, single).tri).toBe('UNKNOWN');
    const flat = makeLookupResolver({ bars: win([bar('10.10'), bar('10.20')]) });
    expect(evaluateCondition(crosses, flat).tri).toBe('FALSE');
  });

  it('within_session and event_state are UNKNOWN when the page has no server state', () => {
    const w: Condition = { node: 'within_session', clock, session_kinds: ['continuous'] };
    expect(evaluateCondition(w, r).tri).toBe('UNKNOWN');
    const stub: OperandResolver = { ...r, inSession: () => true };
    expect(evaluateCondition(w, stub).tri).toBe('TRUE');
  });

  it('nested tree: all[TRUE, any[FALSE, UNKNOWN], not UNKNOWN] is UNKNOWN; adding a FALSE child makes it FALSE', () => {
    const tree: Condition = {
      node: 'all', clock, children: [
        cmp('lte', feat('close_price_raw'), k('11')),
        { node: 'any', clock, children: [cmp('gte', feat('close_price_raw'), k('12')), { node: 'within_session', clock, session_kinds: ['x'] }] },
        { node: 'not', clock, child: cmp('lte', feat('macd_hist'), k('0')) },
      ],
    };
    expect(evaluateCondition(tree, r).tri).toBe('UNKNOWN');
    const withFalse: Condition = { ...tree, children: [...(tree as Extract<Condition, { node: 'all' }>).children, cmp('gt', feat('close_price_raw'), k('50'))] } as Condition;
    expect(evaluateCondition(withFalse, r).tri).toBe('FALSE');
  });
});

describe('ConditionTree rendering', () => {
  it('shows TRUE / FALSE / UNKNOWN distinctly, with the reason for UNKNOWN', () => {
    const r = makeLookupResolver({ bars: win([bar('9.80'), bar('10.20')]) });
    const tree: Condition = { node: 'all', clock, children: [cmp('gte', feat('close_price_raw'), k('10')), cmp('gt', feat('close_price_raw'), k('50')), cmp('lte', feat('macd_hist'), k('0'))] };
    render(<ConditionTree node={evaluateCondition(tree, r)} />);
    expect(screen.getAllByText('TRUE').length).toBeGreaterThan(0);
    expect(screen.getAllByText('FALSE').length).toBeGreaterThan(0);
    expect(screen.getAllByText('UNKNOWN').length).toBeGreaterThan(0);
    expect(screen.getByText(/缺少.*确认值.*UNKNOWN/)).toBeInTheDocument();
    expect(screen.getAllByText(/成立/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/无法判断/).length).toBeGreaterThan(0);
  });
});
