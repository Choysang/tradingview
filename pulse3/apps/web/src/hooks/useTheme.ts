/**
 * Light / dark / auto theme. Stored in localStorage as a harmless UI preference only
 * (spec product 6: browser-local state is limited to unimportant UI preferences;
 * layouts, watchlists and drawings go through the API). Storage access is guarded.
 */
import { useCallback, useEffect, useState } from 'react';

export type ThemeChoice = 'auto' | 'light' | 'dark';
const KEY = 'pulse3.theme';

function read(): ThemeChoice {
  try {
    const v = localStorage.getItem(KEY);
    return v === 'light' || v === 'dark' ? v : 'auto';
  } catch {
    return 'auto';
  }
}

export function useTheme(): { choice: ThemeChoice; cycle: () => void; version: number } {
  const [choice, setChoice] = useState<ThemeChoice>(read);
  const [version, setVersion] = useState(0);

  useEffect(() => {
    const root = document.documentElement;
    if (choice === 'auto') root.removeAttribute('data-theme'); else root.setAttribute('data-theme', choice);
    setVersion((v) => v + 1);
  }, [choice]);

  useEffect(() => {
    const mq = window.matchMedia?.('(prefers-color-scheme: dark)');
    if (!mq) return undefined;
    const on = (): void => setVersion((v) => v + 1);
    mq.addEventListener('change', on);
    return () => mq.removeEventListener('change', on);
  }, []);

  const cycle = useCallback(() => {
    setChoice((c) => {
      const next: ThemeChoice = c === 'auto' ? 'light' : c === 'light' ? 'dark' : 'auto';
      try { localStorage.setItem(KEY, next); } catch { /* storage unavailable: preference just is not remembered */ }
      return next;
    });
  }, []);

  return { choice, cycle, version };
}
