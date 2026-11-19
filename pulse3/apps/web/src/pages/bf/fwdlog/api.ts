/** Client of the fwdlog lane reads (GET /v1/x/bf/fwdlog/*); query parameters are exactly the names below (drift-guarded by the tests). */
import { useQuery } from '@tanstack/react-query';
import { extGet } from '../../ext/a/api';
import type { ExperimentKey, ExperimentsData, Resp, RowsData } from './types';

export const REQUEST_FIELDS = { rows: ['from', 'to', 'limit', 'offset'] } as const;

export const fwdlogOps = {
  experiments: (signal?: AbortSignal) => extGet<ExperimentsData>('/v1/x/bf/fwdlog/experiments', {}, signal),
  rows: (key: ExperimentKey, limit: number, signal?: AbortSignal) => extGet<RowsData>(`/v1/x/bf/fwdlog/experiments/${key}/rows`, { limit }, signal),
};

export function useExperiments() {
  return useQuery<Resp<ExperimentsData>>({ queryKey: ['bf-fwdlog', 'experiments'], queryFn: ({ signal }) => fwdlogOps.experiments(signal), staleTime: 60_000 });
}

/** The daily rows are read only once a card is opened. */
export function useRows(key: ExperimentKey, enabled: boolean, limit = 120) {
  return useQuery<Resp<RowsData>>({ queryKey: ['bf-fwdlog', 'rows', key, limit], queryFn: ({ signal }) => fwdlogOps.rows(key, limit, signal), enabled, staleTime: 60_000 });
}
