/** 我的账户: authorized account selector with 委托 / 绩效 / 机会 tabs. */
import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { Badge, MissingText } from '../../../components/Badges';
import { EmptyState, Loading, SectionTitle } from '../../../components/States';
import { fmtInt, fmtTime, SIDE_LABEL } from '../../../lib/format';
import { extGet } from './api';
import { ArtifactView, ExtError, LoadMore, Pending, StockLink, Strip, useNowAnchor, usePaged } from './common';
import { fmtMetricValue } from './fmt';
import { isoSeconds } from './time';
import { OpportunitiesView } from './Opportunities';
import type { AccountListEntry, AccountsData, OpportunitiesData, OrdersData, PerformanceData, ReadModelContext, ReadModelContextData } from './types';

type Tab = 'orders' | 'performance' | 'opportunities';
const TABS: Array<[Tab, string]> = [['orders', '委托'], ['performance', '绩效'], ['opportunities', '机会']];
export const COST_PROFILES = [['x1', '成本 x1（基准）'], ['x2', '成本 x2'], ['x3', '成本 x3']] as const;
export const VALUATION_POLICY = 'last-raw-close-in-snapshot-v1';
export const performancePolicy = (c: string): string => `twr-cost-${c}-v1`;

export const NO_READMODEL_TEXT = '尚无已生成的读模型（由任务 account_readmodel_build 或 CLI 生成）';
const newest = (cs: ReadModelContext[], kind: ReadModelContext['projection_kind']): ReadModelContext | undefined => cs.find((c) => c.projection_kind === kind); // contexts arrive newest first

function Orders({ acct, ctx }: { acct: AccountListEntry; ctx: ReadModelContext | undefined }) {
  const params = useMemo(() => ({ account_id: acct.account_id, account_version: ctx?.account_version, window: ctx?.window, knowledge_as_of: ctx?.knowledge_as_of }), [acct, ctx]);
  const paged = usePaged<OrdersData>('query-account-orders', params, 50, !!ctx);
  if (!ctx) return <EmptyState>{NO_READMODEL_TEXT}</EmptyState>;
  const orders = paged.pages.flatMap((p) => p.data.orders);
  return (
    <div>
      <Strip meta={paged.pages[0]?.meta} title="账户委托" />
      <Pending loading={paged.isLoading} error={paged.error} onRetry={paged.refetch} what="委托" />
      {!paged.isLoading && !paged.error && orders.length === 0 && <EmptyState>这个读模型窗口内没有委托。</EmptyState>}
      {orders.length > 0 && (
        <div className="table-wrap"><table className="table"><caption className="sr-only">委托（窗口 {fmtTime(ctx.window.start)} ~ {fmtTime(ctx.window.end)}）</caption>
          <thead><tr><th scope="col">证券</th><th scope="col">方向</th><th scope="col">状态</th><th scope="col" className="num">委托量</th><th scope="col" className="num">已成交</th><th scope="col" className="num">剩余</th><th scope="col" className="num">限价</th><th scope="col">状态时间</th></tr></thead>
          <tbody>{orders.map((o) => (
            <tr key={o.order_id}>
              <td><StockLink id={o.instrument_id} /></td>
              <td>{SIDE_LABEL[o.side] ?? o.side}</td>
              <td><Badge tone={o.quality_status === 'valid' ? 'neutral' : 'warn'} title={`quality_status = ${o.quality_status}`}>{o.state}</Badge></td>
              <td className="num">{fmtInt(o.original_quantity)}</td><td className="num">{fmtInt(o.cumulative_filled_quantity)}</td><td className="num">{fmtInt(o.leaves_quantity)}</td>
              <td className="num">{o.limit_price_raw ?? '—'}</td>
              <td className="small muted">{fmtTime(o.state_observed_at)}</td>
            </tr>))}</tbody></table></div>
      )}
      <LoadMore paged={paged} />
    </div>
  );
}

function Performance({ acct, ctx }: { acct: AccountListEntry; ctx: ReadModelContext | undefined }) {
  const [cost, setCost] = useState<string>('x1');
  const params = useMemo(() => ({
    account_id: acct.account_id, account_version: ctx?.account_version, window: ctx?.window, knowledge_as_of: ctx?.knowledge_as_of,
    valuation_snapshot_refs: ctx?.valuation_snapshot_refs ?? [], performance_policy_version: performancePolicy(cost), valuation_policy_version: VALUATION_POLICY,
  }), [acct, ctx, cost]);
  const q = useQuery({ queryKey: ['extc', 'performance', params], enabled: !!ctx, queryFn: ({ signal }) => extGet<PerformanceData>('query-account-performance', params, signal) });
  const p = q.data?.data.performance;
  return (
    <div>
      <div className="filters">
        <label className="select-label">交易成本假设
          <select value={cost} onChange={(e) => setCost(e.target.value)}>{COST_PROFILES.map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select>
        </label>
        {ctx && <span className="muted small">窗口 {fmtTime(ctx.window.start)} ~ {fmtTime(ctx.window.end)} · 时间加权收益 · 估值口径 {VALUATION_POLICY}</span>}
      </div>
      {!ctx ? <EmptyState>{NO_READMODEL_TEXT}</EmptyState> : q.isLoading ? <Loading /> : q.error ? <ExtError error={q.error} onRetry={() => { void q.refetch(); }} what="绩效" notReadyText="该成本档位尚未生成" /> : p ? (
        <>
          <Strip meta={q.data?.meta} title="账户绩效" />
          {p.metrics.length === 0 ? <EmptyState>没有绩效指标。</EmptyState> : (
            <div className="table-wrap"><table className="table"><caption className="sr-only">账户绩效指标</caption>
              <thead><tr><th scope="col">指标</th><th scope="col" className="num">取值</th><th scope="col">单位</th><th scope="col" className="num">样本数</th><th scope="col">缺失原因</th></tr></thead>
              <tbody>{p.metrics.map((m) => (
                <tr key={`${m.name}-${m.definition_version}`}>
                  <td>{m.name}<div className="muted small">定义 {m.definition_version}</div></td>
                  <td className="num">{m.value === null ? <MissingText /> : fmtMetricValue(m.value, m.unit)}</td>
                  <td>{m.unit}</td><td className="num">{m.sample_count}</td><td className="small">{m.missing_reason ?? ''}</td>
                </tr>))}</tbody></table></div>
          )}
          <details className="sub-panel"><summary>明细（{p.artifact.contract_id}）</summary><ArtifactView artifact={p.artifact} /></details>
        </>
      ) : null}
    </div>
  );
}

function Opportunities({ acct, nowMs }: { acct: AccountListEntry; nowMs: number }) {
  // experiment_id is nullable in the contract; null is sent as an omitted query param.
  const params = useMemo(() => ({ account_id: acct.account_id, experiment_id: null, as_of: isoSeconds(nowMs) }), [acct, nowMs]);
  const paged = usePaged<OpportunitiesData>('query-opportunities', params);
  const first = paged.pages[0]?.data.opportunity_artifact;
  return (
    <div>
      <Strip meta={paged.pages[0]?.meta} title="账户机会" />
      <Pending loading={paged.isLoading} error={paged.error} onRetry={paged.refetch} what="机会" />
      {paged.pages.map((p, i) => <ArtifactView key={`${p.data.opportunity_artifact.ref}-${i}`} artifact={p.data.opportunity_artifact} render={(a) => <OpportunitiesView body={a.body} />} />)}
      {!paged.isLoading && !paged.error && !first && <EmptyState>当前没有机会。</EmptyState>}
      <LoadMore paged={paged} />
    </div>
  );
}

export function MyAccounts() {
  const [now] = useNowAnchor();
  const accounts = usePaged<AccountsData>('list-authorized-accounts', {}, 50);
  const list = accounts.pages.flatMap((p) => p.data.accounts);
  const [sel, setSel] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>('orders');
  const acct = list.find((a) => a.account_id === sel) ?? list[0] ?? null;
  const ctxQ = useQuery({
    queryKey: ['extc', 'readmodel-context', acct?.account_id], enabled: !!acct,
    queryFn: ({ signal }) => extGet<ReadModelContextData>('c/readmodel-context', { account_id: acct?.account_id }, signal),
  });
  const contexts = ctxQ.data?.data.contexts ?? [];
  return (
    <section aria-label="我的账户">
      <SectionTitle title="我的账户" hint="只列出你被授权查看的账户。委托与绩效来自已生成的读模型（取最新一份）；尚未生成时会明确提示，不会补 0。" />
      <Pending loading={accounts.isLoading} error={accounts.error} onRetry={accounts.refetch} what="账户列表" />
      {!accounts.isLoading && !accounts.error && !acct && <EmptyState>你还没有被授权的账户。</EmptyState>}
      {acct && (
        <>
          <div className="filters">
            <label className="select-label">账户
              <select value={acct.account_id} onChange={(e) => setSel(e.target.value)}>
                {list.map((a) => <option key={a.account_id} value={a.account_id}>{a.account_id.slice(0, 8)} · v{a.account_version} · {a.base_currency}</option>)}
              </select>
            </label>
          </div>
          <div className="tabs" role="tablist" aria-label="账户视图">
            {TABS.map(([k, l]) => <button key={k} role="tab" id={`acct-tab-${k}`} aria-selected={tab === k} aria-controls="acct-panel" onClick={() => setTab(k)}>{l}</button>)}
          </div>
          <div id="acct-panel" role="tabpanel" aria-labelledby={`acct-tab-${tab}`}>
            {tab !== 'opportunities' && (ctxQ.isLoading ? <Loading /> : ctxQ.error ? <ExtError error={ctxQ.error} onRetry={() => { void ctxQ.refetch(); }} what="读模型" /> : null)}
            {tab === 'orders' && !ctxQ.isLoading && !ctxQ.error && <Orders acct={acct} ctx={newest(contexts, 'orders')} />}
            {tab === 'performance' && !ctxQ.isLoading && !ctxQ.error && <Performance acct={acct} ctx={newest(contexts, 'performance')} />}
            {tab === 'opportunities' && <Opportunities acct={acct} nowMs={now} />}
          </div>
        </>
      )}
    </section>
  );
}
