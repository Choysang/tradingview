/** 前向主书（检验中）: legacy forward-tracking books. Always labelled as under test and never part of ranking weight. */
import { useQuery } from '@tanstack/react-query';
import { Badge, MissingText } from '../../../components/Badges';
import { EmptyState, Loading, SectionTitle } from '../../../components/States';
import { fmtDate, fmtInt, fmtMoney } from '../../../lib/format';
import { extGet } from './api';
import { ExtError, StockLink, Strip } from './common';
import { ruleSummary, sparkSegments } from './fmt';
import type { ForwardBook, ForwardBooksData } from './types';

const W = 320;
const H = 56;

export function NavSpark({ history }: { history: ForwardBook['history'] }) {
  const segs = sparkSegments(history, W, H);
  if (segs.length === 0) return <p className="muted small">暂无可画的净值点。</p>;
  const missing = history.filter((h) => h.nav_cny === null).length;
  return (
    <div>
      <svg className="spark" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`净值走势，共 ${history.length} 个交易日${missing ? `，其中 ${missing} 天净值缺失（断线处）` : ''}`}>
        {segs.map((s, i) => (segsIsPoint(s) ? <circle key={i} cx={s.split(',')[0]} cy={s.split(',')[1]} r="2" fill="var(--accent)" /> : <polyline key={i} points={s} />))}
      </svg>
      <div className="muted small">{fmtDate(history[0]?.trade_date)} ~ {fmtDate(history[history.length - 1]?.trade_date)}{missing > 0 ? `；${missing} 天净值缺失，曲线在缺失处断开` : ''}</div>
    </div>
  );
}
const segsIsPoint = (s: string): boolean => !s.includes(' ');

function BookCard({ b }: { b: ForwardBook }) {
  const l = b.latest;
  return (
    <article className="card book-card" aria-label={b.label}>
      <div className="item-head">
        <h3 style={{ margin: 0 }}>{b.label}</h3>
        <Badge tone="warn" title={`tracking_kind = ${b.tracking_kind}`}>{b.status_label || '检验中'}</Badge>
        <span className="muted small">冻结于 {b.frozen_on} · 账户版本 {b.account_version}</span>
      </div>
      <div className="book-banner" role="note">仅用于前向检验，不参与排名权重{b.ranking_weight !== 'none' ? `（服务端权重字段为 ${b.ranking_weight}，请核对）` : ''}</div>
      <p className="small"><b>规则：</b>{ruleSummary(b.rule)}</p>
      {l === null ? <EmptyState>这本书还没有产生任何交易日记录。</EmptyState> : (
        <>
          <dl className="facts">
            <div><dt>最新交易日</dt><dd>{l.trade_date}</dd></div>
            <div><dt>净值（{b.base_currency}）</dt><dd>{l.nav_cny === null ? <MissingText /> : fmtMoney(l.nav_cny)}</dd></div>
            <div><dt>现金</dt><dd>{l.cash_cny === null ? <MissingText /> : fmtMoney(l.cash_cny)}</dd></div>
            <div><dt>缺价持仓</dt><dd>{l.missing_marks > 0 ? <Badge tone="warn">{l.missing_marks} 只缺价</Badge> : '无'}</dd></div>
          </dl>
          <h4>持仓（{l.holdings.length}）</h4>
          {l.holdings.length === 0 ? <p className="muted small">当前空仓。</p> : (
            <div className="table-wrap"><table className="table"><caption className="sr-only">{b.label}持仓</caption>
              <thead><tr><th scope="col">证券</th><th scope="col" className="num">数量</th><th scope="col" className="num">成本</th><th scope="col" className="num">最新价</th></tr></thead>
              <tbody>{l.holdings.map((h) => (
                <tr key={h.instrument_id}>
                  <td><StockLink id={h.instrument_id} code={h.code} name={h.name} /></td>
                  <td className="num">{fmtInt(h.quantity)}</td>
                  <td className="num">{h.cost_cny === null ? <MissingText /> : fmtMoney(h.cost_cny)}</td>
                  <td className="num">{h.mark_price === null ? <MissingText>缺价</MissingText> : fmtMoney(h.mark_price)}</td>
                </tr>))}</tbody></table></div>
          )}
          <h4>计划买入（{l.planned_buys.length}）</h4>
          {l.planned_buys.length === 0 ? <p className="muted small">今日没有计划买入。</p> : (
            <ol className="plain">{l.planned_buys.map((p) => <li key={p.instrument_id}><span className="mono muted">#{p.alpha_rank}</span> <StockLink id={p.instrument_id} code={p.code} name={p.name} /></li>)}</ol>
          )}
        </>
      )}
      <h4>净值走势</h4>
      <NavSpark history={b.history} />
      {b.notes.length > 0 && <ul className="plain">{b.notes.map((n) => <li key={n} className="flag flag-warn">{n}</li>)}</ul>}
    </article>
  );
}

export function ForwardBooks() {
  const q = useQuery({ queryKey: ['extc', 'forward-books'], queryFn: ({ signal }) => extGet<ForwardBooksData>('c/forward-books', undefined, signal) });
  return (
    <section aria-label="前向主书">
      <SectionTitle title="前向主书（检验中）" hint="历史遗留的前向跟踪账本。仅用于观察规则在真实前向数据上的表现，结论未经验证。" />
      <Strip meta={q.data?.meta} title="前向主书" />
      {q.isLoading ? <Loading /> : q.error ? <ExtError error={q.error} onRetry={() => { void q.refetch(); }} what="前向主书" /> : (q.data?.data.books.length ?? 0) === 0 ? <EmptyState>没有可用的前向主书。</EmptyState> : (
        <div className="books-grid">{q.data?.data.books.map((b) => <BookCard key={b.account_id} b={b} />)}</div>
      )}
    </section>
  );
}
