import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it } from 'vitest';
import { usePinnedPages } from '../src/hooks/usePinnedPages';
import { acceptPage, accumulatePages, initialPaging, type PageInput } from '../src/lib/pagination';

interface E { id: string; rank: number }
const key = (e: E): string => e.id;
const pin = (release: string, ranking: string) => ({ releaseId: release, rankingId: ranking, actionabilityId: null });
const page = (release: string, ranking: string, entries: E[], next: string | null): PageInput<E> => ({ pin: pin(release, ranking), entries, nextCursor: next });

describe('pinned pagination reducer', () => {
  it('keeps server order across pages and never re-sorts (page 2 may hold better ranks)', () => {
    const p1 = page('R1', 'K1', [{ id: 'a', rank: 5 }, { id: 'b', rank: 4 }], 'c1');
    const p2 = page('R1', 'K1', [{ id: 'c', rank: 9 }, { id: 'd', rank: 1 }], null);
    const s = accumulatePages([p1, p2], key);
    expect(s.entries.map((e) => e.id)).toEqual(['a', 'b', 'c', 'd']);
    expect(s.rejected).toBeNull();
    expect(s.nextCursor).toBeNull();
    expect(s.pages).toBe(2);
  });

  it('rejects a second page that belongs to a different ranking_id and keeps page 1 intact', () => {
    let s = acceptPage(initialPaging<E>(), page('R1', 'K1', [{ id: 'a', rank: 1 }], 'c1'), key);
    s = acceptPage(s, page('R1', 'K2', [{ id: 'z', rank: 1 }], null), key);
    expect(s.rejected?.reason).toContain('榜单版本已变化');
    expect(s.entries.map((e) => e.id)).toEqual(['a']);
    expect(s.nextCursor).toBe('c1');
  });

  it('rejects a page from another release_id even if ranking_id matches', () => {
    let s = acceptPage(initialPaging<E>(), page('R1', 'K1', [{ id: 'a', rank: 1 }], 'c1'), key);
    s = acceptPage(s, page('R2', 'K1', [{ id: 'b', rank: 2 }], null), key);
    expect(s.rejected).not.toBeNull();
    expect(s.entries).toHaveLength(1);
  });

  it('pins actionability_id for account lists', () => {
    const mk = (act: string): PageInput<E> => ({ pin: { releaseId: 'R1', rankingId: 'K1', actionabilityId: act }, entries: [{ id: act, rank: 1 }], nextCursor: null });
    const s = accumulatePages([mk('A1'), mk('A2')], key);
    expect(s.rejected).not.toBeNull();
    expect(s.entries.map((e) => e.id)).toEqual(['A1']);
  });

  it('drops duplicate keys without reordering the rest', () => {
    const s = accumulatePages([page('R1', 'K1', [{ id: 'a', rank: 1 }, { id: 'b', rank: 2 }], 'c'), page('R1', 'K1', [{ id: 'b', rank: 2 }, { id: 'c', rank: 3 }], null)], key);
    expect(s.entries.map((e) => e.id)).toEqual(['a', 'b', 'c']);
  });
});

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe('usePinnedPages (hook)', () => {
  it('loads page 2 with the cursor of page 1 and refuses a page from another ranking', async () => {
    const calls: Array<string | undefined> = [];
    const pages: Record<string, { meta: { next_cursor: string | null }; r: string; items: string[] }> = {
      first: { meta: { next_cursor: 'p2' }, r: 'K1', items: ['x', 'y'] },
      p2: { meta: { next_cursor: null }, r: 'K-OTHER', items: ['q'] },
    };
    const { result } = renderHook(() => usePinnedPages({
      queryKey: ['t'],
      fetchPage: async (cursor: string | undefined) => { calls.push(cursor); return pages[cursor ?? 'first'] as never; },
      pinOf: (p: { r: string }) => ({ releaseId: 'R1', rankingId: p.r, actionabilityId: null }),
      entriesOf: (p: { items: string[] }) => p.items,
      keyOf: (e: string) => e,
    } as never), { wrapper });
    await waitFor(() => expect(result.current.paging.entries).toEqual(['x', 'y']));
    expect(result.current.hasMore).toBe(true);
    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.paging.rejected).not.toBeNull());
    expect(calls).toEqual([undefined, 'p2']);
    expect(result.current.paging.entries).toEqual(['x', 'y']);
    expect(result.current.hasMore).toBe(false);
  });
});
