/**
 * Mock for the fill-market backfill reads (`/v1/x/bf/fillm/crowd`, `/v1/x/bf/fillm/stock-stats-plus`), dev build only (`pnpm dev:mock`).
 * Why: lets the crowd panel and the stats card additions be developed against clearly marked synthetic, contract-valid answers.
 * Invariants: the seal success rate states how many sessions it covers (partial window); name clusters are unavailable with a reason;
 * unknown values are null with a reason. Not covered: real data. Checked against backfill_fillm.schema.json by
 * `pnpm mock:dump:fillm` + scripts/validate_bf_fillm_mock.py.
 */
import type { CrowdSnapshot, StockStatsPlus } from '../src/pages/bf/fillm/api';
import { INSTRUMENTS, instrumentById, type ReleaseRec } from './market.ts';
import { buildMeta, errorResponse, iso, type MockResponse } from './util.ts';

const stock = (i: number, pct: string | null, lu: boolean, streak: number) => {
  const inst = INSTRUMENTS[i % INSTRUMENTS.length] as { id: string; code?: string; name?: string };
  return { instrument_id: inst.id, code: inst.code ?? null, name: inst.name ?? null, pct_change: pct, is_limit_up: lu, limit_streak: streak };
};

export function buildCrowd(releaseId: string, session: string): CrowdSnapshot {
  const days = ['2026-11-06', '2026-11-09', '2026-11-10', '2026-11-11', session];
  return {
    release_id: releaseId, session_date: session, taxonomy_id: 'legacy_boards_industry', taxonomy_version: 1, membership_snapshot_id: '00000000-0000-4000-8000-000000000002',
    method: 'industry-diffusion-v1 (synthetic mock data)', sessions_in_spread: days,
    industries: [{
      node_id: 'bk_mock', name: '模拟行业', members: 12, observed: 12, limit_ups: 3, up_share: '66.6667', avg_pct: '3.1200',
      leader: stock(0, '10.0000', true, 3), followers: [stock(1, '4.5000', false, 0), stock(2, null, false, 0)],
      spread: days.map((d, k) => ({ session: d, observed: 12, limit_ups: k, up_share: k === 0 ? null : String(20 * k), strong_followers: k })),
    }],
    institutions: {
      status: 'ok', session_date: session, reports_seen: 2, basis: 'synthetic mock rows',
      rows: [
        { instrument_id: stock(0, null, false, 0).instrument_id, code: null, name: null, institution_rows: 1, buy_cny: '300000000.00', sell_cny: '100000000.00', net_cny: '200000000.00', net_reason: null },
        { instrument_id: stock(1, null, false, 0).instrument_id, code: null, name: null, institution_rows: 1, buy_cny: '50000000.00', sell_cny: null, net_cny: null, net_reason: '披露行没有卖出金额，不猜净额' },
      ],
      notes: [],
    },
    name_clusters: { status: 'unavailable', reason: 'pulse3 keeps no security-name history (mock text).', retire_recommended: true },
    notes: ['synthetic mock data'],
  };
}

export function buildStatsPlus(instrumentId: string, releaseId: string, decisionAsOf: string): StockStatsPlus {
  return {
    instrument_id: instrumentId, release_id: releaseId, decision_as_of: decisionAsOf,
    fields: [
      { key: 'volume_ratio', label: '量比', status: 'ok', value: '1.8200', value_kind: 'decimal', unit: '倍', as_of: '2026-11-12', basis: '最新日线成交量 ÷ 此前5日平均成交量', reason: null, coverage: null },
      { key: 'float_cap', label: '流通市值', status: 'estimated', value: '50.00', value_kind: 'decimal', unit: '亿元', as_of: '2026-10-30', basis: '截点前可见的最新流通市值版本',
        reason: '取自 2026-10-30 的历史快照（不是当日值）；这是流通市值，不是旧站的实际自由流通市值', coverage: null },
      { key: 'seal_success_rate_1y', label: '封板成功率', status: 'estimated', value: '50.00', value_kind: 'decimal', unit: '%', as_of: '2026-11-10',
        basis: '触及涨停 2 次，其中收盘封住 1 次', reason: '只覆盖 3 个交易日（2026-11-04 至 2026-11-10），不是250个交易日（约1年）的值；封板事实由约60秒一次的快照估算',
        coverage: { window_sessions: 250, sessions_covered: 3, first_session: '2026-11-04', last_session: '2026-11-10', touched: 2, sealed_at_close: 1, policy_version: 'seal-facts-v2', sessions_excluded_other_policy: 0 } },
    ],
  };
}

export function handleBfFillm(path: string, q: URLSearchParams, now: number, release: ReleaseRec | undefined): MockResponse {
  if (!release) return errorResponse(now, 404, 'NOT_FOUND', '发布不存在');
  const meta = buildMeta(now, { releaseId: release.id, decisionAsOf: iso(release.decisionAsOf), snapshots: ['bars'] });
  if (path.endsWith('/crowd')) return { status: 200, body: { meta, data: buildCrowd(release.id, iso(release.decisionAsOf).slice(0, 10)) } };
  if (path.endsWith('/stock-stats-plus')) {
    const id = q.get('instrument_id');
    if (!id) return errorResponse(now, 400, 'BAD_REQUEST', '缺少 instrument_id', [{ path: '/instrument_id', code: 'required', message: '必填' }]);
    if (!instrumentById(id)) return errorResponse(now, 404, 'NOT_FOUND', '证券不存在');
    return { status: 200, body: { meta, data: buildStatsPlus(id, release.id, iso(release.decisionAsOf)) } };
  }
  return errorResponse(now, 404, 'NOT_FOUND', '接口不存在');
}
