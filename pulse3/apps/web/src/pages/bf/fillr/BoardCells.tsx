/**
 * Playbook board cells (triage #16 #82 #150): fitness ring, verdict, out-of-sample average and a NAV sparkline per playbook, computed on the server from
 * the stored replay and drawn here. Honest states: a playbook without a replay says 未回测 / 缺数据 / 失败 (never a made-up score); a figure with no
 * out-of-sample split is labelled 仅样本内; the fitness ring is empty when there is no fitness. The block-bootstrap note (E082) shows when it is unreliable.
 */
import { useQuery } from '@tanstack/react-query';
import { useMemo } from 'react';
import { Badge, MissingText } from '../../../components/Badges';
import type { BoardItem, EvaluationBlock, PlaybookBoard } from './api';
import { bfFillr } from './api';
import { numText, pctText, ringDash, signClass, sparkPoints, VERDICT_TEXT, VERDICT_TONE, verdictLabel } from './format';
import './fillr.css';

export function usePlaybookBoard() {
  const q = useQuery({ queryKey: ['bffr', 'board'], queryFn: ({ signal }) => bfFillr.board(signal), staleTime: 60_000, retry: false });
  const byPlaybook = useMemo(() => new Map<string, BoardItem>((q.data?.data.items ?? []).map((i) => [i.playbook_id, i])), [q.data]);
  return { query: q, byPlaybook, board: q.data?.data as PlaybookBoard | undefined };
}

export function FitnessRing({ fitness }: { fitness: string | null }) {
  const d = ringDash(fitness);
  return (
    <svg className="fit-ring" viewBox="0 0 36 36" width="32" height="32" role="img" aria-label={fitness == null ? '适配度缺失' : `适配度 ${Number(fitness).toFixed(0)} / 100`}>
      <circle cx="18" cy="18" r="15" className="ring-bg" />
      {d && <circle cx="18" cy="18" r="15" className="ring-fg" strokeDasharray={`${d.filled.toFixed(1)} ${(d.circumference - d.filled).toFixed(1)}`} transform="rotate(-90 18 18)" />}
      <text x="18" y="21.5" textAnchor="middle" className="ring-text">{fitness == null ? '—' : Number(fitness).toFixed(0)}</text>
    </svg>
  );
}

export function Sparkline({ values, label }: { values: string[]; label: string }) {
  const pts = sparkPoints(values);
  if (!pts) return <MissingText>无走势</MissingText>;
  const up = Number(values[values.length - 1]) >= Number(values[0]);
  return <svg className={`spark ${up ? 'pos' : 'neg'}`} viewBox="0 0 72 22" width="72" height="22" role="img" aria-label={label}><polyline points={pts} fill="none" /></svg>;
}

export const BOARD_COLUMNS = ['适配度', '判定', '样本外均值', '净值走势'];
export function BoardHead() {
  return <>{BOARD_COLUMNS.map((c) => <th key={c}>{c}</th>)}</>;
}

/** The four board cells of one playbook (inside a table row). */
export function BoardCells({ item }: { item: BoardItem | undefined }) {
  if (!item) return <td colSpan={4}><MissingText>—</MissingText> <span className="muted small">适配度读取中或不可用</span></td>;
  const ev = item.evaluation;
  if (item.state !== 'evaluated' || !ev) {
    return <td colSpan={4}><Badge tone={item.state === 'failed' ? 'bad' : 'neutral'} title={item.reason ?? undefined}>{verdictLabel(item)}</Badge> <span className="muted small">{reasonText(item)}</span></td>;
  }
  const oos = ev.out_of_sample;
  return (
    <>
      <td title={ev.fitness_reasons.join('\n')}><FitnessRing fitness={ev.fitness} /></td>
      <td>
        {ev.verdict ? <Badge tone={VERDICT_TONE[ev.verdict]} title={ev.verdict_reason}>{verdictLabel(item)}</Badge> : <Badge title={ev.verdict_reason}>仅样本内</Badge>}
        {ev.block && !ev.block.reliable && ev.verdict === 'effective' && <span className="muted small" title="块自助块数不足 10，p 值只供参考（E082）"> 块自助仅供参考</span>}
      </td>
      <td className={`num ${signClass(oos?.avg_return)}`} title={ev.split_day ? `样本外自 ${ev.split_day} 起，${oos?.trades ?? 0} 笔已平仓；单笔毛收益均值` : '实验没有记录样本外切分'}>
        {oos == null ? <MissingText>无样本外</MissingText> : oos.avg_return == null ? <MissingText>无成交</MissingText> : pctText(oos.avg_return)}
        <span className="muted small"> {oos ? `${oos.trades}笔` : '仅样本内'}</span>
      </td>
      <td title={item.sparkline_start ? `${item.sparkline_start} ~ ${item.sparkline_end}（净值，已扣费）` : undefined}><Sparkline values={item.sparkline} label={`${item.name} 净值走势`} /></td>
    </>
  );
}

function reasonText(item: BoardItem): string {
  if (item.state === 'not_run') return '还没有回放结果（ext-b-run）';
  return item.reason ?? '';
}

/** The detail block of an evaluated playbook: the sample table, the verdict reason and every fitness component. */
export function BoardDetail({ item }: { item: BoardItem | undefined }) {
  return item?.evaluation ? <EvaluationDetail ev={item.evaluation} /> : null;
}

export function EvaluationDetail({ ev }: { ev: EvaluationBlock }) {
  const rows: Array<[string, typeof ev.full | null]> = [['全样本', ev.full], ['样本内', ev.in_sample], ['样本外', ev.out_of_sample], ['近 60 日', ev.recent]];
  return (
    <section className="board-detail" aria-label="适配度与判定">
      <p><b>{ev.verdict ? VERDICT_TEXT[ev.verdict] : '仅样本内'}</b>：{ev.verdict_reason}</p>
      <table className="table dense">
        <thead><tr><th>样本</th><th>笔数</th><th>胜率</th><th>平均收益(毛)</th><th>t 值</th><th>夏普</th></tr></thead>
        <tbody>
          {rows.map(([label, s]) => (
            <tr key={label}><td>{label}{label === '样本外' && ev.split_day ? <span className="muted small">（{ev.split_day} 起）</span> : null}</td>
              {s == null ? <td colSpan={5}><MissingText>实验没有记录样本外切分</MissingText></td> : (
                <><td className="num">{s.trades}</td><td className="num">{pctText(s.win_rate, 0)}</td><td className={`num ${signClass(s.avg_return)}`}>{pctText(s.avg_return)}</td>
                  <td className="num">{numText(s.t_stat)}</td><td className="num">{numText(s.sharpe)}</td></>
              )}
            </tr>
          ))}
        </tbody>
      </table>
      {ev.block && <p className="muted small">块自助（块长 {ev.block.block} 个交易日，{ev.block.blocks} 块）：p = {numText(ev.block.p_value, 3)}{ev.block.reliable ? '' : '（块数不足，仅供参考）'}。</p>}
      <details><summary>适配度 {ev.fitness == null ? '缺失' : Number(ev.fitness).toFixed(1)} / 100 的构成</summary><ul className="plain small">{ev.fitness_reasons.map((r) => <li key={r}>{r}</li>)}</ul></details>
      {ev.missing.length > 0 && <p className="muted small">缺失的输入：{ev.missing.join('、')}（对应分项按 0 计，没有用默认值代替）。</p>}
    </section>
  );
}
