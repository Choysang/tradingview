/**
 * Stock workbench /stock/:instrumentId (spec 7, 4.1, 4.2).
 * Layout: header (identity, quote time, release time, horizon, plan state) / chart /
 * four evidence tabs on the left; live quote overlay, plan cards, strategy
 * contributions, notes and drawings on the right. The frozen release analysis and the
 * live quote overlay are separate cards with their own timestamps.
 */
import { useMemo, useState } from 'react';
import { useQueries, useQuery } from '@tanstack/react-query';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { ops } from '../api/ops';
import { keys, planQuery, workspaceQuery } from '../api/keys';
import type { Annotation, BarWindow, QuoteSnapshot } from '../api/types';
import { Badge, FreshnessBadge, GradeBadge, MissingText } from '../components/Badges';
import { DetailsButton } from '../components/Diagnostics';
import { CommunityTab, EVIDENCE_TABS, FundTab, NewsTab, SeatsTab } from '../components/EvidenceTabs';
import { WatchStar } from '../components/Lists';
import { MetaStrip } from '../components/MetaStrip';
import { PlanCard } from '../components/PlanCard';
import { PriceChart, type ChartLine } from '../components/PriceChart';
import { ReleaseBar } from '../components/ReleaseBar';
import { EmptyState, ErrorState, Loading, SectionTitle } from '../components/States';
import { useToast } from '../components/Toast';
import { QUOTE_POLICY_VERSION, horizonByKey } from '../config';
import { useAccountContext } from '../hooks/useAccount';
import { useReleaseSelection } from '../hooks/useRelease';
import { useStrategyCatalog } from '../hooks/useStrategyCatalog';
import { useTheme } from '../hooks/useTheme';
import { useAnnotations, useLayout, type ChartTimeframe, type HlineBody } from '../hooks/useUserState';
import { deriveBarsParams } from '../lib/barsParams';
import { fmtPrice, fmtTime, PLAN_STATE_LABEL, ROLE_LABEL, VIEW_KIND_LABEL } from '../lib/format';
import { gradeOfInstance } from '../lib/grades';
import { classifyPlan, sortPlans } from '../lib/planKind';
import { toChartNumber } from '../lib/decimal';
import type { AdjustmentMode } from '../api/types';

const TF_GROUPS: Array<{ label: string; options: Array<[ChartTimeframe, string]> }> = [
  { label: '分钟', options: [['1m', '1分'], ['5m', '5分'], ['15m', '15分'], ['1h', '60分']] },
  { label: '日', options: [['1d', '日线']] },
  { label: '周', options: [['1w', '周线']] },
  { label: '月', options: [['1mo', '月线']] },
  { label: '年', options: [['1y', '年线']] },
];
const ADJ_OPTIONS: Array<[AdjustmentMode, string]> = [['raw', '不复权（原始价）'], ['forward_adjusted', '前复权'], ['backward_adjusted', '后复权']];

export function StockPage() {
  const { instrumentId = '' } = useParams();
  const rel = useReleaseSelection();
  const [sp, setSp] = useSearchParams();
  const acct = useAccountContext();
  const horizon = horizonByKey(sp.get('h'));
  const view = sp.get('view') === 'account' && acct.complete ? 'account_holding' : 'public_research';
  const rid = rel.effectiveReleaseId;

  const wsQ = useQuery({ ...workspaceQuery(instrumentId, rid, view, acct.ctx.accountId), placeholderData: (p) => p });
  const ws = wsQ.data;
  const catalog = useStrategyCatalog();
  const layout = useLayout();
  const theme = useTheme();
  const toast = useToast();
  const [drawMode, setDrawMode] = useState(false);
  const tf = layout.doc.timeframe;
  const adj = layout.doc.adjustment;

  // ---- plans: main + sub-plans, all pinned to the workspace release
  const planQs = useQueries({ queries: (ws?.data.plan_refs ?? []).map((p) => planQuery(p.plan_id, p.plan_version)) });
  const plans = useMemo(() => {
    const byInstance = catalog.data?.byInstance ?? new Map();
    return sortPlans(planQs.filter((q) => q.data).map((q) => ({ env: q.data!, kind: classifyPlan(q.data!.data, byInstance) })));
  }, [planQs, catalog.data]);
  const mainPlan = plans.find((p) => p.kind === 'main') ?? plans[0];

  // ---- bars (frozen, from the release's snapshot)
  const barsParams = ws ? deriveBarsParams(ws, tf, adj) : null;
  const barsQ = useQuery({
    queryKey: barsParams?.ok ? keys.bars(instrumentId, tf, adj, barsParams.params.adjustment_version, barsParams.params.snapshot_id, barsParams.params.start, barsParams.params.end) : ['bars', 'none'],
    queryFn: ({ signal }) => ops.getStockBars(instrumentId, (barsParams as { ok: true; params: Parameters<typeof ops.getStockBars>[1] }).params, { signal }),
    enabled: !!barsParams?.ok,
    placeholderData: (p) => p,
    staleTime: 60_000,
  });

  // ---- live quote overlay: independent of the frozen analysis
  const policy = ws?.data.quote_overlay?.quote_policy_version ?? QUOTE_POLICY_VERSION;
  const quoteQ = useQuery({
    queryKey: keys.quote(instrumentId, policy ?? 'none'),
    queryFn: ({ signal }) => ops.getStockQuote(instrumentId, { quote_policy_version: policy as string }, { signal }),
    enabled: !!policy,
    refetchInterval: 5_000,
  });
  const quote: QuoteSnapshot | null = quoteQ.data?.data ?? ws?.data.quote_overlay ?? null;

  const ann = useAnnotations(instrumentId);
  const wsReleaseId = ws?.data.release_id ?? null;
  const staleWs = !!ws && !!rid && ws.data.release_id !== rid;

  const setView = (v: 'public' | 'account'): void => setSp((p) => { const n = new URLSearchParams(p); if (v === 'account') n.set('view', 'account'); else n.delete('view'); return n; }, { replace: true });

  // ---- chart lines: plan zone (raw only) + visible drawings
  const lines: ChartLine[] = useMemo(() => {
    const out: ChartLine[] = [];
    const zone = mainPlan?.env.data.entry_price_zone;
    if (adj === 'raw' && zone) {
      const lo = toChartNumber(zone.lower_price_raw); const hi = toChartNumber(zone.upper_price_raw);
      if (lo !== null) out.push({ id: 'zone-lo', price: lo, title: '买入区间下沿', tone: 'plan', dashed: true });
      if (hi !== null && hi !== lo) out.push({ id: 'zone-hi', price: hi, title: '买入区间上沿', tone: 'plan', dashed: true });
    }
    for (const a of ann.items) {
      const b = a.body as unknown as Partial<HlineBody>;
      if (a.annotation_kind === 'drawing' && b.kind === 'hline' && !b.hidden && b.adjustment_mode === adj) {
        const p = toChartNumber(b.price);
        if (p !== null) out.push({ id: a.annotation_id, price: p, title: b.label ?? '水平线', tone: 'draw' });
      }
    }
    return out;
  }, [mainPlan, ann.items, adj]);

  if (wsQ.isLoading || (!ws && rel.pointer.isLoading)) return <div className="page"><Loading label="读取个股工作台…" /></div>;
  if (wsQ.error && !ws) return <div className="page"><ErrorState error={wsQ.error} what="个股工作台" onRetry={() => void wsQ.refetch()} /></div>;
  if (!ws) return <div className="page"><EmptyState>当前没有可显示的发布版本。</EmptyState></div>;

  const inst = ws.data.instrument;
  const barWindow: BarWindow | null = barsQ.data?.data ?? null;
  const picked = (price: number): void => {
    ann.save({ kind: 'drawing', body: { kind: 'hline', price: price.toFixed(2), label: `水平线 ${price.toFixed(2)}`, adjustment_mode: adj } satisfies HlineBody, releaseId: wsReleaseId, snapshotId: barWindow?.snapshot_id ?? null });
    setDrawMode(false);
    toast.push('info', `已保存水平线 ${price.toFixed(2)}（${ADJ_OPTIONS.find((o) => o[0] === adj)?.[1]}）`);
  };
  const noteAnnotations = ann.items.filter((a) => a.annotation_kind !== 'drawing');
  const drawAnnotations = ann.items.filter((a) => a.annotation_kind === 'drawing' && !(a.body as { hidden?: boolean }).hidden);

  return (
    <div className="page">
      <ReleaseBar rel={rel} displayedReleaseId={wsReleaseId} />
      <p><Link to={`/?h=${horizon.key}${rel.pinnedReleaseId ? `&release=${rel.pinnedReleaseId}` : ''}`}>← 返回首页</Link></p>

      <header className="card stock-head">
        <div className="stock-title">
          <h1>{inst.display_name}</h1>
          <span className="muted mono">{inst.source_symbols[0]?.source_symbol}</span>
          {inst.is_risk_warning && <Badge tone="warn">风险警示</Badge>}
          {inst.trading_status !== 'active' && <Badge tone="bad">{inst.trading_status}</Badge>}
          <WatchStar instrumentId={instrumentId} releaseId={wsReleaseId} />
        </div>
        <dl className="facts">
          <div><dt>报价时间</dt><dd>{quote?.quote.source_event_time ? fmtTime(quote.quote.source_event_time) : <MissingText>无报价</MissingText>}</dd></div>
          <div><dt>分析发布</dt><dd>{fmtTime(rel.release.data?.data.published_at)}</dd></div>
          <div><dt>分析数据截至</dt><dd>{fmtTime(ws.meta.decision_as_of)}</dd></div>
          <div><dt>目标期限</dt><dd>{horizon.label}</dd></div>
          <div><dt>计划状态</dt><dd>{mainPlan ? PLAN_STATE_LABEL[mainPlan.env.data.state] : <MissingText>本次发布无计划</MissingText>}</dd></div>
          <div><dt>视角</dt><dd>
            <span className="segmented small-seg" role="group" aria-label="视角">
              <button className={view === 'public_research' ? 'on' : ''} onClick={() => setView('public')}>{VIEW_KIND_LABEL.public_research}</button>
              <button className={view === 'account_holding' ? 'on' : ''} disabled={!acct.complete} title={acct.complete ? '' : '未选择账户'} onClick={() => setView('account')}>{VIEW_KIND_LABEL.account_holding}</button>
            </span>
          </dd></div>
        </dl>
        <MetaStrip meta={ws.meta} title="个股工作台（冻结分析）" extra={staleWs ? <Badge tone="warn">正在更新，显示旧版本</Badge> : undefined} rows={[
          ['projection_version', String(ws.data.projection_version)], ['projection_hash', ws.data.projection_hash], ['scope_key', ws.data.scope_key], ['view_kind', ws.data.view_kind],
          ['features', ws.data.features.map((f) => `${f.key.feature_id}:${f.result_state}`).join('、')],
        ]} />
      </header>

      <div className="workbench">
        <div className="wb-main">
          <section className="card">
            <SectionTitle title="K线图" right={<DetailsButton title="K线窗口" meta={barsQ.data?.meta} rows={barWindow ? [['window_hash', barWindow.window_hash], ['snapshot_id', barWindow.snapshot_id], ['session_policy_version', barWindow.session_policy_version], ['adjustment', `${barWindow.adjustment_mode} ${barWindow.adjustment_version ?? ''}`], ['window', `${fmtTime(barWindow.start)} ~ ${fmtTime(barWindow.end)}`]] : []} />} />
            <div className="chart-toolbar">
              <div className="segmented" role="tablist" aria-label="周期">
                {TF_GROUPS.map((g) => g.options.map(([v, label]) => (
                  <button key={v} role="tab" aria-selected={tf === v} className={tf === v ? 'on' : ''} onClick={() => layout.save({ ...layout.doc, timeframe: v })} title={`${g.label}周期`}>{label}</button>
                )))}
              </div>
              <label className="select-label">复权
                <select value={adj} onChange={(e) => layout.save({ ...layout.doc, adjustment: e.target.value as AdjustmentMode })} aria-label="复权方式">
                  {ADJ_OPTIONS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                </select>
              </label>
              <label className="check"><input type="checkbox" checked={layout.doc.panes.volume} onChange={(e) => layout.save({ ...layout.doc, panes: { ...layout.doc.panes, volume: e.target.checked } })} />成交量</label>
              <label className="check"><input type="checkbox" checked={layout.doc.panes.ma} onChange={(e) => layout.save({ ...layout.doc, panes: { ...layout.doc.panes, ma: e.target.checked } })} />MA</label>
              <label className="check"><input type="checkbox" checked={layout.doc.panes.ema} onChange={(e) => layout.save({ ...layout.doc, panes: { ...layout.doc.panes, ema: e.target.checked } })} />EMA</label>
              <label className="check"><input type="checkbox" checked={layout.doc.panes.macd} onChange={(e) => layout.save({ ...layout.doc, panes: { ...layout.doc.panes, macd: e.target.checked } })} />MACD</label>
              <button className={`btn btn-small ${drawMode ? 'btn-on' : ''}`} aria-pressed={drawMode} onClick={() => setDrawMode((d) => !d)}>{drawMode ? '点击图上位置画线…' : '画水平线'}</button>
              {layout.saving && <span className="muted small">保存布局…</span>}
            </div>
            <p className="muted small">
              当前：{ADJ_OPTIONS.find((o) => o[0] === adj)?.[1]}。{adj !== 'raw' ? '复权价只用于看走势，买卖价区间仍按原始价，已在复权图上隐藏。' : '买卖价格区间按原始可交易价格显示。'}
              布局随账户保存在服务器。
            </p>
            {barsParams && !barsParams.ok && <div className="notice notice-warn">{barsParams.reason}</div>}
            {barsQ.isLoading && <Loading label="读取K线…" />}
            {barsQ.error && !barWindow && <ErrorState error={barsQ.error} what="K线" onRetry={() => void barsQ.refetch()} />}
            {barWindow && (
              <>
                {barWindow.bars.length === 0 ? <EmptyState>该窗口没有K线（{barWindow.window_complete ? '已确认为空' : '未确认完整，可能只是尚未取到'}）。</EmptyState> : (
                  <PriceChart bars={barWindow} showVolume={layout.doc.panes.volume} lines={lines} drawMode={drawMode} onPickPrice={picked} themeVersion={theme.version} />
                )}
                <BarNotes win={barWindow} meta={barsQ.data!.meta} />
                <IndicatorPanes layoutPanes={layout.doc.panes} features={ws.data.features} />
              </>
            )}
          </section>

          <section className="card">
            <SectionTitle title="四个维度的证据" hint="消息、情绪、资金、龙虎榜各自独立呈现，不合成一个分数。" />
            <div className="segmented" role="tablist" aria-label="证据维度">
              {EVIDENCE_TABS.map((t) => <button key={t.key} role="tab" aria-selected={layout.doc.evidenceTab === t.key} className={layout.doc.evidenceTab === t.key ? 'on' : ''} onClick={() => layout.save({ ...layout.doc, evidenceTab: t.key })}>{t.label}</button>)}
            </div>
            <p className="muted small">{EVIDENCE_TABS.find((t) => t.key === layout.doc.evidenceTab)?.hint}</p>
            {wsReleaseId && layout.doc.evidenceTab === 'news' && <NewsTab instrumentId={instrumentId} releaseId={wsReleaseId} />}
            {wsReleaseId && layout.doc.evidenceTab === 'community' && <CommunityTab instrumentId={instrumentId} releaseId={wsReleaseId} />}
            {wsReleaseId && layout.doc.evidenceTab === 'fund' && <FundTab instrumentId={instrumentId} releaseId={wsReleaseId} />}
            {wsReleaseId && layout.doc.evidenceTab === 'seats' && <SeatsTab instrumentId={instrumentId} releaseId={wsReleaseId} />}
          </section>
        </div>

        <aside className="wb-side">
          <QuoteCard quote={quote} loading={quoteQ.isLoading} error={quoteQ.error} />

          <section aria-label="交易计划">
            <SectionTitle title="买卖计划" hint="冻结在本次发布。计划是条件与区间，不是一定能成交的价格。" />
            {ws.data.plan_refs.length === 0 && <EmptyState>本次发布没有为这只股票生成计划（数据不足时不会硬凑）。</EmptyState>}
            {planQs.some((q) => q.isLoading) && <Loading />}
            {planQs.map((q, i) => q.error ? <ErrorState key={i} error={q.error} what="计划" onRetry={() => void q.refetch()} /> : null)}
            {wsReleaseId && plans.map((p) => (
              <PlanCard key={`${p.env.data.plan_id}:${p.env.data.plan_version}`} planEnv={p.env} kind={p.kind} workspaceReleaseId={wsReleaseId}
                bars={barWindow} quote={quote} byInstance={catalog.data?.byInstance ?? new Map()} instanceById={catalog.data?.instanceById ?? new Map()} />
            ))}
          </section>

          <section className="card" aria-label="策略贡献">
            <SectionTitle title="哪些策略把它选出来" hint="贡献分是各策略自己的相对分，不能相加成概率。" />
            {ws.data.strategy_contributions.length === 0 ? <EmptyState>没有策略贡献记录。</EmptyState> : (
              <ul className="plain">
                {ws.data.strategy_contributions.map((c) => {
                  const s = catalog.data?.byInstance.get(c.instance_ref.instance_id);
                  const i = catalog.data?.instanceById.get(c.instance_ref.instance_id);
                  return (
                    <li key={c.instance_ref.instance_id}>
                      <b>{s?.display_name ?? `策略 ${c.instance_ref.strategy_id.slice(0, 8)}`}</b> <span className="muted small">（{ROLE_LABEL[c.role]}）</span>{' '}
                      <GradeBadge grade={i ? gradeOfInstance(i) : 'unregistered'} /> <span className="muted small">贡献分 {c.contribution_score}</span>
                    </li>
                  );
                })}
              </ul>
            )}
          </section>

          <section className="card" aria-label="笔记与画线">
            <SectionTitle title="我的笔记与画线" hint="保存在服务器，与图表布局一样不会因清缓存丢失。" />
            <NoteForm onSave={(text) => ann.save({ kind: 'note', body: { kind: 'note', text }, releaseId: wsReleaseId, snapshotId: barWindow?.snapshot_id ?? null })} saving={ann.saving} />
            {ann.isLoading && <Loading />}
            {ann.error && <ErrorState error={ann.error} what="笔记" />}
            <ul className="plain">
              {noteAnnotations.map((a) => <li key={a.annotation_id}>{(a.body as { text?: string }).text ?? '（无内容）'} <span className="muted small">· {fmtTime(a.updated_at)}</span></li>)}
              {drawAnnotations.map((a) => <HlineItem key={a.annotation_id} a={a} onHide={() => ann.save({ existing: a, kind: 'drawing', body: { ...(a.body as object), hidden: true }, releaseId: a.anchor_release_id, snapshotId: a.anchor_snapshot_id })} />)}
            </ul>
            {noteAnnotations.length === 0 && drawAnnotations.length === 0 && !ann.isLoading && <p className="muted small">暂无。</p>}
          </section>
        </aside>
      </div>
    </div>
  );
}

function HlineItem({ a, onHide }: { a: Annotation; onHide: () => void }) {
  const b = a.body as unknown as HlineBody;
  return <li>水平线 {fmtPrice(b.price)} 元（{b.adjustment_mode === 'raw' ? '原始价' : b.adjustment_mode === 'forward_adjusted' ? '前复权' : '后复权'}） <button className="link-btn" onClick={onHide}>隐藏</button></li>;
}

function NoteForm({ onSave, saving }: { onSave: (text: string) => void; saving: boolean }) {
  const [text, setText] = useState('');
  return (
    <form className="note-form" onSubmit={(e) => { e.preventDefault(); const t = text.trim(); if (t) { onSave(t); setText(''); } }}>
      <input value={text} onChange={(e) => setText(e.target.value)} placeholder="写一条笔记…" aria-label="新笔记" maxLength={500} />
      <button className="btn btn-small" disabled={saving || !text.trim()}>保存</button>
    </form>
  );
}

function QuoteCard({ quote, loading, error }: { quote: QuoteSnapshot | null; loading: boolean; error: unknown }) {
  return (
    <section className="card quote-card" aria-label="实时报价覆盖层">
      <div className="quote-head">
        <b>实时报价</b>
        <Badge tone="neutral" title="报价是独立的覆盖层，不会改写左侧冻结的分析">独立于冻结分析</Badge>
        {quote && <FreshnessBadge state={quote.freshness_state} />}
      </div>
      {loading && !quote && <Loading />}
      {!quote && !loading && (error ? <ErrorState error={error} what="报价" /> : <EmptyState>没有报价。不会用日线收盘价冒充实时报价。</EmptyState>)}
      {quote && (
        <>
          <div className="quote-price">{quote.quote.last_price_raw ? fmtPrice(quote.quote.last_price_raw) : <MissingText>无最新价</MissingText>}<span className="muted small"> 元（原始价）</span></div>
          <div className="muted small">
            买一 {quote.quote.bid_price_raw ? fmtPrice(quote.quote.bid_price_raw) : <MissingText />} · 卖一 {quote.quote.ask_price_raw ? fmtPrice(quote.quote.ask_price_raw) : <MissingText />}
          </div>
          <div className="muted small">报价时间 {fmtTime(quote.quote.source_event_time)} · 判断新鲜度于 {fmtTime(quote.freshness_evaluated_at)}</div>
          <DetailsButton title="报价详情" rows={[['quote_snapshot_id', quote.quote_snapshot_id], ['quote_policy_version', quote.quote_policy_version], ['live_quote_version', String(quote.quote.live_quote_version)], ['source', `${quote.quote.source_id}/${quote.quote.endpoint_id}`], ['available_at', fmtTime(quote.quote.available_at)]]} />
        </>
      )}
    </section>
  );
}

function BarNotes({ win, meta }: { win: BarWindow; meta: { quality_status: string } }) {
  const gaps = win.bars.filter((b) => b.quality_status !== 'valid').length;
  const unfinished = win.bars.filter((b) => !b.is_final).length;
  return (
    <ul className="flags">
      <li className="flag">复权方式：{win.adjustment_mode === 'raw' ? '不复权（原始价）' : win.adjustment_mode === 'forward_adjusted' ? `前复权（${win.adjustment_version}）` : `后复权（${win.adjustment_version}）`}</li>
      {!win.window_complete && <li className="flag flag-warn">窗口未确认完整：{gaps > 0 ? `有 ${gaps} 根K线数据源缺失，` : '请求区间超出数据集声明的覆盖范围，或交易日历无法核验，'}缺口保持空白，没有用前值填补。</li>}
      {unfinished > 0 && <li className="flag flag-warn">最后 {unfinished} 根K线所在周期尚未结束（is_final = false），颜色较浅。</li>}
      {meta.quality_status !== 'valid' && gaps === 0 && <li className="flag flag-warn">本窗口数据质量：{meta.quality_status}</li>}
    </ul>
  );
}

/**
 * MA / EMA / MACD must come from server values. v3.0.0 has no operation or field that
 * carries indicator series (gap G2), so the panes show what the server reports about the
 * feature and say plainly that the series is missing. Nothing is computed here.
 */
function IndicatorPanes({ layoutPanes, features }: { layoutPanes: { ma: boolean; ema: boolean; macd: boolean }; features: Array<{ key: { feature_id: string }; result_state: string; computed_at: string | null }> }) {
  const rows: Array<[keyof typeof layoutPanes, string, string]> = [['ma', '均线 MA', 'price.ma'], ['ema', '指数均线 EMA', 'price.ema'], ['macd', 'MACD', 'price.macd']];
  const shown = rows.filter(([k]) => layoutPanes[k]);
  if (shown.length === 0) return null;
  return (
    <div className="indicator-panes" aria-label="指标副图">
      {shown.map(([k, label, id]) => {
        const f = features.find((x) => x.key.feature_id === id);
        const why = !f ? '服务器没有为这次发布提供该指标' : f.result_state === 'READY' ? `服务器已计算（${fmtTime(f.computed_at)}），但当前接口版本没有提供可读取的数值序列` : f.result_state === 'FAILED' ? '服务器计算失败' : '服务器仍在计算';
        return (
          <div key={k} className="indicator-pane">
            <b>{label}</b> <MissingText>缺失</MissingText>
            <span className="muted small"> {why}。页面不会自己计算指标。</span>
          </div>
        );
      })}
    </div>
  );
}
