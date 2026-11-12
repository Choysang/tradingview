/**
 * Pinned list pagination (spec 11: "榜单分页必须固定 ranking_id/release_id").
 * Why: page 2 must continue the exact ranking page 1 came from. The reducer pins
 * the first page's identity and rejects any later page from another ranking /
 * release / actionability snapshot. Invariants: server order is preserved - this
 * module never sorts; duplicates (same key) are dropped, not reordered.
 * Not covered: fetching and cursor construction (the cursor is opaque).
 */

export interface PagePin {
  releaseId: string | null;
  rankingId: string | null;
  actionabilityId: string | null;
}

export interface PageInput<E> {
  pin: PagePin;
  entries: readonly E[];
  nextCursor: string | null;
}

export interface PagingState<E> {
  pin: PagePin | null;
  entries: E[];
  nextCursor: string | null;
  pages: number;
  /** Set when a page from a different identity was refused. */
  rejected: { reason: string; pin: PagePin } | null;
}

export function initialPaging<E>(): PagingState<E> {
  return { pin: null, entries: [], nextCursor: null, pages: 0, rejected: null };
}

function samePin(a: PagePin, b: PagePin): boolean {
  return a.releaseId === b.releaseId && a.rankingId === b.rankingId && a.actionabilityId === b.actionabilityId;
}

export function acceptPage<E>(
  state: PagingState<E>,
  page: PageInput<E>,
  keyOf: (e: E) => string,
): PagingState<E> {
  if (state.pin && !samePin(state.pin, page.pin)) {
    return {
      ...state,
      rejected: { reason: '榜单版本已变化：第二页不是同一份排名，已拒绝合并', pin: page.pin },
    };
  }
  const seen = new Set(state.entries.map(keyOf));
  const added = page.entries.filter((e) => {
    const k = keyOf(e);
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
  return {
    pin: state.pin ?? page.pin,
    entries: [...state.entries, ...added],
    nextCursor: page.nextCursor,
    pages: state.pages + 1,
    rejected: null,
  };
}

export function accumulatePages<E>(
  pages: readonly PageInput<E>[],
  keyOf: (e: E) => string,
): PagingState<E> {
  return pages.reduce((s, p) => acceptPage(s, p, keyOf), initialPaging<E>());
}
