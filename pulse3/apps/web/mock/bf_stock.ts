/**
 * Mock for the stock backfill reads (`/v1/x/bf/stock/indicators`, `/v1/x/bf/stock/stats`), dev build only (`pnpm dev:mock`).
 * Why: lets the stock page be developed against clearly marked synthetic, contract-valid answers. The series are computed from the
 * synthetic bars of the SAME window the mock bars handler serves (so window_hash matches and the page draws them); the real server
 * computes them with `pulse.domain.features`. Invariants: warm-up values are null; seal fields are unavailable with a reason.
 * Not covered: real data. Output is checked against backfill_stock.schema.json by `pnpm mock:validate:bf` (scripts/validate_bf_stock_mock.py).
 */
import type { IndicatorSet, IndicatorSeries, StatField, StockIndicators, StockStats } from '../src/pages/bf/stock/api';
import { INSTRUMENTS, buildBarWindow, instrumentById, type ReleaseRec } from './market.ts';
import { buildMeta, dec, errorResponse, hash64, iso, type MockResponse } from './util.ts';

export const INSTRUMENT_ID = (INSTRUMENTS[0] as { id: string }).id;
export const RELEASE_ID = 'mock-release';
const SESSION = 'cn-a-continuous-v1';
const DAY = 86_400_000;
/** Mirror of LOOKBACK_MS in src/lib/barsParams.ts (not imported: that module reads import.meta.env, which the vite config cannot). */
const LOOKBACK_MS: Record<string, number> = { '1m': DAY, '5m': 5 * DAY, '15m': 10 * DAY, '1h': 30 * DAY, '1d': 400 * DAY, '1w': 800 * DAY, '1mo': 1800 * DAY, '1y': 4000 * DAY };
const ALL_SETS: IndicatorSet[] = ['ma', 'ema', 'boll', 'macd', 'kdj', 'rsi'];

type Num = number | null;
const sma = (xs: Num[], n: number): Num[] => xs.map((_, i) => {
  if (i < n - 1) return null;
  const w = xs.slice(i - n + 1, i + 1);
  return w.some((v) => v === null) ? null : (w as number[]).reduce((a, b) => a + b, 0) / n;
});
const ema = (xs: Num[], n: number): Num[] => {
  const out: Num[] = xs.map(() => null);
  const a = 2 / (n + 1);
  let prev: number | null = null;
  xs.forEach((v, i) => {
    if (v === null) { prev = null; return; }
    if (prev === null) {
      const w = xs.slice(i - n + 1, i + 1);
      if (i >= n - 1 && w.every((x) => x !== null)) { prev = (w as number[]).reduce((s, x) => s + x, 0) / n; out[i] = prev; }
      return;
    }
    prev += a * (v - prev);
    out[i] = prev;
  });
  return out;
};
const wave = (i: number, first: number, amp: number, mid: number): Num => (i < first ? null : mid + amp * Math.sin(i / 5));
const wire = (v: Num): string | null => (v === null || !Number.isFinite(v) ? null : dec(v, 6));

function series(key: string, indicator: IndicatorSet, output: string, pane: 'price' | 'sub', label: string, params: Record<string, number>, warm: number, values: Num[]): IndicatorSeries {
  const w = values.map(wire);
  const idx = w.findIndex((v) => v !== null);
  return { key, indicator, output, pane, label, params, definition_version: 'v1', implementation_digest: hash64(`impl:${key}`), warmup_bars: warm,
    first_valid_index: idx < 0 ? null : idx, valid_count: w.filter((v) => v !== null).length, values: w };
}

function seriesFor(set: IndicatorSet, close: Num[]): IndicatorSeries[] {
  const n = close.length;
  switch (set) {
    case 'ma': return [5, 10, 20, 60].map((p) => series(`ma${p}`, 'ma', 'ma', 'price', `MA${p}`, { n: p }, p - 1, sma(close, p)));
    case 'ema': return [12, 26].map((p) => series(`ema${p}`, 'ema', 'ema', 'price', `EMA${p}`, { n: p }, p - 1, ema(close, p)));
    case 'boll': {
      const mid = sma(close, 20);
      const sd = (i: number): Num => {
        const m = mid[i];
        if (m === null || m === undefined) return null;
        const w = close.slice(i - 19, i + 1) as number[];
        return Math.sqrt(w.reduce((s, v) => s + (v - m) ** 2, 0) / 20);
      };
      const up = mid.map((m, i) => (m === null ? null : m + 2 * (sd(i) as number)));
      const lo = mid.map((m, i) => (m === null ? null : m - 2 * (sd(i) as number)));
      const p = { n: 20, k: 2 };
      return [series('boll_mid', 'boll', 'mid', 'price', 'BOLL中轨', p, 19, mid), series('boll_upper', 'boll', 'upper', 'price', 'BOLL上轨', p, 19, up), series('boll_lower', 'boll', 'lower', 'price', 'BOLL下轨', p, 19, lo)];
    }
    case 'macd': {
      const f = ema(close, 12); const s = ema(close, 26);
      const dif = f.map((v, i) => (v === null || s[i] === null ? null : v - (s[i] as number)));
      const dea = ema(dif, 9);
      const hist = dif.map((v, i) => (v === null || dea[i] === null ? null : v - (dea[i] as number)));
      const p = { fast: 12, slow: 26, signal: 9 };
      return [series('macd', 'macd', 'macd', 'sub', 'DIF', p, 25, dif), series('macd_signal', 'macd', 'signal', 'sub', 'DEA', p, 33, dea), series('macd_hist', 'macd', 'hist', 'sub', 'MACD柱', p, 33, hist)];
    }
    case 'kdj': {
      const k = Array.from({ length: n }, (_, i) => wave(i, 8, 25, 50));
      const d = Array.from({ length: n }, (_, i) => wave(i + 2, 8 + 2, 18, 50));
      const j = k.map((v, i) => (v === null || d[i] === null ? null : 3 * v - 2 * (d[i] as number)));
      return [series('kdj_k', 'kdj', 'k', 'sub', 'K', { n: 9 }, 8, k), series('kdj_d', 'kdj', 'd', 'sub', 'D', { n: 9 }, 8, d), series('kdj_j', 'kdj', 'j', 'sub', 'J', { n: 9 }, 8, j)];
    }
    case 'rsi': return [6, 14].map((p) => series(`rsi${p}`, 'rsi', 'rsi', 'sub', `RSI${p}`, { n: p }, p, Array.from({ length: n }, (_, i) => wave(i, p, 22, 50))));
    default: return [];
  }
}

export function buildIndicators(o: { timeframe: string; bars: number; set: IndicatorSet[] }): StockIndicators {
  const bars = Array.from({ length: o.bars }, (_, i) => {
    const day = new Date(Date.UTC(2026, 0, 5 + i * 7 / 5 | 0));
    const open = Date.UTC(2026, 0, 5, 1, 30) + i * 300_000;
    const intraday = /^[0-9]+(s|m|h)$/.test(o.timeframe);
    return { bar_open_time: iso(intraday ? open : day.getTime() + 3_600_000), trade_date: intraday ? '2026-01-05' : day.toISOString().slice(0, 10), is_final: true };
  });
  const close = bars.map((_, i) => 10 + Math.sin(i / 6) * 2 + i * 0.03);
  return {
    instrument_id: INSTRUMENT_ID, release_id: RELEASE_ID, decision_as_of: iso(Date.UTC(2026, 10, 12, 7)), availability_policy_version: 'synthetic-observed-clock-v1',
    timeframe: o.timeframe, adjustment_mode: 'raw', adjustment_version: null, snapshot_id: hash64('snap').slice(0, 8) + '-0000-4000-8000-000000000000',
    window_hash: hash64('window'), window_start: iso(Date.UTC(2025, 0, 1)), window_end: iso(Date.UTC(2026, 10, 12, 7)), window_complete: true, numeric_policy: 'float64-v1',
    requested: ALL_SETS.filter((s) => o.set.includes(s)), bars, series: ALL_SETS.filter((s) => o.set.includes(s)).flatMap((s) => seriesFor(s, close)),
  };
}

// ---- statistics ----------------------------------------------------------------------------------------------------
const NO_INTRADAY = 'pulse3 尚未采集分时与盘口记录，日线无法还原这个数值，这里不推断、不填充';
const ok = (key: StatField['key'], label: string, value: string, kind: StatField['value_kind'], unit: string | null, basis: string): StatField =>
  ({ key, label, status: 'ok', value, value_kind: kind, unit, as_of: '2026-11-12', basis, reason: null });
const unavailable = (key: StatField['key'], label: string, unit: string | null): StatField =>
  ({ key, label, status: 'unavailable', value: null, value_kind: null, unit, as_of: null, basis: null, reason: NO_INTRADAY });

export function buildStats(): StockStats {
  return {
    instrument_id: INSTRUMENT_ID, release_id: RELEASE_ID, decision_as_of: iso(Date.UTC(2026, 10, 12, 7)), availability_policy_version: 'synthetic-observed-clock-v1',
    price_basis: 'forward_adjusted', bars_snapshot_id: '00000000-0000-4000-8000-000000000001', last_trade_date: '2026-11-12',
    fields: [
      ok('turnover_amount', '成交额', '523400000', 'decimal', '元', '日线 turnover_amount_cny'),
      { key: 'turnover_rate', label: '换手率', status: 'estimated', value: '1.2345', value_kind: 'decimal', unit: '%', as_of: '2026-11-12', basis: '日线成交额 ÷ 流通市值(2026-10-30)',
        reason: '换手率按 成交额 ÷ 流通市值 估算（不是 成交量 ÷ 流通股本）：流通市值取自历史快照，单位未经数据源声明，不是当日值。' },
      ok('dist_to_60d_high', '距60日高点', '-3.1416', 'decimal', '%', '最新收盘 相对 最近60根日线最高价；≥0 表示已创新高'),
      ok('limit_up_count_1y', '近1年涨停次数', '3', 'integer', '次', '最近250个交易日；按板块涨跌幅 10% 的收盘涨幅口径'),
      ok('limit_streak', '当前连板', '0', 'integer', '板', '按板块涨跌幅 10% 的收盘涨幅口径'),
      ok('up_streak', '当前连阳', '2', 'integer', '天', '连续收盘高于前一日收盘的天数'),
      ok('listing_date', '上市日期', '2003-06-09', 'date', null, '证券主数据'),
      unavailable('first_seal_time', '首次封板时间', null), unavailable('last_seal_time', '最后封板时间', null),
      unavailable('seal_amount', '封单金额', '元'), unavailable('open_board_count', '炸板次数', '次'),
    ],
  };
}

// ---- handler -------------------------------------------------------------------------------------------------------
export function handleBfStock(path: string, q: URLSearchParams, now: number, release: ReleaseRec | undefined): MockResponse {
  const instrumentId = q.get('instrument_id');
  const inst = instrumentId ? instrumentById(instrumentId) : undefined;
  if (!instrumentId) return errorResponse(now, 400, 'BAD_REQUEST', '缺少 instrument_id', [{ path: '/instrument_id', code: 'required', message: '必填' }]);
  if (!inst) return errorResponse(now, 404, 'NOT_FOUND', '证券不存在');
  if (!release) return errorResponse(now, 404, 'NOT_FOUND', '发布不存在');
  const meta = buildMeta(now, { releaseId: release.id, decisionAsOf: iso(release.decisionAsOf), snapshots: ['bars'] });
  if (path.endsWith('/stats')) {
    const stats = { ...buildStats(), instrument_id: inst.id, release_id: release.id, decision_as_of: iso(release.decisionAsOf) };
    return { status: 200, body: { meta, data: stats } };
  }
  if (!path.endsWith('/indicators')) return errorResponse(now, 404, 'NOT_FOUND', '接口不存在');
  const tf = q.get('timeframe') ?? '1d';
  const mode = (q.get('adjustment') ?? 'raw') as 'raw' | 'forward_adjusted' | 'backward_adjusted';
  if (!(tf in LOOKBACK_MS)) return errorResponse(now, 400, 'BAD_REQUEST', '请求参数无效', [{ path: '/timeframe', code: 'invalid_enum', message: 'timeframe 无效' }]);
  const names = (q.get('set') ?? ALL_SETS.join(',')).split(',').map((s) => s.trim()).filter(Boolean);
  const bad = names.filter((s) => !ALL_SETS.includes(s as IndicatorSet));
  if (bad.length || names.length === 0) return errorResponse(now, 400, 'BAD_REQUEST', '请求参数无效', [{ path: '/set', code: 'invalid_enum', message: `未知指标集 ${bad.join(', ')}` }]);
  const end = Date.parse(iso(release.decisionAsOf));                       // the page sends whole seconds
  const start = Date.parse(iso(end - LOOKBACK_MS[tf]!));
  const win = buildBarWindow(inst, tf, mode, mode === 'raw' ? undefined : 'adj-v1', SESSION, start, end, end);
  const close: Num[] = win.bars.map((b) => {
    const v = 'close_price_raw' in b ? b.close_price_raw : b.close_price;
    return v === null ? null : Number(v);
  });
  const set = ALL_SETS.filter((s) => names.includes(s));
  const data: StockIndicators = {
    instrument_id: inst.id, release_id: release.id, decision_as_of: iso(end), availability_policy_version: 'synthetic-observed-clock-v1', timeframe: tf,
    adjustment_mode: mode, adjustment_version: win.adjustment_version ?? null, snapshot_id: win.snapshot_id, window_hash: win.window_hash, window_start: win.start,
    window_end: win.end, window_complete: win.window_complete, numeric_policy: 'float64-v1', requested: set,
    bars: win.bars.map((b) => ({ bar_open_time: b.bar_open_time, trade_date: b.trade_date, is_final: b.is_final })), series: set.flatMap((s) => seriesFor(s, close)),
  };
  return { status: 200, body: { meta, data } };
}
