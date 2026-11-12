/**
 * Four evidence dimensions of the workbench (spec 5): 消息 / 情绪与社区 / 资金 / 龙虎榜席位.
 * They stay separate - no combined score. Each block pins to the workspace release;
 * a block answered for another release is hidden instead of mixed in.
 */
import type { ReactNode } from 'react';
import { ops } from '../api/ops';
import type { CommunityObservation, EvidenceClaim, EvidenceDocument, FundFlowObservation, ResponseMeta, SeatReport } from '../api/types';
import { usePinnedPages, type PinnedPages } from '../hooks/usePinnedPages';
import {
  ASSERTION_LABEL, CLAIM_STATUS_LABEL, DOC_STATUS_LABEL, MEASUREMENT_CLASS_LABEL, ORIGIN_LABEL, RESOLUTION_LABEL, fmtMoney, fmtTime,
} from '../lib/format';
import { checkVersionConsistency } from '../lib/version';
import { Badge, MissingText, QualityBadge } from './Badges';
import { DetailsButton } from './Diagnostics';
import { MetaStrip } from './MetaStrip';
import { EmptyState, ErrorState, Loading } from './States';
import type { Tone } from '../lib/version';

export type EvidenceTabKey = 'news' | 'community' | 'fund' | 'seats';
export const EVIDENCE_TABS: Array<{ key: EvidenceTabKey; label: string; hint: string }> = [
  { key: 'news', label: '消息', hint: '以「主张」为单位核对来源；多家转载只算一个来源' },
  { key: 'community', label: '情绪/社区', hint: '讨论是抽样测量，评论人数不等于买入人数' },
  { key: 'fund', label: '资金', hint: '不同平台、不同口径的资金流不能直接相加' },
  { key: 'seats', label: '龙虎榜席位', hint: '披露的是已发生的交易，不预示次日涨跌' },
];

interface BlockProps<P extends { meta: ResponseMeta }, E> {
  pages: PinnedPages<P, E>;
  workspaceReleaseId: string;
  title: string;
  empty: string;
  render: (entries: E[]) => ReactNode;
  note?: string;
}

function Block<P extends { meta: ResponseMeta }, E>({ pages, workspaceReleaseId, title, empty, render, note }: BlockProps<P, E>) {
  if (pages.isLoading) return <Loading />;
  if (pages.error && !pages.firstPage) return <ErrorState error={pages.error} what={title} onRetry={pages.reload} />;
  const first = pages.firstPage;
  if (!first) return <EmptyState>{empty}</EmptyState>;
  const consistent = checkVersionConsistency(workspaceReleaseId, first.meta);
  if (!consistent.ok) return <div className="notice notice-warn">{title}属于另一个发布版本，已隐藏以免与当前分析混用。{consistent.reason}</div>;
  return (
    <div>
      <MetaStrip meta={first.meta} title={title} />
      {note && <p className="muted small">{note}</p>}
      {pages.paging.entries.length === 0 ? <EmptyState>{empty}</EmptyState> : render(pages.paging.entries)}
      {pages.paging.rejected && <ErrorState error={new Error(pages.paging.rejected.reason)} what="翻页" onRetry={pages.reload} />}
      {pages.hasMore && <button className="btn" onClick={pages.loadMore} disabled={pages.isFetchingMore}>{pages.isFetchingMore ? '加载中…' : '加载更多'}</button>}
    </div>
  );
}

type NewsItem = { t: 'doc'; d: EvidenceDocument } | { t: 'claim'; c: EvidenceClaim };
const CLAIM_TONE: Record<string, Tone> = { supported: 'good', contradicted: 'bad', disputed: 'warn', unverified: 'neutral', not_falsifiable_yet: 'neutral' };

export function NewsTab({ instrumentId, releaseId }: { instrumentId: string; releaseId: string }) {
  const pages = usePinnedPages<{ meta: ResponseMeta; data: { release_id: string; instrument_id: string; documents: EvidenceDocument[]; claims: EvidenceClaim[] } }, NewsItem>({
    queryKey: ['evidence', releaseId, instrumentId],
    fetchPage: (cursor, signal) => ops.getStockEvidence(instrumentId, { release_id: releaseId, cursor }, { signal }),
    pinOf: (p) => ({ releaseId: p.data.release_id, rankingId: `${instrumentId}:evidence`, actionabilityId: null }),
    entriesOf: (p) => [...p.data.documents.map((d): NewsItem => ({ t: 'doc', d })), ...p.data.claims.map((c): NewsItem => ({ t: 'claim', c }))],
    keyOf: (e) => (e.t === 'doc' ? `d:${e.d.document_revision_id}` : `c:${e.c.claim_revision_id}`),
  });
  return (
    <Block pages={pages} workspaceReleaseId={releaseId} title="消息与来源核对" empty="这只股票暂时没有相关消息。" note="「来源」看的是原始发布者是谁；转载不增加独立证明。"
      render={(entries) => (
        <>
          <h4>核对后的主张</h4>
          <ul className="cards-list">
            {entries.filter((e): e is Extract<NewsItem, { t: 'claim' }> => e.t === 'claim').map(({ c }) => (
              <li key={c.claim_revision_id} className="card item">
                <div className="item-head"><Badge tone={CLAIM_TONE[c.claim_status] ?? 'neutral'}>{CLAIM_STATUS_LABEL[c.claim_status]}</Badge><Badge>{ASSERTION_LABEL[c.assertion_type]}</Badge><QualityBadge status={c.quality_status} /></div>
                <div>{c.predicate}</div>
                <div className="muted small">依据 {c.evidence.length} 份文档（{c.evidence.map((x) => x.relation === 'supports' ? '支持' : x.relation === 'refutes' ? '反驳' : x.relation === 'attributes' ? '转述' : '背景').join('、')}）· {fmtTime(c.available_at)}</div>
                <DetailsButton title="主张详情" rows={[['claim_revision_id', c.claim_revision_id], ['verification_policy_version', c.verification_policy_version], ['claim_schema_version', c.claim_schema_version], ...c.evidence.map((x, i): [string, string] => [`证据${i + 1}`, `${x.document_revision_id} (${x.relation})`])]} />
              </li>
            ))}
          </ul>
          <h4>相关文档</h4>
          <ul className="cards-list">
            {entries.filter((e): e is Extract<NewsItem, { t: 'doc' }> => e.t === 'doc').map(({ d }) => (
              <li key={d.document_revision_id} className="card item">
                <div className="item-head"><Badge>{ORIGIN_LABEL[d.origin_status]}</Badge><Badge tone={d.document_status === 'published' ? 'neutral' : 'warn'}>{DOC_STATUS_LABEL[d.document_status]}</Badge><QualityBadge status={d.quality_status} /></div>
                <div><b>{d.title ?? <MissingText>无标题</MissingText>}</b></div>
                <div className="muted small">来源 {d.source_id} · 发布 {d.published_at ? fmtTime(d.published_at) : <MissingText>发布时间缺失</MissingText>} · 第 {d.revision} 版</div>
                <DetailsButton title="文档详情" rows={[['document_revision_id', d.document_revision_id], ['raw_object_id', d.raw_object_id], ['first_seen_at', fmtTime(d.first_seen_at)], ['available_at', fmtTime(d.available_at)], ['origin_group', d.origin_group ?? '无']]} />
              </li>
            ))}
          </ul>
        </>
      )}
    />
  );
}

export function CommunityTab({ instrumentId, releaseId }: { instrumentId: string; releaseId: string }) {
  const pages = usePinnedPages<{ meta: ResponseMeta; data: { release_id: string; observations: CommunityObservation[] } }, CommunityObservation>({
    queryKey: ['community', releaseId, instrumentId],
    fetchPage: (cursor, signal) => ops.getStockCommunity(instrumentId, { release_id: releaseId, cursor }, { signal }),
    pinOf: (p) => ({ releaseId: p.data.release_id, rankingId: `${instrumentId}:community`, actionabilityId: null }),
    entriesOf: (p) => p.data.observations,
    keyOf: (o) => o.community_observation_id,
  });
  return (
    <Block pages={pages} workspaceReleaseId={releaseId} title="情绪与社区" empty="暂时没有社区讨论样本。" note="样本覆盖范围、平台与时间窗口见详情；数量表示被抽到的讨论，不代表全部投资者。"
      render={(obs) => (
        <ul className="cards-list">
          {obs.map((o) => {
            const payload = o.analysis_payload as Record<string, unknown>;
            return (
              <li key={o.community_observation_id} className="card item">
                <div className="item-head">
                  <Badge tone={o.coverage_status === 'complete_for_declared_scope' ? 'good' : 'warn'}>{o.coverage_status === 'complete_for_declared_scope' ? '声明范围内完整' : o.coverage_status === 'partial' ? '抽样不完整' : '覆盖未知'}</Badge>
                  <QualityBadge status={o.quality_status} />
                </div>
                <div>立场：{String(payload.stance ?? '未知')} · 话题：{String(payload.topic ?? '未知')}</div>
                <div className="muted small">点赞 {o.like_count ?? <MissingText />} · 回复 {o.reply_count ?? <MissingText />} · 独立参与者 {o.unique_participant_count ?? <MissingText />} · 采样于 {fmtTime(o.sampled_at)}</div>
                <DetailsButton title="社区样本详情" rows={[['source_id', o.source_id], ['post_id', o.post_id], ['sampling_entry', o.sampling_entry], ['sampling_sort', o.sampling_sort], ['window', `${fmtTime(o.sampling_window_start)} ~ ${fmtTime(o.sampling_window_end)}`]]} />
              </li>
            );
          })}
        </ul>
      )}
    />
  );
}

export function FundTab({ instrumentId, releaseId }: { instrumentId: string; releaseId: string }) {
  const pages = usePinnedPages<{ meta: ResponseMeta; data: { release_id: string; observations: FundFlowObservation[] } }, FundFlowObservation>({
    queryKey: ['fundflow', releaseId, instrumentId],
    fetchPage: (cursor, signal) => ops.getStockFundFlow(instrumentId, { release_id: releaseId, cursor }, { signal }),
    pinOf: (p) => ({ releaseId: p.data.release_id, rankingId: `${instrumentId}:fund`, actionabilityId: null }),
    entriesOf: (p) => p.data.observations,
    keyOf: (o) => o.funding_observation_id,
  });
  return (
    <Block pages={pages} workspaceReleaseId={releaseId} title="资金" empty="暂时没有资金数据。" note="每条都标明口径（披露/平台推算）。口径不同的数字并排显示，不合计、不求和。"
      render={(obs) => (
        <ul className="cards-list">
          {obs.map((o) => (
            <li key={o.funding_observation_id} className="card item">
              <div className="item-head"><Badge tone={o.measurement_class === 'disclosed_observation' ? 'good' : 'warn'}>{MEASUREMENT_CLASS_LABEL[o.measurement_class]}</Badge><QualityBadge status={o.quality_status} /></div>
              <div><b>{o.metric_name}</b> <span className="muted small">（{o.metric_namespace}）</span></div>
              <div className="fund-value">{o.canonical_value === null ? <MissingText>{o.missing_reason ?? '缺失'}</MissingText> : <>{o.canonical_unit === 'CNY' ? fmtMoney(o.canonical_value) : o.canonical_value} {o.canonical_unit === 'CNY' ? '元' : o.canonical_unit}</>}</div>
              <div className="muted small">口径 {o.methodology_id} v{o.methodology_version} · 窗口 {fmtTime(o.observation_window_start)} ~ {fmtTime(o.observation_window_end)}</div>
              <DetailsButton title="资金观测详情" rows={[['source_id', o.source_id], ['raw_value', String(o.raw_value ?? '无')], ['raw_unit', o.raw_unit], ['side_convention', o.side_convention], ['basis', o.trade_or_order_basis]]} />
            </li>
          ))}
        </ul>
      )}
    />
  );
}

export function SeatsTab({ instrumentId, releaseId }: { instrumentId: string; releaseId: string }) {
  const pages = usePinnedPages<{ meta: ResponseMeta; data: { release_id: string; reports: SeatReport[] } }, SeatReport>({
    queryKey: ['seats', releaseId, instrumentId],
    fetchPage: (cursor, signal) => ops.getStockSeats(instrumentId, { release_id: releaseId, cursor }, { signal }),
    pinOf: (p) => ({ releaseId: p.data.release_id, rankingId: `${instrumentId}:seats`, actionabilityId: null }),
    entriesOf: (p) => p.data.reports,
    keyOf: (r) => r.report_revision_id,
  });
  return (
    <Block pages={pages} workspaceReleaseId={releaseId} title="龙虎榜席位" empty="这只股票近期没有龙虎榜披露。" note="席位名称只在能唯一对应营业部时才标为「已确认」；地区简称不会被猜成某个具体营业部或某个人。"
      render={(reports) => (
        <>
          {reports.map((r) => (
            <div key={r.report_revision_id} className="card item">
              <div className="item-head"><Badge>披露原因</Badge><span>{r.disclosure_reason}</span><QualityBadge status={r.quality_status} /></div>
              <div className="muted small">统计窗口 {r.window_start} ~ {r.window_end} · 发布 {fmtTime(r.published_at)}（披露在收盘后，不能当作当日下午已知）</div>
              <div className="seat-grid">
                {(['buy', 'sell'] as const).map((side) => (
                  <div key={side}>
                    <h4>{side === 'buy' ? '买入前列' : '卖出前列'}</h4>
                    <ul className="plain">
                      {r.rows.filter((x) => x.side === side).map((x) => (
                        <li key={`${side}${x.rank}`}>
                          <b>{x.rank}.</b> {x.raw_seat_name} <span className="muted">{fmtMoney(x.canonical_amount_cny)} 元</span>
                          <br /><Badge tone={x.resolution_status === 'resolved' ? 'good' : x.resolution_status === 'unresolved' ? 'bad' : 'warn'}>{RESOLUTION_LABEL[x.resolution_status]}</Badge>
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
              </div>
              <DetailsButton title="席位披露详情" rows={[['report_revision_id', r.report_revision_id], ['rule_version', r.rule_version], ['source_id', r.source_id], ['available_at', fmtTime(r.available_at)]]} />
            </div>
          ))}
        </>
      )}
    />
  );
}
