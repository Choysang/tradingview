/**
 * "Follow latest" vs "pinned version" selection (spec 11).
 * The pin lives in the URL (?release=<id>) so links carry the release (个股跳转携带
 * 当前 release_id) and reload keeps the same version. Following uses the current
 * pointer; when it moves the page switches as a whole (blocks keep showing their own
 * release until the new one arrives and are flagged stale meanwhile).
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import { currentReleaseQuery, releaseQuery } from '../api/keys';
import { resolveReleaseSelection, type ReleaseSelection } from '../lib/version';

export function useReleaseSelection() {
  const [sp, setSp] = useSearchParams();
  const pinned = sp.get('release');
  const pointer = useQuery(currentReleaseQuery());
  const currentId = pointer.data?.data.current_release_id ?? null;
  const selection = resolveReleaseSelection(pinned, currentId);
  const release = useQuery(releaseQuery(selection.effectiveReleaseId));

  const pin = useCallback((id: string | null) => {
    setSp((prev) => { const n = new URLSearchParams(prev); if (id) n.set('release', id); else n.delete('release'); return n; }, { replace: true });
  }, [setSp]);

  // version-change notice: only while following
  const prev = useRef<string | null>(null);
  const [notice, setNotice] = useState<{ from: string; to: string } | null>(null);
  useEffect(() => {
    if (!pinned && currentId && prev.current && prev.current !== currentId) setNotice({ from: prev.current, to: currentId });
    if (currentId) prev.current = currentId;
  }, [currentId, pinned]);

  return {
    ...selection,
    pinnedReleaseId: pinned,
    pointer,
    release,
    notice,
    dismissNotice: () => setNotice(null),
    pinCurrent: () => pin(selection.effectiveReleaseId),
    follow: () => pin(null),
    switchToLatest: () => pin(null),
  };
}

export type ReleaseCtx = ReturnType<typeof useReleaseSelection>;
