/**
 * Tracks whether any response carried the SYNTHETIC_FIXTURE warning, so the
 * "合成样本" banner shows even if a non-mock server serves synthetic data (M4 rule:
 * synthetic data must never be mistaken for real selection results).
 */
import { useSyncExternalStore } from 'react';
import { USE_MOCK } from '../config';

let seen = USE_MOCK;
const listeners = new Set<() => void>();

export function noteResponseMeta(meta: { warnings?: Array<{ code: string }> } | undefined): void {
  if (seen || !meta?.warnings) return;
  if (meta.warnings.some((w) => w.code === 'SYNTHETIC_FIXTURE')) {
    seen = true;
    listeners.forEach((l) => l());
  }
}

export function useSyntheticSeen(): boolean {
  return useSyncExternalStore(
    (cb) => { listeners.add(cb); return () => { listeners.delete(cb); }; },
    () => seen,
    () => seen,
  );
}

export function resetSyntheticForTests(value = false): void {
  seen = value;
  listeners.forEach((l) => l());
}
