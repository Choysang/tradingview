import { createHash } from 'node:crypto';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../src/api/client';
import { buildParameterArtifact, canonicalJson, encodeQuery, errorCode, explainError, extGet, extPost, getJobRun, ZERO_UUID } from '../src/pages/ext/c/api';
import { alwaysTrueCondition, buildRuleBody, validateDraft } from '../src/pages/ext/c/alertRule';
import { groupAlertsByTier, parseDelivery, parseEvidence, parseTier, type AlertItem } from '../src/pages/ext/c/alertTier';
import { pageSnapshotsConsistent } from '../src/pages/ext/c/common';
import { eventTimeLabel, fmtCnyAmount, fmtMeasurement, groupByMonth, ruleSummary, sparkSegments } from '../src/pages/ext/c/fmt';
import { validateJobForm } from '../src/pages/ext/c/RunJobForm';
import { lastWeekday, shanghaiDayStart, windowLastDays } from '../src/pages/ext/c/time';
import type { AlertView } from '../src/pages/ext/c/types';

const okEnv = { meta: { request_id: 'r' }, data: { x: 1 } };
function mockFetch(status: number, body: unknown) {
  const f = vi.fn(async () => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }));
  vi.stubGlobal('fetch', f);
  return f;
}
afterEach(() => { vi.unstubAllGlobals(); });

describe('query encoding', () => {
  it('sends scalars as text and objects/arrays as JSON text, dropping null/undefined', () => {
    const qs = encodeQuery({ snapshot_id: ZERO_UUID, window: { start: 'a', end: 'b' }, page: { limit: 50 }, refs: [], none: null, nope: undefined, n: 3, flag: false });
    const sp = new URLSearchParams(qs);
    expect(sp.get('snapshot_id')).toBe(ZERO_UUID);
    expect(JSON.parse(sp.get('window') ?? '')).toEqual({ start: 'a', end: 'b' });
    expect(sp.get('page')).toBe('{"limit":50}');
    expect(sp.get('refs')).toBe('[]');
    expect(sp.get('n')).toBe('3');
    expect(sp.get('flag')).toBe('false');
    expect(sp.has('none')).toBe(false);
    expect(sp.has('nope')).toBe(false);
  });
});

describe('extGet / extPost', () => {
  it('GETs /v1/x/<op> with the encoded query and same-origin credentials', async () => {
    const f = mockFetch(200, okEnv);
    const r = await extGet('list-user-alerts', { page: { limit: 5, cursor: 'c1' } });
    expect(r.data).toEqual({ x: 1 });
    const [url, init] = f.mock.calls[0] as unknown as [string, RequestInit];
    expect(url.startsWith('/v1/x/list-user-alerts?')).toBe(true);
    expect(decodeURIComponent(url)).toContain('{"limit":5,"cursor":"c1"}');
    expect(init.credentials).toBe('same-origin');
    expect(init.method).toBe('GET');
  });

  it('POSTs a JSON body with the Idempotency-Key header', async () => {
    const f = mockFetch(200, okEnv);
    await extPost('save-alert-rule', { rule_id: 'r' }, 'key-1');
    const [url, init] = f.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/v1/x/save-alert-rule');
    expect((init.headers as Record<string, string>)['Idempotency-Key']).toBe('key-1');
    expect(init.body).toBe('{"rule_id":"r"}');
  });

  it('turns a typed error body into ApiError with code and field errors', async () => {
    mockFetch(422, { meta: null, error: { code: 'CONTRACT_VIOLATION', message: 'bad', retryable: false, field_errors: [{ path: 'window', code: 'x', message: '缺少 end' }] } });
    const err = await extGet('x').catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe('CONTRACT_VIOLATION');
    expect(explainError(err)).toContain('请求参数不符合要求');
    expect(explainError(err)).toContain('window 缺少 end');
  });

  it('maps bare HTTP statuses, network failures and unreadable bodies', async () => {
    mockFetch(401, null);
    expect(errorCode(await extGet('x').catch((e: unknown) => e))).toBe('UNAUTHORIZED');
    mockFetch(403, null);
    expect(explainError(await extGet('x').catch((e: unknown) => e))).toBe('没有权限执行此操作');
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('down'); }));
    const net = await extGet('x').catch((e: unknown) => e);
    expect((net as ApiError).status).toBe(0);
    mockFetch(200, { nothing: true });
    expect(await extGet('x').catch((e: unknown) => e)).toBeInstanceOf(ApiError);
  });

  it('has Chinese text for the documented codes', () => {
    const mk = (code: string) => new ApiError(409, { error: { code, message: 'm', retryable: false, field_errors: [] } });
    expect(explainError(mk('VERSION_CONFLICT'))).toContain('版本冲突');
    expect(explainError(mk('IDEMPOTENCY_CONFLICT'))).toContain('请求编号');
    expect(explainError(mk('RESULT_NOT_READY'))).toBe('结果尚未生成');
    expect(explainError(mk('AUTH_REQUIRED'))).toContain('登录');
  });
});

describe('canonical JSON and parameter artifact', () => {
  it('sorts keys recursively, has no spaces and keeps non-ASCII', () => {
    expect(canonicalJson({ b: [true, null, { z: 1, a: '中' }], a: 1.5 })).toBe('{"a":1.5,"b":[true,null,{"a":"中","z":1}]}');
    expect(canonicalJson({ a: undefined, b: 1 })).toBe('{"b":1}');
    expect(() => canonicalJson({ a: Number.NaN })).toThrow();
  });

  it('builds an inline-json ref whose hash is the sha256 of the canonical UTF-8 bytes', async () => {
    const params = { window: 5, name: '日', a: [1, 2] };
    const art = await buildParameterArtifact(params);
    const canon = canonicalJson(params);
    expect(art.contract_id).toBe('pulse.job_parameters');
    expect(art.schema_version).toBe('1');
    expect(art.content_hash).toBe(createHash('sha256').update(canon, 'utf8').digest('hex'));
    const b64 = art.ref.replace('inline-json:', '').replace(/-/g, '+').replace(/_/g, '/');
    expect(art.ref).not.toMatch(/[=+/]/);
    expect(Buffer.from(b64, 'base64').toString('utf8')).toBe(canon);
  });
});

describe('parameter artifact contract from the job definition', () => {
  it('uses the definition contract id/version when given', async () => {
    const art = await buildParameterArtifact({}, 'pulse.job.account_readmodel_build', '2');
    expect(art).toMatchObject({ contract_id: 'pulse.job.account_readmodel_build', schema_version: '2' });
    expect(art.ref).toBe('inline-json:e30');
  });
  it('GETs the core job run at /v1/jobs/<id>', async () => {
    const f = mockFetch(200, { meta: {}, data: { run_state: 'running' } });
    const r = await getJobRun('abc');
    expect(r.data.run_state).toBe('running');
    expect((f.mock.calls[0] as unknown as [string])[0]).toBe('/v1/jobs/abc');
  });
});

describe('alert tiers', () => {
  const alert = (id: string, at: string): AlertView => ({ alert_id: id, rule_id: 'r', rule_version: 1, raised_at: at, evidence_artifact: { ref: id, content_hash: 'h', contract_id: 'c', schema_version: '1' }, delivery_states: [], outcome_artifact: null });
  it('parses the tier from the evidence body, unknown otherwise', () => {
    expect(parseTier('P1')).toBe('P1');
    expect(parseTier('P9')).toBe('unknown');
    expect(parseTier('RULE')).toBe('RULE');
    expect(parseEvidence({ tier: 'P1', headline: 'h', verification_note: '来源未证实', held_in_industry: true })).toMatchObject({ verification_note: '来源未证实', held_in_industry: true });
    expect(parseEvidence({ tier: 'P0', headline: '某公司被立案', direction: 'negative', instruments: [{ instrument_id: 'i', code: '600000', name: '浦发' }] })?.tier).toBe('P0');
    expect(parseEvidence(null)).toBeNull();
    expect(parseEvidence({ tier: 'zzz' })?.headline).toBe('（无标题）');
  });
  it('groups P0, P1, P2 then unclassified, newest first inside a tier', () => {
    const ev = (tier: string) => parseEvidence({ tier, headline: 'h' });
    const items: AlertItem[] = [
      { alert: alert('a', '2026-11-01T00:00:00Z'), evidence: ev('P2') },
      { alert: alert('b', '2026-11-02T00:00:00Z'), evidence: ev('P0') },
      { alert: alert('c', '2026-11-03T00:00:00Z'), evidence: null },
      { alert: alert('d', '2026-11-04T00:00:00Z'), evidence: ev('P0') },
    ];
    const g = groupAlertsByTier(items);
    expect(g.map((x) => x.tier)).toEqual(['P0', 'P2', 'unknown']);
    const withRule = groupAlertsByTier([...items, { alert: alert('e', '2026-11-05T00:00:00Z'), evidence: ev('RULE') }]);
    expect(withRule.map((x) => x.tier)).toEqual(['P0', 'P2', 'RULE', 'unknown']);
    expect(g[0]?.items.map((i) => i.alert.alert_id)).toEqual(['d', 'b']);
  });
  it('parses delivery states with and without channel', () => {
    expect(parseDelivery('confirmed')).toEqual({ channel: 'unknown', state: 'confirmed' });
    expect(parseDelivery('webhook:failed')).toEqual({ channel: 'webhook', state: 'failed' });
    expect(parseDelivery('in_app=accepted')).toEqual({ channel: 'in_app', state: 'accepted' });
  });
});

describe('alert rule body', () => {
  it('builds a contract-shaped body with an always-true compare condition', () => {
    const b = buildRuleBody({ scopeKind: 'news_tier', scopeKey: 'P0', destinationIds: ['d1'], enabled: true });
    expect(b).toMatchObject({ scope: { scope_kind: 'news_tier', scope_key: 'P0', membership_snapshot_id: null }, policy_version: 'alert-rule-v1', annotation_id: null, annotation_version: null, destination_ids: ['d1'], enabled: true });
    expect(alwaysTrueCondition()).toMatchObject({ node: 'compare', operator: 'eq', clock: { kind: 'event_available' } });
  });
  it('validates the draft', () => {
    expect(validateDraft({ scopeKind: 'news_tier', scopeKey: 'P7', destinationIds: ['d'], enabled: true })).toContain('P0');
    expect(validateDraft({ scopeKind: 'news_tier', scopeKey: 'P0', destinationIds: [], enabled: true })).toContain('通知去向');
    expect(validateDraft({ scopeKind: 'news_tier', scopeKey: 'P1', destinationIds: ['d'], enabled: true })).toBeNull();
  });
});

describe('display helpers', () => {
  it('formats yuan as 万/亿 without float and never shows null as 0', () => {
    expect(fmtCnyAmount('123456789.00')).toBe('1.23 亿元');
    expect(fmtCnyAmount('52000')).toBe('5.20 万元');
    expect(fmtCnyAmount('9999.5')).toBe('9,999.50 元');
    expect(fmtCnyAmount(null)).toBe('缺失');
    expect(fmtCnyAmount('abc')).toBe('缺失');
  });
  it('shows missing measurements as — / 缺失', () => {
    expect(fmtMeasurement(null)).toBe('—');
    expect(fmtMeasurement({ value: null, text_value: null, unit: null, reference_period: 'x', source_revision_id: 'r' })).toBe('缺失');
    expect(fmtMeasurement({ value: '2.5', text_value: null, unit: 'percent', reference_period: 'x', source_revision_id: 'r' })).toBe('2.5 %');
  });
  it('never shows a clock time for date-only events and groups by month', () => {
    const dateOnly = { scheduled_at: null, scheduled_date: '2026-12-01', time_precision: 'date' };
    expect(eventTimeLabel(dateOnly)).toBe('2026-12-01（仅日期）');
    expect(eventTimeLabel({ scheduled_at: '2026-12-01T00:00:00Z', scheduled_date: null, time_precision: 'date' })).toContain('仅日期');
    expect(eventTimeLabel({ scheduled_at: '2026-12-01T01:30:00Z', scheduled_date: null, time_precision: 'minute' })).toContain('09:30');
    const g = groupByMonth([dateOnly, { scheduled_at: '2026-11-20T01:30:00Z', scheduled_date: null, time_precision: 'minute' }]);
    expect(g.map((x) => x.month)).toEqual(['2026 年 11 月', '2026 年 12 月']);
  });
  it('summarises forward book rules', () => {
    expect(ruleSummary({ hold_days: 5, slots: 20, new_per_day: 4, offer_top: 40, per_name_cny: 50000 })).toBe('5日持有 / 20只 / 每日4只 / 前40 / 每只5.00 万元');
  });
  it('splits the NAV sparkline at null points', () => {
    const h = (n: string | null, i: number) => ({ trade_date: `2026-11-0${i + 1}`, nav_cny: n, positions: 1 });
    const segs = sparkSegments([h('100', 0), h('101', 1), h(null, 2), h('102', 3)], 100, 40);
    expect(segs).toHaveLength(2);
    expect(segs[0]?.split(' ')).toHaveLength(2);
    expect(sparkSegments([h(null, 0)], 100, 40)).toEqual([]);
  });
  it('detects a snapshot change between pages', () => {
    const p = (h: string) => ({ page: { next_cursor: null, snapshot_hash: h } });
    expect(pageSnapshotsConsistent([p('a'), p('a')])).toBe(true);
    expect(pageSnapshotsConsistent([p('a'), p('b')])).toBe(false);
  });
});

describe('time + job form validation', () => {
  it('computes Shanghai day starts and windows', () => {
    expect(shanghaiDayStart('2026-11-12')).toBe('2026-11-11T16:00:00Z');
    expect(shanghaiDayStart('bad')).toBeNull();
    expect(windowLastDays(1, Date.parse('2026-11-12T00:00:00Z'))).toEqual({ start: '2026-11-11T00:00:00Z', end: '2026-11-12T00:00:00Z' });
    expect(lastWeekday(Date.parse('2026-11-15T04:00:00Z'))).toBe('2026-11-13'); // Sunday -> Friday
  });
  it('validates the run-job form', () => {
    const def = { job_id: 'j', job_version: 1, application_operation: 'op', manual_trigger: true, enabled: true, definition_hash: 'a'.repeat(64), parameter_contract_id: null, parameter_schema_version: null };
    const ok = { definition: def, params: '{}', scheduledFor: '2026-11-12T10:00' };
    expect(validateJobForm(ok)).toEqual({});
    expect(Object.keys(validateJobForm({ definition: null, params: '{', scheduledFor: '' }))).toEqual(['definition', 'params', 'scheduledFor']);
    expect(validateJobForm({ ...ok, definition: { ...def, enabled: false } }).definition).toBe('该任务已停用');
  });
});
