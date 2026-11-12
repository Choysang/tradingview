/**
 * Papers, data status, portfolio. Small pages that share the same honesty rules.
 */
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { ops } from '../api/ops';
import { dataStatusQuery } from '../api/keys';
import type { PaperMethodCard, SourceHealth } from '../api/types';
import { AccountSummary, ActionabilityList } from '../components/Account';
import { Badge, FreshnessBadge, MissingText } from '../components/Badges';
import { MetaStrip } from '../components/MetaStrip';
import { ReleaseBar } from '../components/ReleaseBar';
import { EmptyState, ErrorState, Loading, SectionTitle } from '../components/States';
import { usePinnedPages } from '../hooks/usePinnedPages';
import { useAccountContext, useAccountProjection } from '../hooks/useAccount';
import { useReleaseSelection } from '../hooks/useRelease';
import { useWatchlists } from '../hooks/useUserState';
import { PUB_STATUS_LABEL, READ_DEPTH_LABEL, RUNTIME_STATE_LABEL, fmtAge, fmtTime } from '../lib/format';
import type { Tone } from '../lib/version';

export function PapersPage() {
  const [status, setStatus] = useState('');
  const [depth, setDepth] = useState('');
  const pages = usePinnedPages({
    queryKey: ['papers', status, depth],
    fetchPage: (cursor, signal) => ops.listPapers({
      ...(status ? { publication_status: status as PaperMethodCard['publication_status'] } : {}),
      ...(depth ? { read_depth: depth as PaperMethodCard['read_depth'] } : {}), cursor,
    }, { signal }),
    pinOf: () => ({ releaseId: null, rankingId: `${status}|${depth}`, actionabilityId: null }),
    entriesOf: (p) => p.data.items,
    keyOf: (x) => `${x.paper_id}:${x.paper_revision}`,
  });
  return (
    <div className="page">
      <SectionTitle title="研究论文" hint="每篇论文记录读到了哪一层。论文作者报告的结果和本地复现的结果是两回事，没复现的不会被当作已验证策略。" />
      <div className="filters">
        <label className="select-label">发表状态<select value={status} onChange={(e) => setStatus(e.target.value)}><option value="">全部</option>{Object.entries(PUB_STATUS_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
        <label className="select-label">阅读深度<select value={depth} onChange={(e) => setDepth(e.target.value)}><option value="">全部</option>{Object.entries(READ_DEPTH_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
      </div>
      {pages.isLoading && <Loading />}
      {pages.error && !pages.firstPage && <ErrorState error={pages.error} what="论文列表" onRetry={pages.reload} />}
      {pages.firstPage && <MetaStrip meta={pages.firstPage.meta} title="论文列表" hideCoverage />}
      {pages.firstPage && pages.paging.entries.length === 0 && <EmptyState>没有符合条件的论文。</EmptyState>}
      <ul className="cards-list">
        {pages.paging.entries.map((p) => (
          <li key={p.paper_id} className="card item">
            <div className="item-head"><b>{p.title}</b></div>
            <div className="item-head">
              <Badge tone={p.publication_status === 'withdrawn' ? 'bad' : 'neutral'}>{PUB_STATUS_LABEL[p.publication_status]}</Badge>
              <Badge tone={p.read_depth === 'reproduced' ? 'good' : 'neutral'}>{READ_DEPTH_LABEL[p.read_depth]}</Badge>
              <span className="muted small">{p.venue ?? <MissingText>发表场合未知</MissingText>} · {p.publication_year ?? '年份未知'} · {p.canonical_id}</span>
            </div>
            <a href={p.primary_url} target="_blank" rel="noreferrer noopener" className="small">打开原文</a>
          </li>
        ))}
      </ul>
      {pages.hasMore && <button className="btn" onClick={pages.loadMore} disabled={pages.isFetchingMore}>加载更多</button>}
    </div>
  );
}

const RUNTIME_TONE: Record<SourceHealth['runtime_state'], Tone> = { healthy: 'good', degraded: 'warn', unavailable: 'bad', auth_required: 'bad', paused: 'neutral', unconfigured: 'neutral' };

export function DataStatusPage() {
  const q = useQuery(dataStatusQuery());
  const items = q.data?.data.items ?? [];
  return (
    <div className="page">
      <SectionTitle title="数据状态" hint="没有一个总的「绿灯」。每个来源分别显示：是否启用、运行状态、数据新鲜度，以及四个时间点——最近一次请求、传输成功、出现新内容、规范化完成。请求成功不等于有新数据，有新数据不等于已可用。" />
      {q.isLoading && <Loading />}
      {q.error && <ErrorState error={q.error} what="数据状态" onRetry={() => void q.refetch()} />}
      {q.data && <MetaStrip meta={q.data.meta} title="数据状态" hideCoverage />}
      {items.length === 0 && q.data && <EmptyState>没有登记的数据来源。</EmptyState>}
      <div className="source-grid">
        {items.map((s) => (
          <article key={`${s.source_id}/${s.endpoint_id}`} className="card source">
            <div className="item-head"><b>{s.source_id}</b><span className="muted small">/ {s.endpoint_id}</span></div>
            <div className="item-head">
              <Badge tone={s.enabled ? 'good' : 'neutral'} title="是否被配置为启用（不代表已连通）">{s.enabled ? '已启用' : '未启用'}</Badge>
              <Badge tone={RUNTIME_TONE[s.runtime_state]}>{RUNTIME_STATE_LABEL[s.runtime_state]}</Badge>
              <FreshnessBadge state={s.freshness_state} />
            </div>
            <dl className="kv compact">
              <dt>最近一次请求</dt><dd>{s.last_attempt_at ? `${fmtTime(s.last_attempt_at)}（${fmtAge(s.last_attempt_at)}）` : <MissingText>从未请求</MissingText>}</dd>
              <dt>最近传输成功</dt><dd>{s.last_transport_ok_at ? fmtTime(s.last_transport_ok_at) : <MissingText>无</MissingText>}</dd>
              <dt>最近出现新内容</dt><dd>{s.last_new_record_at ? fmtTime(s.last_new_record_at) : <MissingText>无</MissingText>}</dd>
              <dt>最近规范化完成</dt><dd>{s.last_canonical_ready_at ? fmtTime(s.last_canonical_ready_at) : <MissingText>无</MissingText>}</dd>
              <dt>连续失败</dt><dd>{s.consecutive_failures} 次{s.last_error_code ? `（${s.last_error_code}）` : ''}</dd>
              <dt>下次尝试</dt><dd>{s.next_attempt_at ? fmtTime(s.next_attempt_at) : '未排期'}</dd>
            </dl>
            {s.runtime_state === 'healthy' && s.freshness_state === 'stale' && <p className="small flag flag-warn">传输正常但内容已过期：可能是源站一直返回旧内容。</p>}
          </article>
        ))}
      </div>
    </div>
  );
}

export function PortfolioPage() {
  const rel = useReleaseSelection();
  const acct = useAccountContext();
  const wl = useWatchlists();
  const proj = useAccountProjection(acct.ctx, acct.complete);
  const [draft, setDraft] = useState({ account: acct.ctx.accountId ?? '', av: String(acct.ctx.accountVersion ?? ''), vs: acct.ctx.valuationSnapshotId ?? '' });
  const holdings = proj.data?.data.positions.map((p) => p.instrument_id) ?? [];
  const candidates = [...holdings, ...(wl.primary?.members.map((m) => m.instrument_id) ?? [])];
  return (
    <div className="page">
      <ReleaseBar rel={rel} />
      <SectionTitle title="账户与可行动" hint="持仓和现金来自唯一账本；「可行动」只表示当下条件和账户检查通过，不是成交，也不预留资金。" />
      <details className="card" open={!acct.complete}>
        <summary>账户选择</summary>
        <form className="form" onSubmit={(e) => { e.preventDefault(); acct.set(draft); }}>
          <label>账户编号<input value={draft.account} onChange={(e) => setDraft({ ...draft, account: e.target.value })} /></label>
          <label>账户版本<input type="number" value={draft.av} onChange={(e) => setDraft({ ...draft, av: e.target.value })} /></label>
          <label>估值快照编号<input value={draft.vs} onChange={(e) => setDraft({ ...draft, vs: e.target.value })} /></label>
          <button className="btn btn-small">使用</button>
          <p className="muted small">当前合同没有「账户列表」接口，需要给出账户编号、版本和估值快照（也可由部署配置提供）。</p>
        </form>
      </details>
      {!acct.complete ? <EmptyState>请先选择账户。</EmptyState> : (
        <>
          <AccountSummary ctx={acct.ctx} releaseId={rel.effectiveReleaseId} />
          <h3>可行动榜</h3>
          <ActionabilityList ctx={acct.ctx} releaseId={rel.effectiveReleaseId} candidateInstrumentIds={candidates} />
        </>
      )}
    </div>
  );
}
