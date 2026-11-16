/**
 * Playbook replay panel (triage #17 #20 #18 #24): equity curve with drawdown, recent trades, and the average trade return by the market
 * regime at entry. Why: the replay's NAV (net of the user cost profile) and trade list used to be invisible; everything shown here is
 * computed on the server from the stored replay and only drawn here. Missing is shown as 缺失 / —, never 0; a regime cell below the
 * sample gate is faded and flagged 样本不足. An experiment without a stored replay says so and how to produce one.
 */
import { useQuery } from '@tanstack/react-query';
import { ApiError } from '../../../api/client';
import { Badge, MissingText } from '../../../components/Badges';
import { EmptyState, ErrorState, Loading } from '../../../components/States';
import type { PlaybookReplay, ReplayTrade } from './api';
import { bfResearch } from './api';
import { EquityChart } from './EquityChart';
import { exitText, moneyText, pctText, priceText } from './format';
import { RegimeCell, unclassifiedText } from './RegimeCells';
import './research.css';

export function ReplayPanel({ catalogId, tradesLimit = 30 }: { catalogId: string; tradesLimit?: number }) {
  const q = useQuery({ queryKey: ['bfr', 'replay', catalogId, tradesLimit], queryFn: ({ signal }) => bfResearch.playbookReplay(catalogId, tradesLimit, signal), staleTime: 60_000, retry: false });
  if (q.isLoading) return <Loading label="读取回放结果…" />;
  if (q.isError) {
    if (q.error instanceof ApiError && q.error.status === 404) {
      return <EmptyState>这个战法还没有回放结果（净值曲线和成交明细来自实验）：<code>uv run python -m pulse.workers.research ext-b-run --ids {catalogId}</code></EmptyState>;
    }
    return <ErrorState error={q.error} what="回放结果" onRetry={() => q.refetch()} />;
  }
  return <ReplayView data={q.data!.data} />;
}

export function ReplayView({ data }: { data: PlaybookReplay }) {
  const eq = data.equity;
  return (
    <section className="bfr replay" aria-label="回放净值与成交">
      <div className="counts">
        <Badge tone="good" title="扣除用户费用口径后的期末净值 / 期初资金 - 1">净收益 {pctText(eq.net_return)}</Badge>
        <Badge tone="warn" title="相对历史最高净值的最大回撤（峰值从期初资金起算）">最大回撤 {pctText(eq.max_drawdown)}</Badge>
        <Badge>成交 {data.trades.total} 笔（已平仓 {data.trades.closed}，持仓中 {data.trades.open}）</Badge>
        <span className="muted small">{data.window.start} ~ {data.window.end}，{data.window.sessions} 个交易日 · 期初 {moneyText(data.initial_cash)} 元 · 费用口径 {data.experiment.cost_policy_version}</span>
      </div>
      <EquityChart points={eq.points} initial={data.initial_cash} />
      <h4>最近成交</h4>
      <TradesTable items={data.trades.items} total={data.trades.total} />
      <h4>按入场时的市场环境归因</h4>
      <AttributionTable data={data} />
      <ul className="muted small plain">{data.caveats.map((c) => <li key={c}>{c}</li>)}</ul>
    </section>
  );
}

function TradesTable({ items, total }: { items: ReplayTrade[]; total: number }) {
  if (items.length === 0) return <EmptyState>这次回放没有成交。</EmptyState>;
  return (
    <div className="scroll">
      <table className="table dense trades">
        <caption className="small muted">最近 {items.length} 笔 / 共 {total} 笔，按买入日由新到旧；收益为成交均价之比（毛收益，费用已在净值曲线里扣除）</caption>
        <thead><tr><th>代码</th><th>买入日</th><th>卖出日</th><th>买入价</th><th>卖出价</th><th>收益</th><th>持有(交易日)</th><th>卖出原因</th></tr></thead>
        <tbody>
          {items.map((t) => (
            <tr key={`${t.instrument_id}${t.entry_day}`} className={t.status === 'open' ? 'open' : undefined}>
              <td>{t.code ?? t.instrument_id.slice(0, 8)}</td><td>{t.entry_day}</td><td>{t.exit_day ?? <MissingText>持仓中</MissingText>}</td>
              <td className="num">{priceText(t.entry_price)}</td><td className="num">{priceText(t.exit_price)}</td>
              <td className={`num ${t.gross_return == null ? '' : Number(t.gross_return) >= 0 ? 'pos' : 'neg'}`}>{pctText(t.gross_return)}</td>
              <td className="num">{t.holding_sessions ?? '—'}</td><td>{exitText(t.exit_reason, t.status)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function AttributionTable({ data }: { data: PlaybookReplay }) {
  const att = data.attribution;
  if (!att.regime.available) {
    return <EmptyState>这个实验运行时没有可用的市场环境（{att.regime.reason}），所以没有做环境归因。</EmptyState>;
  }
  return (
    <>
      <p className="muted small">环境取自入场前最后一个已完成交易日（{att.regime.regime_version}，数据截至 {att.regime.through ?? '未知'}）；少于 {att.min_trades} 笔的格子淡化并标「样本不足」。已平仓 {att.closed_trades} 笔参与统计，持仓中的 {att.open_trades} 笔没有收益、不计入。</p>
      {att.dimensions.map((d) => (
        <div key={d.dimension} className="dim">
          <table className="table dense attr" aria-label={`${d.label}归因`}>
            <thead><tr><th>{d.label}</th>{d.cells.map((c) => <th key={c.bucket}>{c.label}</th>)}</tr></thead>
            <tbody><tr><th scope="row">平均收益 · 笔数</th>{d.cells.map((c) => <RegimeCell key={c.bucket} cell={c} gate={att.min_trades} />)}</tr></tbody>
          </table>
          {d.unclassified.count > 0 && <p className="muted small">{unclassifiedText(d)}</p>}
        </div>
      ))}
    </>
  );
}
