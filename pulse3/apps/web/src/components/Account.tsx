/**
 * Account projection + actionability list. Actionability is NOT a fill or reservation
 * (contract): eligible only means the plan condition and the account checks pass at
 * checked_at. The list pins actionability_id, so paging never swaps to another batch.
 */
import { Link } from 'react-router-dom';
import { ops } from '../api/ops';
import type { AccountProjection } from '../api/types';
import { useActionabilityId, useAccountProjection } from '../hooks/useAccount';
import { usePinnedPages } from '../hooks/usePinnedPages';
import { ACTIONABILITY_LABEL, SIDE_LABEL, fmtMoney, fmtTime } from '../lib/format';
import { Badge, MissingText } from './Badges';
import { InstrumentName, stockHref } from './Lists';
import { MetaStrip } from './MetaStrip';
import { EmptyState, ErrorState, Loading } from './States';
import type { AccountContext } from '../config';
import type { Tone } from '../lib/version';

export function AccountSummary({ ctx, releaseId }: { ctx: AccountContext; releaseId: string | null }) {
  const q = useAccountProjection(ctx, true);
  if (q.isLoading) return <Loading label="读取账户…" />;
  if (q.error) return <ErrorState error={q.error} what="账户" onRetry={() => void q.refetch()} />;
  const p = q.data?.data as AccountProjection;
  const m = q.data!.meta;
  const money = (v: string | null): string => (v === null ? '缺失' : fmtMoney(v));
  return (
    <div>
      <div className="cards">
        <div className="card stat"><div className="muted small">账户净值（按原始价估值）</div><div className="stat-value">{money(p.nav_cny)}</div></div>
        <div className="card stat"><div className="muted small">可用现金</div><div className="stat-value">{money(p.cash_available_cny)}</div></div>
        <div className="card stat"><div className="muted small">浮动盈亏</div><div className="stat-value">{money(p.unrealized_cny)}</div></div>
        <div className="card stat"><div className="muted small">已实现（毛）</div><div className="stat-value">{money(p.realized_gross_cny)}</div></div>
        <div className="card stat"><div className="muted small">可提现金</div><div className="stat-value">{p.withdrawable_cash_cny === null ? <MissingText>未知</MissingText> : fmtMoney(p.withdrawable_cash_cny)}</div></div>
        <div className="card stat"><div className="muted small">冻结/预留现金</div><div className="stat-value">{money(p.reserved_cash_cny)}</div></div>
      </div>
      <MetaStrip meta={m} title="账户投影" hideCoverage rows={[
        ['account_version', String(p.account_version)], ['valuation_snapshot_id', p.valuation_snapshot_id], ['ledger_policy_version', p.ledger_policy_version],
        ['valuation_policy_version', p.valuation_policy_version], ['numeric_policy_version', p.numeric_policy_version], ['trading_date', p.trading_date],
      ]} />
      <h3>持仓</h3>
      {p.positions.length === 0 ? <EmptyState>当前没有持仓。</EmptyState> : (
        <ul className="rows">
          {p.positions.map((pos) => (
            <li key={pos.instrument_id} className="row">
              <div className="row-main">
                <Link className="row-link" to={stockHref(pos.instrument_id, releaseId)}><InstrumentName instrumentId={pos.instrument_id} releaseId={releaseId} /></Link>
                <div className="row-sub muted small">持有 {pos.quantity_shares} 股 · 今日可卖 {pos.sellable_quantity_shares} 股 · 已预留卖出 {pos.reserved_sell_quantity_shares} 股</div>
              </div>
              <div className="row-side">
                <span>市值 {pos.market_value_cny === null ? <MissingText /> : fmtMoney(pos.market_value_cny)}</span>
                <span className="muted small">成本 {fmtMoney(pos.cost_basis_cny)}</span>
              </div>
              {pos.mark_quality_status !== 'valid' && <Badge tone="warn">估值价质量：{pos.mark_quality_status}</Badge>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

const STATUS_TONE: Record<string, Tone> = { eligible: 'good', not_triggered: 'neutral', blocked: 'warn', unknown: 'bad' };

export function ActionabilityList({ ctx, releaseId, candidateInstrumentIds }: { ctx: AccountContext; releaseId: string | null; candidateInstrumentIds: string[] }) {
  const act = useActionabilityId(ctx, releaseId, candidateInstrumentIds);
  const actId = act.actionabilityId;
  const pages = usePinnedPages({
    queryKey: ['actionability', ctx.accountId, actId],
    enabled: !!actId && !!ctx.accountId,
    fetchPage: (cursor, signal) => ops.getAccountActionability(ctx.accountId as string, actId as string, { cursor }, { signal }),
    pinOf: (p) => ({ releaseId: p.data.release_id, rankingId: p.data.ranking_id, actionabilityId: p.data.actionability_id }),
    entriesOf: (p) => p.data.entries,
    keyOf: (e) => e.instrument_id,
  });
  if (act.workspace.isLoading) return <Loading label="读取可行动快照…" />;
  if (act.workspace.error) return <ErrorState error={act.workspace.error} what="账户视角" onRetry={() => void act.workspace.refetch()} />;
  if (!actId) return <EmptyState>当前发布没有为该账户生成可行动快照。</EmptyState>;
  if (pages.isLoading) return <Loading />;
  if (pages.error) return <ErrorState error={pages.error} what="可行动榜（快照可能已过期）" onRetry={() => { void act.workspace.refetch(); pages.reload(); }} />;
  const first = pages.firstPage;
  if (!first) return null;
  const d = first.data;
  return (
    <div>
      <MetaStrip meta={first.meta} title="账户可行动快照" hideCoverage extra={<Badge tone="neutral">有效至 {fmtTime(d.valid_until)}</Badge>} rows={[
        ['actionability_id', d.actionability_id], ['quote_snapshot_id', d.quote_snapshot_id], ['account_version', String(d.account_version ?? '无')], ['policy', d.actionability_policy_version], ['checked_at', fmtTime(d.checked_at)],
      ]} />
      <p className="muted small">「可执行」只表示在 {fmtTime(d.checked_at)} 条件成立且现金/库存检查通过，不等于已成交，也没有预留资金或库存。翻页始终是同一份快照。</p>
      <ol className="rows">
        {pages.paging.entries.map((e) => (
          <li key={e.instrument_id} className="row">
            <span className="rank" title="账户行动序">{e.account_action_rank ?? '—'}</span>
            <div className="row-main">
              <Link className="row-link" to={stockHref(e.instrument_id, d.release_id)}><InstrumentName instrumentId={e.instrument_id} releaseId={d.release_id} /></Link>
              <div className="row-sub muted small">
                {e.side ? SIDE_LABEL[e.side] : '方向未定'}{e.checked_quantity_shares != null ? ` ${e.checked_quantity_shares} 股` : ''} · 原研究行动序 {e.frozen_action_rank ?? '无'}
                {e.reason_codes.length > 0 && <> · 原因：{e.reason_codes.join('、')}</>}
              </div>
            </div>
            <div className="row-side"><Badge tone={STATUS_TONE[e.actionability_status] ?? 'neutral'}>{ACTIONABILITY_LABEL[e.actionability_status]}</Badge></div>
          </li>
        ))}
      </ol>
      {pages.paging.rejected && <ErrorState error={new Error(pages.paging.rejected.reason)} what="翻页" onRetry={pages.reload} />}
      {pages.hasMore && <button className="btn" onClick={pages.loadMore} disabled={pages.isFetchingMore}>{pages.isFetchingMore ? '加载中…' : '加载更多'}</button>}
    </div>
  );
}
