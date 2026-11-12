/**
 * Persistent user state - layouts, watchlists, annotations/drawings - ONLY through
 * the API (spec 4.2). TanStack cache holds the last server answer for display; a
 * cleared cache loses nothing. Writes carry expected_*_version (409 => reload) and
 * an Idempotency-Key. Not covered: deleting items (no delete operation in v3.0.0;
 * drawings are hidden by a new version instead).
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError, newIdempotencyKey } from '../api/client';
import { keys } from '../api/keys';
import { ops } from '../api/ops';
import type { Annotation, AdjustmentMode, Watchlist } from '../api/types';
import { LAYOUT_ID } from '../config';
import { useToast } from '../components/Toast';

// ---------------------------------------------------------------- layout

export type ChartTimeframe = '1m' | '5m' | '15m' | '1h' | '1d' | '1w' | '1mo' | '1y';

/** Client-defined layout document (contract leaves layout_document as a registered-later object). */
export interface LayoutDoc {
  schema: 'pulse.web.layout/1';
  timeframe: ChartTimeframe;
  adjustment: AdjustmentMode;
  panes: { volume: boolean; ma: boolean; ema: boolean; macd: boolean };
  evidenceTab: 'news' | 'community' | 'fund' | 'seats';
}

export const DEFAULT_LAYOUT: LayoutDoc = {
  schema: 'pulse.web.layout/1',
  timeframe: '1d',
  adjustment: 'raw',
  panes: { volume: true, ma: true, ema: false, macd: true },
  evidenceTab: 'news',
};

export function parseLayoutDoc(doc: unknown): LayoutDoc {
  const d = (doc ?? {}) as Partial<LayoutDoc>;
  if (d.schema !== 'pulse.web.layout/1') return DEFAULT_LAYOUT;
  return {
    ...DEFAULT_LAYOUT,
    ...d,
    panes: { ...DEFAULT_LAYOUT.panes, ...(d.panes ?? {}) },
  };
}

export function useLayout() {
  const qc = useQueryClient();
  const toast = useToast();
  const q = useQuery({
    queryKey: keys.layout(LAYOUT_ID),
    queryFn: async ({ signal }) => {
      try {
        return await ops.getLayout(LAYOUT_ID, { signal });
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return null; // nothing saved yet
        throw e;
      }
    },
    staleTime: 30_000,
  });
  const version = q.data?.data.layout_version ?? 0;
  const doc = parseLayoutDoc(q.data?.data.layout_document);

  const save = useMutation({
    mutationFn: (next: LayoutDoc) =>
      ops.saveLayout(LAYOUT_ID, { expected_layout_version: version, display_name: '我的工作台布局', layout_document: next as never }, newIdempotencyKey()),
    onSuccess: (res) => { qc.setQueryData(keys.layout(LAYOUT_ID), res); },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409) {
        toast.push('bad', '布局已在别处更新，已为你重新载入最新版本，请再操作一次。');
        void qc.invalidateQueries({ queryKey: keys.layout(LAYOUT_ID) });
      } else {
        toast.push('bad', `布局保存失败：${e.message}`);
      }
    },
  });
  return { doc, version, isLoading: q.isLoading, save: (next: LayoutDoc) => save.mutate(next), saving: save.isPending };
}

// ---------------------------------------------------------------- watchlists

export function useWatchlists() {
  const qc = useQueryClient();
  const toast = useToast();
  const q = useQuery({ queryKey: keys.watchlists, queryFn: ({ signal }) => ops.listWatchlists({ signal }), staleTime: 15_000 });
  const lists: Watchlist[] = q.data?.data.items ?? [];
  const primary = lists[0] ?? null;

  const save = useMutation({
    mutationFn: async (vars: { next: Array<{ instrument_id: string; note: string | null; added_release_id: string | null }> }) => {
      const id = primary?.watchlist_id ?? crypto.randomUUID();
      return ops.saveWatchlist(id, {
        expected_watchlist_version: primary?.watchlist_version ?? 0,
        display_name: primary?.display_name ?? '我的自选',
        description: primary?.description ?? null,
        members: vars.next.map((m, i) => ({ instrument_id: m.instrument_id, sort_position: i + 1, note: m.note, added_release_id: m.added_release_id })),
      }, newIdempotencyKey());
    },
    onSuccess: () => { void qc.invalidateQueries({ queryKey: keys.watchlists }); },
    onError: (e) => {
      toast.push('bad', e instanceof ApiError && e.status === 409 ? '自选已在别处更新，已重新载入，请再操作一次。' : `自选保存失败：${e.message}`);
      void qc.invalidateQueries({ queryKey: keys.watchlists });
    },
  });

  const has = (instrumentId: string): boolean => lists.some((l) => l.members.some((m) => m.instrument_id === instrumentId));
  const toggle = (instrumentId: string, releaseId: string | null): void => {
    const cur = (primary?.members ?? []).map((m) => ({ instrument_id: m.instrument_id, note: m.note, added_release_id: m.added_release_id }));
    const next = cur.some((m) => m.instrument_id === instrumentId)
      ? cur.filter((m) => m.instrument_id !== instrumentId)
      : [...cur, { instrument_id: instrumentId, note: null, added_release_id: releaseId }];
    save.mutate({ next });
  };
  return { lists, primary, has, toggle, isLoading: q.isLoading, error: q.error, saving: save.isPending };
}

// ---------------------------------------------------------------- annotations

export interface NoteBody { kind: 'note'; text: string }
export interface HlineBody { kind: 'hline'; price: string; label: string; adjustment_mode: AdjustmentMode; hidden?: boolean }

export function useAnnotations(instrumentId: string) {
  const qc = useQueryClient();
  const toast = useToast();
  const q = useQuery({ queryKey: keys.annotations(instrumentId), queryFn: ({ signal }) => ops.listAnnotations({ instrument_id: instrumentId }, { signal }), staleTime: 15_000 });
  const items: Annotation[] = q.data?.data.items ?? [];

  const save = useMutation({
    mutationFn: (v: { existing?: Annotation; kind: Annotation['annotation_kind']; body: Record<string, unknown>; releaseId: string | null; snapshotId: string | null }) =>
      ops.saveAnnotation(v.existing?.annotation_id ?? crypto.randomUUID(), {
        expected_annotation_version: v.existing?.annotation_version ?? 0,
        instrument_id: instrumentId, watchlist_id: null, annotation_kind: v.kind,
        anchor_release_id: v.releaseId, anchor_snapshot_id: v.snapshotId, body: v.body as never,
      }, newIdempotencyKey()),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: keys.annotations(instrumentId) }); },
    onError: (e) => {
      toast.push('bad', e instanceof ApiError && e.status === 409 ? '标注已在别处更新，已重新载入。' : `标注保存失败：${e.message}`);
      void qc.invalidateQueries({ queryKey: keys.annotations(instrumentId) });
    },
  });
  return { items, isLoading: q.isLoading, error: q.error, save: save.mutate, saving: save.isPending };
}
