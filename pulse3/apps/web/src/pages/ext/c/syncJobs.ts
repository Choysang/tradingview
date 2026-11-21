/** Types and pure helpers of the data-sync lane (contract backfill_sync.schema.json, routes /v1/x/bf/sync/jobs). */

export type SyncKind = 'sync_daily_bars' | 'sync_legacy_import' | 'sync_reconcile' | 'sync_refdata';
export type RefdataMode = 'daily' | 'weekly' | 'adjusted_backfill' | 'raw_backfill';
export type SyncRunState = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';

export interface SyncJob {
  job_run_id: string; kind: SyncKind; run_state: SyncRunState; parameters: Record<string, unknown>; requested_at: string;
  started_at: string | null; finished_at: string | null; exit_status: number | null; error_code: string | null; summary: string | null;
}
export interface SyncBounds { max_codes: number; max_since_days: number; max_count: number; max_last: number; max_refdata_codes: number }
export interface SyncJobList { items: SyncJob[]; kinds: SyncKind[]; defaults: Record<string, Record<string, unknown>>; bounds: SyncBounds; executor_enabled: boolean; worker_hint: string }
export interface SyncReceipt { receipt: { job_run_id: string; resource_id: string; run_state: SyncRunState; poll_operation_id: string }; kind: SyncKind; parameters: Record<string, unknown>; worker_hint: string }

export const SYNC_KIND_LABEL: Record<SyncKind, string> = {
  sync_daily_bars: '日线补数（抓取）',
  sync_legacy_import: '旧仓库导入（旧系统遗留，随旧站下线）',
  sync_reconcile: '多源对账',
  sync_refdata: '参考数据（复权日线 / 指数 / 板块 / 上市日）',
};
export const REFDATA_MODE_LABEL: Record<RefdataMode, string> = {
  daily: '日更（指数日线 + 复权日线增量）', weekly: '周更（上市日 + 板块成分）', adjusted_backfill: '复权日线回填（每只 2 次请求）', raw_backfill: '原始日线回填（每只 1 次请求）',
};
export const SYNC_KIND_HINT: Record<SyncKind, string> = {
  sync_daily_bars: '向公开行情源抓取日线（每秒最多 1 次请求，被封立即停止、不重试）。可填代码列表，或选择整个研究股票池。',
  sync_legacy_import: '把旧系统仓库中最近 N 天的日线导入新库（读本地文件，不访问网络）。',
  sync_reconcile: '对比旧仓库、新浪、腾讯三个来源在同一日期的收盘价与成交量，只读已入库数据。',
  sync_refdata: '实时抓取外部数据源（每秒最多 1 次请求，被封立即停止）：日更约几十次请求，周更约 1000 次，回填按代码数计。只在切换到新系统后才会执行，旧系统在服务器上仍在抓同样的数据。',
};

export interface SyncForm {
  kind: SyncKind;
  mode: 'codes' | 'universe';
  codes: string;
  source: 'sina' | 'tencent' | 'both';
  count: string;
  sinceDays: string;
  boards: 'main' | 'all';
  last: string;
  refdataMode: RefdataMode;
  maxCodes: string;
}
export const EMPTY_SYNC_FORM: SyncForm = { kind: 'sync_daily_bars', mode: 'codes', codes: '', source: 'sina', count: '320', sinceDays: '30', boards: 'main', last: '5', refdataMode: 'daily', maxCodes: '300' };

export type BuildResult = { ok: true; parameters: Record<string, unknown> } | { ok: false; errors: Record<string, string> };

const parseCodes = (raw: string): string[] => [...new Set(raw.split(/[\s,，;；]+/).filter(Boolean))];

function intInRange(raw: string, low: number, high: number): number | null {
  if (!/^\d+$/.test(raw.trim())) return null;
  const n = Number(raw);
  return n >= low && n <= high ? n : null;
}

/** Validate the form against the server's bounds and build the request parameters; the server validates again. */
export function buildSyncParameters(f: SyncForm, b: SyncBounds): BuildResult {
  const errors: Record<string, string> = {};
  const codes = parseCodes(f.codes);
  const badCode = codes.find((c) => !/^\d{6}$/.test(c));
  const checkCodes = (required: boolean): void => {
    if (badCode) errors.codes = `代码必须是 6 位数字：${badCode}`;
    else if (codes.length > b.max_codes) errors.codes = `一次最多 ${b.max_codes} 个代码，现在 ${codes.length} 个`;
    else if (required && codes.length === 0) errors.codes = '请填写至少一个代码';
  };
  const params: Record<string, unknown> = {};
  if (f.kind === 'sync_daily_bars') {
    const count = intInRange(f.count, 1, b.max_count);
    if (count === null) errors.count = `每只股票的日线根数需在 1..${b.max_count}`;
    if (f.mode === 'codes') checkCodes(true);
    Object.assign(params, f.mode === 'universe' ? { universe: true } : { codes }, { source: f.source, count });
  } else if (f.kind === 'sync_legacy_import') {
    const since = intInRange(f.sinceDays, 1, b.max_since_days);
    if (since === null) errors.sinceDays = `天数需在 1..${b.max_since_days}`;
    checkCodes(false);
    Object.assign(params, { since_days: since, boards: f.boards }, codes.length > 0 ? { codes } : {});
  } else if (f.kind === 'sync_refdata') {
    const backfill = f.refdataMode === 'adjusted_backfill' || f.refdataMode === 'raw_backfill';
    const max = intInRange(f.maxCodes, 1, b.max_refdata_codes);
    if (backfill && max === null) errors.maxCodes = `代码数需在 1..${b.max_refdata_codes}`;
    Object.assign(params, { mode: f.refdataMode }, backfill ? { max_codes: max } : {});
  } else {
    const last = intInRange(f.last, 1, b.max_last);
    if (last === null) errors.last = `对比最近几个日期需在 1..${b.max_last}`;
    checkCodes(true);
    Object.assign(params, { codes, last });
  }
  return Object.keys(errors).length > 0 ? { ok: false, errors } : { ok: true, parameters: params };
}

/** One-line description of the stored parameters of a run. */
export function describeSyncParameters(kind: SyncKind, p: Record<string, unknown>): string {
  const codes = Array.isArray(p.codes) ? (p.codes as string[]) : [];
  const shown = codes.length > 4 ? `${codes.slice(0, 4).join(' ')} 等 ${codes.length} 只` : codes.join(' ');
  if (kind === 'sync_daily_bars') return `${p.universe ? '整个研究股票池' : shown} · 来源 ${String(p.source)} · 每只 ${String(p.count)} 根`;
  if (kind === 'sync_legacy_import') return `最近 ${String(p.since_days)} 天 · ${p.boards === 'all' ? '全部板块' : '主板'}${codes.length > 0 ? ` · ${shown}` : ''}`;
  if (kind === 'sync_refdata') return `${REFDATA_MODE_LABEL[p.mode as RefdataMode] ?? String(p.mode)}${p.max_codes ? ` · 最多 ${String(p.max_codes)} 只` : ''}`;
  return `${shown} · 最近 ${String(p.last)} 个日期`;
}

export const isActive = (j: Pick<SyncJob, 'run_state'>): boolean => j.run_state === 'queued' || j.run_state === 'running';
