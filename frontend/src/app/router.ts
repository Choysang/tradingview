/**
 * URL ⇄ workspace. Every screen state worth sharing has an address:
 *   #/screener            a view
 *   #/overview?s=002085   a view with the stock sheet open
 * An agent (or a colleague) can open the exact screen from a link.
 */
import { useEffect } from 'react';
import { flushSync } from 'react-dom';
import { useWorkspace } from '../core/store';
import type { View } from '../core/store';

export const VIEW_IDS: View[] = ['overview', 'picks', 'playbooks', 'regime', 'screener', 'themes', 'patterns', 'lab', 'watchlist'];

function parse(hash: string): { view: View | null; stock: string | null; playbook: string | null } {
  const [path, query = ''] = hash.replace(/^#\/?/, '').split('?');
  const view = VIEW_IDS.includes(path as View) ? (path as View) : null;
  const params = new URLSearchParams(query);
  const stock = params.get('s');
  const playbook = params.get('p');
  return {
    view,
    stock: stock && /^\d{6}$/.test(stock) ? stock : null,
    playbook: playbook && /^[a-z0-9_]{2,40}$/.test(playbook) ? playbook : null,
  };
}

function format(view: View, stock: string | null, playbook: string | null) {
  const params = new URLSearchParams();
  if (view === 'playbooks' && playbook) params.set('p', playbook);
  if (stock) params.set('s', stock);
  const query = params.toString();
  return `#/${view}${query ? `?${query}` : ''}`;
}

/** Switch views inside a view transition when the browser supports it. */
export function navigate(view: View) {
  const apply = () => useWorkspace.getState().setView(view);
  if (view === useWorkspace.getState().view) return;
  const motionOk = !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (document.startViewTransition && motionOk) {
    document.startViewTransition(() => flushSync(apply));
  } else {
    apply();
  }
  document.getElementById('main')?.scrollTo({ top: 0 });
}

export function useHashSync() {
  useEffect(() => {
    const fromHash = () => {
      const { view, stock, playbook } = parse(window.location.hash);
      const state = useWorkspace.getState();
      if (view && view !== state.view) state.setView(view);
      if (stock !== state.stock) state.openStock(stock);
      if (view === 'playbooks' && playbook && playbook !== state.playbook) state.selectPlaybook(playbook);
    };
    fromHash();
    window.addEventListener('hashchange', fromHash);
    const unsubscribe = useWorkspace.subscribe((state, previous) => {
      if (state.view === previous.view && state.stock === previous.stock && state.playbook === previous.playbook) return;
      const next = format(state.view, state.stock, state.playbook);
      if (window.location.hash === next) return;
      if (state.view !== previous.view) window.history.pushState(null, '', next);
      else window.history.replaceState(null, '', next);
    });
    return () => {
      window.removeEventListener('hashchange', fromHash);
      unsubscribe();
    };
  }, []);
}

/** Digits switch views, `/` jumps to the ask bar, ⌘K opens the palette. */
export function useShortcuts() {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        useWorkspace.setState(state => ({ commandOpen: !state.commandOpen }));
        return;
      }
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target as HTMLElement | null;
      if (target && (/^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName) || target.isContentEditable)) return;
      const { commandOpen, sourcesOpen, stock } = useWorkspace.getState();
      if (commandOpen || sourcesOpen || stock) return;
      if (event.key === '/') {
        event.preventDefault();
        navigate('screener');
        window.setTimeout(() => document.getElementById('ask-input')?.focus(), 60);
        return;
      }
      const index = Number(event.key);
      if (Number.isInteger(index) && index >= 1 && index <= VIEW_IDS.length) navigate(VIEW_IDS[index - 1]);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
}
