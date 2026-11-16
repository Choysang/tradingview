/**
 * Reads the server indicator series for the candles on screen and turns them into drawable series.
 * Why: MA/EMA/BOLL/MACD/KDJ/RSI are computed on the server (`/v1/x/bf/stock/indicators`) from the same bars the chart shows;
 * the page only decides what to request and whether the answer belongs to the candles it draws (window_hash check).
 * Invariants: nothing is computed here; a payload that does not match the candle window is not drawn and the reason is shown.
 */
import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import type { BarWindow } from '../../../api/types';
import type { AdjustmentMode } from '../../../api/types';
import { bfStock, type StockIndicators, type SubPaneId } from './api';
import {
  checkAlignment, overlaySeries, requestedSets, subPaneSeries, warmupNote, type Alignment, type DrawnSeries, type OverlayToggles,
} from './indicatorDraw';

export interface IndicatorView {
  enabled: boolean;
  isLoading: boolean;
  error: unknown;
  refetch: () => void;
  data: StockIndicators | null;
  alignment: Alignment;
  overlays: DrawnSeries[];
  subSeries: DrawnSeries[];
  warmup: string | null;
}

const NONE: Alignment = { ok: false, reason: '指标或K线尚未读取' };

export function useStockIndicators(a: {
  instrumentId: string; releaseId: string | null; timeframe: string; adjustment: AdjustmentMode; win: BarWindow | null;
  on: OverlayToggles; sub: SubPaneId | 'none';
}): IndicatorView {
  const sets = useMemo(() => requestedSets(a.on, a.sub), [a.on.ma, a.on.ema, a.on.boll, a.sub]); // eslint-disable-line react-hooks/exhaustive-deps
  const enabled = sets.length > 0 && !!a.win && !!a.releaseId;
  const q = useQuery({
    queryKey: ['bf-stock', 'indicators', a.instrumentId, a.releaseId, a.timeframe, a.adjustment, sets.join(',')],
    queryFn: ({ signal }) => bfStock.indicators({ instrument_id: a.instrumentId, release_id: a.releaseId, timeframe: a.timeframe, adjustment: a.adjustment, set: sets }, signal),
    enabled,
    placeholderData: (p) => p,
    staleTime: 60_000,
  });
  const data = enabled ? q.data?.data ?? null : null;
  const alignment = useMemo(() => (enabled ? checkAlignment(data, a.win) : NONE), [enabled, data, a.win]);
  const overlays = useMemo(() => (data && alignment.ok ? overlaySeries(data, a.on) : []), [data, alignment, a.on.ma, a.on.ema, a.on.boll]); // eslint-disable-line react-hooks/exhaustive-deps
  const subSeries = useMemo(() => (data && alignment.ok && a.sub !== 'none' ? subPaneSeries(data, a.sub) : []), [data, alignment, a.sub]);
  const warmup = useMemo(() => (data && alignment.ok ? warmupNote(data, [...overlays, ...subSeries]) : null), [data, alignment, overlays, subSeries]);
  return { enabled, isLoading: enabled && q.isLoading, error: q.error, refetch: () => void q.refetch(), data, alignment, overlays, subSeries, warmup };
}
