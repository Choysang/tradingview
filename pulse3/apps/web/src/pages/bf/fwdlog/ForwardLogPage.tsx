/**
 * Forward experiments (LEGACY_TRIAGE batch 3, lane fwdlog): E077 tail-of-day picks, E078 index half hours, E089 market-state log, the intraday paper replay.
 * Every experiment is a SHADOW record (nothing booked, no rule changes). The page shows how far each one is from its pre-registered decision day and its
 * daily rows; a metric before that day is marked "进度值" and carries the server's pending_reason. Numbers come from the server; this file formats only.
 */
import { useState } from 'react';
import { Badge } from '../../../components/Badges';
import { EmptyState, ErrorState, Loading, SectionTitle } from '../../../components/States';
import './fwdlog.css';
import { useExperiments, useRows } from './api';
import type { DailyRow, ExperimentCard, ExperimentKey, Metric, RuleRow } from './types';

const STATUS: Record<ExperimentCard['status'], { text: string; tone: 'neutral' | 'warn' | 'good' }> = {
  not_started: { text: '尚无记录', tone: 'neutral' }, recording: { text: '记录中', tone: 'warn' }, decision_reached: { text: '已到决策日', tone: 'good' },
};
const UNIT: Record<string, string> = { decision_days: '个决策日', sessions: '个交易日', independent_episodes: '段独立行情', recorded_sessions: '个录制日' };

/** A decimal string for display; null stays a dash (never 0). */
export function num(v: unknown, digits = 4): string {
  if (v == null || v === '') return '—';
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  const t = n.toFixed(digits);
  return t.includes('.') ? t.replace(/\.?0+$/, '') || '0' : t;
}

export function progressText(c: ExperimentCard): string {
  const p = c.progress;
  return `${p.done} / ${p.target} ${UNIT[p.unit] ?? p.unit}（还差 ${p.remaining}）`;
}

export function metricValue(m: Metric): string {
  if (m.value == null) return '—';
  if (typeof m.value === 'boolean') return m.value ? '是' : '否';
  return typeof m.value === 'number' ? String(m.value) : num(m.value);
}

type Col = { head: string; cell: (v: Record<string, unknown>) => string };
const obj = (v: unknown): Record<string, unknown> => (v && typeof v === 'object' ? (v as Record<string, unknown>) : {});
const flag = (v: unknown) => (v == null ? '—' : v ? '是' : '否');

/** Which fields of a stored row the table shows (the full row stays in the API). */
export const COLUMNS: Record<ExperimentKey, Col[]> = {
  E077: [
    { head: '股票池', cell: (v) => num(v.pool, 0) }, { head: '信号', cell: (v) => (Array.isArray(v.signals) && v.signals.length ? v.signals.join(' ') : '—') },
    { head: '有出场价', cell: (v) => num(v.n, 0) }, { head: '收益%', cell: (v) => num(v.ret, 3) }, { head: '对照%', cell: (v) => num(v.cell, 3) },
    { head: '超额%', cell: (v) => num(v.ex, 3) }, { head: '扣费后%', cell: (v) => num(v.net, 3) },
  ],
  E078: [
    { head: '等权 x%', cell: (v) => num(obj(v.ew).x, 4) }, { head: '等权 y%', cell: (v) => num(obj(v.ew).y, 4) },
    { head: '上证 x%', cell: (v) => num(obj(v.index).x, 4) }, { head: '上证 y%', cell: (v) => num(obj(v.index).y, 4) },
  ],
  E089: [
    { head: '等权涨跌%', cell: (v) => num(v.ew_pct, 4) }, { head: 'ETF 放量比', cell: (v) => num(v.ratio, 4) }, { head: '放量', cell: (v) => flag(v.surge) },
    { head: '恐慌+放量', cell: (v) => flag(v.panic_support) }, { head: '尾盘占比', cell: (v) => num(v.late_share, 4) },
    { head: '录制', cell: (v) => (v.error ? '出错' : v.ext_missing ? '缺失' : String(obj(v.health).end ?? '—')) },
  ],
  PAPER: [
    { head: '持仓只数', cell: (v) => num(v.book_size, 0) }, { head: '市场热度', cell: (v) => String(obj(v.market).heat ?? '—') },
    { head: '影子买点（基准档）', cell: (v) => String(((v.buys as Array<Record<string, unknown>> | undefined) ?? []).filter((b) => b.scenario === 'BASE').reduce((a, b) => a + Number(b.triggered ?? 0), 0)) },
    { head: 'T+1 影子成交', cell: (v) => num(obj(v.t1).booked_buys, 0) },
  ],
};

export function RowsTable({ experimentKey, rows }: { experimentKey: ExperimentKey; rows: DailyRow[] }) {
  const cols = COLUMNS[experimentKey];
  if (!rows.length) return <EmptyState>还没有记录。每日步骤跑过之后才会出现，这里不补数。</EmptyState>;
  return (
    <div className="fl-rows-wrap">
      <table className="fl-rows small" aria-label={`${experimentKey} 每日记录`}>
        <thead><tr><th>交易日</th>{cols.map((c) => <th key={c.head}>{c.head}</th>)}</tr></thead>
        <tbody>
          {[...rows].reverse().map((r) => (
            <tr key={r.row_key}><td className="mono">{r.session_date}</td>{cols.map((c) => <td key={c.head} className="mono num">{c.cell(r.values)}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function RuleTable({ rules }: { rules: RuleRow[] }) {
  return (
    <div className="fl-rows-wrap">
      <table className="fl-rows small" aria-label="卖出规则并排">
        <thead><tr><th>规则</th><th>触发</th><th>相对成本均值%</th><th>相对收盘均值%</th><th>相对收盘中位%</th><th>好于收盘占比</th></tr></thead>
        <tbody>
          {rules.map((r) => (
            <tr key={r.rule}>
              <td>{r.rule}{r.shadow && <> <Badge tone="neutral" title="影子行：旧记录里没有，只记录不记账">影子</Badge></>}</td>
              <td className="mono num">{r.triggered}</td><td className="mono num">{num(r.mean_return_pct, 2)}</td><td className="mono num">{num(r.mean_vs_close_pct, 2)}</td>
              <td className="mono num">{num(r.median_vs_close_pct, 2)}</td><td className="mono num">{num(r.better_than_close_share, 2)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ExperimentCardView({ card }: { card: ExperimentCard }) {
  const [open, setOpen] = useState(false);
  const rows = useRows(card.experiment_key, open);
  const st = STATUS[card.status];
  return (
    <section className="card" aria-label={`${card.experiment_key} ${card.title}`}>
      <div className="fl-head">
        <h3>{card.experiment_key} {card.title}</h3>
        <Badge tone={st.tone}>{st.text}</Badge>
        <Badge tone="neutral" title="影子记录：不下单、不入账、不改任何规则">影子</Badge>
        <span className="muted small">台账 {card.ledger_ids.join(' ')}</span>
      </div>
      <div className="fl-bar" role="progressbar" aria-valuemin={0} aria-valuemax={card.progress.target} aria-valuenow={Math.min(card.progress.done, card.progress.target)}>
        <span style={{ width: `${Math.round(Number(card.progress.share) * 100)}%` }} />
      </div>
      <p className="small" data-testid="progress">进度 <b className="mono">{progressText(card)}</b></p>
      <p className="small muted" data-testid="decision">
        {card.decision.decided ? `已到决策日：${card.decision.verdict ?? '见指标'}` : (card.decision.pending_reason ?? '决策日未到')}
      </p>
      <table className="fl-metrics small" aria-label="预注册指标">
        <tbody>
          {card.metrics.map((m) => (
            <tr key={m.name}>
              <th scope="row">{m.name}</th><td className="mono num">{metricValue(m)}</td>
              <td className="muted">{m.provisional ? <Badge tone="warn" title={m.pending_reason ?? undefined}>进度值，非结论</Badge> : m.pending_reason ?? ''}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {card.rule_table && <RuleTable rules={card.rule_table} />}
      {card.pending.length > 0 && (
        <details className="small">
          <summary>等待中的交易日 {card.pending.length}</summary>
          <ul className="fl-pending">{card.pending.map((p) => <li key={p.session_date}><span className="mono">{p.session_date}</span> {p.reason}</li>)}</ul>
        </details>
      )}
      {card.notes.map((n) => <p key={n} className="small muted">{n}</p>)}
      <p className="small muted fl-sha">
        预注册文件 {card.preregistration.map((p) => <span key={p.file} className="mono" title={p.file}>{p.sha256.slice(0, 12)} </span>)}
        · 规则版本 {card.rules_version} · 研究登记 {card.registry.registered ? `影子运行（${card.registry.run_state}）` : '未登记'}
      </p>
      <button type="button" className="btn" aria-expanded={open} onClick={() => setOpen((v) => !v)}>{open ? '收起每日记录' : '看每日记录'}</button>
      {open && rows.isLoading && <Loading />}
      {open && rows.error && <ErrorState error={rows.error} what="每日记录" onRetry={() => void rows.refetch()} />}
      {open && rows.data && <RowsTable experimentKey={card.experiment_key} rows={rows.data.data.rows} />}
    </section>
  );
}

/** The page: route wired by the integrator (suggested `/research/fwdlog`). */
export function ForwardLogPage() {
  const q = useExperiments();
  return (
    <div className="fl-list">
      <SectionTitle title="前向实验日志" hint="旧系统每天仍在跑的前向实验：规则在预注册时写定，不会改；这里只记录，不下单、不入账。" />
      {q.isLoading && <Loading />}
      {q.error && <ErrorState error={q.error} what="前向实验" onRetry={() => void q.refetch()} />}
      {q.data && q.data.data.experiments.length === 0 && <EmptyState>还没有登记的实验。</EmptyState>}
      {q.data?.data.experiments.map((c) => <ExperimentCardView key={c.experiment_key} card={c} />)}
      {q.data?.data.notes.map((n) => <p key={n} className="small muted">{n}</p>)}
    </div>
  );
}

export default ForwardLogPage;
