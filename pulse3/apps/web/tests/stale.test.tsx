import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { Envelope, ResponseMeta, TradePlan } from '../src/api/types';
import { FreshnessBadge } from '../src/components/Badges';
import { MetaStrip } from '../src/components/MetaStrip';
import { PlanCard } from '../src/components/PlanCard';
import { ReleaseBar } from '../src/components/ReleaseBar';
import type { ReleaseCtx } from '../src/hooks/useRelease';
import { checkVersionConsistency, isDisplayStale, resolveReleaseSelection } from '../src/lib/version';

const meta = (over: Partial<ResponseMeta> = {}): ResponseMeta => ({
  request_id: 'r', generated_at: '2026-11-11T01:00:00Z', trace_id: null, contract_version: '3.0.0', release_id: 'REL-A', decision_as_of: '2026-11-11T00:00:00Z',
  availability_policy_version: 'p', input_snapshot_refs: [], quality_status: 'valid', freshness_state: 'current',
  coverage: { policy_version: 'c', expected_count: 10, observed_count: 8, missing_count: 2, unverifiable_reasons: ['2 只停牌'] }, warnings: [], next_cursor: null, actionability_id: null, ...over,
});

describe('version consistency and stale display', () => {
  it('accepts only the page release (or release-less operational data)', () => {
    expect(checkVersionConsistency('REL-A', { release_id: 'REL-A' }).ok).toBe(true);
    expect(checkVersionConsistency('REL-A', { release_id: 'REL-B' }).ok).toBe(false);
    expect(checkVersionConsistency('REL-A', { release_id: null }).ok).toBe(true);
    expect(checkVersionConsistency('REL-A', null).ok).toBe(false);
  });

  it('flags displayed data from an older release as stale rather than mixing', () => {
    expect(isDisplayStale('REL-A', 'REL-B')).toBe(true);
    expect(isDisplayStale('REL-B', 'REL-B')).toBe(false);
    expect(isDisplayStale(undefined, 'REL-B')).toBe(false);
  });

  it('resolves follow vs pinned', () => {
    expect(resolveReleaseSelection(null, 'C')).toMatchObject({ mode: 'follow', effectiveReleaseId: 'C', newerAvailable: false });
    expect(resolveReleaseSelection('P', 'C')).toMatchObject({ mode: 'pinned', effectiveReleaseId: 'P', newerAvailable: true });
    expect(resolveReleaseSelection('C', 'C').newerAvailable).toBe(false);
  });

  it('FreshnessBadge shows stale as a distinct message', () => {
    render(<FreshnessBadge state="stale" />);
    expect(screen.getByText('数据已过期')).toBeInTheDocument();
  });

  it('MetaStrip shows coverage gaps and exclusion reasons instead of hiding them', () => {
    render(<MetaStrip meta={meta({ freshness_state: 'stale', quality_status: 'partial' })} title="t" />);
    expect(screen.getByText('数据已过期')).toBeInTheDocument();
    expect(screen.getByText('部分缺失')).toBeInTheDocument();
    expect(screen.getByText(/覆盖 8\/10，缺失 2/)).toBeInTheDocument();
    expect(screen.getByText(/排除\/缺失：2 只停牌/)).toBeInTheDocument();
  });

  function fakeRel(over: Partial<ReleaseCtx>): ReleaseCtx {
    return {
      mode: 'pinned', effectiveReleaseId: 'aaaaaaaa-old', currentReleaseId: 'bbbbbbbb-new', newerAvailable: true, pinnedReleaseId: 'aaaaaaaa-old',
      pointer: {} as never, release: { data: undefined } as never, notice: null, dismissNotice: () => undefined,
      pinCurrent: () => undefined, follow: () => undefined, switchToLatest: () => undefined, ...over,
    } as ReleaseCtx;
  }

  it('ReleaseBar: pinned to an old release shows the newer-version notice and a switch button', () => {
    render(<ReleaseBar rel={fakeRel({})} />);
    expect(screen.getByText(/你正在查看固定的旧版本/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '切换到最新版本' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '固定此版本' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('ReleaseBar: follow mode with a version change shows the change notice; blocks from the old version read as updating', () => {
    const rel = fakeRel({ mode: 'follow', pinnedReleaseId: null, newerAvailable: false, effectiveReleaseId: 'bbbbbbbb-new', notice: { from: 'aaaaaaaa-old', to: 'bbbbbbbb-new' } });
    render(<ReleaseBar rel={rel} displayedReleaseId="aaaaaaaa-old" />);
    expect(screen.getByText(/已自动更新到新版本/)).toBeInTheDocument();
    expect(screen.getByText('正在更新，当前显示旧版本')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '跟随最新' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('PlanCard hides a plan built on another release instead of mixing it into the page', () => {
    const planMeta = meta({ release_id: 'other-release' });
    const plan = { based_on_release_id: 'other-release', strategy_refs: [], entry_conditions: {}, exit_conditions: {}, invalidation: {} } as unknown as TradePlan;
    const env: Envelope<TradePlan> = { meta: planMeta, data: plan };
    render(<PlanCard planEnv={env} kind="main" workspaceReleaseId="page-release" bars={null} quote={null} byInstance={new Map()} instanceById={new Map()} />);
    expect(screen.getByText(/基于另一个发布版本/)).toBeInTheDocument();
  });
});
