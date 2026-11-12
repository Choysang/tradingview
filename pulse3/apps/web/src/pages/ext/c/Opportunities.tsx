/** 机会 tab body: opportunity table, denominators, incremental value vs. holding (never 0 for unknown), inventory shortfall. */
import type { ReactNode } from 'react';
import { Badge, MissingText } from '../../../components/Badges';
import { EmptyState } from '../../../components/States';
import { SIDE_LABEL, fmtMoney, fmtTime } from '../../../lib/format';
import { ArtifactBody, StockLink } from './common';
import type { IncrementalVsHold, OpportunitiesBody } from './types';

export const INCREMENTAL_STATUS_TEXT: Record<string, string> = {
  ok: '可比较',
  no_trades: '没有成交，无法与持有比较',
  mark_missing: '缺少估值价格，无法与持有比较',
  not_comparable: '口径不可比，不给出增量数字',
};

const qty = (v: number | string | null | undefined): string => (v === null || v === undefined ? '缺失' : String(v));
const money = (v: string | null): ReactNode => (v === null ? <MissingText /> : `${fmtMoney(v)} 元`);

export function IncrementalValue({ inc }: { inc: IncrementalVsHold | null | undefined }) {
  if (!inc) return <p className="muted">尚无与「一直持有」对比的结果。</p>;
  const shortfall = Object.entries(inc.inventory_shortfall ?? {});
  return (
    <div className="sub-panel" aria-label="相对持有的增量价值">
      <div className="item-head"><h4 style={{ margin: 0 }}>相对一直持有的增量价值</h4><Badge tone={inc.status === 'ok' ? 'good' : 'warn'}>{INCREMENTAL_STATUS_TEXT[inc.status] ?? inc.status}</Badge></div>
      {inc.status === 'ok' ? (
        <dl className="kv compact">
          <dt>增量盈亏</dt><dd>{money(inc.incremental_pnl_cny)}</dd>
          <dt>主动交易费用</dt><dd>{money(inc.active_fees_cny)}</dd>
          <dt>基准（持有）费用</dt><dd>{money(inc.baseline_fees_cny)}</dd>
          <dt>库存价值变化</dt><dd>{money(inc.inventory_value_delta_cny)}</dd>
          <dt>桥接持有</dt><dd>{Array.isArray(inc.bridge_holds) ? `${inc.bridge_holds.length} 项` : inc.bridge_holds == null ? '无' : JSON.stringify(inc.bridge_holds)}</dd>
        </dl>
      ) : <p className="small">{INCREMENTAL_STATUS_TEXT[inc.status] ?? `状态：${inc.status}`}。</p>}
      <h4>已卖未买回的库存缺口</h4>
      {shortfall.length === 0 ? <p className="small muted">没有库存缺口。</p> : (
        <>
          <ul className="plain">{shortfall.map(([id, shares]) => <li key={id}><StockLink id={id} /> 缺口 <b className="mono">{qty(shares)}</b> 股</li>)}</ul>
          <p className="small muted">缺口按股数计：这些股票被卖出后没有买回，不能只按成本价看盈亏。</p>
        </>
      )}
    </div>
  );
}

export function OpportunitiesView({ body }: { body: Record<string, unknown> }) {
  const b = body as OpportunitiesBody;
  if (!Array.isArray(b.opportunities)) return <ArtifactBody body={body} />; // unknown shape: fall back to a generic view
  const byState = Object.entries(b.denominators?.by_state ?? {});
  return (
    <div>
      <div className="chips" aria-label="分母">
        <span className="chip">总数 {b.denominators?.total ?? b.opportunities.length}</span>
        {byState.map(([k, n]) => <span key={k} className="chip">{k}：{n}</span>)}
        {b.as_of && <span className="muted small">截至 {fmtTime(b.as_of)}</span>}
      </div>
      {b.opportunities.length === 0 ? <EmptyState>当前没有机会。</EmptyState> : (
        <div className="table-wrap"><table className="table"><caption className="sr-only">机会</caption>
          <thead><tr><th scope="col">证券</th><th scope="col">方向</th><th scope="col">情景</th><th scope="col">状态</th><th scope="col" className="num">目标量</th><th scope="col" className="num">已成交</th><th scope="col">原因</th><th scope="col">可用时间</th></tr></thead>
          <tbody>{b.opportunities.map((o) => (
            <tr key={`${o.opportunity_id}-${o.opportunity_version}`}>
              <td><StockLink id={o.instrument_id} /></td><td>{SIDE_LABEL[o.side] ?? o.side}</td><td className="mono small">{o.scenario_id}</td>
              <td><Badge>{o.state}</Badge></td><td className="num">{qty(o.target_quantity)}</td><td className="num">{qty(o.filled_quantity)}</td>
              <td className="mono small">{o.reason_code ?? '—'}</td><td className="small muted">{fmtTime(o.available_at)}</td>
            </tr>))}</tbody></table></div>
      )}
      <IncrementalValue inc={b.incremental_vs_hold} />
    </div>
  );
}
