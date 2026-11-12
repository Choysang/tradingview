/**
 * Cursor-paged list pinned to one ranking / release / actionability identity.
 * Wraps useInfiniteQuery; pages are folded by lib/pagination.accumulatePages, which
 * refuses a page from a different identity and never reorders. While the release
 * changes (follow mode) the previous list stays on screen flagged stale
 * (keepPreviousData) instead of being mixed with the new one.
 */
import { keepPreviousData, useInfiniteQuery } from '@tanstack/react-query';
import { useMemo } from 'react';
import type { ResponseMeta } from '../api/types';
import { accumulatePages, type PageInput, type PagePin, type PagingState } from '../lib/pagination';

export interface PinnedPagesOptions<TPage extends { meta: ResponseMeta }, E> {
  queryKey: readonly unknown[];
  fetchPage: (cursor: string | undefined, signal: AbortSignal) => Promise<TPage>;
  pinOf: (p: TPage) => PagePin;
  entriesOf: (p: TPage) => readonly E[];
  keyOf: (e: E) => string;
  enabled?: boolean;
}

export interface PinnedPages<TPage extends { meta: ResponseMeta }, E> {
  paging: PagingState<E>;
  pages: TPage[];
  firstPage: TPage | undefined;
  hasMore: boolean;
  loadMore: () => void;
  reload: () => void;
  isLoading: boolean;
  isFetchingMore: boolean;
  /** true when the shown data belongs to an earlier query key (version switching) */
  isPlaceholder: boolean;
  error: Error | null;
}

export function usePinnedPages<TPage extends { meta: ResponseMeta }, E>(o: PinnedPagesOptions<TPage, E>): PinnedPages<TPage, E> {
  const q = useInfiniteQuery({
    queryKey: o.queryKey,
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam, signal }) => o.fetchPage(pageParam, signal),
    getNextPageParam: (last: TPage) => last.meta.next_cursor ?? undefined,
    placeholderData: keepPreviousData,
    enabled: o.enabled ?? true,
  });
  const pages = q.data?.pages ?? [];
  const paging = useMemo(
    () => accumulatePages<E>(pages.map((p): PageInput<E> => ({ pin: o.pinOf(p), entries: o.entriesOf(p), nextCursor: p.meta.next_cursor })), o.keyOf),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [q.data],
  );
  return {
    paging,
    pages,
    firstPage: pages[0],
    hasMore: !!q.hasNextPage && !paging.rejected,
    loadMore: () => { void q.fetchNextPage(); },
    reload: () => { void q.refetch(); },
    isLoading: q.isLoading,
    isFetchingMore: q.isFetchingNextPage,
    isPlaceholder: q.isPlaceholderData,
    error: q.error,
  };
}
