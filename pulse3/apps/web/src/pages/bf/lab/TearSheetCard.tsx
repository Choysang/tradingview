/**
 * Tear sheet card (triage #160 #161): return, risk and trade statistics of one replay or paper book, computed by the server.
 * Mount it anywhere with a source: <TearSheetCard source={{ catalogId: 'PB:turtle_55' }} />. A missing figure shows 缺失 and WHY (never 0);
 * a short sample is flagged because a Sharpe over 20 sessions is not evidence. The card only draws what the server sent.
 */
import { useQuery } from '@tanstack/react-query';
import { ApiError } from '../../../api/client';
import { Badge, MissingText } from '../../../components/Badges';
import { EmptyState, ErrorState, Loading } from '../../../components/States';
import { MonthlyHeatmapCard } from '../fillr/MonthlyHeatmap';
import type { MetricName, TearSheet, TearSheetData, TearSheetSource } from './api';
import { bfLab, METRIC_NAMES } from './api';
import { METRIC_META, metricText, reasonText, signClass } from './format';
import './lab.css';

/** Below this many sessions the ratio figures are shown with a warning. */
export const SHORT_SAMPLE = 60;
const GROUPS: Array<{ title: string; names: MetricName[] }> = [
  { title: '收益', names: ['total_return', 'annualised_return'] },
  { title: '风险', names: ['annualised_volatility', 'max_drawdown', 'longest_drawdown_sessions'] },
  { title: '风险调整', names: ['sharpe', 'sortino', 'calmar'] },
  { title: '成交', names: ['win_rate', 'profit_factor', 'payoff_ratio', 'avg_win', 'avg_loss', 'avg_win_return', 'avg_loss_return', 'exposure'] },
];

export function TearSheetCard({ source }: { source: TearSheetSource }) {
  const key = JSON.stringify(source);
  const q = useQuery({ queryKey: ['bf-lab', 'tearsheet', key], queryFn: ({ signal }) => bfLab.tearSheet(source, signal), staleTime: 60_000, retry: false });
  if (q.isLoading) return <Loading label="读取业绩报告…" />;
  if (q.isError) {
    if (q.error instanceof ApiError && q.error.status === 404) return <EmptyState>还没有可用于业绩报告的回放或账本（{q.error.message}）。</EmptyState>;
    return <ErrorState error={q.error} what="业绩报告" onRetry={() => q.refetch()} />;
  }
  return (
    <>
      <TearSheetView data={q.data!.data} />
      {'catalogId' in source && <MonthlyHeatmapCard catalogId={source.catalogId} />}
    </>
  );
}

export function TearSheetView({ data }: { data: TearSheetData }) {
  const ts = data.tearsheet;
  return (
    <section className="bfl tearsheet" aria-label={`业绩报告 ${data.subject.label}`}>
      <div className="counts">
        <b>{data.subject.label}</b>
        <span className="muted small">{data.window.start ?? '—'} ~ {data.window.end ?? '—'}，{data.window.sessions} 个交易日，日收益 {ts.returns} 个</span>
        {ts.sessions < SHORT_SAMPLE && <Badge tone="warn" title="样本很短，比率类指标不能当作证据">样本短（&lt; {SHORT_SAMPLE} 个交易日）</Badge>}
        {ts.missing_nav_sessions > 0 && <Badge tone="warn" title="这些交易日没有净值，没有用 0 或旧值补">{ts.missing_nav_sessions} 个交易日缺净值</Badge>}
      </div>
      <MetricGroups ts={ts} />
      <TradeLine ts={ts} />
      <p className="muted small">{data.basis.nav_basis}{data.basis.trade_pnl ? `；盈亏：${data.basis.trade_pnl}` : ''}</p>
      <ul className="muted small plain">{data.caveats.map((c) => <li key={c}>{c}</li>)}</ul>
    </section>
  );
}

function MetricGroups({ ts }: { ts: TearSheet }) {
  return (
    <div className="metric-groups">
      {GROUPS.map((g) => (
        <dl key={g.title} className="metric-group">
          <dt className="group-title">{g.title}</dt>
          {g.names.filter((n) => METRIC_NAMES.includes(n)).map((n) => {
            const m = ts.metrics[n];
            return (
              <div key={n} className="metric" title={METRIC_META[n].hint}>
                <dt>{METRIC_META[n].label}</dt>
                <dd className={`num ${signClass(n, m)}`}>{m.value == null ? <MissingText>缺失</MissingText> : metricText(n, m)}{m.value == null && <span className="reason small">{reasonText(m.reason)}</span>}</dd>
              </div>
            );
          })}
        </dl>
      ))}
    </div>
  );
}

function TradeLine({ ts }: { ts: TearSheet }) {
  const dd = ts.longest_drawdown;
  return (
    <p className="small">
      {ts.trades == null
        ? <span className="muted">成交明细不可用：成交类指标显示为缺失。</span>
        : <>已平仓 {ts.trades.closed} 笔，持仓中 {ts.trades.open} 笔{ts.trades.breakeven > 0 ? `，平手 ${ts.trades.breakeven} 笔` : ''}。</>}
      {dd && <> 最长回撤 {dd.sessions} 个交易日{dd.recovered ? '，已收复' : <Badge tone="warn" title="到最后一个交易日仍未收复，这个数字只是下限">未收复</Badge>}
        {dd.peak_session ? `（高点 ${dd.peak_session}${dd.recovery_session ? `，收复 ${dd.recovery_session}` : ''}）` : '（高点为期初资金）'}。</>}
    </p>
  );
}
