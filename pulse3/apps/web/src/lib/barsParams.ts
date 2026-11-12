/**
 * Builds getStockBars parameters from the frozen workspace response.
 * Why: snapshot_id / decision_as_of / availability_policy_version must be the ones the
 * analysis release used (spec 4: history uses snapshot + as_of + availability policy),
 * never "now". Not covered: calendars/aggregation (server side) - the client only
 * proposes a look-back window.
 */
import type { Envelope, Workspace } from '../api/types';
import { DEFAULT_ADJUSTMENT_VERSION, DEFAULT_SESSION_POLICY_VERSION } from '../config';
import type { ChartTimeframe } from '../hooks/useUserState';
import type { AdjustmentMode } from '../api/types';

export interface BarsParams {
  snapshot_id: string;
  decision_as_of: string;
  availability_policy_version: string;
  timeframe: ChartTimeframe;
  session_policy_version: string;
  adjustment_mode: AdjustmentMode;
  adjustment_version?: string;
  start: string;
  end: string;
}

const DAY = 86_400_000;
export const LOOKBACK_MS: Record<ChartTimeframe, number> = {
  '1m': 1 * DAY, '5m': 5 * DAY, '15m': 10 * DAY, '1h': 30 * DAY,
  '1d': 400 * DAY, '1w': 800 * DAY, '1mo': 1800 * DAY, '1y': 4000 * DAY,
};

export function isIntraday(tf: string): boolean {
  return /^[0-9]+(s|m|h)$/.test(tf);
}

/**
 * Choose the snapshot that can answer this request from the release's declared inputs. Raw intraday needs the
 * dataset of that timeframe, raw daily+ the daily raw dataset, adjusted modes the adjusted dataset. When nothing
 * matches the first bars dataset is returned and the server refuses with a structured 409 that the page shows.
 */
export function pickBarsSnapshot(refs: Envelope<Workspace>['meta']['input_snapshot_refs'], timeframe: string, mode: AdjustmentMode) {
  const bars = refs.filter((s) => /bar/i.test(s.dataset_id));
  const intraday = isIntraday(timeframe);
  const match = bars.find((s) => {
    const id = s.dataset_id.toLowerCase();
    if (mode !== 'raw') return id.includes('adjust') || id.includes('fwd');
    if (intraday) return id.includes('raw') && new RegExp(`_${timeframe}(_|$)`).test(id);
    return id.includes('raw') && /_1d(_|$)/.test(id) && !id.includes('adjust');
  });
  return match ?? bars[0] ?? refs[0];
}

/**
 * The workspace names, per instrument, the bars snapshot that actually holds its window
 * (chart_window_refs, ref form `bars:<snapshot_id>:<instrument_id>:<timeframe>:<mode>`). A release can
 * pin several per-batch snapshots, so this exact reference wins over a dataset-name guess.
 */
export function pickChartWindow(data: Workspace, timeframe: string, mode: AdjustmentMode): { snapshot_id: string } | undefined {
  const refs = (data.chart_window_refs ?? []).map((r) => r.ref.split(':'));
  const want = isIntraday(timeframe) ? timeframe : '1d';
  const hit = refs.find((p) => p[0] === 'bars' && p[2] === data.instrument.instrument_id && p[3] === want && p[4] === mode)
    ?? (isIntraday(timeframe) ? undefined : refs.find((p) => p[0] === 'bars' && p[2] === data.instrument.instrument_id && p[3] === '1d' && p[4] === mode));
  return hit && hit[1] ? { snapshot_id: hit[1] } : undefined;
}

export function deriveBarsParams(ws: Envelope<Workspace>, timeframe: ChartTimeframe, mode: AdjustmentMode): { ok: true; params: BarsParams } | { ok: false; reason: string } {
  const { meta, data } = ws;
  if (!meta.decision_as_of) return { ok: false, reason: '发布没有声明数据截点，无法确定图表窗口' };
  if (!meta.availability_policy_version) return { ok: false, reason: '发布没有声明可得性政策，无法确定图表窗口' };
  const snap = pickChartWindow(data, timeframe, mode) ?? pickBarsSnapshot(meta.input_snapshot_refs, timeframe, mode);
  if (!snap) return { ok: false, reason: '发布没有声明行情快照，无法读取K线' };
  const key = data.features[0]?.key;
  const end = meta.decision_as_of;
  const start = new Date(Date.parse(end) - LOOKBACK_MS[timeframe]).toISOString().replace(/\.\d{3}Z$/, 'Z');
  const params: BarsParams = {
    snapshot_id: snap.snapshot_id,
    decision_as_of: end,
    availability_policy_version: meta.availability_policy_version,
    timeframe,
    session_policy_version: key?.session_policy_version ?? DEFAULT_SESSION_POLICY_VERSION,
    adjustment_mode: mode,
    start,
    end,
  };
  if (mode !== 'raw') {
    const v = key && key.adjustment_mode === mode ? key.adjustment_version : DEFAULT_ADJUSTMENT_VERSION;
    if (v) params.adjustment_version = v;
  }
  return { ok: true, params };
}
