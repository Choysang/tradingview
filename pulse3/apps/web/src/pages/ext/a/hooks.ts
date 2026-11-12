/**
 * Shared hooks of the extension-A pages: the pinned market-today query, panel artifact resolution and the release-switch
 * subscription (PEX16 stream). A `decision.published` event or a `reset` refetches the whole page; nothing is merged.
 */
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { keys } from '../../../api/keys';
import { SLOT_KEY } from '../../../config';
import { extOps, updatesUrl, useArtifact, useEventStream, type StreamState } from './api';
import type { QueryMarketTodayData, Resp, StreamEnvelope, TypedFeature } from './types';

export const PANEL_IDS = ['thermometer', 'vitals', 'ladder', 'breadth', 'themes', 'growth_strip', 'regime'] as const;
export type PanelId = (typeof PANEL_IDS)[number];

export function useMarketToday(releaseId: string | null) {
  return useQuery<Resp<QueryMarketTodayData>>({
    queryKey: ['ext-a', 'today', releaseId],
    queryFn: ({ signal }) => extOps.queryMarketToday(releaseId as string, null, signal),
    enabled: !!releaseId,
    staleTime: 10_000,
  });
}

export function featureOf(data: QueryMarketTodayData | undefined, panel: PanelId): TypedFeature | undefined {
  return data?.state.find((f) => f.feature_id === `market_state.${panel}`);
}

export function usePanel<T>(data: QueryMarketTodayData | undefined, panel: PanelId) {
  const f = featureOf(data, panel);
  const q = useArtifact<T>(f?.result.artifact?.ref);
  return { feature: f, body: q.data?.body, isLoading: q.isLoading, error: q.error };
}

/** Subscribes to the registered update stream; returns the connection state. */
export function useReleaseStream(): StreamState {
  const qc = useQueryClient();
  return useEventStream(updatesUrl(SLOT_KEY), ['update', 'reset'], (name, data) => {
    const env = data as Partial<StreamEnvelope>;
    if (name === 'update' && env.notification?.event_type !== 'decision.published') return;
    void qc.invalidateQueries({ queryKey: keys.currentRelease });
    void qc.invalidateQueries({ queryKey: ['ext-a', 'today'] });
    void qc.invalidateQueries({ queryKey: ['ext-a', 'history'] });
  });
}

export const STREAM_LABEL: Record<StreamState, string> = { connecting: '连接中', open: '实时推送已连接', retrying: '断线重连中', closed: '推送不可用（改用轮询）' };
