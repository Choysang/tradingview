/**
 * Display name for an instrument id. The contract's ranking entries carry only
 * instrument_id, so names come from the (cached) public workspace of the same
 * release - a documented gap (docs/web/M4_evidence.md G1). Falls back to a short id.
 */
import { useQuery } from '@tanstack/react-query';
import { workspaceQuery } from '../api/keys';

export interface InstrumentLabel {
  name: string;
  symbol: string | null;
  isRisk: boolean;
  tradingStatus: string | null;
  loading: boolean;
}

export function useInstrumentLabel(instrumentId: string, releaseId: string | null): InstrumentLabel {
  const q = useQuery(workspaceQuery(instrumentId, releaseId));
  const inst = q.data?.data.instrument;
  return {
    name: inst?.display_name ?? `证券 ${instrumentId.slice(0, 8)}`,
    symbol: inst?.source_symbols[0]?.source_symbol ?? null,
    isRisk: inst?.is_risk_warning ?? false,
    tradingStatus: inst?.trading_status ?? null,
    loading: q.isLoading,
  };
}
