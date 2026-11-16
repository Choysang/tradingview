/**
 * Pure helpers of the command palette and the keyboard shortcuts (triage #50/#51): page matching, key classification.
 * Why: the keyboard rules are easy to get wrong (typing a "/" into a field must not steal focus), so they are plain
 * functions with tests. Client only: nothing here reads or writes data.
 */
export interface PageItem { path: string; label: string }

/** Pages whose label or path contains every whitespace-separated term of the query (empty query = all pages). */
export function matchPages(pages: readonly PageItem[], query: string): PageItem[] {
  const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (terms.length === 0) return [...pages];
  return pages.filter((p) => terms.every((t) => p.label.toLowerCase().includes(t) || p.path.toLowerCase().includes(t)));
}

export function isEditableTarget(t: EventTarget | null): boolean {
  if (!(t instanceof HTMLElement)) return false;
  if (t.isContentEditable) return true;
  const tag = t.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';
}

export type ShortcutAction = 'toggle-palette' | 'focus-ask' | null;

interface KeyLike { key: string; ctrlKey: boolean; metaKey: boolean; altKey: boolean; shiftKey: boolean; target: EventTarget | null; isComposing?: boolean }

/** Ctrl/Cmd+K toggles the palette anywhere; "/" focuses the ask bar on /screen unless the user is already typing. */
export function shortcutFor(e: KeyLike, pathname: string): ShortcutAction {
  if (e.isComposing) return null;
  if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === 'k') return 'toggle-palette';
  if (e.key === '/' && !e.ctrlKey && !e.metaKey && !e.altKey && pathname === '/screen' && !isEditableTarget(e.target)) return 'focus-ask';
  return null;
}

/** Where a picked stock opens: the stock page, carrying the pinned release when the current page has one. */
export function stockTarget(instrumentId: string, release: string | null): string {
  return `/stock/${instrumentId}${release ? `?release=${release}` : ''}`;
}
