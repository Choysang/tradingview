/**
 * 融合分数与前向记录 (triage batch 3, lane fusion): the frozen round-5 static40 score of the main board, its two books (5-day main, 10-day shadow)
 * and the three forward experiments (monitor E068, X20 basket E085, OS20 E081). Everything is read from the server's append-only records;
 * the page only draws. Missing is shown as 缺失 with no number; a flagged monitor date is shown as 不计入 and is never in a statistic.
 * Route proposal for the integrator: /research/fusion (exported: FusionPage, ScoresTable, BookCard, NavLine, MonitorTable, BasketTable, Os20Table).
 */
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { Badge } from '../../../components/Badges';
import { EmptyState, ErrorState, Loading } from '../../../components/States';
import type { BasketRow, Book, BookRow, Experiment, FusionStatus, MonitorRow, Os20Row, ScoreRow } from './api';
import { bfFusion } from './api';
import './fusion.css';

const bad = (v: string | null | undefined): boolean => v == null || v === '' || Number.isNaN(Number(v));
export const num = (v: string | null | undefined, digits = 2): string => (bad(v) ? '缺失' : Number(v).toFixed(digits));
export const pct = (v: string | null | undefined, digits = 2): string => (bad(v) ? '缺失' : `${Number(v) >= 0 ? '+' : ''}${Number(v).toFixed(digits)}%`);
export const signed = (v: string | null | undefined, digits = 3): string => (bad(v) ? '缺失' : `${Number(v) >= 0 ? '+' : ''}${Number(v).toFixed(digits)}`);
const tone = (v: string | null | undefined): string => (bad(v) ? '' : Number(v) > 0 ? 'pos' : Number(v) < 0 ? 'neg' : '');
const names = (xs: string[]): string => (xs.length ? xs.join(' ') : '无');

export function ScoresTable({ rows }: { rows: ScoreRow[] }) {
  if (!rows.length) return <EmptyState>这一天还没有分数。</EmptyState>;
  return (
    <table className="fz-table" aria-label="分数前列">
      <thead><tr><th>名次</th><th>代码</th><th>名称</th><th className="r">分数</th></tr></thead>
      <tbody>{rows.map((r) => <tr key={r.code}><td>{r.rank}</td><td>{r.code}</td><td>{r.display_name ?? '缺失'}</td><td className="r">{signed(r.score, 4)}</td></tr>)}</tbody>
    </table>
  );
}

/** A NAV series as a polyline; the server's numbers, scaled to the box only. */
export function NavLine({ points, label }: { points: { d: string; v: number }[]; label: string }) {
  if (points.length < 2) return <p className="muted small">净值点数不足，暂不画线。</p>;
  const ys = points.map((p) => p.v);
  const lo = Math.min(...ys);
  const span = (Math.max(...ys) - lo) || 1;
  const w = 320, h = 70;
  const path = points.map((p, i) => `${(i / (points.length - 1)) * w},${h - ((p.v - lo) / span) * (h - 6) - 3}`).join(' ');
  return (
    <svg className="fz-nav" viewBox={`0 0 ${w} ${h}`} role="img" aria-label={label}>
      <polyline fill="none" stroke="currentColor" strokeWidth="1.6" points={path} />
    </svg>
  );
}

export function BookCard({ book }: { book: Book }) {
  const last: BookRow | undefined = book.rows[book.rows.length - 1];
  return (
    <article className="fz-card" aria-label={book.label}>
      <h3>{book.label} <Badge tone="neutral">{book.ledger_id}</Badge></h3>
      <p className="muted small">{book.rule}</p>
      {!last && <EmptyState>还没有决策行：每日步骤运行后出现。</EmptyState>}
      {last && (
        <>
          <dl className="fz-kv">
            <dt>最近决策日</dt><dd>{last.date}</dd>
            <dt>闸门</dt><dd>{Number(last.gate_w) < 1 ? `降仓（每日新买 ${last.new_buys_allowed} 只）` : `满仓（每日新买 ${last.new_buys_allowed} 只）`}</dd>
            <dt>持仓</dt><dd data-testid={`holdings-${book.key}`}>{names(last.holdings)}</dd>
            <dt>下一日准备买入</dt><dd data-testid={`trybuy-${book.key}`}>{names(last.try_buy)}</dd>
            <dt>分数前 10</dt><dd>{last.offer_top10.join(' ')}</dd>
            <dt>净值（闸门）</dt><dd>{num(last.nav, 2)} <span className={tone(last.nav_since_freeze_pct)}>{last.nav_since_freeze_pct == null ? '' : `冻结以来 ${pct(last.nav_since_freeze_pct, 2)}`}</span></dd>
            <dt>净值（不加闸门）</dt><dd>{num(last.ungated_nav, 2)}</dd>
          </dl>
          <NavLine label={`${book.key} 净值`} points={book.rows.map((r) => ({ d: r.date, v: Number(r.nav) }))} />
          <table className="fz-table" aria-label={`${book.key} 决策记录`}>
            <thead><tr><th>日期</th><th className="r">闸门</th><th>买入</th><th className="r">持仓数</th><th className="r">净值</th><th className="r">不加闸门</th></tr></thead>
            <tbody>{[...book.rows].reverse().map((r) => (
              <tr key={r.date}><td>{r.date}</td><td className="r">{num(r.gate_w, 2)}</td><td>{names(r.try_buy)}</td><td className="r">{r.holdings.length}</td><td className="r">{num(r.nav, 2)}</td><td className="r">{num(r.ungated_nav, 2)}</td></tr>
            ))}</tbody>
          </table>
        </>
      )}
    </article>
  );
}

function Summary({ s }: { s: Record<string, string | number | boolean | null> }) {
  const keys = Object.keys(s);
  if (!keys.length) return null;
  return <dl className="fz-kv small">{keys.map((k) => <div key={k} className="fz-kv-row"><dt>{k}</dt><dd>{s[k] == null ? '缺失' : String(s[k])}</dd></div>)}</dl>;
}

export function MonitorTable({ rows }: { rows: MonitorRow[] }) {
  if (!rows.length) return <EmptyState>还没有标签完整的决策日。</EmptyState>;
  return (
    <table className="fz-table" aria-label="前向监控">
      <thead><tr><th>决策日</th><th className="r">样本</th><th className="r">秩相关 IC</th><th className="r">十分位差 %</th><th className="r">前 10% 超额 %</th><th className="r">同风格匹配超额 %</th><th>状态</th></tr></thead>
      <tbody>{[...rows].reverse().map((r) => (
        <tr key={r.date} className={r.flagged ? 'flagged' : ''}>
          <td>{r.date}</td><td className="r">{r.n}</td><td className={`r ${tone(r.ic)}`}>{signed(r.ic, 4)}</td><td className="r">{signed(r.decile_spread)}</td>
          <td className="r">{signed(r.top_ex)}</td><td className="r">{signed(r.top_sv)}</td>
          <td>{r.flagged ? <Badge tone="warn" title={r.coverage_fail_days.join(', ')}>不计入：覆盖率不足 {r.coverage_fail_days.join(' ')}</Badge> : <Badge tone="good">计入</Badge>}</td>
        </tr>
      ))}</tbody>
    </table>
  );
}

export function BasketTable({ rows }: { rows: BasketRow[] }) {
  if (!rows.length) return <EmptyState>篮子还没有建仓。</EmptyState>;
  return (
    <table className="fz-table" aria-label="X20 篮子">
      <thead><tr><th>日期</th><th className="r">X20 净值</th><th className="r">等权池</th><th className="r">超额 %</th><th className="r">X20 只数</th><th className="r">池只数</th><th>调仓</th></tr></thead>
      <tbody>{[...rows].reverse().map((r) => (
        <tr key={r.date}><td>{r.date}</td><td className="r">{num(r.x20, 4)}</td><td className="r">{num(r.ew, 4)}</td><td className={`r ${tone(r.excess_pct)}`}>{signed(r.excess_pct)}</td>
          <td className="r">{r.n_x20}</td><td className="r">{r.n_ew}</td><td>{r.trade_day ? `换手 ${num(r.turnover_x20, 3)}` : ''}</td></tr>
      ))}</tbody>
    </table>
  );
}

const GROUP_TEXT: Record<string, string> = { OS20_GAP: '20 日超跌·有冲击', OS20_NOGAP: '20 日超跌·无冲击', OS5_GAP: '5 日超跌·有冲击' };

export function Os20Table({ rows }: { rows: Os20Row[] }) {
  if (!rows.length) return <EmptyState>还没有标签完整的决策日（持 10 日，名单已每日冻结）。</EmptyState>;
  return (
    <table className="fz-table" aria-label="OS20">
      <thead><tr><th>决策日</th><th className="r">池 20 日均涨 %</th>{Object.keys(GROUP_TEXT).map((g) => <th key={g} className="r">{GROUP_TEXT[g]} 只数 / 匹配超额 %</th>)}<th>状态</th></tr></thead>
      <tbody>{[...rows].reverse().map((r) => (
        <tr key={r.date} className={r.flagged ? 'flagged' : ''}>
          <td>{r.date}</td><td className="r">{signed(r.mkt20, 2)}</td>
          {r.groups.map((g) => <td key={g.group} className={`r ${tone(g.ex)}`}>{g.n} / {signed(g.ex)}</td>)}
          <td>{r.flagged ? <Badge tone="warn">不计入</Badge> : <Badge tone="good">计入</Badge>}</td>
        </tr>
      ))}</tbody>
    </table>
  );
}

function ExperimentBlock({ e }: { e: Experiment }) {
  return (
    <section className="fz-card" aria-label={e.label}>
      <h3>{e.label} <Badge tone="neutral">{e.ledger_id}</Badge></h3>
      <p className="muted small">预注册：{e.document}（sha256 {e.document_sha256 ? e.document_sha256.slice(0, 12) : '缺失'}）。规则冻结，不会改。</p>
      <Summary s={e.summary} />
      {e.key === 'monitor' && <MonitorTable rows={e.rows} />}
      {e.key === 'x20' && <BasketTable rows={e.rows} />}
      {e.key === 'os20' && <Os20Table rows={e.rows} />}
    </section>
  );
}

export function StatusStrip({ s }: { s: FusionStatus }) {
  const r = s.last_run;
  return (
    <section className="fz-status" aria-label="运行状态">
      <div>最近一次运行：{r ? <>{r.written_on} <Badge tone={r.status === 'done' ? 'good' : 'bad'}>{r.status === 'done' ? '完成' : `已停止：${r.code ?? ''}`}</Badge> {r.seconds ? `${num(r.seconds, 0)} 秒` : ''} {r.peak_memory_mb ? `峰值内存 ${num(r.peak_memory_mb, 0)} MB` : ''}</> : '还没有运行'}</div>
      <div>分数：晋升 {s.current.dates} 天（{s.current.first_date ?? '缺失'} ~ {s.current.last_date ?? '缺失'}）；首次计算的存档 {s.asof.dates} 天，只追加。</div>
      <div className="muted small">冻结数据：{s.frozen_data.map((f) => `${f.file} ${f.sha256.slice(0, 10)}`).join('；')}</div>
    </section>
  );
}

export function FusionPage() {
  const [date, setDate] = useState<string | null>(null);
  const status = useQuery({ queryKey: ['bf-fusion', 'status'], queryFn: ({ signal }) => bfFusion.status(signal), retry: false });
  const scores = useQuery({ queryKey: ['bf-fusion', 'scores', date], queryFn: ({ signal }) => bfFusion.scores(date, 40, signal), retry: false });
  const books = useQuery({ queryKey: ['bf-fusion', 'books'], queryFn: ({ signal }) => bfFusion.books(signal), retry: false });
  const exps = useQuery({ queryKey: ['bf-fusion', 'experiments'], queryFn: ({ signal }) => bfFusion.experiments(signal), retry: false });
  const sc = scores.data?.data;
  return (
    <div className="fz">
      <h2>融合分数与前向记录</h2>
      <p className="muted small">冻结的第五轮 static40 分数（主板，剔除依赖前复权价格水平的因子）。主书持 5 日、影子书持 10 日、三个前向实验的规则都已预注册，页面只读。</p>
      {status.isLoading && <Loading />}
      {status.isError && <ErrorState error={status.error} what="运行状态" onRetry={() => status.refetch()} />}
      {status.data && <StatusStrip s={status.data.data} />}
      <section aria-label="分数">
        <h3>某日分数前 40</h3>
        {scores.isLoading && <Loading />}
        {scores.isError && <ErrorState error={scores.error} what="分数" onRetry={() => scores.refetch()} />}
        {sc && (
          <>
            <label className="fz-pick">日期
              <select value={sc.date ?? ''} onChange={(e) => setDate(e.target.value || null)} aria-label="选择日期">
                {sc.available_dates.length === 0 && <option value="">无</option>}
                {[...sc.available_dates].reverse().map((d) => <option key={d} value={d}>{d}</option>)}
              </select>
            </label>
            {sc.kind && <Badge tone={sc.kind === 'asof' ? 'good' : 'neutral'} title="asof = 当天首次计算的分数，不再改写">{sc.kind === 'asof' ? '当日首次计算' : '重算后的分数'}</Badge>}
            <ScoresTable rows={sc.rows} />
          </>
        )}
      </section>
      <section aria-label="账本">
        <h3>两本账</h3>
        {books.isLoading && <Loading />}
        {books.isError && <ErrorState error={books.error} what="账本" onRetry={() => books.refetch()} />}
        <div className="fz-grid">{books.data?.data.books.map((b) => <BookCard key={b.key} book={b} />)}</div>
      </section>
      <section aria-label="前向实验">
        <h3>前向实验</h3>
        {exps.isLoading && <Loading />}
        {exps.isError && <ErrorState error={exps.error} what="前向实验" onRetry={() => exps.refetch()} />}
        {exps.data?.data.experiments.map((e) => <ExperimentBlock key={e.key} e={e} />)}
      </section>
    </div>
  );
}
