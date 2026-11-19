/** Client of the batch6 lane read (GET /v1/x/bf/batch6/overview). */
import { useQuery } from '@tanstack/react-query';
import { extGet } from '../../ext/a/api';
import type { Overview, Resp } from './types';

export const batch6Ops = {
  overview: (signal?: AbortSignal) => extGet<Overview>('/v1/x/bf/batch6/overview', {}, signal),
};

export function useOverview() {
  return useQuery<Resp<Overview>>({ queryKey: ['bf-batch6', 'overview'], queryFn: ({ signal }) => batch6Ops.overview(signal), staleTime: 60_000 });
}
