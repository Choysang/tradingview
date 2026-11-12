/**
 * Mock request handler for the 31 contract operations (+ a few /__mock/* dev switches).
 * Why: spec M4 allows UI development against clearly marked synthetic, contract-valid
 * responses. Invariants: only paths from contracts/openapi.json are served under /v1;
 * releases are immutable once created; cursors bind ranking identity; writes honour
 * expected versions and Idempotency-Key. Not covered: real data, auth, load behaviour.
 */
import type {
  Annotation, ExperimentRequest, Layout, UpdateNotification, UpdatePage, Warning, Watchlist,
} from '../src/api/types';
import {
  HORIZON_IDS, INSTRUMENTS, POOL_ID, buildBarWindow, buildPool, buildQuote, buildRanking, buildRelease, buildSectorOverview,
  instrumentById, rankingId, rankingTotal, taxonomyNodes, type ReleaseRec,
} from './market.ts';
import {
  ACCOUNT_ID, ACCOUNT_VERSION, KNOWN_EXPERIMENTS, LEAGUE_ID, STRATEGIES, STRATEGY_INSTANCES, buildAccount, buildActionability,
  buildDataStatus, buildEvaluation, buildJobRun, buildLeague, buildModelRun, buildPapers, type CreatedExperiment,
} from './research.ts';
import { EVIDENCE_PAGE, buildCommunity, buildEvidence, buildFundFlow, buildPlan, buildSeats, buildWorkspace } from './stock.ts';
import { buildMeta, decodeCursor, encodeCursor, errorResponse, iso, opaque, uuid, type MockResponse } from './util.ts';

const RANK_PAGE = 5;
const LIST_PAGE = 4;
const MIN = 60_000;

export interface MockState {
  releases: ReleaseRec[];
  stale: boolean;
  epoch: string;
  log: UpdateNotification[];
  layouts: Map<string, Layout>;
  watchlists: Map<string, Watchlist>;
  annotations: Map<string, Annotation>;
  idem: Map<string, MockResponse>;
  experiments: CreatedExperiment[];
  eventSeq: number;
}

export function createMockState(now = Date.now()): MockState {
  const r1: ReleaseRec = { n: 1, id: uuid('release:1'), decisionAsOf: now - 26 * MIN, publishedAt: now - 24 * MIN };
  const wl: Watchlist = {
    watchlist_id: uuid('watchlist:default'), watchlist_version: 1, display_name: '我的自选', description: null,
    members: [INSTRUMENTS[2]!, INSTRUMENTS[4]!].map((i, k) => ({ instrument_id: i.id, sort_position: k + 1, added_release_id: r1.id, note: k === 0 ? '关注储能订单' : null, added_at: iso(now - 86_400_000) })),
    updated_at: iso(now - 86_400_000),
  };
  const ann: Annotation = {
    annotation_id: uuid('annotation:1'), annotation_version: 1, instrument_id: (INSTRUMENTS[0] as { id: string }).id, watchlist_id: null,
    annotation_kind: 'note', anchor_release_id: r1.id, anchor_snapshot_id: null, body: opaque({ text: '合成样本：回踩前低再观察。' }), updated_at: iso(now - 7_200_000),
  };
  return {
    releases: [r1], stale: false, epoch: 'epoch-1', log: [], layouts: new Map(), watchlists: new Map([[wl.watchlist_id, wl]]),
    annotations: new Map([[ann.annotation_id, ann]]), idem: new Map(), experiments: [], eventSeq: 0,
  };
}

export const currentRelease = (s: MockState): ReleaseRec => s.releases[s.releases.length - 1] as ReleaseRec;

function pushEvent(s: MockState, now: number, type: UpdateNotification['event_type'], aggregateId: string, version: number, payload: Record<string, unknown> = {}, eventId?: string): void {
  s.eventSeq += 1;
  s.log.push({
    event_id: eventId ?? uuid(`event:${s.epoch}:${s.eventSeq}`),
    event_type: type,
    schema_version: '3.0.0',
    aggregate_id: aggregateId,
    aggregate_version: version,
    recorded_at: iso(now),
    payload: opaque(payload),
  });
}

export interface MockRequest {
  method: string;
  path: string;
  query: URLSearchParams;
  headers: Record<string, string | undefined>;
  body: unknown;
  now?: number;
}

const ok = (meta: ReturnType<typeof buildMeta>, data: unknown, status = 200): MockResponse => ({ status, body: { meta, data } });

function missingParams(now: number, q: URLSearchParams, required: string[]): MockResponse | null {
  const miss = required.filter((k) => !q.get(k));
  if (!miss.length) return null;
  return errorResponse(now, 400, 'INVALID_PARAMETER', '缺少必填参数', miss.map((m) => ({ path: `query.${m}`, code: 'REQUIRED', message: '必填' })));
}

function findRelease(s: MockState, id: string | null): ReleaseRec | null {
  return s.releases.find((r) => r.id === id) ?? null;
}

const rankKinds = ['sector_opportunity_rank', 'sector_attention_rank', 'sector_rising_rank'] as const;

function coverageFor(total: number, observed: number): { expected_count: number; observed_count: number; missing_count: number; unverifiable_reasons: string[] } {
  return {
    expected_count: total, observed_count: observed, missing_count: total - observed,
    unverifiable_reasons: total > observed ? ['2 只停牌股票未纳入排名', '1 只因关键数据缺失无法评分'] : [],
  };
}

function relFreshness(s: MockState, r: ReleaseRec): 'current' | 'stale' {
  return s.stale || r.id !== currentRelease(s).id ? 'stale' : 'current';
}

const EXCLUDE_WARNING: Warning = { code: 'UNIVERSE_EXCLUSION', message: '部分股票因停牌或数据缺失未参与本榜，榜单只在可评分的股票之间比较。', affected_component: 'market_ranking', evidence_ref: null };

export function handle(s: MockState, req: MockRequest): MockResponse {
  const now = req.now ?? Date.now();
  const { method, path, query: q } = req;

  // ---- dev switches (not part of the contract) ----
  if (path.startsWith('/__mock/')) return devSwitch(s, req, now);

  const m = (re: RegExp): RegExpExecArray | null => re.exec(path);
  let g: RegExpExecArray | null;

  if (method === 'GET' && path === '/v1/releases/current') {
    const bad = missingParams(now, q, ['slot_key']); if (bad) return bad;
    const r = currentRelease(s);
    return ok(buildMeta(now, { releaseId: r.id, decisionAsOf: iso(r.decisionAsOf), freshness: s.stale ? 'stale' : 'current' }), {
      slot_key: q.get('slot_key'), current_release_id: r.id, pointer_generation: r.n, issued_stage_generation: r.n, published_stage_generation: r.n, updated_at: iso(r.publishedAt),
    });
  }

  if (method === 'GET' && (g = m(/^\/v1\/releases\/([^/]+)$/))) {
    const r = findRelease(s, g[1] as string);
    if (!r) return errorResponse(now, 404, 'RELEASE_NOT_FOUND', '没有这个发布版本');
    return ok(buildMeta(now, { releaseId: r.id, decisionAsOf: iso(r.decisionAsOf), freshness: relFreshness(s, r) }), buildRelease(r));
  }

  if (method === 'GET' && path === '/v1/rankings') {
    const bad = missingParams(now, q, ['release_id', 'horizon_id', 'forecast_target']); if (bad) return bad;
    const r = findRelease(s, q.get('release_id'));
    if (!r) return errorResponse(now, 404, 'RELEASE_NOT_FOUND', '没有这个发布版本');
    const horizon = q.get('horizon_id') as string;
    if (!(HORIZON_IDS as readonly string[]).includes(horizon)) return errorResponse(now, 400, 'INVALID_PARAMETER', '未知期限', [{ path: 'query.horizon_id', code: 'UNKNOWN', message: '未知期限' }]);
    const total = rankingTotal({ kind: 'market' });
    const paged = pageOffset(now, q.get('cursor'), rankingId(r, horizon)); if ('status' in paged) return paged;
    const next = paged.offset + RANK_PAGE < total ? encodeCursor([rankingId(r, horizon), paged.offset + RANK_PAGE]) : null;
    return ok(buildMeta(now, { releaseId: r.id, decisionAsOf: iso(r.decisionAsOf), freshness: relFreshness(s, r), nextCursor: next, coverage: coverageFor(14, 12), warnings: [EXCLUDE_WARNING], quality: 'partial' }),
      buildRanking(r, horizon, q.get('forecast_target') as string, { kind: 'market' }, [paged.offset, paged.offset + RANK_PAGE]));
  }

  if (method === 'GET' && path === '/v1/sectors/rankings') {
    const bad = missingParams(now, q, ['release_id', 'taxonomy_id', 'taxonomy_version', 'rank_kind', 'horizon_id', 'forecast_target']); if (bad) return bad;
    const r = findRelease(s, q.get('release_id'));
    if (!r) return errorResponse(now, 404, 'RELEASE_NOT_FOUND', '没有这个发布版本');
    const kind = q.get('rank_kind') as (typeof rankKinds)[number];
    if (!rankKinds.includes(kind)) return errorResponse(now, 400, 'INVALID_PARAMETER', '未知榜单类型', [{ path: 'query.rank_kind', code: 'UNKNOWN', message: '未知' }]);
    const data = buildSectorOverview(r, q.get('taxonomy_id') as string, Number(q.get('taxonomy_version')), kind, q.get('horizon_id') as string, q.get('forecast_target') as string);
    return ok(buildMeta(now, { releaseId: r.id, decisionAsOf: iso(r.decisionAsOf), freshness: relFreshness(s, r), coverage: { expected_count: data.entries.length, observed_count: data.entries.length, missing_count: 0 } }), data);
  }

  if (method === 'GET' && (g = m(/^\/v1\/sectors\/([^/]+)\/rankings$/))) {
    const bad = missingParams(now, q, ['release_id', 'taxonomy_id', 'taxonomy_version', 'horizon_id', 'forecast_target']); if (bad) return bad;
    const r = findRelease(s, q.get('release_id'));
    if (!r) return errorResponse(now, 404, 'RELEASE_NOT_FOUND', '没有这个发布版本');
    const node = decodeURIComponent(g[1] as string);
    const taxonomy = q.get('taxonomy_id') as string;
    if (!taxonomyNodes(taxonomy).includes(node)) return errorResponse(now, 404, 'SECTOR_NOT_FOUND', '没有这个板块');
    const horizon = q.get('horizon_id') as string;
    const scope = { kind: 'sector' as const, taxonomy, version: Number(q.get('taxonomy_version')), node };
    const total = rankingTotal(scope);
    const rid = rankingId(r, horizon, `sector:${taxonomy}:${node}`);
    const paged = pageOffset(now, q.get('cursor'), rid); if ('status' in paged) return paged;
    const next = paged.offset + RANK_PAGE < total ? encodeCursor([rid, paged.offset + RANK_PAGE]) : null;
    return ok(buildMeta(now, { releaseId: r.id, decisionAsOf: iso(r.decisionAsOf), freshness: relFreshness(s, r), nextCursor: next, coverage: { expected_count: total, observed_count: total, missing_count: 0 } }),
      buildRanking(r, horizon, q.get('forecast_target') as string, scope, [paged.offset, paged.offset + RANK_PAGE]));
  }

  if (method === 'GET' && (g = m(/^\/v1\/candidate-pools\/([^/]+)$/))) {
    const bad = missingParams(now, q, ['release_id']); if (bad) return bad;
    const r = findRelease(s, q.get('release_id'));
    if (!r) return errorResponse(now, 404, 'RELEASE_NOT_FOUND', '没有这个发布版本');
    if (g[1] !== POOL_ID) return errorResponse(now, 404, 'POOL_NOT_FOUND', '没有这个候选池');
    const pool = buildPool(r);
    return ok(buildMeta(now, { releaseId: r.id, decisionAsOf: iso(r.decisionAsOf), freshness: relFreshness(s, r), quality: 'partial', coverage: { expected_count: 6, observed_count: 6, missing_count: 0 },
      warnings: [{ code: 'HEATING_COMPONENT_MISSING', message: '快速升温的部分组成项缺失，候选仅作提前关注，不是买入建议。', affected_component: 'candidate_pool', evidence_ref: null }] }), pool);
  }

  if (method === 'GET' && (g = m(/^\/v1\/stocks\/([^/]+)\/workspace$/))) {
    const bad = missingParams(now, q, ['release_id', 'view_kind']); if (bad) return bad;
    const r = findRelease(s, q.get('release_id'));
    if (!r) return errorResponse(now, 404, 'RELEASE_NOT_FOUND', '没有这个发布版本');
    const inst = instrumentById(g[1] as string);
    if (!inst) return errorResponse(now, 404, 'INSTRUMENT_NOT_FOUND', '没有这只证券');
    const view = q.get('view_kind') === 'account_holding' ? 'account_holding' : 'public_research';
    const accountId = q.get('account_id') ?? undefined;
    if (view === 'account_holding' && accountId !== ACCOUNT_ID) return errorResponse(now, 403, 'ACCOUNT_FORBIDDEN', '无权查看该账户');
    const actId = view === 'account_holding' ? uuid(`act:${r.id}`) : null;
    const ws = buildWorkspace(inst, r, view, accountId, actId);
    const quote = buildQuote(inst, now, r.decisionAsOf, s.stale);
    ws.quote_overlay = quote;
    return ok(buildMeta(now, { releaseId: r.id, decisionAsOf: iso(r.decisionAsOf), freshness: relFreshness(s, r), snapshots: ['bars', 'membership', 'quote'], actionabilityId: actId }), { ...ws, generated_at: iso(now) });
  }

  if (method === 'GET' && (g = m(/^\/v1\/stocks\/([^/]+)\/quote$/))) {
    const bad = missingParams(now, q, ['quote_policy_version']); if (bad) return bad;
    const inst = instrumentById(g[1] as string);
    if (!inst) return errorResponse(now, 404, 'INSTRUMENT_NOT_FOUND', '没有这只证券');
    const r = currentRelease(s);
    const quote = buildQuote(inst, now, r.decisionAsOf, s.stale);
    return ok(buildMeta(now, { snapshots: ['quote'], freshness: quote.freshness_state, warnings: s.stale ? [{ code: 'QUOTE_STALE', message: '报价已超过允许的新鲜度。', affected_component: 'quote', evidence_ref: null }] : [] }), quote);
  }

  if (method === 'GET' && (g = m(/^\/v1\/stocks\/([^/]+)\/bars$/))) {
    const bad = missingParams(now, q, ['snapshot_id', 'decision_as_of', 'availability_policy_version', 'timeframe', 'session_policy_version', 'adjustment_mode', 'start', 'end']); if (bad) return bad;
    const inst = instrumentById(g[1] as string);
    if (!inst) return errorResponse(now, 404, 'INSTRUMENT_NOT_FOUND', '没有这只证券');
    const mode = q.get('adjustment_mode') as 'raw' | 'forward_adjusted' | 'backward_adjusted';
    if (!['raw', 'forward_adjusted', 'backward_adjusted'].includes(mode)) return errorResponse(now, 400, 'INVALID_PARAMETER', '未知复权方式', [{ path: 'query.adjustment_mode', code: 'UNKNOWN', message: '未知' }]);
    const decisionMs = Date.parse(q.get('decision_as_of') as string);
    const win = buildBarWindow(inst, q.get('timeframe') as string, mode, q.get('adjustment_version') ?? undefined, q.get('session_policy_version') as string,
      Date.parse(q.get('start') as string), Date.parse(q.get('end') as string), decisionMs);
    return ok(buildMeta(now, { decisionAsOf: q.get('decision_as_of'), quality: win.window_complete ? 'valid' : 'partial', freshness: s.stale ? 'stale' : 'current',
      warnings: win.window_complete ? [] : [{ code: 'BAR_WINDOW_INCOMPLETE', message: '窗口内有数据源缺失的K线，缺口未被填补。', affected_component: 'bars', evidence_ref: null }] }), win);
  }

  if (method === 'GET' && (g = m(/^\/v1\/stocks\/([^/]+)\/(evidence|community|fund-flow|seats)$/))) {
    const bad = missingParams(now, q, ['release_id']); if (bad) return bad;
    const r = findRelease(s, q.get('release_id'));
    if (!r) return errorResponse(now, 404, 'RELEASE_NOT_FOUND', '没有这个发布版本');
    const inst = instrumentById(g[1] as string);
    if (!inst) return errorResponse(now, 404, 'INSTRUMENT_NOT_FOUND', '没有这只证券');
    const kind = g[2] as string;
    const rid = `${inst.id}:${kind}`;
    const paged = pageOffset(now, q.get('cursor'), rid); if ('status' in paged) return paged;
    const off = paged.offset;
    const built = kind === 'evidence' ? buildEvidence(inst, r, off) : kind === 'community' ? buildCommunity(inst, r, off) : kind === 'fund-flow' ? buildFundFlow(inst, r, off) : buildSeats(inst, r, off);
    const next = off + EVIDENCE_PAGE < built.total ? encodeCursor([rid, off + EVIDENCE_PAGE]) : null;
    const warnings: Warning[] = kind === 'community'
      ? [{ code: 'COMMUNITY_SAMPLE_PARTIAL', message: '社区讨论为抽样，评论人数不等于买入人数。', affected_component: 'community', evidence_ref: null }]
      : kind === 'fund-flow'
        ? [{ code: 'INCOMPATIBLE_METHODS', message: '不同平台的净流入口径不同，不能直接相加。', affected_component: 'fund_flow', evidence_ref: null }]
        : [];
    return ok(buildMeta(now, { releaseId: r.id, decisionAsOf: iso(r.decisionAsOf), freshness: relFreshness(s, r), nextCursor: next, warnings,
      quality: kind === 'community' ? 'partial' : 'valid', coverage: { expected_count: built.total, observed_count: built.total, missing_count: 0 } }), built.data);
  }

  if (method === 'GET' && (g = m(/^\/v1\/plans\/([^/]+)\/versions\/(\d+)$/))) {
    const version = Number(g[2]);
    const r = s.releases.find((x) => x.n === version);
    if (!r) return errorResponse(now, 404, 'PLAN_NOT_FOUND', '没有这个计划版本');
    const plan = buildPlan(g[1] as string, version, r, now);
    if (!plan) return errorResponse(now, 404, 'PLAN_NOT_FOUND', '没有这个计划');
    return ok(buildMeta(now, { releaseId: r.id, decisionAsOf: iso(r.decisionAsOf), freshness: relFreshness(s, r) }), plan);
  }

  if (method === 'GET' && path === '/v1/strategies') {
    const list = STRATEGIES.filter((st) =>
      (!q.get('role') || st.role === q.get('role')) && (!q.get('horizon_id') || st.horizon_id === q.get('horizon_id')) &&
      (!q.get('method_family') || st.method_family === q.get('method_family')) &&
      (!q.get('lifecycle') || st.instances.some((i) => i.lifecycle === q.get('lifecycle'))));
    const paged = pageOffset(now, q.get('cursor'), 'strategies'); if ('status' in paged) return paged;
    const next = paged.offset + LIST_PAGE < list.length ? encodeCursor(['strategies', paged.offset + LIST_PAGE]) : null;
    return ok(buildMeta(now, { nextCursor: next, coverage: { expected_count: list.length, observed_count: list.length, missing_count: 0 } }), { items: list.slice(paged.offset, paged.offset + LIST_PAGE) });
  }

  if (method === 'GET' && path === '/v1/data-status') {
    const items = buildDataStatus(now, s.stale);
    return ok(buildMeta(now, { freshness: 'unknown', snapshots: [], quality: 'partial' }), { items });
  }

  if (method === 'POST' && path === '/v1/experiments') {
    const key = req.headers['idempotency-key'];
    if (!key) return errorResponse(now, 400, 'INVALID_PARAMETER', '缺少幂等键', [{ path: 'header.Idempotency-Key', code: 'REQUIRED', message: '必填' }]);
    const replay = s.idem.get(`POST:${path}:${key}`);
    if (replay) return replay;
    const b = (req.body ?? {}) as Partial<ExperimentRequest>;
    const errs = (['instance_id', 'hypothesis_id', 'hypothesis_version', 'evaluation_context_id', 'run_purpose', 'random_seed'] as const)
      .filter((k) => b[k] === undefined || b[k] === null || b[k] === '').map((k) => ({ path: `body.${k}`, code: 'REQUIRED', message: '必填' }));
    if (errs.length) return errorResponse(now, 400, 'INVALID_CONTRACT', '实验请求不完整', errs);
    const n = s.experiments.length + 1;
    const exp: CreatedExperiment = {
      experimentId: uuid(`exp:new:${n}`), jobRunId: uuid(`job:new:${n}`), createdAt: now,
      request: b as CreatedExperiment['request'],
    };
    s.experiments.push(exp);
    const res = ok(buildMeta(now, { snapshots: [] }), { job_run_id: exp.jobRunId, resource_id: exp.experimentId, run_state: 'queued', poll_operation_id: 'getJobRun' }, 202);
    s.idem.set(`POST:${path}:${key}`, res);
    return res;
  }

  if (method === 'GET' && (g = m(/^\/v1\/experiments\/([^/]+)$/))) {
    const id = g[1] as string;
    const created = s.experiments.find((e) => e.experimentId === id);
    if (created) {
      const job = buildJobRun(created.jobRunId, created.createdAt, now);
      const st = job.run_state === 'succeeded' ? 'completed' : job.run_state === 'running' ? 'running' : 'queued';
      return ok(buildMeta(now, { snapshots: [] }), buildEvaluation(id, created.request.instance_id, created.request.run_purpose, st, now, { id: created.request.hypothesis_id, version: created.request.hypothesis_version }));
    }
    const key = KNOWN_EXPERIMENTS[id];
    if (key) {
      const inst = (STRATEGY_INSTANCES.find((i) => i.instance_id === uuid(`instance:${key}`)) as { instance_id: string });
      const running = id === uuid('exp:d') || id === uuid('exp:e');
      return ok(buildMeta(now, { snapshots: [] }), buildEvaluation(id, inst.instance_id, 'validation', running ? 'running' : 'completed', now));
    }
    return errorResponse(now, 404, 'EXPERIMENT_NOT_FOUND', '没有这个实验');
  }

  if (method === 'GET' && (g = m(/^\/v1\/leagues\/([^/]+)\/versions\/(\d+)$/))) {
    if (g[1] !== LEAGUE_ID || g[2] !== '1') return errorResponse(now, 404, 'LEAGUE_NOT_FOUND', '没有这个联赛版本');
    return ok(buildMeta(now, { snapshots: [] }), buildLeague());
  }

  if (method === 'GET' && path === '/v1/research/papers') {
    let list = buildPapers().filter((p) =>
      (!q.get('publication_year') || p.publication_year === Number(q.get('publication_year'))) &&
      (!q.get('publication_status') || p.publication_status === q.get('publication_status')) &&
      (!q.get('read_depth') || p.read_depth === q.get('read_depth')));
    const paged = pageOffset(now, q.get('cursor'), 'papers'); if ('status' in paged) return paged;
    const next = paged.offset + LIST_PAGE < list.length ? encodeCursor(['papers', paged.offset + LIST_PAGE]) : null;
    list = list.slice(paged.offset, paged.offset + LIST_PAGE);
    return ok(buildMeta(now, { nextCursor: next, snapshots: [] }), { items: list });
  }

  if (method === 'GET' && (g = m(/^\/v1\/accounts\/([^/]+)$/))) {
    const bad = missingParams(now, q, ['account_version', 'valuation_snapshot_id']); if (bad) return bad;
    if (g[1] !== ACCOUNT_ID) return errorResponse(now, 403, 'ACCOUNT_FORBIDDEN', '无权查看该账户');
    if (Number(q.get('account_version')) !== ACCOUNT_VERSION) return errorResponse(now, 409, 'ACCOUNT_VERSION_STALE', '账户版本已变化，请重新读取', [], true);
    return ok(buildMeta(now, { snapshots: ['valuation'] }), buildAccount(currentRelease(s)));
  }

  if (method === 'GET' && (g = m(/^\/v1\/accounts\/([^/]+)\/actionability\/([^/]+)$/))) {
    if (g[1] !== ACCOUNT_ID) return errorResponse(now, 403, 'ACCOUNT_FORBIDDEN', '无权查看该账户');
    const r = s.releases.find((x) => uuid(`act:${x.id}`) === g?.[2]);
    if (!r) return errorResponse(now, 404, 'ACTIONABILITY_NOT_FOUND', '没有这个可行动快照（可能已过期，请重新读取）');
    const paged = pageOffset(now, q.get('cursor'), g[2] as string); if ('status' in paged) return paged;
    const built = buildActionability(r, now, paged.offset, RANK_PAGE);
    const next = paged.offset + RANK_PAGE < built.total ? encodeCursor([g[2] as string, paged.offset + RANK_PAGE]) : null;
    return ok(buildMeta(now, { releaseId: r.id, decisionAsOf: iso(r.decisionAsOf), snapshots: ['bars', 'quote'], nextCursor: next, actionabilityId: built.data.actionability_id }), built.data);
  }

  if (method === 'GET' && (g = m(/^\/v1\/analysis-runs\/([^/]+)$/))) return ok(buildMeta(now, { snapshots: [] }), buildModelRun(g[1] as string, now));
  if (method === 'GET' && (g = m(/^\/v1\/jobs\/([^/]+)$/))) {
    const e = s.experiments.find((x) => x.jobRunId === g?.[1]);
    if (!e) return errorResponse(now, 404, 'JOB_NOT_FOUND', '没有这个任务');
    return ok(buildMeta(now, { snapshots: [] }), buildJobRun(e.jobRunId, e.createdAt, now));
  }

  // ---- persistent user state ----
  if ((g = m(/^\/v1\/workspace-layouts\/([^/]+)$/))) {
    const id = g[1] as string;
    if (method === 'GET') {
      const l = s.layouts.get(id);
      return l ? ok(buildMeta(now, { snapshots: [] }), l) : errorResponse(now, 404, 'LAYOUT_NOT_FOUND', '还没有保存过布局');
    }
    if (method === 'PUT') {
      return idempotent(s, req, now, () => {
        const b = (req.body ?? {}) as { expected_layout_version?: number; display_name?: string; layout_document?: Record<string, unknown> };
        const cur = s.layouts.get(id);
        const have = cur?.layout_version ?? 0;
        if (b.expected_layout_version !== have) return errorResponse(now, 409, 'VERSION_CONFLICT', '布局已被其他窗口更新，请刷新后再保存', [], false);
        const next: Layout = { layout_id: id, layout_version: have + 1, display_name: b.display_name ?? '布局', layout_document: opaque(b.layout_document ?? {}), updated_at: iso(now) };
        s.layouts.set(id, next);
        return ok(buildMeta(now, { snapshots: [] }), next);
      });
    }
  }

  if (method === 'GET' && path === '/v1/watchlists') return ok(buildMeta(now, { snapshots: [] }), { items: [...s.watchlists.values()] });
  if (method === 'PUT' && (g = m(/^\/v1\/watchlists\/([^/]+)$/))) {
    const id = g[1] as string;
    return idempotent(s, req, now, () => {
      const b = (req.body ?? {}) as { expected_watchlist_version?: number; display_name?: string; description?: string | null; members?: Array<{ instrument_id: string; sort_position: number; note: string | null; added_release_id: string | null }> };
      const cur = s.watchlists.get(id);
      const have = cur?.watchlist_version ?? 0;
      if (b.expected_watchlist_version !== have) return errorResponse(now, 409, 'VERSION_CONFLICT', '自选已被其他窗口更新，请刷新后再保存');
      const prev = new Map((cur?.members ?? []).map((x) => [x.instrument_id, x]));
      const next: Watchlist = {
        watchlist_id: id, watchlist_version: have + 1, display_name: b.display_name ?? '自选', description: b.description ?? null,
        members: (b.members ?? []).map((x) => ({ instrument_id: x.instrument_id, sort_position: x.sort_position, added_release_id: x.added_release_id, note: x.note, added_at: prev.get(x.instrument_id)?.added_at ?? iso(now) })),
        updated_at: iso(now),
      };
      s.watchlists.set(id, next);
      return ok(buildMeta(now, { snapshots: [] }), next);
    });
  }

  if (method === 'GET' && path === '/v1/annotations') {
    const items = [...s.annotations.values()].filter((a) => (!q.get('instrument_id') || a.instrument_id === q.get('instrument_id')) && (!q.get('watchlist_id') || a.watchlist_id === q.get('watchlist_id')));
    return ok(buildMeta(now, { snapshots: [] }), { items });
  }
  if (method === 'PUT' && (g = m(/^\/v1\/annotations\/([^/]+)$/))) {
    const id = g[1] as string;
    return idempotent(s, req, now, () => {
      const b = (req.body ?? {}) as Partial<Annotation> & { expected_annotation_version?: number };
      const cur = s.annotations.get(id);
      const have = cur?.annotation_version ?? 0;
      if (b.expected_annotation_version !== have) return errorResponse(now, 409, 'VERSION_CONFLICT', '标注已被更新，请刷新后再保存');
      const next: Annotation = {
        annotation_id: id, annotation_version: have + 1, instrument_id: b.instrument_id ?? null, watchlist_id: b.watchlist_id ?? null,
        annotation_kind: b.annotation_kind ?? 'note', anchor_release_id: b.anchor_release_id ?? null, anchor_snapshot_id: b.anchor_snapshot_id ?? null,
        body: opaque(b.body ?? {}), updated_at: iso(now),
      };
      s.annotations.set(id, next);
      return ok(buildMeta(now, { snapshots: [] }), next);
    });
  }

  if (method === 'GET' && path === '/v1/updates') {
    const bad = missingParams(now, q, ['slot_key']); if (bad) return bad;
    const len = s.log.length;
    const resume = `${s.epoch}:${len}`;
    const cursor = q.get('cursor');
    let page: UpdatePage;
    if (!cursor) {
      page = { stream_epoch: s.epoch, events: [], resume_cursor: resume, reset_required: false };
    } else {
      const [ep, nStr] = cursor.split(':');
      const n = Number(nStr);
      if (ep !== s.epoch || !Number.isInteger(n) || n < 0 || n > len) {
        page = { stream_epoch: s.epoch, events: [], resume_cursor: resume, reset_required: true };
      } else {
        page = { stream_epoch: s.epoch, events: s.log.slice(n, n + 50), resume_cursor: `${s.epoch}:${Math.min(len, n + 50)}`, reset_required: false };
      }
    }
    return ok(buildMeta(now, { snapshots: [], freshness: 'current' }), page);
  }

  return errorResponse(now, 404, 'NOT_FOUND', `合同中没有这个接口：${method} ${path}`);
}

function pageOffset(now: number, cursor: string | null, expectedId: string): { offset: number } | MockResponse {
  if (!cursor) return { offset: 0 };
  const parts = decodeCursor(cursor);
  if (!parts || parts.length !== 2 || parts[0] !== expectedId || !Number.isInteger(Number(parts[1]))) {
    return errorResponse(now, 409, 'CURSOR_BINDING_MISMATCH', '分页游标与当前榜单版本不匹配，请重新加载第一页');
  }
  return { offset: Number(parts[1]) };
}

function idempotent(s: MockState, req: MockRequest, now: number, run: () => MockResponse): MockResponse {
  const key = req.headers['idempotency-key'];
  if (!key) return errorResponse(now, 400, 'INVALID_PARAMETER', '缺少幂等键', [{ path: 'header.Idempotency-Key', code: 'REQUIRED', message: '必填' }]);
  const k = `${req.method}:${req.path}:${key}`;
  const hit = s.idem.get(k);
  if (hit) return hit;
  const res = run();
  if (res.status === 200) s.idem.set(k, res);
  return res;
}

function devSwitch(s: MockState, req: MockRequest, now: number): MockResponse {
  const action = req.path.replace('/__mock/', '');
  switch (action) {
    case 'publish': {
      const prev = currentRelease(s);
      const n = prev.n + 1;
      s.releases.push({ n, id: uuid(`release:${n}`), decisionAsOf: now - 60_000, publishedAt: now });
      pushEvent(s, now, 'decision.published', req.query.get('slot_key') ?? 'market.default', n, { release_id: uuid(`release:${n}`) });
      pushEvent(s, now, 'actionability.ready', uuid(`act:${uuid(`release:${n}`)}`), 1, {});
      break;
    }
    case 'glitch': {
      // duplicate + out-of-order delivery of earlier events: clients must ignore both
      const last = s.log[s.log.length - 1];
      if (last) s.log.push({ ...last });
      pushEvent(s, now, 'decision.published', req.query.get('slot_key') ?? 'market.default', Math.max(1, currentRelease(s).n - 1), {});
      break;
    }
    case 'expire':
      s.epoch = `epoch-${Number(s.epoch.split('-')[1]) + 1}`;
      s.log = [];
      break;
    case 'stale':
      s.stale = !s.stale;
      pushEvent(s, now, 'source.health.changed', 'syn.quote_feed', s.eventSeq + 1, {});
      break;
    case 'account':
      pushEvent(s, now, 'account.updated', ACCOUNT_ID, s.eventSeq + 1, {});
      break;
    case 'state':
      break;
    default:
      return errorResponse(now, 404, 'NOT_FOUND', '未知的 mock 开关');
  }
  return { status: 200, body: { mock: true, stale: s.stale, epoch: s.epoch, releases: s.releases.map((r) => r.id), current: currentRelease(s).id, events: s.log.length } };
}

