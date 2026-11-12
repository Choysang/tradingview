/**
 * Strategy registry lookup (instance_id -> strategy) used for evidence grades and for
 * classifying plans. Reads listStrategies pages until exhausted (bounded).
 */
import { useQuery } from '@tanstack/react-query';
import { ops } from '../api/ops';
import type { Strategy, StrategyInstance } from '../api/types';

export interface StrategyCatalog {
  strategies: Strategy[];
  byInstance: Map<string, Strategy>;
  instanceById: Map<string, StrategyInstance>;
}

const MAX_PAGES = 20;

export function useStrategyCatalog() {
  return useQuery({
    queryKey: ['strategies', 'catalog'],
    staleTime: 60_000,
    queryFn: async ({ signal }): Promise<StrategyCatalog> => {
      const strategies: Strategy[] = [];
      let cursor: string | undefined;
      for (let i = 0; i < MAX_PAGES; i += 1) {
        const res = await ops.listStrategies({ cursor }, { signal });
        strategies.push(...res.data.items);
        cursor = res.meta.next_cursor ?? undefined;
        if (!cursor) break;
      }
      const byInstance = new Map<string, Strategy>();
      const instanceById = new Map<string, StrategyInstance>();
      for (const s of strategies) for (const i of s.instances) { byInstance.set(i.instance_id, s); instanceById.set(i.instance_id, i); }
      return { strategies, byInstance, instanceById };
    },
  });
}
