/**
 * Batch 6 short-term research (E093 leader linkage, E094 seats, E095 call auction, E096 active buying). Shadow records only: nothing is booked.
 * History: matched excess after cost per hypothesis and segment (PRE / DEV), fill rate, block bootstrap p, Holm-adjusted p and the pass verdict from the pre-registered rule.
 * Forward: progress toward 40 signal days; descriptive figures at 20; a verdict only at 40. Numbers come from the server; this file formats only.
 */
import { Badge } from '../../../components/Badges';
import { EmptyState, ErrorState, Loading, SectionTitle } from '../../../components/States';
import './batch6.css';
import { useOverview } from './api';
import type { FieldCheck, ForwardRow, HistoryBlock, HistoryRow, Overview } from './types';

/** A decimal string for display; null stays a dash (never 0). */
export function num(v: string | number | null | undefined, digits = 4): string {
  if (v == null || v === '') return '—';
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  const t = n.toFixed(digits);
  return t.includes('.') ? t.replace(/\.?0+$/, '') || '0' : t;
}

/** A ratio shown as a signed percent (the server sends ratios, not percents). */
export function pct(v: string | null | undefined, digits = 2): string {
  if (v == null || v === '') return '—';
  const n = Number(v) * 100;
  if (!Number.isFinite(n)) return String(v);
  return `${n > 0 ? '+' : ''}${n.toFixed(digits)}%`;
}

export const STATE: Record<ForwardRow['state'], { text: string; tone: 'neutral' | 'warn' | 'good' }> = {
  collecting: { text: '收集中', tone: 'neutral' }, descriptive: { text: '仅描述（已满 20 日）', tone: 'warn' }, judged: { text: '已判定', tone: 'good' },
};

export function progressShare(done: number, target: number): number {
  return target > 0 ? Math.min(100, Math.round((Math.min(done, target) / target) * 100)) : 0;
}

export function HistoryTable({ rows, verdicts }: { rows: HistoryRow[]; verdicts: HistoryBlock['verdicts'] }) {
  const v = new Map(verdicts.map((x) => [x.hypothesis, x]));
  return (
    <div className="b6-table-wrap">
      <table className="b6-table" aria-label="历史结果">
        <thead>
          <tr>
            <th>假设</th><th>段</th><th>信号</th><th>成交率</th><th>匹配样本</th><th>信号日</th><th>扣费后均值</th><th>匹配超额</th><th>p</th><th>Holm p</th><th>块数</th><th>判定</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={`${r.hypothesis}:${r.segment}`}>
              <td>{r.hypothesis} · {r.experiment}（{r.sign < 0 ? '应为负' : '应为正'}，持有 {r.hold} 日）</td>
              <td>{r.segment}</td>
              <td>{r.signals}</td>
              <td>{pct(r.fill_rate, 1)}</td>
              <td>{r.matched}</td>
              <td>{r.signal_days}</td>
              <td>{pct(r.mean_ret_net)}</td>
              <td>{pct(r.excess, 3)}</td>
              <td>{num(r.p_value, 4)}</td>
              <td>{r.segment === 'PRE' ? num(r.holm_p_adj, 4) : '—'}</td>
              <td>{r.blocks}{r.reliable ? '' : '（仅报告）'}</td>
              <td>{r.segment === 'PRE' ? <Badge tone={v.get(r.hypothesis)?.passed ? 'good' : 'neutral'}>{v.get(r.hypothesis)?.passed ? '通过' : '未通过'}</Badge> : '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ForwardCard({ r }: { r: ForwardRow }) {
  const s = STATE[r.state];
  const tag = r.state === 'judged' ? '判定值' : '进度值';
  return (
    <section className="b6-block" aria-label={`${r.experiment} ${r.hypothesis}`}>
      <h3>{r.experiment} · {r.hypothesis} <Badge tone={s.tone}>{s.text}</Badge></h3>
      <div className="b6-bar" role="progressbar" aria-valuenow={r.experiment_signal_days} aria-valuemax={r.target_days}>
        <span style={{ width: `${progressShare(r.experiment_signal_days, r.target_days)}%` }} />
      </div>
      <p>有信号的交易日 {r.experiment_signal_days} / {r.target_days}（本假设 {r.signal_days} 日，至少 25 日）；已评估 {r.days_evaluated} 个交易日</p>
      <table className="b6-table" aria-label={`${r.hypothesis} 数字`}>
        <tbody>
          <tr><th className="left">信号 / 成交</th><td>{r.signals} / {r.filled}（成交率 {pct(r.fill_rate, 1)}）；卖不掉待顺延 {r.exit_deferred}，已了结 {r.resolved_later}</td></tr>
          <tr><th className="left">扣费后均值（{tag}）</th><td>{pct(r.mean_ret_net)}</td></tr>
          <tr><th className="left">匹配超额（{tag}）</th><td>{pct(r.excess, 3)}</td></tr>
          <tr><th className="left">p / Holm p</th><td>{r.state === 'collecting' ? '—' : num(r.p_value, 4)} / {r.state === 'judged' ? num(r.holm_p_adj, 4) : '—'}</td></tr>
          <tr><th className="left">判定</th><td>{r.state === 'judged' ? (r.passed ? '通过' : '未通过') : '满 40 个信号日之前不判定'}{r.reason && r.state === 'judged' ? `：${r.reason}` : ''}</td></tr>
        </tbody>
      </table>
    </section>
  );
}

export function FieldCheckView({ f }: { f: FieldCheck }) {
  return (
    <section className="b6-block" aria-label="字段核对">
      <p>核对日：{f.sessions.join('、') || '—'}；竞价行 {f.auction_rows}，09:25 行 {f.final_rows}</p>
      <ul className="b6-notes">
        <li>09:15–09:24 买一量 / 卖一量：{f.bid1_ask1_lots_0915_0924}</li>
        <li>09:15–09:24 买二量 / 卖二量：{f.bid2_ask2_lots_0915_0924}</li>
        <li>09:25 买卖盘：{f.bid_ask_book_at_0925}</li>
        <li>unmatched_buy：{f.unmatched_buy_computable ? '可计算' : '无法计算'}（{f.unmatched_buy_definition}）</li>
      </ul>
      <p className="muted">{f.shares.map((s) => `${s.name} ${pct(s.value, 2)}`).join('；')}</p>
    </section>
  );
}

export function Batch6View({ data }: { data: Overview }) {
  const h = data.history;
  return (
    <div>
      <SectionTitle title="E093 / E094 历史回测" hint="日线次日执行；PRE ≤ 2026-06-30，DEV 之后" />
      {h == null ? <EmptyState>尚未运行历史回测（batch6 history）</EmptyState> : (
        <div className="b6-block">
          <HistoryTable rows={h.rows} verdicts={h.verdicts} />
          <p>
            决策日 {h.decision_days?.first ?? '—'} 至 {h.decision_days?.last ?? '—'}（{h.decision_days?.sessions ?? 0} 个交易日）；事件：
            {h.event_counts.map((c) => `${c.key} ${c.count}`).join('，') || '—'}
          </p>
          <ul className="b6-notes">{h.notes.map((n) => <li key={n}>{n}</li>)}</ul>
        </div>
      )}
      <SectionTitle title="E095 / E096 前向" hint={`60 秒快照；首个评估日 ${data.first_evaluated}`} />
      {data.forward.length === 0 ? <EmptyState>尚无前向记录</EmptyState> : data.forward.map((r) => <ForwardCard key={r.hypothesis} r={r} />)}
      <SectionTitle title="竞价字段核对" />
      {data.fieldcheck == null ? <EmptyState>尚未核对（batch6 fieldcheck）</EmptyState> : <FieldCheckView f={data.fieldcheck} />}
      <p className="b6-sha muted">预注册 sha256 {data.spec_sha256}。影子研究：不下单、不入账、不改任何规则。</p>
    </div>
  );
}

export function Batch6Page() {
  const q = useOverview();
  if (q.isLoading) return <Loading />;
  if (q.isError || !q.data) return <ErrorState error={q.error} what="第六批结果" onRetry={() => void q.refetch()} />;
  return <Batch6View data={q.data.data} />;
}

export default Batch6Page;
